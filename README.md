# paperbot

Send a link to a Telegram bot. If it's a paper, it gets indexed in your Zotero
library and its PDF is dropped into a Google Drive folder your iPad PDF reader
syncs. Non-paper links get a "not a paper" reply.

```
You ──link──▶ Telegram bot ──▶ bot.py (VPS)
                                 ├─ translation-server (Docker): URL → Zotero JSON + PDF link
                                 ├─ paper? (itemType check)
                                 ├─ Zotero Web API: add item → indexed
                                 ├─ PDF: translation-server attachment → Unpaywall fallback
                                 └─ rclone copyto → gdrive:Papers/ → iPad syncs it
```

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
# optional: RCLONE_REMOTE=gdrive  DRIVE_DIR=Papers  TRANSLATION_SERVER=http://localhost:1969
EOF
sudo chmod 600 /etc/paperbot/env
```

### 5. Service
```sh
sudo cp paperbot.service /etc/systemd/system/
sudo systemctl enable --now paperbot
journalctl -u paperbot -f      # watch it
```

## Verify
- Offline logic: `python bot.py --selftest`  → prints `selftest ok`.
- Live: message the bot an `arxiv.org/abs/...` link → a Zotero item appears and
  the PDF lands in `gdrive:Papers/` (then on your iPad after sync). A news link →
  "Not a paper, ignored." A paywalled, non-open DOI → indexed in Zotero + "no open PDF".

## Notes
- Zotero holds the searchable *record*; the readable PDF lives in Drive for the
  iPad. To also attach the PDF inside Zotero, add the file-upload API calls later.
- Uses Telegram long-polling (no domain/TLS needed) and raw `requests` instead of
  a Telegram SDK — one dependency, synchronous, matches the sequential pipeline.
