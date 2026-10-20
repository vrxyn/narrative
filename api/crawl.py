import os, sys
from http.server import BaseHTTPRequestHandler

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _handlers import serve


class handler(BaseHTTPRequestHandler):
    def do_POST(self):
        serve(self, "crawl")

    def do_GET(self):
        serve(self, "crawl")

    def log_message(self, *args):
        pass
