"""X-Ray pipeline: crawl -> chunk -> extract claims -> verify quotes -> grounded X-Ray.

Model calls go through the Claude Code CLI (`claude -p`), so runs use the
logged-in Claude subscription instead of an API key.
"""
import difflib, json, os, re, shutil, subprocess, time, urllib.error, urllib.parse, urllib.request
from concurrent.futures import ThreadPoolExecutor
from html.parser import HTMLParser

MAX_PAGES = int(os.environ.get("XRAY_MAX_PAGES", "8"))
MAX_CHARS_PAGE = 12000
CHUNK_CHARS = 700
MODEL = os.environ.get("XRAY_MODEL", "")  # empty = CLI default
PRIORITY = ["product", "platform", "solution", "about", "customer", "case-stud", "case",
            "partner", "pricing", "use-case", "how-it-works", "security", "compliance",
            "team", "company", "industr", "developer", "docs"]
SKIP_TAGS = {"script", "style", "nav", "footer", "noscript", "svg", "form", "template", "iframe"}
SKIP_ATTR = re.compile(r"cookie|consent|gdpr|navbar|nav-|menu|footer|banner-cookie|modal|popup", re.I)
VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}
BLOCK = {"p", "div", "section", "article", "li", "h1", "h2", "h3", "h4", "h5", "h6", "tr", "br", "main", "header"}


# ---------- crawl ----------

class Extract(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts, self.links, self.stack, self.skip_at, self.title, self.meta = [], [], [], None, "", ""
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "a" and a.get("href"):
            self.links.append(a["href"])
        if tag == "title":
            self._in_title = True
        if tag == "meta" and (a.get("name") or a.get("property") or "").lower() in ("description", "og:description"):
            self.meta = self.meta or (a.get("content") or "")
        if tag in VOID:
            if tag == "br":
                self.parts.append("\n")
            return
        self.stack.append(tag)
        if self.skip_at is None:
            marker = " ".join(filter(None, [a.get("class"), a.get("id"), a.get("aria-label"), a.get("role")]))
            if tag in SKIP_TAGS or (tag in ("div", "section", "aside", "header") and SKIP_ATTR.search(marker or "")):
                self.skip_at = len(self.stack)
        if tag in BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False
        if tag in VOID or tag not in self.stack:
            return
        while self.stack:
            t = self.stack.pop()
            if self.skip_at is not None and len(self.stack) < self.skip_at:
                self.skip_at = None
            if t == tag:
                break
        if tag in BLOCK:
            self.parts.append("\n")

    def handle_data(self, d):
        if self._in_title:
            self.title += d
        elif self.skip_at is None and d.strip():
            self.parts.append(d.strip() + " ")


def clean(text):
    text = re.sub(r"(?:\b\d\b[\s,.]*){6,}", " ", text)          # animated counter digits
    text = re.sub(r"[ \t ‍]+", " ", text)
    lines, seen = [], set()
    for ln in (l.strip() for l in text.split("\n")):
        if len(ln) < 3 or ln.lower() in seen:
            continue
        seen.add(ln.lower())
        lines.append(ln)
    return "\n".join(lines)


def fetch(url):
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126 Safari/537.36",
        "Accept-Language": "en"})
    with urllib.request.urlopen(req, timeout=20) as r:
        if "html" not in r.headers.get("Content-Type", "html"):
            raise ValueError("not html")
        return r.geturl(), r.read().decode("utf-8", "replace")


def parse(html):
    p = Extract()
    p.feed(html)
    body = clean("".join(p.parts))
    head = " ".join(x.strip() for x in (p.title, p.meta) if x.strip())
    return (head + "\n" + body).strip()[:MAX_CHARS_PAGE], p.links, p.title.strip()


def normalize_url(u):
    u = u.strip()
    if not re.match(r"https?://", u, re.I):
        u = "https://" + u
    return u


