import http.client
import json
import os
import socket
import tempfile
import threading
import unittest
from pathlib import Path

with socket.socket() as _s:
    _s.bind(("127.0.0.1", 0))
    PORT = _s.getsockname()[1]
DATA = tempfile.mkdtemp()
os.environ.update(NOTES_STUDIO_PORT=str(PORT), NOTES_STUDIO_DATA=DATA)

import server  # noqa: E402  (needs the env above)


class Pure(unittest.TestCase):
    def test_srs_again_resets_and_retries_in_ten_minutes(self):
        self.assertEqual(server.schedule(10, 2.5, 5, 0), (0, 2.3, 0, 600))

    def test_srs_first_two_reviews_have_fixed_steps(self):
        self.assertEqual(server.schedule(0, 2.5, 0, 2)[:3], (1, 2.5, 1))
        self.assertEqual(server.schedule(1, 2.5, 1, 2)[:3], (3, 2.5, 2))
        self.assertEqual(server.schedule(0, 2.5, 0, 3)[0], 3)          # easy on the first review

    def test_srs_interval_grows_by_ease_and_never_below_one_day(self):
        ivl, ease, reps, _ = server.schedule(6, 2.5, 2, 2)
        self.assertEqual((round(ivl, 2), reps), (15.0, 3))
        self.assertGreaterEqual(server.schedule(1, 1.3, 5, 1)[0], 1)

    def test_srs_ease_floor(self):
        ease = 1.4
        for _ in range(20):
            _, ease, _, _ = server.schedule(5, ease, 5, 1)
        self.assertEqual(ease, 1.3)

    def test_fts_query_neutralises_operators(self):
        self.assertEqual(server.fts_query('foo OR "bar" -baz*'), '"foo"* "OR"* "bar"* "baz"*')
        self.assertEqual(server.fts_query("!!!"), "")

    def test_tags_normalised_sorted_deduped(self):
        self.assertEqual(server.norm_tags(" B,a ,A,b!"), "a,b")
        self.assertEqual(server.norm_tags(["x", "X"]), "x")

    def test_safe_name_strips_paths(self):
        self.assertEqual(server.safe_name("../../etc/pa ss$wd"), "pa ss_wd")
        self.assertEqual(server.safe_name(""), "file")


class Live(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        server.init()
        cls.httpd = server.ThreadingHTTPServer(("127.0.0.1", PORT), server.H)
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()

    def call(self, method, path, body=None, token=True, host=None, origin=None):
        c = http.client.HTTPConnection("127.0.0.1", PORT)
        h = {"Host": host or f"127.0.0.1:{PORT}"}
        if token:
            h["X-Token"] = server.TOKEN
        if origin:
            h["Origin"] = origin
        c.request(method, path, json.dumps(body) if body is not None else None, h)
        r = c.getresponse()
        raw = r.read()
        try:
            return r.status, json.loads(raw)
        except ValueError:
            return r.status, raw

    def test_security_gates(self):
        self.assertEqual(self.call("GET", "/api/themes", token=False)[0], 401)
        self.assertEqual(self.call("GET", "/api/themes", host="evil.example")[0], 403)
        self.assertEqual(self.call("POST", "/api/note", {"title": "x"}, origin="http://evil.example")[0], 403)
        self.assertEqual(self.call("GET", "/api/themes")[0], 200)

    def test_page_embeds_token_only_for_the_right_host(self):
        c = http.client.HTTPConnection("127.0.0.1", PORT)
        c.request("GET", "/", headers={"Host": "evil.example"})
        self.assertEqual(c.getresponse().status, 403)

    def test_create_search_trash_restore(self):
        _, made = self.call("POST", "/api/note", {"title": "Raft", "body": "leader election and log replication", "theme": "Distributed", "tags": "Consensus, notes"})
        nid = made["id"]
        _, found = self.call("GET", "/api/notes?q=replic")                   # prefix search through FTS5
        self.assertIn(nid, [n["id"] for n in found["notes"]])
        self.call("PUT", f"/api/note/{nid}", {"body": "changed completely"})
        self.assertNotIn(nid, [n["id"] for n in self.call("GET", "/api/notes?q=replication")[1]["notes"]])   # index follows edits
        self.call("DELETE", f"/api/note/{nid}")
        self.assertNotIn(nid, [n["id"] for n in self.call("GET", "/api/notes")[1]["notes"]])
        self.assertIn(nid, [n["id"] for n in self.call("GET", "/api/notes?view=trash")[1]["notes"]])
        self.call("POST", f"/api/note/{nid}/restore")
        self.assertIn(nid, [n["id"] for n in self.call("GET", "/api/notes")[1]["notes"]])

    def test_empty_theme_falls_back_to_inbox(self):
        _, made = self.call("POST", "/api/note", {"title": "t", "body": "b"})
        self.call("PUT", f"/api/note/{made['id']}", {"theme": "  "})
        self.assertEqual(self.call("GET", f"/api/note/{made['id']}")[1]["theme"], "Inbox")

    def test_grade_validation_and_scheduling(self):
        _, made = self.call("POST", "/api/note", {"title": "card", "body": "front"})
        self.assertEqual(self.call("POST", "/api/study/grade", {"id": made["id"], "grade": 9})[0], 400)
        st, out = self.call("POST", "/api/study/grade", {"id": made["id"], "grade": 2})
        self.assertEqual((st, out["next_days"]), (200, 1.0))
        self.assertEqual(self.call("POST", "/api/study/grade", {"id": 999999, "grade": 2})[0], 404)


if __name__ == "__main__":
    unittest.main()
