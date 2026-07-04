# translation-server on armhf (ODROID XU4)

Reinstall guide for running Zotero `translation-server` natively on a 32-bit ARM
(`armv7l` / armhf) board like the ODROID XU4. Tested on Ubuntu 24.04 LTS
(glibc 2.39), translation-server **2.0.5**.

## Why not Docker?

The official `zotero/translation-server` image is **amd64-only** — there is no
`arm/v7` manifest, so `docker run` on the XU4 fails with `exec format error`.
translation-server is pure JavaScript (no mandatory native deps), so we run it
directly on Node instead.

## Node version

- Node **22 LTS** is the last LTS with official `armv7l` binaries — Node 24
  dropped 32-bit ARM. Use 22, not "latest".
- Node 18+ needs glibc ≥ 2.28; Ubuntu 24.04 (glibc 2.39) is fine.

Install via nvm:
```sh
nvm install 22
node -v          # should print v22.x
```
(Or NodeSource, which gives a cleaner `/usr/bin/node` for the systemd unit below.)

## Build

```sh
sudo git clone --recurse-submodules \
  https://github.com/zotero/translation-server /opt/translation-server
sudo chown -R $USER:$USER /opt/translation-server
cd /opt/translation-server
npm install
```

**Do NOT run `npm audit fix --force`.** The ~28 reported advisories are in pinned
transitive deps; force-upgrading them breaks the app. Ignore them.

## Smoke test

```sh
npm start
# → "Translators initialized with 742 loaded", "Listening on 0.0.0.0:1969"
```

In another terminal:
```sh
curl -d 'https://arxiv.org/abs/2003.08934' \
  -H 'Content-Type: text/plain' http://localhost:1969/web
```
A JSON array with the paper's title/authors/DOI means it works. Ctrl-C the
`npm start` window, then install the service below.

## systemd service (survives reboot)

```sh
sudo tee /etc/systemd/system/translation-server.service >/dev/null <<EOF
[Unit]
Description=Zotero translation-server
After=network.target

[Service]
Type=simple
User=$USER
WorkingDirectory=/opt/translation-server
ExecStart=$(which node) src/server.js
Restart=on-failure
Environment=NODE_ENV=production

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now translation-server
systemctl status translation-server --no-pager
```

Notes:
- `$(which node)` bakes in the absolute node path. With **nvm** that's under
  `~/.nvm/versions/node/v22.x/bin/node` — fine because the service runs as
  `User=$USER`, but keep your home dir readable (default perms are OK). With
  **NodeSource** it's `/usr/bin/node`, which is cleaner and independent of the
  login shell.
- If you upgrade Node via nvm later, the baked-in path changes — re-run the
  `tee` block and `daemon-reload`.

## Upgrading translation-server

```sh
cd /opt/translation-server
git pull --recurse-submodules
git submodule update --init --recursive
npm install
sudo systemctl restart translation-server
```

## Where paperbot expects it

`bot.py` defaults to `TRANSLATION_SERVER=http://localhost:1969`, so no bot config
change is needed when it runs on the same host. See the main [README](README.md).
