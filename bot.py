#!/usr/bin/env python3
"""Telegram -> Zotero + Google Drive paper pipeline.

Send the bot a link. If it's a paper: index it in Zotero (Web API) and drop the
PDF into a Google Drive folder (via rclone) that your iPad reader syncs.
Send the bot a PDF file and it asks, with buttons, which Drive subfolder to
put it in. /subcollection <name> creates a Zotero collection under PAPERBOT, and
a paper's reply offers a button per such subcollection to file it there too.

Run:   python bot.py   (long-poll loop, needs env vars below)
Tests: pytest          (offline, no network)

Env (see README): TELEGRAM_TOKEN ALLOWED_CHAT_ID ZOTERO_API_KEY ZOTERO_USER_ID
                  UNPAYWALL_EMAIL [ZOTERO_COLLECTION=<name>]
                  [RCLONE_REMOTE=gdrive] [DRIVE_DIR=Papers]
"""

import os
import re
import sys
import json
import socket
import ipaddress
import subprocess
import tempfile
import uuid
from urllib.parse import urljoin, urlparse

import requests

MAX_PDF_BYTES = 100 * 1024 * 1024  # 100 MB — a single PDF can't fill a 1GB VPS

TRANSLATION_SERVER = os.environ.get("TRANSLATION_SERVER", "http://localhost:1969")
RCLONE_REMOTE = os.environ.get("RCLONE_REMOTE", "gdrive")
DRIVE_DIR = os.environ.get("DRIVE_DIR", "Papers")
# /subcollection creates collections under this one; papers can be filed into them
SUBCOLLECTION_PARENT = "PAPERBOT"
# asked when /subcollection comes without a name; a reply to it is the name
SUBCOLLECTION_PROMPT = f"Name for the new {SUBCOLLECTION_PARENT} subcollection?"

PAPER_TYPES = {
    "journalArticle",
    "conferencePaper",
    "preprint",
    "book",
    "bookSection",
    "thesis",
    "report",
}
URL_RE = re.compile(r"https?://[^\s]+")
ARXIV_ID_RE = re.compile(r"\b(\d{4}\.\d{4,5}(?:v\d+)?)\b")


# --- pure helpers (covered by tests/test_bot.py) ----------------------------


def is_paper(items):
    """True if the first translated item looks like a paper."""
    return bool(items) and items[0].get("itemType") in PAPER_TYPES


def first_url(text):
    m = URL_RE.search(text or "")
    return m.group(0).rstrip(").,]") if m else None


def slugify(item):
    """FirstAuthor_Year_TitleWords -> safe pdf basename (no extension)."""
    creators = item.get("creators") or []
    author = "Unknown"
    for c in creators:
        author = c.get("lastName") or c.get("name") or author
        if author != "Unknown":
            break
    ym = re.search(r"\d{4}", item.get("date", "") or "")
    year = ym.group(0) if ym else "n.d."
    title = item.get("title", "") or "untitled"
    words = re.findall(r"[A-Za-z0-9]+", title)[:6]
    parts = [re.sub(r"[^A-Za-z0-9]", "", author), year] + words
    name = "_".join(p for p in parts if p)
    return name[:120] or "paper"


def safe_name(file_name):
    """A user-supplied filename -> safe pdf basename (no extension, no path).

    The name comes straight from Telegram and ends up in an rclone destination,
    so strip the directory part (blocks ../ escaping the chosen folder) and keep
    only harmless characters."""
    name = os.path.basename(file_name or "").strip()
    if name.lower().endswith(".pdf"):
        name = name[:-4]
    name = re.sub(r"[^A-Za-z0-9._ -]", "_", name).strip(". ")
    return name[:120] or "document"


def folder_keyboard(token, subdirs):
    """Inline keyboard: one row per Drive subfolder, then root, then cancel.

    Buttons carry `<token>:<index>` — Telegram caps callback_data at 64 bytes,
    which a file_id would blow past, hence the index into PENDING."""
    rows = [
        [{"text": d, "callback_data": f"{token}:{i}"}] for i, d in enumerate(subdirs)
    ]
    rows.append(
        [{"text": f"\U0001f4c1 {DRIVE_DIR} (root)", "callback_data": f"{token}:r"}]
    )
    rows.append([{"text": "\u2716 Cancel", "callback_data": f"{token}:x"}])
    return {"inline_keyboard": rows}


