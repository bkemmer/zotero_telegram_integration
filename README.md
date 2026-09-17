# paperbot

Send a link to a Telegram bot. If it's a paper, it gets indexed in your Zotero
library and its PDF is dropped into a Google Drive folder your iPad PDF reader
syncs. Non-paper links get a "not a paper" reply.

![paperbot architecture: bot.py orchestrates a Telegram link
through translation-server, the Zotero Web API, PDF sources (arXiv/Unpaywall)
and Google Drive, which syncs to the iPad.](docs/architecture.svg)

## VPS setup (in order)

On an Oracle Cloud Always Free VM, read [oci-setup.md](docs/oci-setup.md) first —
it covers swap, the firewall and host-specific steps, then sends you back here.

### 1. translation-server
Run it natively on Node 22 — see
[translation-server-armhf.md](docs/translation-server-armhf.md); the build is the
same on amd64, only the unit's node path differs.

> **Docker only if a published image matches your architecture.** The current
> `zotero/translation-server` tags (`latest`, `2.0.6`) are **arm64-only**, the last
> amd64 tag is `2.0.4` (2021), and there is no `arm/v7` manifest at all. On arm64:
> ```sh
> docker run -d -p 127.0.0.1:1969:1969 --restart unless-stopped zotero/translation-server
> ```
> Bind loopback — the bot talks to it locally, so the port needs no public exposure.

### 2. rclone + Google Drive remote
```sh
sudo apt install rclone        # or: curl https://rclone.org/install.sh | sudo bash
rclone config                  # create a remote named "gdrive", type "drive" (OAuth)
rclone mkdir gdrive:Papers     # the folder your iPad reader syncs
```
On a headless VPS, `rclone config` will tell you to run `rclone authorize "drive"`
on a machine with a browser and paste the token back.

### 3. Install
Run `rclone config` as yourself first (step 2), then from the repo dir:
```sh
sudo apt install python3-venv   # setup.sh builds the /opt/paperbot venv with this
sudo ./setup.sh
```
This creates the `paperbot` system user, the `/opt/paperbot` venv, copies `bot.py`
and your `rclone.conf` in, installs the systemd unit, and stubs an
`/etc/paperbot/env` with placeholders (a re-run never overwrites an existing one).

### 4. Secrets
Replace the placeholder env file `setup.sh` created. Tips go **above** each var:
systemd's `EnvironmentFile` only strips full-line comments, so a trailing `# …`
would end up inside the value.
```sh
sudo tee /etc/paperbot/env >/dev/null <<'EOF'
# from @BotFather
TELEGRAM_TOKEN=...
# your Telegram user id (from @userinfobot) — only you can use the bot
ALLOWED_CHAT_ID=...
# zotero.org/settings/keys (needs write access)
ZOTERO_API_KEY=...
# the numeric "Your userID" on that same page
ZOTERO_USER_ID=...
UNPAYWALL_EMAIL=you@example.com

# optional — uncomment to override defaults:
# collection to file papers into (default: PAPERBOT, auto-created)
#ZOTERO_COLLECTION=ML Papers
#RCLONE_REMOTE=gdrive
#DRIVE_DIR=Papers
#TRANSLATION_SERVER=http://localhost:1969
EOF
sudo chmod 600 /etc/paperbot/env
```
The service runs as `paperbot` and reads rclone from `/opt/paperbot/rclone.conf`
(set in the unit), so no `RCLONE_CONFIG` line is needed here.

### 5. Start
```sh
sudo systemctl enable --now paperbot
journalctl -u paperbot -f      # watch it
```
To watch logs as your own user without `sudo`:
```sh
sudo usermod -aG systemd-journal $USER
# log out/in (or `newgrp systemd-journal`) for it to take effect
journalctl -u paperbot -f
```

### 6. Restart
After updating `bot.py` or `/etc/paperbot/env`, restart the service:
```sh
sudo systemctl restart paperbot
```

## Update
To deploy changes from your local repo to the VPS:
```sh
scp bot.py paperbot@your-vps:/opt/paperbot/bot.py
ssh paperbot@your-vps "sudo systemctl restart paperbot"
```
Or re-run `setup.sh` (also updates the service unit if changed):
```sh
sudo ./setup.sh
sudo systemctl restart paperbot
```

## Verify
- Offline logic: `pytest` → runs the offline unit tests (needs the dev deps: `uv sync`, or `pip install pytest pytest-mock`).
- Live: message the bot an `arxiv.org/abs/...` link (or a bare arXiv id like `2608.11888v1`) → a Zotero item appears and
  the PDF lands in `gdrive:Papers/` (then on your iPad after sync). A news link →
  "Not a paper, ignored." A paywalled, non-open DOI → indexed in Zotero + "no open PDF".

## Sending a PDF
Send the bot a PDF file (not a link) and it replies with a button per subfolder of
`gdrive:Papers/` — tap one and the file is uploaded there under its original name
(plus a root option and Cancel). Folders are listed live with
`rclone lsf --dirs-only`, so create them in Drive and they show up on the next PDF.
Telegram's bot API caps downloads at 20 MB; larger files are rejected with a note.

## Commands
- `whoami` — returns the bot's local network IP (useful for debugging on a headless VPS).
- `/subcollection <name>` — creates a Zotero collection `<name>` under `PAPERBOT` (which is
  created too if missing); sending an existing name just replies "already exists". Sent
  without a name (e.g. Tab-completed from the menu), the bot asks for it — just reply.
- `/listSubcollections` — lists the subcollections of `PAPERBOT` by name. The name prompt
  above links to it too.

The bot registers both in Telegram's `/` menu on startup, so no BotFather `/setcommands`
step. Telegram only allows lowercase menu names, so the menu shows `/listsubcollections`;
commands are matched case-insensitively, so either spelling works when typed.

## Filing into a subcollection
When a paper is added, or found already indexed, the reply carries one button per
subcollection of `PAPERBOT`. Tap one to also file the paper there. It stays in its default
collection, which is what duplicate detection lists, so re-sending the link still says
"Already in". No subcollections yet → no buttons.

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