def crawl(url, log=print):
    root_url, html = fetch(normalize_url(url))
    text, links, title = parse(html)
    pages = [{"url": root_url, "title": title, "text": text}]
    host = urllib.parse.urlparse(root_url).netloc.replace("www.", "")
    cands = {}
    for l in links:
        u = urllib.parse.urljoin(root_url, l).split("#")[0].split("?")[0].rstrip("/")
        pu = urllib.parse.urlparse(u)
        if pu.scheme not in ("http", "https") or pu.netloc.replace("www.", "") != host:
            continue
        if u == root_url.rstrip("/") or re.search(r"\.(pdf|png|jpe?g|svg|zip)$|/(blog|news|careers|jobs|legal|privacy|terms)\b", u, re.I):
            continue
        path = pu.path.lower()
        score = min((i for i, k in enumerate(PRIORITY) if k in path), default=99)
        score += path.count("/") * 0.1                       # shallower first
        cands[u] = min(score, cands.get(u, 999))
    ranked = [u for u, _ in sorted(cands.items(), key=lambda x: x[1])][: MAX_PAGES * 2]

    def get_page(u):
        try:
            final, h = fetch(u)
            t, _, ti = parse(h)
            return {"url": final, "title": ti, "text": t}
        except Exception as e:
            log(f"skipped {u}: {e}")
            return None

    with ThreadPoolExecutor(max_workers=6) as ex:
        fetched = list(ex.map(get_page, ranked))
    for p in fetched:
        if len(pages) >= MAX_PAGES:
            break
        if p and len(p["text"]) > 200 and all(p["text"][:300] != q["text"][:300] for q in pages):
            pages.append(p)
            log(f"crawled {p['url']}")
    return pages


def chunk(pages):
    chunks = []
    for pi, p in enumerate(pages, 1):
        buf, n = "", 0
        for sent in re.split(r"(?<=[.!?])\s+|\n", p["text"]):
            if buf and len(buf) + len(sent) > CHUNK_CHARS:
                n += 1
                chunks.append({"id": f"P{pi}.{n}", "url": p["url"], "text": buf.strip()})
                buf = ""
            buf += sent + " "
        if buf.strip():
            n += 1
            chunks.append({"id": f"P{pi}.{n}", "url": p["url"], "text": buf.strip()})
    return chunks


# ---------- model ----------

class ModelError(RuntimeError):
    pass


def provider():
    p = os.environ.get("XRAY_PROVIDER", "").lower()
    return p or ("gemini" if os.environ.get("GEMINI_API_KEY") else "claude")


def gemini(prompt):
    key = os.environ["GEMINI_API_KEY"]
    models = [os.environ.get("GEMINI_MODEL", "gemini-3.1-flash-lite")] + [
        m.strip() for m in os.environ.get("GEMINI_FALLBACKS", "gemini-3.5-flash,gemini-3.8-flash").split(",") if m.strip()]
    body = json.dumps({"contents": [{"parts": [{"text": prompt}]}],
                       "generationConfig": {"responseMimeType": "application/json", "temperature": 0.2}}).encode()
    deadline = time.time() + 52          # stay inside a 60 s serverless limit
    last = ""
    for model in models:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        left = deadline - time.time()
        if left < 8:
            break
        req = urllib.request.Request(url, body, {"Content-Type": "application/json", "x-goog-api-key": key})
        try:
            with urllib.request.urlopen(req, timeout=min(left, 28)) as r:
                d = json.load(r)
        except urllib.error.HTTPError as e:
            last = f"{model}: HTTP {e.code} {e.read().decode('utf-8', 'replace')[:200]}"
            continue                      # overloaded, rate limited or retired: try the next model
        except (TimeoutError, OSError) as e:
            last = f"{model}: {type(e).__name__}"
            continue
        cands = d.get("candidates") or []
        if not cands or not cands[0].get("content"):
            last = f"{model}: no answer " + json.dumps(d.get("promptFeedback") or "")[:150]
            continue
        text = "".join(p.get("text", "") for p in cands[0]["content"].get("parts", []))
        u = d.get("usageMetadata", {})
        return text, {"in": u.get("promptTokenCount", 0),
                      "out": u.get("candidatesTokenCount", 0) + u.get("thoughtsTokenCount", 0),
                      "cost": 0.0, "model": model}
    raise ModelError("Gemini is busy or rate-limited right now. Wait a minute and try again. (" + last + ")")


