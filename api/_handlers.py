"""Shared request logic for the Vercel functions and the local server."""
import hmac, json, os, sqlite3, urllib.request
from http.server import BaseHTTPRequestHandler
import _pipeline as p

HERE = os.path.dirname(os.path.abspath(__file__))
LOCAL_DB = os.path.join(HERE, "..", "runs_local.db")


def _need_code():
    return os.environ.get("ACCESS_CODE", "")


def config(body):
    return {"auth_required": bool(_need_code()), "provider": p.provider(),
            "logging": "supabase" if os.environ.get("SUPABASE_URL") else ("local" if not os.environ.get("VERCEL") else "off")}


def crawl(body):
    if not body.get("url"):
        raise ValueError("Enter a company URL.")
    return p.stage_crawl(body["url"], body.get("texts"))


def claims(body):
    return p.stage_claims(body["url"], body["chunks"])


def xray(body):
    return p.stage_xray(body["url"], body.get("buyer", ""), body.get("objective", ""), body["claims"])


def log(body):
    s = body.get("stats", {})
    row = {"url": body.get("url"), "company": body.get("company"), "buyer": body.get("buyer"),
           "objective": body.get("objective"), "provider": s.get("provider"), "model": s.get("model"),
           "pages": s.get("pages"), "claims": s.get("claims"), "verified": s.get("verified"),
           "verify_rate": s.get("verify_rate"), "tokens_in": s.get("tokens_in"), "tokens_out": s.get("tokens_out"),
           "cost_usd": s.get("cost_usd"), "seconds": s.get("seconds"), "result": body.get("result")}
    sb, key = os.environ.get("SUPABASE_URL", "").rstrip("/"), os.environ.get("SUPABASE_SERVICE_KEY", "")
    if sb and key:
        req = urllib.request.Request(sb + "/rest/v1/runs", json.dumps(row).encode(), {
            "apikey": key, "Authorization": "Bearer " + key, "Content-Type": "application/json", "Prefer": "return=minimal"})
        urllib.request.urlopen(req, timeout=15).read()
        return {"logged": "supabase"}
    if not os.environ.get("VERCEL"):
        c = sqlite3.connect(LOCAL_DB)
        c.execute("""CREATE TABLE IF NOT EXISTS runs(id INTEGER PRIMARY KEY, ts TEXT DEFAULT (datetime('now')), url, company,
            buyer, objective, provider, model, pages, claims, verified, verify_rate, tokens_in, tokens_out, cost_usd,
            seconds, result)""")
        cols = [k for k in row]
        vals = [json.dumps(v) if k == "result" else v for k, v in row.items()]
        c.execute(f"INSERT INTO runs({','.join(cols)}) VALUES({','.join('?' * len(cols))})", vals)
        c.commit(); c.close()
        return {"logged": "local"}
    return {"logged": "off"}


def stash(body):
    """Local only: lets a browser-rendered page hand its text to the app for sites a plain fetch can't read."""
    if os.environ.get("VERCEL"):
        raise ValueError("not available")
    name = "".join(ch for ch in body.get("name", "") if ch.isalnum() or ch in "-_")[:40] or "site"
    d = os.path.join(HERE, "..", "stash")
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, name + ".json"), "w", encoding="utf-8") as f:
        json.dump(body.get("texts", []), f)
    return {"saved": name, "pages": len(body.get("texts", []))}


ROUTES = {"stash": stash, "config": config, "crawl": crawl, "claims": claims, "xray": xray, "log": log}


def serve(h, name):
    def send(code, obj):
        b = json.dumps(obj).encode()
        h.send_response(code)
        h.send_header("Content-Type", "application/json")
        h.send_header("Content-Length", str(len(b)))
        h.end_headers()
        h.wfile.write(b)
    fn = ROUTES.get(name)
    if not fn:
        return send(404, {"error": "not found"})
    need = _need_code()
    if need and name != "config" and not hmac.compare_digest(h.headers.get("x-access-code", ""), need):
        return send(401, {"error": "Wrong or missing access code."})
    try:
        n = int(h.headers.get("Content-Length") or 0)
        body = json.loads(h.rfile.read(n) or b"{}") if n else {}
        send(200, fn(body))
    except p.ModelError as e:
        send(502, {"error": str(e)})
    except ValueError as e:
        send(400, {"error": str(e)})
    except Exception as e:
        send(500, {"error": f"{type(e).__name__}: {e}"})


def make_handler(name):
    class handler(BaseHTTPRequestHandler):
        def do_POST(self):
            serve(self, name)

        do_GET = do_POST

        def log_message(self, *a):
            pass
    return handler
