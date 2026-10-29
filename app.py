#!/usr/bin/env python3
"""Kobo Inbox: drop EPUB/PDF files into a web page, pick them up in KOReader.

Upload books from any browser (phone or computer). KOReader's built-in OPDS
catalog sync then downloads anything new into a folder on the e-reader.

  Laptop on your Wi-Fi:  pip install -r requirements.txt && python app.py
  Server behind HTTPS:   docker compose up -d --build   (same code; see README.md)

Everything the app remembers lives in DATA_DIR (default: ./data):
  books/           the uploaded files, stored as <id>.epub or <id>.pdf
  library.json     title, author, size and upload time of each book, oldest first
  credentials.txt  generated passwords for the "upload" and "reader" accounts
"""
from __future__ import annotations

import hmac
import html
import json
import logging
import os
import re
import secrets
import socket
import threading
import unicodedata
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit
from xml.etree import ElementTree as ET

from flask import Flask, Response, abort, redirect, render_template_string, request, send_file

app = Flask(__name__)
log = logging.getLogger("kobo-inbox")
LOCK = threading.Lock()  # only one upload or delete changes library.json at a time

KINDS = {"epub": "application/epub+zip", "pdf": "application/pdf"}
ROLES = ("upload", "reader")  # upload: full access. reader: feed + downloads only (for the e-reader)
ATOM = "http://www.w3.org/2005/Atom"
OPDS_TYPE = "application/atom+xml;profile=opds-catalog;kind=acquisition"
ACQUISITION = "http://opds-spec.org/acquisition"
ET.register_namespace("", ATOM)


# ---------------------------------------------------------------- setup ----

def configure(data_dir=None, credentials=None, max_upload_mb=None, usernames=None) -> Flask:
    """Point the app at its data folder and load accounts. Called once at startup.
    Usernames default to the role names; change them with UPLOAD_USER / READER_USER."""
    data = Path(data_dir or os.environ.get("DATA_DIR") or Path(__file__).with_name("data")).resolve()
    (data / "books").mkdir(parents=True, exist_ok=True)
    (data / "tmp").mkdir(exist_ok=True)
    for leftover in (data / "tmp").glob("*.part"):  # half-finished uploads from a previous run
        leftover.unlink(missing_ok=True)
    mb = int(max_upload_mb or os.environ.get("MAX_UPLOAD_MB", "90"))  # Cloudflare's limit is 100 MB
    passwords = credentials or load_credentials(data / "credentials.txt")
    names = usernames or {role: os.environ.get(f"{role.upper()}_USER") or role for role in ROLES}
    if len(set(names.values())) != len(ROLES):
        raise SystemExit("UPLOAD_USER and READER_USER must be different.")
    app.config.update(
        DATA_DIR=data,
        BOOKS_DIR=data / "books",
        LIBRARY_FILE=data / "library.json",
        MAX_UPLOAD_MB=mb,
        MAX_CONTENT_LENGTH=mb * 1024 * 1024,
        USERNAMES=names,
        ACCOUNTS={names[role]: (role, passwords[role]) for role in ROLES},  # username -> (role, password)
    )
    return app


def load_credentials(path: Path) -> dict[str, str]:
    """Password for each role. Comes from UPLOAD_PASSWORD / READER_PASSWORD if set,
    otherwise from credentials.txt. Missing ones are generated once and saved there."""
    saved: dict[str, str] = {}
    if path.exists():
        for line in path.read_text("utf-8").splitlines():
            key, sep, value = line.partition("=")
            if sep:
                saved[key.strip()] = value.strip()
    creds, generated = {}, False
    allow_weak = os.environ.get("ALLOW_WEAK_PASSWORDS") == "1"
    for role in ROLES:
        password = os.environ.get(f"{role.upper()}_PASSWORD") or saved.get(role)
        if not password:
            password = saved[role] = secrets.token_urlsafe(24)
            generated = True
        if len(password) < 12:
            if not allow_weak:
                raise SystemExit(f"The '{role}' password must be at least 12 characters long "
                                 "(set ALLOW_WEAK_PASSWORDS=1 to allow it anyway).")
            log.warning("The '%s' password is short and easy to guess. Change it soon.", role)
        creds[role] = password
    if generated:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write("".join(f"{role}={pw}\n" for role, pw in saved.items()))
        log.info("Generated new passwords in %s", path)
    return creds


