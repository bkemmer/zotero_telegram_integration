# paperbot

Send a link to a Telegram bot. If it's a paper, it gets indexed in your Zotero
library and its PDF is dropped into a Google Drive folder your iPad PDF reader
syncs. Non-paper links get a "not a paper" reply.

![paperbot architecture: bot.py on the odroid orchestrates a Telegram link
through translation-server, the Zotero Web API, PDF sources (arXiv/Unpaywall)
and Google Drive, which syncs to the iPad.](docs/architecture.svg)

## VPS setup (in order)

### 1. translation-server (Docker)
```sh
docker run -d -p 1969:1969 --restart unless-stopped zotero/translation-server
```
> **On 32-bit ARM (armv7l / armhf, e.g. ODROID XU4):** the Docker image is
> amd64-only. Run translation-server natively on Node instead — see
> [translation-server-armhf.md](translation-server-armhf.md).

### 2. rclone + Google Drive remote
```sh
sudo apt install rclone        # or: curl https://rclone.org/install.sh | sudo bash
rclone config                  # create a remote named "gdrive", type "drive" (OAuth)
rclone mkdir gdrive:Papers     # the folder your iPad reader syncs
```
On a headless VPS, `rclone config` will tell you to run `rclone authorize "drive"`
on a machine with a browser and paste the token back.

### 3. The bot
```sh
sudo mkdir -p /opt/paperbot && sudo cp bot.py /opt/paperbot/
sudo python3 -m venv /opt/paperbot/venv
sudo /opt/paperbot/venv/bin/pip install -r requirements.txt
```

### 4. Secrets
```sh
sudo mkdir -p /etc/paperbot
sudo tee /etc/paperbot/env >/dev/null <<'EOF'
TELEGRAM_TOKEN=...        # from @BotFather
ALLOWED_CHAT_ID=...       # your Telegram user id (from @userinfobot) — only you can use the bot
ZOTERO_API_KEY=...        # zotero.org/settings/keys (needs write access)
ZOTERO_USER_ID=...        # the numeric "Your userID" on that same page
UNPAYWALL_EMAIL=you@example.com
# optional: ZOTERO_COLLECTION=ML Papers  # collection to file papers into (default: PAPERBOT, auto-created)
# optional: RCLONE_REMOTE=gdrive  DRIVE_DIR=Papers  TRANSLATION_SERVER=http://localhost:1969
# the service runs as root, which has no rclone config of its own — point it at yours:
RCLONE_CONFIG=/home/YOURUSER/.config/rclone/rclone.conf
EOF
sudo chmod 600 /etc/paperbot/env
```

### 5. Service
```sh
sudo cp paperbot.service /etc/systemd/system/
sudo systemctl enable --now paperbot
journalctl -u paperbot -f      # watch it
```
To watch logs as your own user without `sudo`:
```sh
sudo usermod -aG systemd-journal $USER
# log out/in (or `newgrp systemd-journal`) for it to take effect
journalctl -u paperbot -f
```

## Verify
- Offline logic: `python bot.py --selftest`  → prints `selftest ok`.
- Live: message the bot an `arxiv.org/abs/...` link → a Zotero item appears and
  the PDF lands in `gdrive:Papers/` (then on your iPad after sync). A news link →
  "Not a paper, ignored." A paywalled, non-open DOI → indexed in Zotero + "no open PDF".

## Notes
- Duplicates are skipped: before adding, the bot lists the target collection (`PAPERBOT` by
  default) and, if the same paper is already there — matched by DOI, or by exact title when
  there's no DOI (e.g. arXiv preprints) — replies "Already in …" and skips the add and PDF
  upload. It checks the collection listing rather than Zotero's quick search, because the
  search index lags behind writes and would miss a paper added moments earlier.
- Every bot-added item gets a `paperbot` tag (easy to filter in Zotero) and is filed into
  a collection — `PAPERBOT` by default (created automatically on first use), or the name in
  `ZOTERO_COLLECTION` if set. Which collection best fits a paper is decided by
  `pick_collection()` in `bot.py` — a fixed-default stub for now, ready to swap for a real
  classifier; whatever name it returns is created if it doesn't exist.
- Zotero holds the searchable *record*; the readable PDF lives in Drive for the
  iPad. To also attach the PDF inside Zotero, add the file-upload API calls later.
- Uses Telegram long-polling (no domain/TLS needed) and raw `requests` instead of
  a Telegram SDK — one dependency, synchronous, matches the sequential pipeline.
