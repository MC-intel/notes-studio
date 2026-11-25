# Notes Studio

A private, local-first notes editor, organiser and **study tool** in one standard-library Python file plus one
HTML page. No dependencies, no accounts, nothing leaves the machine.

I built it to turn a pile of loose study notes (exported from a phone, scattered across folders) into
something I could search, reorganise and actually revise from, without handing the contents to a cloud service.

## What it does

* **Themes and tags**, with rename and merge, and bulk move / tag / merge / trash.
* **Full-text search** (SQLite FTS5, prefix matching) across titles, bodies and tags.
* **Markdown editor** with autosave and preview.
* **Import by drag-and-drop or upload** of txt, md, docx, odt, pdf and html. Text is extracted for search and the
  original is kept as an attachment.
* **Study mode**: spaced repetition (an SM-2-style scheduler) and fill-in-the-blank cards built from your notes.
  Notes marked sensitive are never used as cards.
* **Export** everything to a zip, **trash with restore**, starred notes.

## Design decisions

| Decision | Why |
|---|---|
| Binds `127.0.0.1` only, checks the `Host` header, and requires a random per-run token on every API call and a matching `Origin` | A local web app is still reachable by any web page you visit; these three checks stop DNS-rebinding and cross-site requests |
| Meant to be opened in a **separate browser profile** (`google-chrome --user-data-dir=... --app=URL`) | Extensions in your everyday profile can't read the page |
| Exits after four idle hours | A forgotten server shouldn't keep your notes reachable |
| SQLite with FTS5 kept in sync by triggers | One file to back up, and search that follows every edit (tested) |
| Scheduler is a **pure function** (`schedule()`) | The part most likely to be subtly wrong is trivially unit-testable |
| Search input is turned into quoted prefix tokens before it reaches FTS5 | User text can't inject FTS operators |
| Uploaded filenames are reduced to a safe character set and stored under a generated name | Path traversal through a filename is impossible |
| Data directory is mode 700 | Notes are private by default |

The scheduler's behaviours are pinned by tests: "again" resets progress and retries in ten minutes, the first two
reviews use fixed steps, later intervals grow by the ease factor, and ease never falls below 1.3.

## Run it

```sh
python3 server.py                                  # http://127.0.0.1:8765
NOTES_STUDIO_DATA=~/notes-data NOTES_STUDIO_PORT=9000 python3 server.py
NOTES_STUDIO_IMPORT=~/old-notes python3 server.py  # optional first-run import of a folder-per-theme tree
```

Python 3.10+ with SQLite FTS5 (standard in CPython builds). Optional: `pdftotext` for PDF text extraction.

## Tests

```sh
PYTHONPATH=. python3 -m unittest discover -s tests     # 12 tests, including a live server on a free port
```