# --------------------------------------------------------------- state ----

def load_library() -> list[dict]:
    """Every book, oldest first. Upload order is what KOReader's sync relies on."""
    try:
        return json.loads(app.config["LIBRARY_FILE"].read_text("utf-8"))
    except FileNotFoundError:
        return []


def save_library(books: list[dict]) -> None:
    path = app.config["LIBRARY_FILE"]
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(books, indent=2, ensure_ascii=False), "utf-8")
    os.replace(tmp, path)  # atomic: nobody ever reads a half-written file


def book_path(book: dict) -> Path:
    return app.config["BOOKS_DIR"] / f"{book['id']}.{book['kind']}"


def newest_first() -> list[dict]:
    return [b for b in reversed(load_library()) if book_path(b).exists()]


# ------------------------------------------------------------- security ----

def client_ip() -> str:
    # For log messages only: anyone on the LAN could fake this header.
    return request.headers.get("CF-Connecting-IP") or request.remote_addr or "?"


def require(*roles: str) -> str:
    """Return the logged-in user's role, or stop the request with 401 (who are you?)
    or 403 (you can't do that)."""
    auth = request.authorization
    role = None
    if auth and auth.type == "basic":
        account = app.config["ACCOUNTS"].get(auth.username or "")
        if account and hmac.compare_digest((auth.password or "").encode(), account[1].encode()):
            role = account[0]
    if role is None:
        if auth:
            log.warning("Failed login as %r from %s", auth.username, client_ip())
        abort(Response("Login required.\n", 401,
                       {"WWW-Authenticate": 'Basic realm="Kobo Inbox", charset="UTF-8"'}))
    if role not in roles:
        abort(403)
    return role


def require_same_origin() -> None:
    """Reject form posts coming from other websites (cross-site request forgery).
    Browsers re-send saved passwords automatically, so this check matters."""
    site = request.headers.get("Sec-Fetch-Site")
    origin = request.headers.get("Origin")
    if site is not None:
        ok = site == "same-origin"
    elif origin is not None:
        ok = urlsplit(origin).netloc == request.host
    else:
        ok = True  # not a browser (curl, scripts): nothing was sent on the user's behalf
    if not ok:
        log.warning("Blocked cross-site %s %s from %s", request.method, request.path, client_ip())
        abort(403)


@app.after_request
def security_headers(resp: Response) -> Response:
    resp.headers["Cache-Control"] = "private, no-store"  # no proxy or CDN may keep a copy
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Referrer-Policy"] = "same-origin"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Content-Security-Policy"] = (
        "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; "
        "frame-ancestors 'none'; base-uri 'none'")
    return resp


# ------------------------------------------------------- reading books ----

def read_small(z: zipfile.ZipFile, name: str, limit: int = 2_000_000) -> bytes:
    info = z.getinfo(name)
    if info.file_size > limit:  # guard against "zip bomb" files
        raise ValueError("EPUB metadata is unreasonably large")
    return z.read(info)


def first_tag(xml: bytes, tag: bytes) -> str | None:
    """Text of the first <tag> or <prefix:tag> element. A regex instead of an XML
    parser keeps hostile files from abusing the parser."""
    pattern = rb"<(?:[\w.-]+:)?" + tag + rb"\b[^>]*>(.*?)</(?:[\w.-]+:)?" + tag + rb"\s*>"
    match = re.search(pattern, xml, re.S)
    return match.group(1).decode("utf-8", "replace") if match else None


def epub_metadata(path: Path) -> tuple[str | None, str | None]:
    """Return (title, author) from an EPUB, or raise ValueError if it isn't one."""
    try:
        with zipfile.ZipFile(path) as z:
            container = read_small(z, "META-INF/container.xml")
            match = re.search(rb'full-path\s*=\s*["\']([^"\']+)["\']', container)
            if not match:
                raise ValueError("not a valid EPUB file")
            opf = read_small(z, match.group(1).decode("utf-8", "replace"))
    except ValueError:
        raise
    except Exception:  # not a zip, corrupt, encrypted, missing files...
        raise ValueError("not a valid EPUB file") from None
    return first_tag(opf, b"title"), first_tag(opf, b"creator")


def check_pdf(path: Path) -> None:
    with open(path, "rb") as f:
        if b"%PDF-" not in f.read(1024):
            raise ValueError("not a valid PDF file")