def claude_cli(prompt):
    native = os.path.join(os.path.expanduser("~"), ".local", "bin", "claude.exe")
    exe = os.environ.get("CLAUDE_BIN") or (native if os.path.exists(native) else None)         or shutil.which("claude.cmd") or shutil.which("claude")
    if not exe:
        raise ModelError("Claude Code CLI not found. Install it, or set GEMINI_API_KEY to use Gemini.")
    cmd = [exe, "-p", "--output-format", "json", "--tools", ""]
    if MODEL:
        cmd += ["--model", MODEL]
    r = subprocess.run(cmd, input=prompt, capture_output=True, text=True, encoding="utf-8", timeout=600)
    try:
        d = json.loads(r.stdout)
    except json.JSONDecodeError:
        raise ModelError((r.stderr or r.stdout or "no output from claude CLI")[:500])
    if d.get("is_error"):
        msg = d.get("result") or "claude CLI error"
        if "auth" in msg.lower() or "login" in msg.lower() or "oauth" in msg.lower():
            msg += ". Run `claude auth login` in a terminal, then try again."
        raise ModelError(msg)
    u = d.get("usage", {})
    usage = {"in": u.get("input_tokens", 0) + u.get("cache_creation_input_tokens", 0) + u.get("cache_read_input_tokens", 0),
             "out": u.get("output_tokens", 0), "cost": d.get("total_cost_usd") or 0.0,
             "model": ",".join(d.get("modelUsage", {}).keys())}
    return d.get("result", ""), usage


def llm(prompt):
    return gemini(prompt) if provider() == "gemini" else claude_cli(prompt)


def parse_json(text):
    m = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.S)
    s = m.group(1) if m else text[text.find("{"): text.rfind("}") + 1]
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        raise ModelError("The model's reply wasn't valid JSON. Try again.")


EXTRACT = """You extract claims from a company's website for a sales-research tool.

Below are text chunks from {url}, each with an ID. List the claims the company makes about itself that a skeptical enterprise buyer would care about: what the product does, who it is for, named customers or partners, numbers and metrics, licences and certifications, team credentials, funding, comparisons ("only", "best", "largest"), and promises.

Rules:
- Every claim must carry an EXACT verbatim quote copied character-for-character from ONE chunk, 5 to 30 words. Do not fix typos, do not join text from two chunks, do not paraphrase inside "quote".
- "chunk" is the ID of the chunk containing the quote.
- Do not invent anything. Skip navigation, legal and cookie text.
- Return {count} claims, most important first.

Return only JSON:
{{"company": "<company name>", "claims": [{{"id": "C1", "chunk": "P1.2", "quote": "...", "claim": "<plain-English restatement>", "type": "product|buyer|customer|partner|metric|credential|compliance|team|funding|comparison|promise"}}]}}

CHUNKS:
{chunks}"""

XRAY = """You are writing an X-Ray: a short, sharp analysis of a company's public story, used to start a sales conversation with that company. The reader is the company's founder. It must show we understood their business, and name where their story is weak in a way that makes them want help.

Today's date: {today}. Dates before today are in the past: a certification, launch or deadline dated earlier than today has already happened, and you must not describe it as upcoming. If the site gives a past date but no proof the event happened, say the proof is missing.

Company site: {url}
Target buyer to analyse: {buyer}
Commercial objective: {objective}

You may ONLY use the verified claims below. Each has an ID. Absence of evidence is itself a finding: if something a cautious buyer needs (named customers, licences, numbers with sources, security attestations) is missing from the claims, say so.

Answer four questions. Each point is one or two plain sentences, specific, with no filler.
1. sells: what the company actually sells, as opposed to what it says it sells.
2. buyer: who appears to buy it (roles, company types), and how that compares with the target buyer if one is given.
3. beliefs: what it asks that buyer to believe before saying yes. Phrase each as a belief ("That ...").
4. gaps: where the evidence for those beliefs is thin, missing or contradictory. This is the most important section. Be concrete: name the claim and the proof that is missing. 3 to 6 points.

Label every point:
- known: directly stated in the cited claims
- inferred: your reasonable reading of the cited claims
- unknown: the claims don't say (cite the nearest claims that show the gap, or none)
- conflicting: cited claims disagree with each other

Every point must cite claim IDs from the list in "claims" (empty list only for "unknown"). Never cite an ID not in the list.

Return only JSON:
{{"company": "<name>",
 "headline": "<one sentence: the single most useful finding for the founder>",
 "sells": [{{"text": "...", "label": "known|inferred|unknown|conflicting", "claims": ["C1"]}}],
 "buyer": [...], "beliefs": [...], "gaps": [...],
 "opener": "<one or two sentences the founder could use to open an email to this company, referencing the top gap without being rude>"}}

VERIFIED CLAIMS:
{claims}"""


# ---------- verify ----------

def norm(s):
    s = s.lower().replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
    s = s.replace("–", "-").replace("—", "-")
    return re.sub(r"\s+", " ", s).strip()