def subcollection_name(msg):
    """The subcollection a message asks for: the name, "" when /subcollection
    came without one (so ask), or None when it isn't a subcollection request.

    A reply to SUBCOLLECTION_PROMPT is a name, matched by the prompt's text, so
    no per-chat state is kept and a link sent normally is never taken as a name.
    "/cmd@botname" is what Telegram sends when picked from the menu."""
    text = (msg.get("text") or "").strip()
    if (msg.get("reply_to_message") or {}).get("text") == SUBCOLLECTION_PROMPT:
        return text
    cmd, _, arg = text.partition(" ")
    return arg.strip() if cmd.split("@")[0].lower() == "/subcollection" else None


def file_keyboard(item_key, collections):
    """Buttons filing Zotero item `item_key` into a subcollection of
    SUBCOLLECTION_PARENT, or None when there's no item or no subcollection.

    Buttons carry `z:<itemKey>:<collectionKey>` \u2014 Zotero keys are 8 chars, so it
    fits Telegram's 64-byte callback_data cap with no pending state to keep."""
    cols = collections or []
    parent = next((c["key"] for c in cols if c["name"] == SUBCOLLECTION_PARENT), None)
    subs = sorted(
        (c for c in cols if parent and c["parent"] == parent),
        key=lambda c: c["name"].lower(),
    )
    if not (item_key and subs):
        return None
    return {
        "inline_keyboard": [
            [{"text": c["name"], "callback_data": f"z:{item_key}:{c['key']}"}]
            for c in subs
        ]
    }


def pdf_url_from_item(item):
    """PDF url translation-server attached to the item, if any."""
    for att in item.get("attachments", []) or []:
        if att.get("mimeType") == "application/pdf" and att.get("url"):
            return att["url"]
    return None


def normalize_url(url):
    # arxiv /pdf/<id> -> /abs/<id> so ponytail can translate the metadata.
    # ponytail's arxiv translator only handles abstract/list pages, not direct
    # pdf urls; after translation the item has no pdf attachment, but
    # arxiv_pdf() below converts /abs/ back to /pdf/ for download.
    m = re.match(r"https?://arxiv\.org/pdf/(\S+?)(?:\.pdf)?$", url or "")
    if m:
        return f"https://arxiv.org/abs/{m.group(1)}"
    return url


def resolve_url(text):
    """Message text -> paper url: first http(s) link (arxiv /pdf/ -> /abs/),
    else a bare arXiv id like 2608.11888v1 -> https://arxiv.org/abs/<id>."""
    url = first_url(text)
    if url:
        return normalize_url(url)
    m = ARXIV_ID_RE.search(text or "")
    return f"https://arxiv.org/abs/{m.group(1)}" if m else None


def arxiv_pdf(url):
    # ponytail: arxiv's translator only snapshots the abstract page (no pdf
    # attachment, no DOI), but /abs/<id> -> /pdf/<id> is a stable url pattern.
    m = re.match(r"https?://arxiv\.org/abs/(\S+)", url or "")
    return f"https://arxiv.org/pdf/{m.group(1)}" if m else None


def doi_of(item):
    doi = item.get("DOI")
    if doi:
        return doi
    # some translators stash it in extra
    m = re.search(r"10\.\d{4,9}/\S+", item.get("extra", "") or "")
    return m.group(0) if m else None


def _doi_matches(data, doi):
    """True if a Zotero item's data holds `doi` (DOI field or stashed in extra)."""
    if not doi:
        return False
    d = doi.lower()
    return (data.get("DOI") or "").lower() == d or d in (
        data.get("extra") or ""
    ).lower()


def _item_matches(data, doi, title):
    """True if a Zotero item is the same paper: same DOI, or (no DOI) same title."""
    if _doi_matches(data, doi):
        return True
    t = (title or "").strip().lower()
    # ponytail: exact-title fallback for DOI-less papers (arXiv); a same-title
    # collision is possible but vanishingly rare for real papers.
    return bool(t) and (data.get("title") or "").strip().lower() == t


