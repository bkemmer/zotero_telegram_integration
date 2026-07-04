#!/bin/sh
# One-time paperbot install on the VPS. Run as root from the repo dir: sudo ./setup.sh
set -eu

APP=/opt/paperbot

# system user: no login, home = the app dir
id paperbot >/dev/null 2>&1 || \
  useradd --system --home-dir "$APP" --shell /usr/sbin/nologin paperbot

# app dir + code
install -d -o paperbot -g paperbot "$APP"
install -o paperbot -g paperbot -m 644 bot.py "$APP/bot.py"

# venv + deps
python3 -m venv "$APP/venv"
"$APP/venv/bin/pip" install -q requests
chown -R paperbot:paperbot "$APP"

# move your rclone config in (run `rclone config` first, as yourself)
if [ -f "$HOME/.config/rclone/rclone.conf" ]; then
  install -o paperbot -g paperbot -m 600 "$HOME/.config/rclone/rclone.conf" "$APP/rclone.conf"
else
  echo "WARN: no rclone.conf found — run 'rclone config' then copy it to $APP/rclone.conf"
fi

# secrets file (fill in the 5 values, see README)
install -d /etc/paperbot
[ -f /etc/paperbot/env ] || {
  install -m 600 /dev/null /etc/paperbot/env
  echo "Created empty /etc/paperbot/env — fill in the secrets (see README)."
}

# service
install -m 644 paperbot.service /etc/systemd/system/paperbot.service
systemctl daemon-reload
echo "Done. Fill /etc/paperbot/env, then: sudo systemctl enable --now paperbot"
