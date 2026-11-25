#!/usr/bin/env python3
"""Notes Studio: private local notes editor / organizer / study tool.
Stdlib only. Listens on 127.0.0.1 only; Host + token checks guard against other web pages.
Data: ~/Documents/NotesStudio (override with NOTES_STUDIO_DATA; notes.db + uploads/, mode 700). Nothing is sent anywhere."""
import io, json, os, re, secrets, shutil, sqlite3, subprocess, sys, threading, time, zipfile, html
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs, unquote

PORT = int(os.environ.get("NOTES_STUDIO_PORT", "8765"))
HOME = Path.home()
DATA = Path(os.environ.get("NOTES_STUDIO_DATA", HOME / "Documents" / "NotesStudio"))
IMPORT_FROM = Path(os.environ["NOTES_STUDIO_IMPORT"]) if os.environ.get("NOTES_STUDIO_IMPORT") else None   # optional first-run import of a folder-per-theme tree
HERE = Path(__file__).resolve().parent
TOKEN = secrets.token_urlsafe(24)
MAX_UPLOAD = 60 * 1024 * 1024
IDLE_EXIT = 4 * 3600
last_req = time.time()
DB_LOCK = threading.Lock()

def now(): return int(time.time())

def db():
    c = sqlite3.connect(DATA / "notes.db", timeout=20)
    c.row_factory = sqlite3.Row
    return c

SCHEMA = """
CREATE TABLE IF NOT EXISTS notes(
 id INTEGER PRIMARY KEY, title TEXT NOT NULL DEFAULT '', body TEXT NOT NULL DEFAULT '',
 theme TEXT NOT NULL DEFAULT 'Inbox', tags TEXT NOT NULL DEFAULT '', source TEXT NOT NULL DEFAULT '',
 created INTEGER, modified INTEGER, deleted INTEGER NOT NULL DEFAULT 0,
 due INTEGER NOT NULL DEFAULT 0, ivl REAL NOT NULL DEFAULT 0, ease REAL NOT NULL DEFAULT 2.5, reps INTEGER NOT NULL DEFAULT 0,
 starred INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS attachments(
 id INTEGER PRIMARY KEY, note_id INTEGER NOT NULL, filename TEXT, stored TEXT, mime TEXT, size INTEGER);
CREATE VIRTUAL TABLE IF NOT EXISTS fts USING fts5(title, body, tags, content='notes', content_rowid='id', tokenize='unicode61');
CREATE TRIGGER IF NOT EXISTS n_ai AFTER INSERT ON notes BEGIN
 INSERT INTO fts(rowid,title,body,tags) VALUES (new.id,new.title,new.body,new.tags); END;
CREATE TRIGGER IF NOT EXISTS n_ad AFTER DELETE ON notes BEGIN
 INSERT INTO fts(fts,rowid,title,body,tags) VALUES('delete',old.id,old.title,old.body,old.tags); END;
CREATE TRIGGER IF NOT EXISTS n_au AFTER UPDATE ON notes BEGIN
 INSERT INTO fts(fts,rowid,title,body,tags) VALUES('delete',old.id,old.title,old.body,old.tags);
 INSERT INTO fts(rowid,title,body,tags) VALUES (new.id,new.title,new.body,new.tags); END;
"""

def init():
    DATA.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(DATA, 0o700)
    (DATA / "uploads").mkdir(exist_ok=True, mode=0o700)
    with DB_LOCK, db() as c:
        c.executescript(SCHEMA)
        n = c.execute("SELECT count(*) FROM notes").fetchone()[0]
        if n == 0 and IMPORT_FROM and IMPORT_FROM.is_dir():
            import_tree(c)

def theme_name(folder):
    t = re.sub(r"^\d+-", "", folder).replace("-", " ").strip()
    return t or "Inbox"

def import_tree(c):
    import datetime
    for d in sorted(IMPORT_FROM.iterdir()):
        if not d.is_dir() or d.name.startswith("_"): continue
        th = theme_name(d.name)
        for f in sorted(d.glob("*.md")):
            raw = f.read_text(errors="replace")
            parts = raw.split("---\n", 2)
            meta, body = (parts[1], parts[2]) if len(parts) == 3 and raw.startswith("---\n") else ("", raw)
            m = re.search(r"created: (\d{4}-\d\d-\d\d)", meta)
            ts = int(datetime.datetime.strptime(m.group(1), "%Y-%m-%d").timestamp()) if m else now()
            body = body.strip()
            title = (body.split("\n")[0] if body else "(empty)")[:80]
            c.execute("INSERT INTO notes(title,body,theme,source,created,modified,due) VALUES (?,?,?,?,?,?,?)",
                      (title, body, th, "Apple Notes", ts, ts, 0))