def build_payload(item, collection_key=None):
    """Item payload for the Zotero API: drop nested children, always tag
    paperbot (idempotent), optionally file into a collection."""
    payload = {k: v for k, v in item.items() if k not in ("attachments", "notes")}
    tags = list(payload.get("tags") or [])
    if not any(t.get("tag") == "paperbot" for t in tags):
        tags.append({"tag": "paperbot"})
    payload["tags"] = tags
    if collection_key:
        payload["collections"] = [collection_key]
    return payload


def pick_collection(item, collections, default_name=None):
    # ponytail: fixed default for now. A real classifier replaces this body
    # (it returns a collection NAME; the ensure step creates it if missing);
    # the signature already carries the paper + the collection list it needs.
    return default_name or "PAPERBOT"


# --- network steps -----------------------------------------------------------


def translate(url):
    r = requests.post(
        f"{TRANSLATION_SERVER}/web",
        data=url.encode(),
        headers={"Content-Type": "text/plain"},
        timeout=60,
    )
    # 300 "Multiple Choices" = a listing/search page: the body is a dict of
    # candidate items, not a list. raise_for_status() ignores 3xx, so without
    # this the dict becomes [] and the user is told "Not a paper".
    if r.status_code == 300:
        raise RuntimeError("that page lists several items — send one paper's link")
    r.raise_for_status()
    data = r.json()
    return data if isinstance(data, list) else []


def zotero_collections(api_key, user_id):
    # ponytail: single page (limit=100); paginate only if a library exceeds it.
    r = requests.get(
        f"https://api.zotero.org/users/{user_id}/collections",
        headers={"Zotero-API-Key": api_key},
        params={"limit": 100},
        timeout=30,
    )
    r.raise_for_status()
    return [
        {
            "key": c["key"],
            "name": c["data"]["name"],
            "parent": c["data"].get("parentCollection") or None,
        }
        for c in r.json()
    ]


def zotero_collection_items(collection_key, api_key, user_id):
    """All top-level items in a collection. Uses direct membership listing
    (immediately consistent) rather than the quick-search index, which lags
    behind writes and so misses papers the bot just added."""
    items, start = [], 0
    while True:
        r = requests.get(
            f"https://api.zotero.org/users/{user_id}/collections/{collection_key}/items/top",
            headers={"Zotero-API-Key": api_key},
            params={"limit": 100, "start": start},
            timeout=30,
        )
        r.raise_for_status()
        batch = r.json()
        items += batch
        if len(batch) < 100:
            return items
        start += 100


def zotero_find_existing(item, collection_key, api_key, user_id):
    """Key of an item already in `collection_key` that is the same paper (by
    DOI, else by exact title), or None."""
    doi = doi_of(item)
    title = (item.get("title") or "").strip()
    if not (doi or title):
        return None
    for it in zotero_collection_items(collection_key, api_key, user_id):
        if _item_matches(it.get("data", {}), doi, title):
            return it["key"]
    return None


def zotero_ensure_collection(name, collections, api_key, user_id, parent=None):
    """Return the key of collection `name`, creating it if absent. With `parent`
    (a collection key) it must sit under that parent and is created there; without
    one, any collection of that name matches (kept as-is so an existing, possibly
    nested default collection is never duplicated)."""
    for c in collections:
        if c["name"] == name and (parent is None or c["parent"] == parent):
            return c["key"]
    body = {"name": name}
    if parent:
        body["parentCollection"] = parent
    r = requests.post(
        f"https://api.zotero.org/users/{user_id}/collections",
        headers={"Zotero-API-Key": api_key, "Content-Type": "application/json"},
        data=json.dumps([body]),
        timeout=30,
    )
    r.raise_for_status()
    res = r.json()
    if res.get("failed"):
        raise RuntimeError(f"Zotero rejected collection: {res['failed']}")
    return res["successful"]["0"]["key"]


def zotero_add(item, api_key, user_id, collection_key=None):
    """POST one item (minus nested children) to the Zotero Web API. Returns key."""
    payload = build_payload(item, collection_key)
    r = requests.post(
        f"https://api.zotero.org/users/{user_id}/items",
        headers={"Zotero-API-Key": api_key, "Content-Type": "application/json"},
        data=json.dumps([payload]),
        timeout=60,
    )
    r.raise_for_status()
    res = r.json()
    if res.get("failed"):
        raise RuntimeError(f"Zotero rejected item: {res['failed']}")
    return res["successful"]["0"]["key"]