def clean(text: str | None, fallback: str, limit: int = 150) -> str:
    """Make metadata safe to display and to use in a file name on the e-reader."""
    text = re.sub(r"<[^>]*>", "", html.unescape(text or ""))
    text = "".join(ch for ch in text if unicodedata.category(ch)[0] != "C")  # control chars
    text = " ".join(text.split())[:limit].strip()
    return text or fallback


def unique_title(books: list[dict], book: dict) -> str:
    """KOReader saves synced books as "Author - Title.ext" and skips names it already
    has, so two different books must never share an author + title."""
    taken = {(b["author"].casefold(), b["title"].casefold(), b["kind"]) for b in books}
    title, n = book["title"], 2
    while (book["author"].casefold(), title.casefold(), book["kind"]) in taken:
        title, n = f"{book['title']} ({n})", n + 1
    return title


def store_upload(upload) -> dict:
    original = re.split(r"[\\/]", upload.filename or "")[-1]  # never trust the sender's path
    kind = original.rsplit(".", 1)[-1].lower() if "." in original else ""
    if kind not in KINDS:
        raise ValueError("only .epub and .pdf files are accepted")
    book_id = f"{datetime.now(timezone.utc):%Y%m%d-%H%M%S}-{secrets.token_hex(4)}"
    part = app.config["DATA_DIR"] / "tmp" / f"{book_id}.part"
    upload.save(part)
    try:
        if kind == "epub":
            title, author = epub_metadata(part)
        else:
            check_pdf(part)
            title, author = None, None
        book = {
            "id": book_id,
            "kind": kind,
            "title": clean(title, clean(Path(original).stem.replace("_", " ").strip(" ."), "Untitled")),
            "author": clean(author, "Unknown Author"),
            "size": part.stat().st_size,
            "uploaded": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        with LOCK:
            books = load_library()
            book["title"] = unique_title(books, book)
            os.replace(part, book_path(book))
            books.append(book)
            save_library(books)
    finally:
        part.unlink(missing_ok=True)
    log.info("Added %s (%s, %d bytes) from %s", book["title"], kind, book["size"], client_ip())
    return book


# --------------------------------------------------------------- pages ----

PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Kobo Inbox</title>
<style>
 body{font-family:system-ui,sans-serif;max-width:42rem;margin:1.5rem auto;padding:0 1rem;line-height:1.45}
 form.upload{border:2px dashed #999;border-radius:.6rem;padding:1rem;margin:1rem 0}
 button{font-size:1rem;padding:.4rem .9rem}
 .ok{color:#185c1e}.error{color:#a01010}
 li{margin:.5rem 0}small{color:#555}
 details{display:inline}details form{display:inline}
 code{background:#eee;padding:.1rem .3rem;border-radius:.2rem;word-break:break-all}
</style></head><body>
<h1>&#128218; Kobo Inbox</h1>
{% for kind, text in messages %}<p class="{{ kind }}">{{ text }}</p>{% endfor %}
<form class="upload" method="post" action="upload" enctype="multipart/form-data">
 <input type="file" name="files" accept=".epub,.pdf,application/epub+zip,application/pdf" multiple required>
 <button>Upload</button><br>
 <small>EPUB or PDF, up to {{ max_mb }} MB per upload.</small>
</form>
<h2>Books ({{ books|length }})</h2>
<ul>
{% for b in books %}
 <li><a href="books/{{ b.id }}.{{ b.kind }}">{{ b.title }}</a> &mdash; {{ b.author }}<br>
  <small>{{ b.kind|upper }} &middot; {{ "%.1f"|format(b.size / 1048576) }} MB &middot; {{ b.uploaded[:10] }}</small>
  <details><summary>Delete</summary>
   <form method="post" action="books/{{ b.id }}/delete"><button>Really delete</button></form>
  </details></li>
{% else %}
 <li>No books yet.</li>
{% endfor %}
</ul>
<p><small>KOReader catalog: <code>{{ feed_url }}</code>, user <code>{{ reader_user }}</code>.</small></p>
</body></html>
"""


def page(messages=(), status=200):
    scheme = request.headers.get("X-Forwarded-Proto", request.scheme)  # https behind a tunnel
    return render_template_string(
        PAGE, books=newest_first(), messages=messages, max_mb=app.config["MAX_UPLOAD_MB"],
        feed_url=f"{scheme}://{request.host}/opds",
        reader_user=app.config["USERNAMES"]["reader"]), status


@app.get("/")
def index():
    require("upload")
    return page()


@app.post("/upload")
def upload():
    require("upload")
    require_same_origin()
    files = [f for f in request.files.getlist("files") if f.filename]
    messages = []
    for f in files:
        try:
            book = store_upload(f)
            messages.append(("ok", f"Added \u201c{book['title']}\u201d by {book['author']}."))
        except ValueError as err:
            messages.append(("error", f"{f.filename}: {err}"))
    if not files:
        messages.append(("error", "Choose at least one EPUB or PDF file."))
    return page(messages, 200 if any(kind == "ok" for kind, _ in messages) else 400)


@app.post("/books/<book_id>/delete")
def delete(book_id):
    require("upload")
    require_same_origin()
    with LOCK:
        books = load_library()
        book = next((b for b in books if b["id"] == book_id), None)
        if book is None:
            abort(404)
        books.remove(book)
        save_library(books)
        book_path(book).unlink(missing_ok=True)
    log.info("Deleted %s from %s", book["title"], client_ip())
    return redirect("/", 303)


@app.get("/books/<name>")
def download(name):
    require("reader", "upload")
    book_id, _, kind = name.partition(".")
    book = next((b for b in load_library() if b["id"] == book_id and b["kind"] == kind), None)
    if book is None or not book_path(book).exists():
        abort(404)
    filename = re.sub(r"[\\/]", "-", f"{book['author']} - {book['title']}.{kind}")
    return send_file(book_path(book), mimetype=KINDS[kind], as_attachment=True,
                     download_name=filename)


@app.get("/opds")
def opds():
    """The catalog KOReader reads: an Atom feed listing every book, newest first.
    KOReader remembers the newest link it downloaded and next time fetches only
    entries above it, so the order matters and links must never change."""
    require("reader", "upload")
    books = newest_first()
    feed = ET.Element(f"{{{ATOM}}}feed")

    def add(parent, tag, text=None, **attrs):
        element = ET.SubElement(parent, f"{{{ATOM}}}{tag}", attrs)
        element.text = text
        return element

    add(feed, "id", "urn:kobo-inbox:library")
    add(feed, "title", "Kobo Inbox")
    add(feed, "updated", books[0]["uploaded"] if books else
        datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
    add(add(feed, "author"), "name", "Kobo Inbox")
    add(feed, "link", rel="self", href="opds", type=OPDS_TYPE)
    add(feed, "link", rel="start", href="opds", type=OPDS_TYPE)
    for b in books:
        entry = add(feed, "entry")
        add(entry, "title", b["title"])
        add(entry, "id", f"urn:kobo-inbox:book:{b['id']}")
        add(entry, "updated", b["uploaded"])
        add(add(entry, "author"), "name", b["author"])
        add(entry, "link", rel=ACQUISITION, href=f"books/{b['id']}.{b['kind']}",
            type=KINDS[b["kind"]], length=str(b["size"]))
    ET.indent(feed)
    body = ET.tostring(feed, encoding="utf-8", xml_declaration=True)
    return Response(body, content_type=f"{OPDS_TYPE}; charset=utf-8")


@app.get("/healthz")
def healthz():
    return "ok\n"


@app.errorhandler(413)
def too_large(_err):
    return f"That upload is too big (limit {app.config['MAX_UPLOAD_MB']} MB).\n", 413


# ---------------------------------------------------------------- main ----

def lan_ip() -> str:
    """Best guess at this computer's address on the local network (sends nothing)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def main() -> None:
    from waitress import serve

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    configure()
    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "8080"))
    if not Path("/.dockerenv").exists():  # inside Docker this address would be misleading
        url, names = f"http://{lan_ip()}:{port}", app.config["USERNAMES"]
        print(f"\n  Upload page:      {url}/       (user: {names['upload']})")
        print(f"  KOReader catalog: {url}/opds   (user: {names['reader']})")
        creds_file = app.config["DATA_DIR"] / "credentials.txt"
        print(f"  Passwords:        {creds_file if creds_file.exists() else 'from environment'}\n")
    serve(app, host=host, port=port, threads=4, ident="kobo-inbox",
          # Refuse oversized bodies before buffering them, even from strangers.
          max_request_body_size=app.config["MAX_CONTENT_LENGTH"] + 1024 * 1024)


if __name__ == "__main__":
    main()