# ---------- document text extraction ----------
def extract_text(name, data):
    ext = Path(name).suffix.lower()
    try:
        if ext in (".txt", ".md", ".markdown", ".csv", ".log", ".json", ".yaml", ".yml", ".py", ".js", ".sql", ".ini", ".toml", ".xml"):
            return data.decode("utf-8", "replace")
        if ext in (".html", ".htm"):
            t = re.sub(r"(?is)<(script|style).*?</\1>", "", data.decode("utf-8", "replace"))
            return html.unescape(re.sub(r"<[^>]+>", " ", re.sub(r"(?i)</(p|div|h\d|li|br)>", "\n", t)))
        if ext in (".docx", ".odt"):
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                xml = z.read("word/document.xml" if ext == ".docx" else "content.xml").decode("utf-8", "replace")
            xml = re.sub(r"</w:p>|</text:p>|</text:h>", "\n", xml)
            xml = re.sub(r"<w:tab/>", "\t", xml)
            return html.unescape(re.sub(r"<[^>]+>", "", xml))
        if ext == ".pdf":
            p = subprocess.run(["pdftotext", "-layout", "-", "-"], input=data, capture_output=True, timeout=60)
            return p.stdout.decode("utf-8", "replace")
    except Exception as e:
        return f"(could not read text from this file: {type(e).__name__})"
    return ""

def safe_name(n):
    n = re.sub(r"[^A-Za-z0-9._ -]", "_", os.path.basename(n))[:120]
    return n or "file"

MIME = {".pdf": "application/pdf", ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif",
        ".webp": "image/webp", ".txt": "text/plain; charset=utf-8", ".md": "text/plain; charset=utf-8"}

# ---------- API ----------
def row(r, full=True):
    d = dict(r)
    if not full: d["body"] = d["body"][:300]
    d["tags"] = [t for t in d["tags"].split(",") if t]
    return d

def norm_tags(v):
    if isinstance(v, list): v = ",".join(v)
    return ",".join(sorted({re.sub(r"[^\w -]", "", t).strip().lower() for t in str(v).split(",") if t.strip()}))

def fts_query(q):
    toks = re.findall(r"\w+", q)
    return " ".join(f'"{t}"*' for t in toks)

def schedule(ivl, ease, reps, g):
    """SM-2-style spaced repetition. g: 0 again, 1 hard, 2 good, 3 easy.
    Returns (interval_days, ease, reps, seconds_until_due). 'Again' resets progress and retries in 10 minutes;
    ease never drops below 1.3 so a hard card can't be scheduled into an ever-shrinking interval."""
    if g == 0:
        return 0, max(1.3, ease - 0.2), 0, 600
    reps += 1
    if reps == 1: ivl = 1 if g < 3 else 3
    elif reps == 2: ivl = 3 if g < 3 else 6
    else: ivl = max(1, ivl * ease * {1: 0.8, 2: 1.0, 3: 1.3}[g])
    ease = max(1.3, ease + {1: -0.15, 2: 0, 3: 0.15}[g])
    return ivl, ease, reps, int(ivl * 86400)

