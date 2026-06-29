# Paper-link → Zotero + Google Drive → iPad pipeline

## Context
You want a single "share target": send a link to one app, and if the link is a
paper it gets (a) indexed in your Zotero library and (b) its PDF dropped into a
Google Drive folder that your iPad PDF reader already syncs. Non-paper links are
just acknowledged. Decisions locked in:

- **Trigger app:** Telegram bot (only one with a free, easy API; you paste or
  share-to-Telegram a link to the bot).
- **Host:** small cloud VPS, always-on, long-polling (no public URL/TLS needed).
- **Zotero:** you have a zotero.org sync account → add items via the Zotero Web API.

This is a brand-new project (empty dir). Everything below is new.

## Architecture (one script + two helper services)

```
You ──link──▶ Telegram bot ──▶ bot.py (VPS, systemd)
                                  │
                                  ├─ translation-server (Docker) : URL → Zotero JSON + PDF link
                                  ├─ is it a paper? (itemType check)
                                  ├─ Zotero Web API : POST item  → indexed
                                  ├─ get PDF (translation-server attachment → Unpaywall fallback)
                                  └─ rclone copyto PDF → gdrive:Papers/  → iPad syncs it
```

One Python file does the orchestration. translation-server and rclone are
off-the-shelf; we write no metadata-mapping or Drive-upload code ourselves.

## Components on the VPS
1. **Docker** running `zotero/translation-server` (the same engine Zotero uses;
   turns a URL into Zotero-ready item JSON and surfaces attachment/PDF URLs).
   `docker run -d -p 1969:1969 --restart unless-stopped zotero/translation-server`
2. **rclone** with a Google Drive remote named `gdrive` (one-time interactive
   OAuth: `rclone config`). Upload is one line: `rclone copyto file.pdf gdrive:Papers/<name>.pdf`.
3. **Python venv** with `python-telegram-bot` and `requests` (stdlib for the rest).
4. **systemd unit** `paperbot.service` running `bot.py`, restart on failure.
5. **Secrets** in `/etc/paperbot/env` (chmod 600, loaded by systemd `EnvironmentFile`):
   - `TELEGRAM_TOKEN` (from BotFather)
   - `ALLOWED_CHAT_ID` (your Telegram user id — bot ignores everyone else)
   - `ZOTERO_API_KEY`, `ZOTERO_USER_ID` (from zotero.org/settings/keys)
   - `UNPAYWALL_EMAIL` (your email; free Unpaywall OA-PDF lookup)

## Files to create
- `bot.py` — the whole pipeline (see flow below).
- `requirements.txt` — `python-telegram-bot`, `requests`.
- `paperbot.service` — systemd unit (ExecStart venv python bot.py, EnvironmentFile).
- `README.md` — the VPS setup steps above, in order.

## bot.py flow
1. Long-poll Telegram. On each message: reject if `chat_id != ALLOWED_CHAT_ID`.
2. Extract first URL (regex). No URL → reply "no link found", stop.
3. `POST http://localhost:1969/web` with the URL → items JSON.
4. **Paper test:** first item's `itemType` in
   {journalArticle, conferencePaper, preprint, book, bookSection, thesis, report}.
   Not a paper → reply "not a paper, ignored", stop. *(ponytail: simple allowlist;
   widen the set if something legit gets skipped.)*
5. **Index in Zotero:** `POST https://api.zotero.org/users/<id>/items` with the
   item JSON (Zotero-API-Key header). Capture the new item key.
6. **Get PDF:** use the attachment PDF URL from translation-server if present;
   else if item has a DOI, query Unpaywall (`api.unpaywall.org/v2/<doi>?email=`)
   for an open-access PDF URL. Download bytes. No PDF found → skip step 7, tell user.
7. **To Drive:** filename `FirstAuthor_Year_TitleSlug.pdf` from the metadata;
   `rclone copyto` into `gdrive:Papers/`. iPad reader syncs from there.
8. Reply: title, "✓ Zotero" + item link, "✓ Drive" or "no open PDF".

## Deliberate simplifications (ponytail)
- **No PDF attached *inside* Zotero** — Zotero holds the searchable metadata
  record; the readable PDF lives in Drive for the iPad. Add the Zotero
  file-attachment upload (3-step authorize/upload API) later only if you want the
  PDF in Zotero too. Add when you actually open papers from Zotero on desktop.
- **Long polling, not webhooks** — no domain/TLS to manage. Add a webhook only if
  instant delivery ever matters (it won't for saving papers).
- **Non-paper links just get acknowledged** — no routing to UpNote/Notes (neither
  has a usable API). Add when you have a concrete second destination.
- **translation-server over hand-rolled Crossref+arXiv parsing** — it's one Docker
  command and returns correct Zotero JSON, so it's *less* of our code and more
  robust across publishers. If you'd rather not run Docker, the fallback is
  arXiv API + Crossref + Unpaywall in pure Python (more code, fewer sites covered).

## Verification (end to end)
- **Self-check (no network):** `bot.py --selftest` runs the paper-test and
  filename-builder against a saved sample translation-server response and a
  non-paper response; asserts paper→True/non-paper→False and the slug format.
  This is the one runnable check guarding the non-trivial logic.
- **Live:** with services up, send the bot an arXiv link (e.g. an `arxiv.org/abs/...`)
  → expect a Zotero item to appear in your library and a PDF in `gdrive:Papers/`,
  then on the iPad after sync. Send a DOI/journal link (OA) → same. Send a
  non-paper link (news article) → "not a paper, ignored". Send a paywalled,
  non-OA DOI → indexed in Zotero, reply "no open PDF".