def zotero_file_item(item_key, collection_key, api_key, user_id):
    """Add an existing item to a collection, keeping the ones it's already in.
    Returns False if it was already there."""
    url = f"https://api.zotero.org/users/{user_id}/items/{item_key}"
    r = requests.get(url, headers={"Zotero-API-Key": api_key}, timeout=30)
    r.raise_for_status()
    data = r.json()["data"]
    current = data.get("collections") or []
    if collection_key in current:
        return False
    r = requests.patch(
        url,
        headers={
            "Zotero-API-Key": api_key,
            "Content-Type": "application/json",
            # optimistic lock: Zotero answers 412 if the item changed since the GET
            "If-Unmodified-Since-Version": str(data["version"]),
        },
        data=json.dumps({"collections": current + [collection_key]}),
        timeout=30,
    )
    r.raise_for_status()
    return True


def unpaywall_pdf(doi, email):
    if not doi:
        return None
    r = requests.get(
        f"https://api.unpaywall.org/v2/{doi}", params={"email": email}, timeout=30
    )
    if r.status_code != 200:
        return None
    loc = (r.json() or {}).get("best_oa_location") or {}
    return loc.get("url_for_pdf")


def get_local_ip():
    """Return the local network IP address."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        # connect to an external address (no data sent) to discover the
        # outgoing interface the kernel would use for internet traffic.
        s.connect(("8.8.8.8", 53))
        return s.getsockname()[0]


def _check_public_url(url):
    """Reject non-http(s) URLs and hosts resolving to private/loopback/link-local
    addresses. Blocks SSRF (cloud metadata 169.254.169.254, localhost, LAN) via a
    hostile link the owner pastes.
    ponytail: re-checks every redirect hop but not the final connect, so a
    DNS-rebind between check and fetch could still slip through; drop to a pinned
    resolver + IP-bound connect if that threat matters."""
    p = urlparse(url)
    if p.scheme not in ("http", "https") or not p.hostname:
        raise ValueError(f"refusing non-http(s) url: {url}")
    for info in socket.getaddrinfo(p.hostname, p.port or 0, proto=socket.IPPROTO_TCP):
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
            raise ValueError(f"refusing url to non-public address {ip}: {url}")


def download(url):
    # ponytail: streams to disk (stream=True + iter_content), so the PDF is
    # never held whole in RAM — keeps memory flat on a 1GB VPS. Redirects are
    # followed manually so each hop's host gets SSRF-checked, not just the first.
    for _ in range(6):
        _check_public_url(url)
        r = requests.get(
            url,
            timeout=120,
            stream=True,
            allow_redirects=False,
            headers={"User-Agent": "paperbot/1.0"},
        )
        if r.is_redirect or r.is_permanent_redirect:
            url = urljoin(url, r.headers["Location"])
            r.close()
            continue
        r.raise_for_status()
        fd, path = tempfile.mkstemp(suffix=".pdf")
        size = 0
        try:
            with os.fdopen(fd, "wb") as f:
                for chunk in r.iter_content(8192):
                    size += len(chunk)
                    if size > MAX_PDF_BYTES:
                        raise ValueError(f"pdf exceeds {MAX_PDF_BYTES} bytes")
                    f.write(chunk)
        except BaseException:
            os.remove(path)
            raise
        return path
    raise ValueError(f"too many redirects: {url}")


def to_drive(local_path, name, subdir=None):
    folder = f"{DRIVE_DIR}/{subdir}" if subdir else DRIVE_DIR
    dest = f"{RCLONE_REMOTE}:{folder}/{name}.pdf"
    subprocess.run(["rclone", "copyto", local_path, dest], check=True)
    return dest


def drive_subdirs():
    """Subfolder names directly under DRIVE_DIR on the remote."""
    out = subprocess.run(
        ["rclone", "lsf", "--dirs-only", f"{RCLONE_REMOTE}:{DRIVE_DIR}"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return sorted(d.rstrip("/") for d in out.splitlines() if d.strip())


# --- pipeline ----------------------------------------------------------------


def handle(text, env):
    url = resolve_url(text)
    if not url:
        print("no url in message, ignoring", flush=True)
        return "No link found.", None
    print(f"translating {url}", flush=True)
    try:
        items = translate(url)
    except Exception as e:
        print(f"translate failed: {e}", flush=True)
        return f"Couldn't read that link: {e}", None
    if not is_paper(items):
        # log what came back: "not a paper" is otherwise undiagnosable after the fact
        print(
            f"not a paper, ignoring: {[i.get('itemType') for i in items] or 'no items'}",
            flush=True,
        )
        return "Not a paper, ignored.", None

    item = items[0]
    title = item.get("title", "(untitled)")
    print(f"paper found: {title}", flush=True)
    lines = [title]

    collections = None
    try:
        collections = zotero_collections(env["ZOTERO_API_KEY"], env["ZOTERO_USER_ID"])
    except Exception as e:
        print(f"collection fetch failed: {e}", flush=True)
    coll_key = None
    name = None
    if collections is not None:  # skip on fetch failure so we don't create a dup
        name = pick_collection(item, collections, env.get("ZOTERO_COLLECTION"))
        try:
            coll_key = zotero_ensure_collection(
                name, collections, env["ZOTERO_API_KEY"], env["ZOTERO_USER_ID"]
            )
            print(f"collection: {name} ({coll_key})", flush=True)
        except Exception as e:
            print(f"collection ensure failed: {e}", flush=True)
    else:
        print("collection: skipped (fetch failed), library root", flush=True)

    if coll_key:  # dedup against the collection listing (no search-index lag)
        try:
            existing = zotero_find_existing(
                item, coll_key, env["ZOTERO_API_KEY"], env["ZOTERO_USER_ID"]
            )
        except Exception as e:
            print(f"dup check failed: {e}", flush=True)
            existing = None  # fall through and add rather than risk losing the paper
        if existing:
            print(f"already in zotero: {existing}", flush=True)
            lines.append(f"↺ Already in {name} ({existing}) — skipped")
            return "\n".join(lines), file_keyboard(existing, collections)

    item_key = None
    try:
        key = zotero_add(item, env["ZOTERO_API_KEY"], env["ZOTERO_USER_ID"], coll_key)
        item_key = key
        print(f"zotero add ok: {key}", flush=True)
        lines.append(f"✓ Zotero ({key})")
    except Exception as e:
        print(f"zotero add failed: {e}", flush=True)
        lines.append(f"✗ Zotero failed: {e}")

    pdf = (
        pdf_url_from_item(item)
        or arxiv_pdf(url)
        or unpaywall_pdf(doi_of(item), env["UNPAYWALL_EMAIL"])
    )
    if not pdf:
        print("no open pdf found", flush=True)
        lines.append("no open PDF")
        return "\n".join(lines), file_keyboard(item_key, collections)
    try:
        print(f"downloading pdf: {pdf}", flush=True)
        path = download(pdf)
        try:
            to_drive(path, slugify(item))
            print("uploaded to drive", flush=True)
            lines.append("✓ Drive")
        finally:
            os.remove(path)
    except Exception as e:
        print(f"pdf/drive failed: {e}", flush=True)
        lines.append(f"✗ PDF/Drive failed: {e}")
    return "\n".join(lines), file_keyboard(item_key, collections)


def new_subcollection(name, env):
    """/subcollection <name>: create `name` under SUBCOLLECTION_PARENT (which is
    created too if missing). Returns the reply text."""
    name = name.strip()[:255]  # Zotero caps collection names at 255 chars
    if not name:
        return "Usage: /subcollection <name>"
    label = f"{SUBCOLLECTION_PARENT} › {name}"
    try:
        api_key, user_id = env["ZOTERO_API_KEY"], env["ZOTERO_USER_ID"]
        cols = zotero_collections(api_key, user_id)
        parent = zotero_ensure_collection(SUBCOLLECTION_PARENT, cols, api_key, user_id)
        key = zotero_ensure_collection(name, cols, api_key, user_id, parent=parent)
    except Exception as e:
        print(f"subcollection {label!r} failed: {e}", flush=True)
        return f"✗ Couldn't create {label}: {e}"
    if key in {c["key"] for c in cols}:
        print(f"subcollection exists: {label} ({key})", flush=True)
        return f"↺ {label} already exists ({key})"
    print(f"subcollection created: {label} ({key})", flush=True)
    return f"✓ Created {label} ({key})"


def handle_file_callback(cq, env, api):
    """A subcollection button under a paper was tapped: file the paper there.
    Returns the text that replaces the message (dropping its buttons)."""
    tg(api, "answerCallbackQuery", callback_query_id=cq["id"])
    text = cq["message"].get("text", "")
    try:
        _, item_key, coll_key = (cq.get("data") or "").split(":")
        api_key, user_id = env["ZOTERO_API_KEY"], env["ZOTERO_USER_ID"]
        cols = zotero_collections(api_key, user_id)
        name = next((c["name"] for c in cols if c["key"] == coll_key), coll_key)
        added = zotero_file_item(item_key, coll_key, api_key, user_id)
    except Exception as e:
        print(f"filing failed: {e}", flush=True)
        return f"{text}\n✗ Filing failed: {e}"
    print(f"filed {item_key} into {name} ({coll_key}), added={added}", flush=True)
    note = "" if added else " (already there)"
    return f"{text}\n📁 {SUBCOLLECTION_PARENT} › {name}{note}"


# --- pdf upload: ask which drive subfolder -----------------------------------

PENDING = {}  # chat_id -> (token, local_path, name, subdirs)
# ponytail: in-memory, one pending upload per chat (a new PDF drops the previous
# one and its temp file, so nothing accumulates). A restart forgets the prompt;
# PrivateTmp= takes the temp file with it and re-sending the PDF costs nothing.


def tg(api, method, **data):
    """Call a Telegram bot API method, return the parsed response."""
    return requests.post(f"{api}/{method}", data=data, timeout=30).json()


def _drop_pending(chat_id):
    """Forget a chat's pending upload and delete its temp file."""
    prev = PENDING.pop(chat_id, None)
    if prev:
        try:
            os.remove(prev[1])
        except OSError:
            pass


