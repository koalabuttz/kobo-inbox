# Kobo Inbox

Upload an EPUB or PDF from your phone or computer, then pull it onto your
e-reader with KOReader's built-in **OPDS catalog sync**. Nothing extra is
installed on the e-reader.

```
phone / laptop browser ──upload──▶  app.py  ◀──OPDS feed + downloads── KOReader
                                     │
                         data/books/ + data/library.json
```

| File | What it is |
|---|---|
| `app.py` | the whole app: upload page, catalog feed, downloads, security checks |
| `test_app.py` | unit tests (`python -m unittest -v`) |
| `smoke_test.py` | checks a running copy from the outside |
| `Dockerfile`, `compose.yaml`, `.env.example` | running it on a server |

The same `app.py` runs in two places. Only the way you reach it changes:

| | Laptop on your Wi-Fi | Server on the internet |
|---|---|---|
| Start | `python app.py` | `docker compose up -d --build` |
| Address | `http://<laptop-ip>:8080` | `https://books.example.com` via an HTTPS tunnel |
| Encryption | none: use only on a network you trust | HTTPS from the tunnel |

## 1. Run it on a laptop

```sh
pip install -r requirements.txt
python app.py
```

It prints the upload page address, the KOReader catalog address, and where the
passwords are saved (`data/credentials.txt`, created on first start):

- **upload**: for the browser upload page. Can add and delete books.
- **reader**: for the e-reader. Can only read the catalog and download.

Open the upload page on your phone (same Wi-Fi), log in as `upload`, and add a
DRM-free EPUB or PDF.

## 2. Set up KOReader (needs KOReader 2025.08 or newer; latest stable recommended)

1. In KOReader's file browser, open the **search (magnifying glass) menu** and
   choose **OPDS catalog**.
2. Tap the **menu button (top left) → Add catalog** (older versions: the **+** button):
   - Catalog URL: the address ending in `/opds`
   - Username `reader` and its password from `credentials.txt`
   - Tick **Sync catalog**. Leave **Use server filenames** unticked.
3. In the same top-left menu, choose **Set sync folder** (create a folder such
   as `Books/Inbox` first) and **Set file types to sync** → `epub, pdf`.
4. Choose **Sync all catalogs**. New books land in the sync folder as
   `Author - Title.epub`.

From then on: upload, then *OPDS catalog → menu → Sync all catalogs*. KOReader
only fetches books newer than the last one it downloaded.

## 3. Run it on a server behind HTTPS

On a Linux server with Docker:

```sh
cp .env.example .env    # set BIND_ADDR to the server's LAN IP, pick HOST_PORT
mkdir -p data           # must be owned by PUID:PGID from .env
docker compose up -d --build
cat data/credentials.txt
```

Then point an HTTPS tunnel (for example a Cloudflare Tunnel public hostname) at
`http://<server-lan-ip>:<HOST_PORT>` and use `https://<hostname>/opds` in KOReader.

- **Never forward the port on your router.** The tunnel provides HTTPS; plain
  HTTP over the internet would expose your passwords.
- **Don't put a login page (such as Cloudflare Access) in front of it.** KOReader
  can't get past it; the app's own passwords protect everything.
- Cloudflare's free plan limits uploads to 100 MB per request, so the app caps
  uploads at 90 MB (`MAX_UPLOAD_MB`).

## Change usernames and passwords

The accounts are set with environment variables. Anything you don't set falls back
to the usernames `upload` / `reader` and the generated passwords in
`data/credentials.txt`.

| Variable | Meaning |
|---|---|
| `UPLOAD_USER`, `UPLOAD_PASSWORD` | login for the upload page (full access) |
| `READER_USER`, `READER_PASSWORD` | login for the e-reader (read only) |
| `ALLOW_WEAK_PASSWORDS=1` | allow passwords under 12 characters (logs a warning) |

- **Laptop:** `UPLOAD_USER=me UPLOAD_PASSWORD='a long passphrase' python app.py`
  (PowerShell: `$env:UPLOAD_USER="me"` and so on, then `python app.py`).
  Or just edit the passwords in `data/credentials.txt` and restart.
- **Docker:** put the variables in `.env`, then run `docker compose up -d`.

The upload account can also read the feed, so one login can do everything. If you
change the login the e-reader uses, update it in KOReader too: long-press the
catalog in the OPDS catalog list and edit it.

## How it works

- **State:** every upload is saved as `data/books/<id>.epub|pdf` and added to the
  end of `data/library.json` with its title, author, size and time. Title and
  author come from the EPUB itself; PDFs use their file name. Back up `data/`.
- **API:** `/opds` is an Atom feed in the OPDS catalog format, listing every book
  **newest first**. KOReader remembers the link of the newest book it
  downloaded and next time fetches only the entries above it. Download links
  (`books/<id>.epub`) never change and are never reused, which keeps that
  working.
- Because KOReader names files `Author - Title`, the app adds " (2)" to repeated
  titles so two different books never collide on the e-reader.

## Security choices

- Everything except `/healthz` needs a password. Passwords are long and random,
  compared in constant time, and the e-reader gets a read-only account.
- Only real EPUB and PDF files are accepted (contents are checked, not just
  the extension). Uploaded file names never become paths on disk.
- Upload and delete forms reject requests that come from other websites (browsers
  re-send saved passwords automatically, so this matters).
- Responses are marked `private, no-store` so no proxy or CDN keeps a copy, and
  downloads are sent as attachments, never displayed inline.
- In Docker: unprivileged user, read-only filesystem, no Linux capabilities,
  memory and process limits.

## Testing

```sh
python -m unittest -v                                        # unit tests
python smoke_test.py http://127.0.0.1:8080 some-book.epub    # against a running copy
```

`smoke_test.py` uploads the book, checks the feed, download and security rules,
then deletes its test copy (add `--keep` to leave it).

## Known limits

- Syncing takes a few taps; KOReader has no "sync on startup" option for OPDS.
- Deleting the **newest** book makes KOReader's next sync list the remaining
  books once as "already on the device". Deleting older books is harmless.
- DRM-protected books won't open in KOReader. Use DRM-free files.
- The e-reader's password is stored on the device; if the device is lost, change
  that password (see above).
