# TODO — paperbot setup

Code is done (`bot.py`, `requirements.txt`, `paperbot.service`, `README.md`).
Remaining steps are the manual ones — see README for the exact commands.

## Accounts / secrets (do first, can't be automated)
- [ ] Create the bot via @BotFather → copy `TELEGRAM_TOKEN`
- [ ] Get your `ALLOWED_CHAT_ID` from @userinfobot
- [ ] Create a Zotero API key (write access) → `ZOTERO_API_KEY` + numeric `ZOTERO_USER_ID`
      at zotero.org/settings/keys
- [ ] Pick the email for `UNPAYWALL_EMAIL`

## VPS
- [ ] `docker run` the `zotero/translation-server` (README step 1)
- [ ] Install rclone, `rclone config` a remote named `gdrive`, `rclone mkdir gdrive:Papers`
      (headless: `rclone authorize "drive"` on a browser machine, paste token back)
- [ ] Copy `bot.py` to `/opt/paperbot`, make venv, `pip install -r requirements.txt`
- [ ] Write `/etc/paperbot/env` (chmod 600) with the 5 secrets above
- [ ] Install + enable the service: `sudo systemctl enable --now paperbot`

## Verify
- [ ] `python bot.py --selftest` → `selftest ok`  (needs `requests` installed)
- [ ] Send an arxiv.org link → Zotero item + PDF in `gdrive:Papers/`
- [ ] Confirm the PDF appears on the iPad after Drive sync
- [ ] Send a news link → "Not a paper, ignored"

## Maybe later (skipped on purpose)
- [ ] Attach the PDF inside Zotero too (file-upload API) — only if you open papers from Zotero desktop
- [ ] Confirm `gdrive:Papers/` is exactly the folder your iPad reader syncs