def handle_document(doc, chat_id, api, token):
    """A PDF arrived: fetch it, then ask which Drive subfolder with buttons.
    Returns a reply string, or None when the keyboard is the reply."""
    file_name = doc.get("file_name") or ""
    if doc.get("mime_type") != "application/pdf" and not file_name.lower().endswith(
        ".pdf"
    ):
        return "Only PDFs, sorry."
    try:
        res = tg(api, "getFile", file_id=doc["file_id"])
        if not res.get("ok"):
            desc = res.get("description") or "unknown error"
            if "too big" in desc.lower():
                return "That PDF is too big — Telegram caps bot downloads at 20 MB."
            return f"Telegram refused the file: {desc}"
        url = f"https://api.telegram.org/file/bot{token}/{res['result']['file_path']}"
        path = download(url)
    except Exception as e:
        # that url embeds the bot token and requests errors quote the url back
        err = str(e).replace(token, "<redacted>")
        print(f"pdf fetch failed: {err}", flush=True)
        return f"Couldn't fetch that PDF: {err}"
    try:
        subdirs = drive_subdirs()
    except Exception as e:
        print(f"drive listing failed: {e}", flush=True)
        os.remove(path)
        return f"Couldn't list Drive folders: {e}"

    _drop_pending(chat_id)
    tok = uuid.uuid4().hex[:8]
    name = safe_name(file_name)
    PENDING[chat_id] = (tok, path, name, subdirs)
    print(f"pdf pending: {name}.pdf ({len(subdirs)} folders)", flush=True)
    tg(
        api,
        "sendMessage",
        chat_id=chat_id,
        text=f"Where should {name}.pdf go?",
        reply_markup=json.dumps(folder_keyboard(tok, subdirs)),
    )
    return None


