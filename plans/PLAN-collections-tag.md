# Plan: Zotero collections + `paperbot` tag

## Context
Right now `bot.py` adds every paper to the Zotero **library root** with no tag. We want
two things: (1) each incoming paper should carry a `paperbot` tag so bot-added items are
easy to find/filter, and (2) the bot should file the paper into a Zotero **collection**
(folder). The collection choice will eventually be automatic ("which collection best fits
this paper?"), but that classifier is future work. So this change builds the *plumbing* —
fetch the collection list, always tag `paperbot`, and file into a default collection via a
`pick_collection()` hook that a real classifier can replace later.

All work is in `bot.py` (single file), plus docs.

## Zotero Web API facts
- List collections: `GET https://api.zotero.org/users/{id}/collections` → items shaped
  `{"key": "...", "data": {"name": "...", "parentCollection": false|"<key>"}}`.
- File an item into a collection: add `"collections": ["<key>"]` to the item payload.
- Tag an item: add `"tags": [{"tag": "paperbot"}]` to the item payload.

## Changes to `bot.py`

### 1. Pure payload builder (replaces the inline dict in `zotero_add`)
Factor payload construction out of `zotero_add` so it's unit-testable offline:
```python
def build_payload(item, collection_key=None):
    payload = {k: v for k, v in item.items() if k not in ("attachments", "notes")}
    tags = list(payload.get("tags") or [])
    if not any(t.get("tag") == "paperbot" for t in tags):   # idempotent
        tags.append({"tag": "paperbot"})
    payload["tags"] = tags
    if collection_key:
        payload["collections"] = [collection_key]
    return payload
```
Preserves any tags the translator already returned; never duplicates `paperbot`.

### 2. Fetch collections
```python
def zotero_collections(api_key, user_id):
    # ponytail: single page (limit=100); paginate only if a library exceeds it.
    r = requests.get(f"https://api.zotero.org/users/{user_id}/collections",
                     headers={"Zotero-API-Key": api_key},
                     params={"limit": 100}, timeout=30)
    r.raise_for_status()
    return [{"key": c["key"], "name": c["data"]["name"],
             "parent": c["data"].get("parentCollection") or None}
            for c in r.json()]
```

### 3. The classifier hook (fixed default for now)
```python
def pick_collection(item, collections, default_name=None):
    # ponytail: fixed default for now. A real classifier replaces this body;
    # the signature already carries the paper + the collection list it needs.
    if not default_name:
        return None
    for c in collections:
        if c["name"] == default_name:
            return c["key"]
    return None
```
Default is chosen by **name** (env `ZOTERO_COLLECTION`), resolved against the fetched
list — no need to paste an opaque key. Unset / not-found → `None` → item lands in the
library root (current behaviour).

### 4. Wire it into `handle()` and `zotero_add()`
- `zotero_add(item, api_key, user_id, collection_key=None)` → `payload = build_payload(item, collection_key)`.
- In `handle()`, after `is_paper`, fetch collections (best-effort: on failure log and
  proceed with `collections = []`), call `pick_collection(item, collections, env.get("ZOTERO_COLLECTION"))`,
  pass the key to `zotero_add`. Add a `flush=True` print of the chosen collection for journalctl.
- In `__main__`, add `env["ZOTERO_COLLECTION"] = os.environ.get("ZOTERO_COLLECTION")`
  (optional, not in the `required`/`missing` check).

### 5. Update the module docstring env line
Add `[ZOTERO_COLLECTION=<name>]` to the optional-env comment near the top.

## Docs
- `README.md` §4 Secrets: add an optional `ZOTERO_COLLECTION=ML Papers` line to the env
  block, one line explaining it's the collection name papers are filed into (blank = library root).
- `README.md` Notes/Verify: mention items now carry a `paperbot` tag.
- `TODO.md` "Maybe later": add "auto-pick collection by content (replace `pick_collection` stub)".

No change to `paperbot.service` (env comes from `/etc/paperbot/env`).

## Skipped on purpose (say so if you want them)
- No inline-keyboard / numbered picker and no `/folders` command — classification is
  automatic, so nothing user-facing is needed.
- Single-page collection fetch (limit=100); no pagination.
- Collections fetched per message, not cached — one small GET per paper.

## Verification
- **Offline:** extend `selftest()` and run `python bot.py --selftest` → `selftest ok`.
  New asserts:
  - `build_payload(paper)` contains `{"tag": "paperbot"}`, has no `attachments`/`notes`,
    and no `collections` key.
  - `build_payload(paper, "ABCD")["collections"] == ["ABCD"]`.
  - Idempotent tag: `build_payload({...tags:[{"tag":"nlp"},{"tag":"paperbot"}]})` keeps a
    single `paperbot`.
  - `pick_collection(paper, [{"key":"K1","name":"ML Papers","parent":None}], "ML Papers") == "K1"`;
    unknown name and `None` default both return `None`.
- **Live:** message the bot an `arxiv.org/abs/...` link → the Zotero item appears with a
  `paperbot` tag, and (if `ZOTERO_COLLECTION` is set to an existing collection name) inside
  that collection.
