"""Tests for Kobo Inbox. Run with:  python -m unittest -v"""
import io
import os
import tempfile
import unittest
import zipfile
from unittest import mock
from pathlib import Path
from xml.etree import ElementTree as ET

import app as inbox

PASSWORDS = {"upload": "upload-password-123", "reader": "reader-password-456"}
UPLOAD = ("upload", PASSWORDS["upload"])
READER = ("reader", PASSWORDS["reader"])
ATOM = "{http://www.w3.org/2005/Atom}"


def make_epub(title="A Test Book", author="Ada Lovelace") -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        z.writestr("META-INF/container.xml",
                   '<?xml version="1.0"?><container version="1.0" '
                   'xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles>'
                   '<rootfile full-path="OEBPS/content.opf" '
                   'media-type="application/oebps-package+xml"/></rootfiles></container>')
        z.writestr("OEBPS/content.opf",
                   '<?xml version="1.0"?><package xmlns="http://www.idpf.org/2007/opf" version="3.0">'
                   '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
                   f'<dc:title>{title}</dc:title><dc:creator id="c1">{author}</dc:creator>'
                   '</metadata></package>')
    return buf.getvalue()


PDF = b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n"


class InboxTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        inbox.configure(self.tmp.name, credentials=dict(PASSWORDS), max_upload_mb=1)
        inbox.app.config["TESTING"] = True
        self.client = inbox.app.test_client()
        self.data = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def upload(self, *files, auth=UPLOAD, headers=None):
        return self.client.post("/upload", auth=auth, headers=headers or {},
                                content_type="multipart/form-data",
                                data={"files": [(io.BytesIO(body), name) for name, body in files]})

    def feed(self, auth=READER):
        resp = self.client.get("/opds", auth=auth)
        self.assertEqual(resp.status_code, 200)
        return ET.fromstring(resp.data)

    def entries(self):
        return self.feed().findall(f"{ATOM}entry")

    # --- logging in ---------------------------------------------------------

    def test_everything_but_health_check_needs_a_password(self):
        for path in ("/", "/opds", "/books/x.epub"):
            resp = self.client.get(path)
            self.assertEqual(resp.status_code, 401, path)
            self.assertIn("Basic", resp.headers["WWW-Authenticate"])
        self.assertEqual(self.client.get("/healthz").status_code, 200)

    def test_wrong_password_and_unknown_user_are_rejected(self):
        self.assertEqual(self.client.get("/opds", auth=("reader", "nope")).status_code, 401)
        self.assertEqual(self.client.get("/opds", auth=("admin", PASSWORDS["upload"])).status_code, 401)
        self.assertEqual(self.client.get("/opds", auth=("reader", PASSWORDS["upload"])).status_code, 401)

    def test_reader_account_can_only_read(self):
        self.upload(("book.pdf", PDF))
        book = inbox.load_library()[0]
        self.assertEqual(self.client.get("/", auth=READER).status_code, 403)
        self.assertEqual(self.upload(("x.pdf", PDF), auth=READER).status_code, 403)
        self.assertEqual(self.client.post(f"/books/{book['id']}/delete", auth=READER).status_code, 403)
        self.assertEqual(len(inbox.load_library()), 1)

    # --- uploading ----------------------------------------------------------

    def test_epub_title_and_author_come_from_the_book(self):
        resp = self.upload(("download (3).epub", make_epub("Persuasion", "Jane Austen")))
        self.assertEqual(resp.status_code, 200)
        [entry] = self.entries()
        self.assertEqual(entry.findtext(f"{ATOM}title"), "Persuasion")
        self.assertEqual(entry.findtext(f"{ATOM}author/{ATOM}name"), "Jane Austen")

    def test_pdf_title_comes_from_file_name(self):
        self.upload(("Week_2_Reading.pdf", PDF))
        [entry] = self.entries()
        self.assertEqual(entry.findtext(f"{ATOM}title"), "Week 2 Reading")
        self.assertEqual(entry.findtext(f"{ATOM}author/{ATOM}name"), "Unknown Author")

    def test_wrong_types_and_fakes_are_rejected_and_leave_nothing_behind(self):
        resp = self.upload(("notes.txt", b"hello"), ("fake.epub", b"PK not really"),
                           ("fake.pdf", b"<html>"), ("empty.pdf", b""))
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(inbox.load_library(), [])
        self.assertEqual(list((self.data / "books").iterdir()), [])
        self.assertEqual(list((self.data / "tmp").iterdir()), [])

    def test_a_zip_that_is_not_an_epub_is_rejected(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("hello.txt", "hi")
        self.assertEqual(self.upload(("archive.epub", buf.getvalue())).status_code, 400)

    def test_file_name_cannot_escape_the_books_folder(self):
        self.upload(("../../../etc/evil.pdf", PDF), ("..\\..\\win.pdf", PDF))
        files = sorted(p.name for p in (self.data / "books").iterdir())
        self.assertEqual(len(files), 2)
        self.assertTrue(all(name.endswith(".pdf") and "/" not in name for name in files))
        self.assertFalse((self.data.parent / "etc").exists())
        titles = {b["title"] for b in inbox.load_library()}
        self.assertEqual(titles, {"evil", "win"})

    def test_same_title_gets_a_number_so_koreader_does_not_skip_it(self):
        self.upload(("document.pdf", PDF), ("document.pdf", PDF), ("DOCUMENT.pdf", PDF))
        titles = [e.findtext(f"{ATOM}title") for e in self.entries()]
        self.assertEqual(sorted(titles), ["DOCUMENT (3)", "document", "document (2)"])

    def test_too_big_upload_is_refused(self):
        resp = self.upload(("huge.pdf", PDF + b"0" * (2 * 1024 * 1024)))
        self.assertEqual(resp.status_code, 413)
        self.assertEqual(inbox.load_library(), [])

    def test_cross_site_form_posts_are_blocked(self):
        for headers in ({"Sec-Fetch-Site": "cross-site"}, {"Sec-Fetch-Site": "same-site"},
                        {"Origin": "https://evil.example"}, {"Origin": "null"}):
            self.assertEqual(self.upload(("a.pdf", PDF), headers=headers).status_code, 403, headers)
        ok = self.upload(("a.pdf", PDF), headers={"Sec-Fetch-Site": "same-origin",
                                                  "Origin": "http://localhost"})
        self.assertEqual(ok.status_code, 200)

    def test_html_in_metadata_is_not_executed(self):
        self.upload(("x.epub", make_epub("Tom &amp; Jerry &lt;script&gt;alert(1)&lt;/script&gt;",
                                         "<b>Bold</b> Author")))
        book = inbox.load_library()[0]
        self.assertEqual(book["title"], "Tom & Jerry alert(1)")
        self.assertEqual(book["author"], "Bold Author")
        page = self.client.get("/", auth=UPLOAD).get_data(as_text=True)
        self.assertNotIn("<script>", page)
        self.assertIn("Tom &amp; Jerry", page)

    # --- the feed and downloads ---------------------------------------------

    def test_feed_is_newest_first_with_stable_relative_links(self):
        self.upload(("first.pdf", PDF))
        self.upload(("second.epub", make_epub("Second", "B")))
        self.upload(("third.pdf", PDF))
        entries = self.entries()
        self.assertEqual([e.findtext(f"{ATOM}title") for e in entries], ["third", "Second", "first"])
        link = entries[1].find(f"{ATOM}link")
        self.assertEqual(link.get("rel"), "http://opds-spec.org/acquisition")
        self.assertEqual(link.get("type"), "application/epub+zip")
        self.assertRegex(link.get("href"), r"^books/\d{8}-\d{6}-[0-9a-f]{8}\.epub$")

    def test_download_returns_the_file_without_caching(self):
        epub = make_epub("Emma", "Jane Austen")
        self.upload(("emma.epub", epub))
        href = self.entries()[0].find(f"{ATOM}link").get("href")
        for auth in (READER, UPLOAD):
            resp = self.client.get("/" + href, auth=auth)
            self.assertEqual(resp.status_code, 200)
            self.assertEqual(resp.data, epub)
            self.assertEqual(resp.mimetype, "application/epub+zip")
            self.assertIn("attachment", resp.headers["Content-Disposition"])
            self.assertIn("Jane Austen - Emma.epub", resp.headers["Content-Disposition"])
            self.assertEqual(resp.headers["Cache-Control"], "private, no-store")
            resp.close()
        self.assertEqual(self.client.get("/books/nope.epub", auth=READER).status_code, 404)
        self.assertEqual(self.client.get("/" + href.replace(".epub", ".pdf"), auth=READER).status_code, 404)

    def test_delete_removes_book_and_file(self):
        self.upload(("gone.pdf", PDF))
        book = inbox.load_library()[0]
        resp = self.client.post(f"/books/{book['id']}/delete", auth=UPLOAD)
        self.assertEqual(resp.status_code, 303)
        self.assertEqual(inbox.load_library(), [])
        self.assertEqual(list((self.data / "books").iterdir()), [])
        self.assertEqual(self.entries(), [])

    # --- accounts -----------------------------------------------------------

    def test_usernames_can_be_changed(self):
        inbox.configure(self.tmp.name, credentials=dict(PASSWORDS), max_upload_mb=1,
                        usernames={"upload": "maria", "reader": "kobo"})
        self.assertEqual(self.client.get("/", auth=("maria", PASSWORDS["upload"])).status_code, 200)
        self.assertEqual(self.client.get("/opds", auth=("kobo", PASSWORDS["reader"])).status_code, 200)
        self.assertEqual(self.client.get("/", auth=("kobo", PASSWORDS["reader"])).status_code, 403)
        self.assertEqual(self.client.get("/", auth=UPLOAD).status_code, 401)  # old name is gone
        with self.assertRaises(SystemExit):
            inbox.configure(self.tmp.name, credentials=dict(PASSWORDS),
                            usernames={"upload": "same", "reader": "same"})

    def test_short_passwords_need_an_explicit_opt_in(self):
        path = self.data / "credentials.txt"
        env = {"UPLOAD_PASSWORD": "tiny", "READER_PASSWORD": "reader-password-456"}
        with mock.patch.dict(os.environ, env):
            with self.assertRaises(SystemExit):
                inbox.load_credentials(path)
            with mock.patch.dict(os.environ, {"ALLOW_WEAK_PASSWORDS": "1"}):
                self.assertEqual(inbox.load_credentials(path)["upload"], "tiny")

    def test_passwords_are_generated_once_and_kept_private(self):
        path = self.data / "credentials.txt"
        first = inbox.load_credentials(path)
        self.assertEqual(set(first), {"upload", "reader"})
        self.assertGreaterEqual(min(len(pw) for pw in first.values()), 30)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(inbox.load_credentials(path), first)


if __name__ == "__main__":
    unittest.main()
