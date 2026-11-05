"""The PC-side step: turn pasted text or a page into file links."""

import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "pc"))
import linkfind  # noqa: E402


PAGE = b"""<!doctype html><html><body>
<a href="/files/game.zip">game</a>
<a href="/about">about</a>
<a href="pack.pkg" download="pack.pkg">pkg</a>
<a href="/dl" download="renamed.bin">direct</a>
<a href="mailto:me@example.com">mail</a>
</body></html>"""

FILES = {
    "/files/game.zip": b"zip-bytes",
    "/pack.pkg": b"pkg-bytes-here",
    "/dl": b"renamed-bytes",
}


class Handler(BaseHTTPRequestHandler):
    def do_HEAD(self):
        self._reply(head=True)

    def do_GET(self):
        self._reply(head=False)

    def _reply(self, head):
        if self.path == "/page":
            body, ctype, name = PAGE, "text/html", None
        elif self.path in FILES:
            body, ctype, name = FILES[self.path], "application/octet-stream", self.path.rsplit("/", 1)[-1]
        else:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        if name == "dl" or self.path == "/dl":
            self.send_header("Content-Disposition", 'attachment; filename="renamed.bin"')
        self.end_headers()
        if not head:
            self.wfile.write(body)

    def log_message(self, fmt, *args):
        return


class LinkFindTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.thread.join(timeout=5)

    def test_extracts_urls_and_ignores_other_schemes(self):
        text = "see https://example.com/a.zip, and ftp://example.com/b.zip."
        self.assertEqual(linkfind.extract_urls(text), ["https://example.com/a.zip"])
        self.assertEqual(linkfind.name_from_url("https://example.com/a.zip"), "a.zip")

    def test_page_yields_file_links_only(self):
        found = linkfind.resolve(f"http://127.0.0.1:{self.port}/page")
        by_name = {link["name"]: link for link in found["links"]}
        self.assertEqual(set(by_name), {"game.zip", "pack.pkg", "renamed.bin"})
        self.assertNotIn("about", " ".join(by_name))
        self.assertEqual(by_name["game.zip"]["size"], len(FILES["/files/game.zip"]))
        self.assertEqual(by_name["renamed.bin"]["size"], len(FILES["/dl"]))
        self.assertIn(f"http://127.0.0.1:{self.port}/page", by_name["game.zip"]["headers"])
        self.assertTrue(found["errors"] == [] or all("No file" not in e for e in found["errors"]))

    def test_direct_file_is_not_downloaded_here(self):
        url = f"http://127.0.0.1:{self.port}/files/game.zip"
        found = linkfind.resolve(url)
        self.assertEqual(len(found["links"]), 1)
        self.assertEqual(found["links"][0]["name"], "game.zip")
        self.assertEqual(found["links"][0]["size"], len(FILES["/files/game.zip"]))
        self.assertEqual(found["errors"], [])

    def test_unique_names_and_header_filter(self):
        links = unique = [
            {"name": "a.zip", "url": "http://h/a"},
            {"name": "a.zip", "url": "http://h/b"},
        ]
        linkfind.unique_names(unique)
        self.assertEqual([l["name"] for l in links], ["a.zip", "a-2.zip"])
        self.assertEqual(
            linkfind.clean_headers("Referer: http://h\nX-Evil: 1\nCookie: a=b"),
            "Referer: http://h\nCookie: a=b")
        with self.assertRaises(ValueError):
            linkfind.check_url("file:///etc/passwd")

    def test_empty_and_unreachable(self):
        self.assertIn("Paste", linkfind.resolve("   ")["errors"][0])
        missed = linkfind.resolve(f"http://127.0.0.1:{self.port}/nope")
        self.assertEqual(missed["links"], [])
        self.assertTrue(missed["errors"])