def verify(claim, chunks_by_id, all_text):
    q = norm(claim.get("quote", ""))
    if len(q) < 8:
        return "failed", None
    c = chunks_by_id.get(claim.get("chunk"))
    if c and q in norm(c["text"]):
        return "exact", c
    # quote may straddle or sit in another chunk
    for cc in chunks_by_id.values():
        if q in norm(cc["text"]):
            return "exact", cc
    target = norm(c["text"]) if c else all_text
    sm = difflib.SequenceMatcher(None, q, target, autojunk=False)
    m = sm.find_longest_match(0, len(q), 0, len(target))
    if m.size / len(q) >= 0.85:
        return "fuzzy", c
    return "failed", c


# ---------- stages (each one fits a short serverless request) ----------

def stage_crawl(url, texts=None):
    """texts: optional [{"url", "text"}] supplied by the caller (for sites that only render in a browser)."""
    t0 = time.time()
    if texts:
        pages = [{"url": t.get("url") or url, "title": "", "text": clean(t["text"])[:MAX_CHARS_PAGE * 2]} for t in texts if t.get("text")]
    else:
        pages = crawl(url, log=lambda m: None)
    chunks = chunk(pages)
    if sum(len(p["text"]) for p in pages) < 400:
        raise ValueError("Couldn't read any text from this site. It probably builds its pages in the browser "
                         "(JavaScript) or blocks automated readers. Try a specific inner page URL, such as "
                         "/about or /products.")
    return {"url": pages[0]["url"],
            "pages": [{"url": p["url"], "title": p["title"], "chars": len(p["text"])} for p in pages],
            "chunks": chunks, "seconds": round(time.time() - t0, 1)}


def _extract(url, chunks, count):
    chunk_txt = "\n\n".join(f"[{c['id']}] ({c['url']})\n{c['text']}" for c in chunks)
    out, usage = llm(EXTRACT.format(url=url, chunks=chunk_txt, count=count))
    return parse_json(out), usage


def stage_claims(url, chunks):
    t0 = time.time()
    by_id = {c["id"]: c for c in chunks}
    if len(chunks) > 24:                       # two smaller requests in parallel keep each one fast
        mid = len(chunks) // 2
        parts = [(chunks[:mid], "10 to 22"), (chunks[mid:], "10 to 22")]
    else:
        parts = [(chunks, "15 to 35")]
    with ThreadPoolExecutor(max_workers=len(parts)) as ex:
        results = list(ex.map(lambda pc: _extract(url, pc[0], pc[1]), parts))
    claims, usage, company = [], {"in": 0, "out": 0, "cost": 0.0, "model": ""}, ""
    for ext, u in results:
        company = company or ext.get("company", "")
        claims += ext.get("claims", [])
        usage["in"] += u["in"]; usage["out"] += u["out"]; usage["cost"] += u["cost"]
        usage["model"] = usage["model"] or u["model"]
    for i, cl in enumerate(claims, 1):
        cl["id"] = f"C{i}"
    all_text = norm(" ".join(c["text"] for c in chunks))
    for cl in claims:
        status, c = verify(cl, by_id, all_text)
        cl["verified"] = status
        if c:
            cl["chunk"], cl["url"] = c["id"], c["url"]
    return {"company": company, "claims": claims, "usage": usage, "seconds": round(time.time() - t0, 1)}


def stage_xray(url, buyer, objective, claims):
    t0 = time.time()
    good = [c for c in claims if c.get("verified") != "failed"]
    claim_txt = "\n".join(f"{c['id']} [{c.get('type', '')}] {c['claim']} | quote: \"{c['quote']}\"" for c in good)
    out, usage = llm(XRAY.format(today=time.strftime("%d %B %Y"), url=url, buyer=buyer or "not given; infer the most likely buyer from the site",
                                 objective=objective or "not given; assume winning more enterprise customers",
                                 claims=claim_txt))
    xr = parse_json(out)
    ok_ids = {c["id"] for c in good}
    cited = bad = 0
    for sec in ("sells", "buyer", "beliefs", "gaps"):
        for pt in xr.get(sec, []):
            ids = pt.get("claims", []) or []
            bad += sum(1 for i in ids if i not in ok_ids)
            pt["claims"] = [i for i in ids if i in ok_ids]
            if pt["claims"] or pt.get("label") == "unknown":
                cited += 1
            elif pt.get("label") == "known":
                pt["label"] = "inferred"
    points = sum(len(xr.get(s, [])) for s in ("sells", "buyer", "beliefs", "gaps"))
    return {"xray": xr, "usage": usage, "points": points, "points_cited": cited, "bad_citations": bad,
            "seconds": round(time.time() - t0, 1)}
