#!/usr/bin/env python3
"""Check a running Kobo Inbox from the outside, the way a browser and KOReader see it.

  python smoke_test.py http://127.0.0.1:8080 --credentials data/credentials.txt book.epub

Uploads the given EPUB/PDF, checks it shows up in the feed and downloads intact,
checks the security rules, then deletes the test copy again (unless --keep).
"""
import argparse
import base64
import hashlib
import os
import re
import secrets
import sys
import urllib.error
import urllib.request
from pathlib import Path


def request(url, user=None, password=None, data=None, headers=None, method=None):
    # Identify ourselves: Cloudflare's Browser Integrity Check blocks Python's default
    # "Python-urllib" user agent (error 1010).
    headers = {"User-Agent": "kobo-inbox-smoke-test/1.0", **(headers or {})}
    if user:
        token = base64.b64encode(f"{user}:{password}".encode()).decode()
        headers["Authorization"] = f"Basic {token}"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    opener = urllib.request.build_opener(NoRedirect)
    try:
        with opener.open(req, timeout=60) as resp:
            return resp.status, resp.headers, resp.read()  # headers: case-insensitive lookups
    except urllib.error.HTTPError as err:
        return err.code, err.headers, err.read()


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def multipart(path: Path):
    boundary = secrets.token_hex(16)
    body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"files\"; "
            f"filename=\"{path.name}\"\r\nContent-Type: application/octet-stream\r\n\r\n").encode()
    body += path.read_bytes() + f"\r\n--{boundary}--\r\n".encode()
    return body, {"Content-Type": f"multipart/form-data; boundary={boundary}"}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("base_url")
    parser.add_argument("book", type=Path)
    parser.add_argument("--credentials", type=Path, default=Path("data/credentials.txt"))
    parser.add_argument("--keep", action="store_true", help="leave the uploaded book in place")
    args = parser.parse_args()
    base = args.base_url.rstrip("/")
    # Same settings as the app: environment variables win over credentials.txt.
    creds = (dict(line.split("=", 1) for line in args.credentials.read_text().split())
             if args.credentials.exists() else {})
    env = os.environ.get
    up = (env("UPLOAD_USER") or "upload", env("UPLOAD_PASSWORD") or creds.get("upload"))
    rd = (env("READER_USER") or "reader", env("READER_PASSWORD") or creds.get("reader"))
    if not (up[1] and rd[1]):
        sys.exit("No passwords found: pass --credentials or set UPLOAD_PASSWORD / READER_PASSWORD.")
    failures = 0

    def check(name, ok, detail=""):
        nonlocal failures
        print(f"{'PASS' if ok else 'FAIL'}  {name}{'  (' + detail + ')' if detail and not ok else ''}")
        failures += not ok

    status, _, _ = request(f"{base}/healthz")
    check("health check answers without a password", status == 200, str(status))
    for path in ("/", "/opds"):
        status, headers, _ = request(base + path)
        check(f"{path} needs a password", status == 401 and "Basic" in headers.get("WWW-Authenticate", ""), str(status))
    status, _, _ = request(f"{base}/opds", rd[0], "wrong-password-here")
    check("wrong password is refused", status == 401, str(status))

    body, headers = multipart(args.book)
    status, _, _ = request(f"{base}/upload", *rd, data=body, headers=headers)
    check("reader account cannot upload", status == 403, str(status))
    status, _, _ = request(f"{base}/upload", *up, data=body, headers={**headers, "Sec-Fetch-Site": "cross-site"})
    check("upload from another website is blocked", status == 403, str(status))
    status, _, page = request(f"{base}/upload", *up, data=body, headers=headers)
    check("upload account can upload", status == 200 and b"Added" in page, str(status))

    status, headers, feed = request(f"{base}/opds", *rd)
    check("reader can read the feed", status == 200 and "opds-catalog" in headers.get("Content-Type", ""), str(status))
    hrefs = re.findall(rb'href="(books/[^"]+)"', feed)
    check("feed lists the new book first", bool(hrefs), "no book links in feed")
    if hrefs:
        href = hrefs[0].decode()
        status, headers, data = request(f"{base}/{href}", *rd)
        same = hashlib.sha256(data).hexdigest() == hashlib.sha256(args.book.read_bytes()).hexdigest()
        check("download is byte-for-byte identical", status == 200 and same, str(status))
        check("download is marked private / not cacheable", headers.get("Cache-Control") == "private, no-store",
              headers.get("Cache-Control", "missing"))
        cache_status = headers.get("CF-Cache-Status")
        if cache_status:  # only present when going through Cloudflare
            check("Cloudflare did not cache the book", cache_status in ("DYNAMIC", "BYPASS"), cache_status)
        status, _, _ = request(f"{base}/{href}")
        check("download needs a password", status == 401, str(status))
        if not args.keep:
            book_id = href.split("/", 1)[1].split(".", 1)[0]
            status, _, _ = request(f"{base}/books/{book_id}/delete", *up, data=b"", method="POST")
            check("test copy deleted again", status == 303, str(status))

    print("\nAll checks passed." if not failures else f"\n{failures} check(s) failed.")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