def handle_callback(cq, chat_id, api):
    """A folder button was tapped: upload the pending PDF there. Returns the
    text that replaces the keyboard message."""
    tg(api, "answerCallbackQuery", callback_query_id=cq["id"])
    tok, _, choice = (cq.get("data") or "").partition(":")
    pending = PENDING.get(chat_id)
    if not pending or pending[0] != tok:  # keyboard from an older/forgotten PDF
        return "That prompt expired — send the PDF again."
    _, path, name, subdirs = pending
    try:
        if choice == "x":
            print("upload cancelled", flush=True)
            return "Cancelled."
        subdir = None if choice == "r" else subdirs[int(choice)]
        try:
            dest = to_drive(path, name, subdir)
        except Exception as e:
            print(f"drive upload failed: {e}", flush=True)
            return f"\u2717 Drive failed: {e}"
        print(f"uploaded to {dest}", flush=True)
        return f"\u2713 Drive: {dest.split(':', 1)[1]}"
    finally:
        _drop_pending(chat_id)


# --- telegram long-poll ------------------------------------------------------


def telegram_loop(env):
    token = env["TELEGRAM_TOKEN"]
    allowed = str(env["ALLOWED_CHAT_ID"])
    api = f"https://api.telegram.org/bot{token}"
    offset = None
    # the "/" command menu in Telegram (what BotFather's /setcommands would set)
    try:
        tg(
            api,
            "setMyCommands",
            commands=json.dumps(
                [
                    {
                        "command": "subcollection",
                        "description": f"New collection under {SUBCOLLECTION_PARENT}",
                    }
                ]
            ),
        )
    except Exception as e:
        print("setMyCommands failed:", str(e).replace(token, "<redacted>"), flush=True)
    print("paperbot: polling", flush=True)
    while True:
        try:
            r = requests.get(
                f"{api}/getUpdates",
                params={"offset": offset, "timeout": 30},
                timeout=40,
            )
            updates = r.json().get("result", [])
        except Exception as e:
            # requests/urllib3 errors embed the request URL, which carries the
            # bot token — scrub it before it reaches the journal.
            print("poll error:", str(e).replace(token, "<redacted>"), flush=True)
            continue
        for upd in updates:
            offset = upd["update_id"] + 1
            cq = upd.get("callback_query")
            if cq:
                chat_id = str(cq["message"]["chat"]["id"])
                if chat_id != allowed:
                    print(f"ignoring callback from chat {chat_id}", flush=True)
                    continue
                # "z:" = file a paper into a subcollection; PDF-folder tokens are
                # hex, so they can never start with "z"
                if (cq.get("data") or "").startswith("z:"):
                    text = handle_file_callback(cq, env, api)
                else:
                    text = handle_callback(cq, chat_id, api)
                tg(
                    api,
                    "editMessageText",
                    chat_id=chat_id,
                    message_id=cq["message"]["message_id"],
                    text=text,
                )
                continue
            msg = upd.get("message") or upd.get("channel_post")
            if not msg:
                continue
            chat_id = str(msg["chat"]["id"])
            if chat_id != allowed:
                print(f"ignoring message from unauthorized chat {chat_id}", flush=True)
                continue  # ponytail: single-user allowlist
            print(f"message from {chat_id}: {msg.get('text', '')!r}", flush=True)
            doc = msg.get("document")
            if doc:
                reply = handle_document(doc, chat_id, api, token)
                if reply:
                    tg(api, "sendMessage", chat_id=chat_id, text=reply)
                continue
            text = (msg.get("text") or "").strip()
            sub_name = subcollection_name(msg)
            markup = None
            if sub_name == "":  # /subcollection with no name: ask for it
                reply = SUBCOLLECTION_PROMPT
                markup = {
                    "force_reply": True,
                    "input_field_placeholder": "Subcollection name",
                }
            elif sub_name is not None:
                reply = new_subcollection(sub_name, env)
            elif text.lower() == "whoami":
                try:
                    reply = f"Your local IP: {get_local_ip()}"
                except Exception as e:
                    reply = f"Couldn't determine local IP: {e}"
            else:
                reply, markup = handle(text, env)
            extra = {"reply_markup": json.dumps(markup)} if markup else {}
            tg(api, "sendMessage", chat_id=chat_id, text=reply, **extra)


if __name__ == "__main__":
    required = [
        "TELEGRAM_TOKEN",
        "ALLOWED_CHAT_ID",
        "ZOTERO_API_KEY",
        "ZOTERO_USER_ID",
        "UNPAYWALL_EMAIL",
    ]
    env = {k: os.environ.get(k) for k in required}
    env["ZOTERO_COLLECTION"] = os.environ.get("ZOTERO_COLLECTION")  # optional
    missing = [k for k, v in env.items() if not v and k in required]
    if missing:
        sys.exit("Missing env vars: " + ", ".join(missing))
    telegram_loop(env)