def api(method, path, qs, body, headers):
    global last_req
    with DB_LOCK, db() as c:
        if path == "/api/themes":
            rows = c.execute("SELECT theme, count(*) n, sum(due<=? AND reps>0) due FROM notes WHERE deleted=0 GROUP BY theme ORDER BY theme", (now(),)).fetchall()
            tr = c.execute("SELECT count(*) FROM notes WHERE deleted=1").fetchone()[0]
            allc = c.execute("SELECT count(*) FROM notes WHERE deleted=0").fetchone()[0]
            st = c.execute("SELECT count(*) FROM notes WHERE deleted=0 AND starred=1").fetchone()[0]
            tags = {}
            for (t,) in c.execute("SELECT tags FROM notes WHERE deleted=0 AND tags!=''"):
                for x in t.split(","): tags[x] = tags.get(x, 0) + 1
            return {"themes": [dict(r) for r in rows], "trash": tr, "all": allc, "starred": st,
                    "tags": sorted(tags.items(), key=lambda kv: (-kv[1], kv[0]))}
        if path == "/api/notes" and method == "GET":
            theme = qs.get("theme", [""])[0]; tag = qs.get("tag", [""])[0]; q = qs.get("q", [""])[0].strip()
            view = qs.get("view", [""])[0]; sort = qs.get("sort", ["modified"])[0]
            where, args = ["deleted=?"], [1 if view == "trash" else 0]
            if theme: where.append("theme=?"); args.append(theme)
            if tag: where.append("(','||tags||',') LIKE ?"); args.append(f"%,{tag},%")
            if view == "starred": where.append("starred=1")
            order = {"modified": "modified DESC", "created": "created DESC", "title": "title COLLATE NOCASE", "oldest": "created ASC"}.get(sort, "modified DESC")
            if q:
                fq = fts_query(q)
                ids = [r[0] for r in c.execute("SELECT rowid FROM fts WHERE fts MATCH ? ORDER BY rank LIMIT 500", (fq,))] if fq else []
                if not ids: return {"notes": []}
                where.append("id IN (%s)" % ",".join("?" * len(ids))); args += ids
            rs = c.execute(f"SELECT * FROM notes WHERE {' AND '.join(where)} ORDER BY {order} LIMIT 1000", args).fetchall()
            return {"notes": [row(r, False) for r in rs]}
        m = re.fullmatch(r"/api/note/(\d+)", path)
        if m:
            nid = int(m.group(1))
            if method == "GET":
                r = c.execute("SELECT * FROM notes WHERE id=?", (nid,)).fetchone()
                if not r: return 404, {"error": "not found"}
                d = row(r)
                d["attachments"] = [dict(a) for a in c.execute("SELECT id,filename,mime,size FROM attachments WHERE note_id=?", (nid,))]
                return d
            if method == "PUT":
                f = {}
                for k in ("title", "body", "theme", "source"):
                    if k in body: f[k] = str(body[k])[:2_000_000]
                if "tags" in body: f["tags"] = norm_tags(body["tags"])
                if "starred" in body: f["starred"] = 1 if body["starred"] else 0
                if "theme" in f and not f["theme"].strip(): f["theme"] = "Inbox"
                if not f: return {"ok": True}
                f["modified"] = now()
                c.execute(f"UPDATE notes SET {','.join(k+'=?' for k in f)} WHERE id=?", list(f.values()) + [nid])
                return {"ok": True, "modified": f["modified"]}
            if method == "DELETE":
                if qs.get("hard", [""])[0] == "1":
                    for a in c.execute("SELECT stored FROM attachments WHERE note_id=?", (nid,)).fetchall():
                        try: (DATA / "uploads" / a[0]).unlink()
                        except OSError: pass
                    c.execute("DELETE FROM attachments WHERE note_id=?", (nid,)); c.execute("DELETE FROM notes WHERE id=?", (nid,))
                else:
                    c.execute("UPDATE notes SET deleted=1, modified=? WHERE id=?", (now(), nid))
                return {"ok": True}
        m = re.fullmatch(r"/api/note/(\d+)/restore", path)
        if m and method == "POST":
            c.execute("UPDATE notes SET deleted=0, modified=? WHERE id=?", (now(), int(m.group(1)))); return {"ok": True}
        if path == "/api/note" and method == "POST":
            t = now()
            cur = c.execute("INSERT INTO notes(title,body,theme,tags,source,created,modified,due) VALUES (?,?,?,?,?,?,?,0)",
                            (str(body.get("title", ""))[:300], str(body.get("body", "")), str(body.get("theme") or "Inbox"),
                             norm_tags(body.get("tags", "")), str(body.get("source", "")), t, t))
            return {"id": cur.lastrowid}
        if path == "/api/bulk" and method == "POST":
            ids = [int(i) for i in body.get("ids", [])]
            if not ids: return {"ok": True, "n": 0}
            ph = ",".join("?" * len(ids)); t = now()
            act = body.get("action")
            if act == "move": c.execute(f"UPDATE notes SET theme=?, modified=? WHERE id IN ({ph})", [body.get("theme") or "Inbox", t] + ids)
            elif act == "tag":
                for (i, tg) in c.execute(f"SELECT id,tags FROM notes WHERE id IN ({ph})", ids).fetchall():
                    c.execute("UPDATE notes SET tags=?, modified=? WHERE id=?", (norm_tags(tg + "," + body.get("tag", "")), t, i))
            elif act == "trash": c.execute(f"UPDATE notes SET deleted=1, modified=? WHERE id IN ({ph})", [t] + ids)
            elif act == "restore": c.execute(f"UPDATE notes SET deleted=0, modified=? WHERE id IN ({ph})", [t] + ids)
            elif act == "merge" and len(ids) > 1:
                rs = c.execute(f"SELECT * FROM notes WHERE id IN ({ph}) ORDER BY created", ids).fetchall()
                bodyt = "\n\n---\n\n".join(r["body"] for r in rs)
                cur = c.execute("INSERT INTO notes(title,body,theme,tags,source,created,modified,due) VALUES (?,?,?,?,?,?,?,0)",
                                ((rs[0]["title"] or "Merged") + " (merged)", bodyt, rs[0]["theme"], norm_tags(",".join(r["tags"] for r in rs)), "merged", t, t))
                c.execute(f"UPDATE notes SET deleted=1, modified=? WHERE id IN ({ph})", [t] + ids)
                return {"ok": True, "id": cur.lastrowid}
            return {"ok": True, "n": len(ids)}
        if path == "/api/theme/rename" and method == "POST":
            c.execute("UPDATE notes SET theme=? WHERE theme=?", (body["to"] or "Inbox", body["from"])); return {"ok": True}
        if path == "/api/study/next" and method == "GET":
            theme = qs.get("theme", [""])[0]; mode = qs.get("mode", ["due"])[0]
            w, a = ["deleted=0", "length(body)>=40", "theme NOT LIKE 'Sensitive%'"], []
            if theme: w.append("theme=?"); a.append(theme)
            if mode == "due":
                w.append("due<=?"); a.append(now()); order = "due ASC, random()"
            else: order = "random()"
            r = c.execute(f"SELECT * FROM notes WHERE {' AND '.join(w)} ORDER BY {order} LIMIT 1", a).fetchone()
            dueleft = c.execute(f"SELECT count(*) FROM notes WHERE {' AND '.join(w)}", a).fetchone()[0]
            return {"note": row(r) if r else None, "remaining": dueleft}
        if path == "/api/study/grade" and method == "POST":
            r = c.execute("SELECT ivl,ease,reps FROM notes WHERE id=?", (int(body["id"]),)).fetchone()
            if not r: return 404, {"error": "not found"}
            g = int(body["grade"])  # 0 again, 1 hard, 2 good, 3 easy
            if g not in (0, 1, 2, 3): return 400, {"error": "grade must be 0-3"}
            ivl, ease, reps, delay = schedule(r["ivl"], r["ease"], r["reps"], g)
            due = now() + delay
            c.execute("UPDATE notes SET ivl=?, ease=?, reps=?, due=? WHERE id=?", (ivl, ease, reps, due, int(body["id"])))
            return {"ok": True, "next_days": round(ivl, 1)}
        if path == "/api/stats":
            t = now()
            return {"total": c.execute("SELECT count(*) FROM notes WHERE deleted=0").fetchone()[0],
                    "words": sum(len(b.split()) for (b,) in c.execute("SELECT body FROM notes WHERE deleted=0")),
                    "due": c.execute("SELECT count(*) FROM notes WHERE deleted=0 AND length(body)>0 AND due<=?", (t,)).fetchone()[0],
                    "learned": c.execute("SELECT count(*) FROM notes WHERE deleted=0 AND reps>0").fetchone()[0]}
        if path == "/api/export" and method == "GET":
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
                for r in c.execute("SELECT * FROM notes WHERE deleted=0"):
                    slug = re.sub(r"[^A-Za-z0-9]+", "-", r["title"])[:50].strip("-") or "note"
                    z.writestr(f"{re.sub(r'[^A-Za-z0-9 _-]', '', r['theme']) or 'Inbox'}/{slug}-{r['id']}.md",
                               f"---\ntitle: {r['title']}\ntags: {r['tags']}\nsource: {r['source']}\n---\n\n{r['body']}\n")
            return ("bytes", buf.getvalue(), "application/zip", "notes-export.zip")
    return 404, {"error": "unknown endpoint"}

