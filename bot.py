#!/usr/bin/env python3
"""Telegram -> Zotero + Google Drive paper pipeline.

Send the bot a link. If it's a paper: index it in Zotero (Web API) and drop the
PDF into a Google Drive folder (via rclone) that your iPad reader syncs.

Run: python bot.py            (long-poll loop, needs env vars below)
     python bot.py --selftest (offline asserts, no network)

Env (see README): TELEGRAM_TOKEN ALLOWED_CHAT_ID ZOTERO_API_KEY ZOTERO_USER_ID
                  UNPAYWALL_EMAIL [ZOTERO_COLLECTION=<name>]
                  [RCLONE_REMOTE=gdrive] [DRIVE_DIR=Papers]
"""

import os
import re
import sys
import json
import subprocess
import tempfile

import requests

TRANSLATION_SERVER = os.environ.get("TRANSLATION_SERVER", "http://localhost:1969")
RCLONE_REMOTE = os.environ.get("RCLONE_REMOTE", "gdrive")
DRIVE_DIR = os.environ.get("DRIVE_DIR", "Papers")

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


# --- pure helpers (covered by --selftest) -----------------------------------


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


def pdf_url_from_item(item):
    """PDF url translation-server attached to the item, if any."""
    for att in item.get("attachments", []) or []:
        if att.get("mimeType") == "application/pdf" and att.get("url"):
            return att["url"]
    return None


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


def zotero_ensure_collection(name, collections, api_key, user_id):
    """Return the key of top-level collection `name`, creating it if absent."""
    for c in collections:
        if c["name"] == name:
            return c["key"]
    r = requests.post(
        f"https://api.zotero.org/users/{user_id}/collections",
        headers={"Zotero-API-Key": api_key, "Content-Type": "application/json"},
        data=json.dumps([{"name": name}]),
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


def download(url):
    # ponytail: streams to disk (stream=True + iter_content), so the PDF is
    # never held whole in RAM — keeps memory flat on a 1GB VPS.
    r = requests.get(
        url, timeout=120, stream=True, headers={"User-Agent": "paperbot/1.0"}
    )
    r.raise_for_status()
    fd, path = tempfile.mkstemp(suffix=".pdf")
    with os.fdopen(fd, "wb") as f:
        for chunk in r.iter_content(8192):
            f.write(chunk)
    return path


def to_drive(local_path, name):
    dest = f"{RCLONE_REMOTE}:{DRIVE_DIR}/{name}.pdf"
    subprocess.run(["rclone", "copyto", local_path, dest], check=True)
    return dest


# --- pipeline ----------------------------------------------------------------


def handle(text, env):
    url = first_url(text)
    if not url:
        print("no url in message, ignoring", flush=True)
        return "No link found."
    print(f"translating {url}", flush=True)
    try:
        items = translate(url)
    except Exception as e:
        print(f"translate failed: {e}", flush=True)
        return f"Couldn't read that link: {e}"
    if not is_paper(items):
        print("not a paper, ignoring", flush=True)
        return "Not a paper, ignored."

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
            return "\n".join(lines)

    try:
        key = zotero_add(item, env["ZOTERO_API_KEY"], env["ZOTERO_USER_ID"], coll_key)
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
        return "\n".join(lines)
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
    return "\n".join(lines)


# --- telegram long-poll ------------------------------------------------------


def telegram_loop(env):
    token = env["TELEGRAM_TOKEN"]
    allowed = str(env["ALLOWED_CHAT_ID"])
    api = f"https://api.telegram.org/bot{token}"
    offset = None
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
            print("poll error:", e, flush=True)
            continue
        for upd in updates:
            offset = upd["update_id"] + 1
            msg = upd.get("message") or upd.get("channel_post")
            if not msg:
                continue
            chat_id = str(msg["chat"]["id"])
            if chat_id != allowed:
                print(f"ignoring message from unauthorized chat {chat_id}", flush=True)
                continue  # ponytail: single-user allowlist
            print(f"message from {chat_id}: {msg.get('text', '')!r}", flush=True)
            reply = handle(msg.get("text", ""), env)
            requests.post(
                f"{api}/sendMessage",
                data={"chat_id": chat_id, "text": reply},
                timeout=30,
            )


def selftest():
    paper = {
        "itemType": "journalArticle",
        "title": "Attention Is All You Need",
        "date": "2017-06-12",
        "creators": [{"lastName": "Vaswani", "firstName": "Ashish"}],
        "DOI": "10.5555/3295222.3295349",
        "attachments": [{"mimeType": "application/pdf", "url": "http://x/p.pdf"}],
    }
    news = {"itemType": "webpage", "title": "Some News", "date": "2024"}
    assert is_paper([paper]) is True
    assert is_paper([news]) is False
    assert is_paper([]) is False
    assert slugify(paper) == "Vaswani_2017_Attention_Is_All_You_Need", slugify(paper)
    assert slugify(news) == "Unknown_2024_Some_News", slugify(news)
    assert (
        first_url("see https://arxiv.org/abs/1706.03762).")
        == "https://arxiv.org/abs/1706.03762"
    )
    assert first_url("no link here") is None
    assert pdf_url_from_item(paper) == "http://x/p.pdf"
    assert pdf_url_from_item(news) is None
    assert doi_of(paper) == "10.5555/3295222.3295349"
    assert (
        arxiv_pdf("https://arxiv.org/abs/2605.30621")
        == "https://arxiv.org/pdf/2605.30621"
    )
    assert arxiv_pdf("https://example.com/x") is None

    p = build_payload(paper)
    assert {"tag": "paperbot"} in p["tags"]
    assert "attachments" not in p and "notes" not in p
    assert "collections" not in p
    assert build_payload(paper, "ABCD")["collections"] == ["ABCD"]
    tagged = dict(paper, tags=[{"tag": "nlp"}, {"tag": "paperbot"}])
    assert build_payload(tagged)["tags"] == [{"tag": "nlp"}, {"tag": "paperbot"}]
    cols = [{"key": "K1", "name": "PAPERBOT", "parent": None}]
    assert pick_collection(paper, cols, None) == "PAPERBOT"  # default
    assert pick_collection(paper, cols, "ML Papers") == "ML Papers"  # env override
    # ensure resolves an existing collection without hitting the network
    assert zotero_ensure_collection("PAPERBOT", cols, "k", "u") == "K1"
    assert _doi_matches({"DOI": "10.5555/X"}, "10.5555/x")  # case-insensitive
    assert _doi_matches({"extra": "DOI: 10.1/abc"}, "10.1/abc")  # stashed in extra
    assert not _doi_matches({"DOI": "10.9/z"}, "10.1/x")
    assert not _doi_matches({}, None)
    # arXiv-style dedup: no DOI, fall back to exact title
    assert _item_matches(
        {"title": "Attention Is All You Need"}, None, "attention is all you need"
    )
    assert _item_matches({"DOI": "10.5555/X"}, "10.5555/x", "different title")
    assert not _item_matches({"title": "Some Paper"}, None, "Other Paper")
    assert not _item_matches({}, None, "")
    assert zotero_find_existing({}, "COLL", "k", "u") is None  # no id, no request
    print("selftest ok")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        selftest()
    else:
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