def do_upload(qs, body, headers):
    name = safe_name(unquote(headers.get("X-Filename", "upload")))
    theme = unquote(headers.get("X-Theme", "")) or "Inbox"
    if len(body) > MAX_UPLOAD: return 413, {"error": "file too large (60 MB max)"}
    text = extract_text(name, body).strip()
    ext = Path(name).suffix.lower()
    stored = f"{secrets.token_hex(8)}{ext}"
    (DATA / "uploads" / stored).write_bytes(body)
    os.chmod(DATA / "uploads" / stored, 0o600)
    t = now()
    with DB_LOCK, db() as c:
        cur = c.execute("INSERT INTO notes(title,body,theme,tags,source,created,modified,due) VALUES (?,?,?,?,?,?,?,0)",
                        (Path(name).stem[:200], text or f"(no text extracted; original file attached: {name})", theme, "document", "upload:" + name, t, t))
        c.execute("INSERT INTO attachments(note_id,filename,stored,mime,size) VALUES (?,?,?,?,?)",
                  (cur.lastrowid, name, stored, MIME.get(ext, "application/octet-stream"), len(body)))
    return {"id": cur.lastrowid, "chars": len(text)}

class H(BaseHTTPRequestHandler):
    server_version = "NotesStudio"
    def log_message(self, *a): pass
    def _ok_host(self):
        h = self.headers.get("Host", "")
        return h in (f"127.0.0.1:{PORT}", f"localhost:{PORT}")
    def _send(self, code, data, ctype="application/json", extra=None):
        if isinstance(data, (dict, list)): data = json.dumps(data).encode()
        elif isinstance(data, str): data = data.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype); self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store"); self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self'; img-src 'self' data: blob:; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; frame-src 'self'; object-src 'self'")
        for k, v in (extra or {}).items(): self.send_header(k, v)
        self.end_headers(); self.wfile.write(data)
    def _handle(self, method):
        global last_req
        last_req = time.time()
        if not self._ok_host(): return self._send(403, {"error": "bad host"})
        u = urlparse(self.path); qs = parse_qs(u.query)
        if method == "GET" and u.path in ("/", "/index.html"):
            page = (HERE / "static" / "index.html").read_text().replace("__TOKEN__", TOKEN)
            return self._send(200, page, "text/html; charset=utf-8")
        if self.headers.get("X-Token") != TOKEN and not (method == "GET" and u.path.startswith(("/file/", "/api/export")) and qs.get("t", [""])[0] == TOKEN):
            return self._send(401, {"error": "bad token"})
        origin = self.headers.get("Origin")
        if origin and origin not in (f"http://127.0.0.1:{PORT}", f"http://localhost:{PORT}"): return self._send(403, {"error": "bad origin"})
        try:
            ln = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(ln) if ln else b""
            if u.path == "/api/upload" and method == "POST":
                res = do_upload(qs, raw, self.headers)
            elif u.path.startswith("/file/"):
                aid = int(u.path.split("/")[2])
                with DB_LOCK, db() as c: a = c.execute("SELECT * FROM attachments WHERE id=?", (aid,)).fetchone()
                if not a: return self._send(404, {"error": "not found"})
                data = (DATA / "uploads" / a["stored"]).read_bytes()
                disp = "inline" if a["mime"] in ("application/pdf",) or a["mime"].startswith("image/") else "attachment"
                return self._send(200, data, a["mime"], {"Content-Disposition": f'{disp}; filename="{a["filename"]}"'})
            else:
                body = json.loads(raw) if raw else {}
                res = api(method, u.path, qs, body, self.headers)
            if isinstance(res, tuple) and res and res[0] == "bytes":
                return self._send(200, res[1], res[2], {"Content-Disposition": f'attachment; filename="{res[3]}"'})
            if isinstance(res, tuple): return self._send(res[0], res[1])
            return self._send(200, res)
        except Exception as e:
            return self._send(500, {"error": f"{type(e).__name__}: {e}"})
    def do_GET(self): self._handle("GET")
    def do_POST(self): self._handle("POST")
    def do_PUT(self): self._handle("PUT")
    def do_DELETE(self): self._handle("DELETE")

def watchdog(srv):
    while True:
        time.sleep(60)
        if time.time() - last_req > IDLE_EXIT:
            srv.shutdown(); return

def main():
    init()
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), H)
    threading.Thread(target=watchdog, args=(srv,), daemon=True).start()
    print(f"Notes Studio on http://127.0.0.1:{PORT}", flush=True)
    srv.serve_forever()

if __name__ == "__main__":
    main()
