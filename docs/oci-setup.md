# paperbot on Oracle Cloud (OCI Always Free)

Host-specific notes for an **VM.Standard.E2.1.Micro** instance (Ubuntu 24.04).
Everything else is the main [README](../README.md) — this file only covers what
differs on OCI. Do the steps here, then README steps 2–5.

That shape is **amd64** with **1 GB RAM**, and the default login user is `ubuntu`.

## 1. Base + swap

```sh
sudo apt update && sudo apt full-upgrade -y && sudo reboot
```

1 GB is not enough on its own — `npm install` for translation-server will be
OOM-killed. Add swap before anything else:

```sh
sudo fallocate -l 2G /swapfile
sudo chmod 600 /swapfile
sudo mkswap /swapfile && sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
swapon --show
```

## 2. translation-server: native Node, not Docker

The published `zotero/translation-server` images do **not** cover amd64 any more —
`latest` and `2.0.6` are arm64-only, and the last amd64 tag is `2.0.4` (2021).
`docker run` warns about the platform mismatch and then fails.

Install Node 22 from NodeSource and follow the build in
[translation-server-armhf.md](translation-server-armhf.md) — the steps are
identical here, with `ExecStart=/usr/bin/node src/server.js` in the unit:

```sh
curl -fsSL https://deb.nodesource.com/setup_22.x | sudo -E bash -
sudo apt install -y nodejs
```

Verify before moving on:

```sh
curl -d 'https://arxiv.org/abs/2003.08934' \
  -H 'Content-Type: text/plain' http://localhost:1969/web
```

If Docker was already installed, reclaim the disk:

```sh
sudo docker system prune -af --volumes
sudo apt purge -y docker.io containerd runc && sudo apt autoremove -y
sudo rm -rf /var/lib/docker /var/lib/containerd
```

## 3. No firewall or security-list changes

The bot long-polls Telegram and connects **outbound only**, so nothing needs to be
opened. Check egress works:

```sh
curl https://api.telegram.org/bot<TOKEN>/getMe
```

If that hangs, the VCN security list / NSG is missing an egress rule to
`0.0.0.0/0` (and a private subnet needs a NAT gateway).

translation-server listens on `0.0.0.0:1969`, but OCI's default iptables admits
only SSH inbound, so it is not reachable from outside. **Don't add an ingress rule
for 1969** — the bot talks to it over localhost.

## 4. GitHub access from the VM

Use a repo **deploy key** (read-only, scoped to this one repo) rather than an
account key:

```sh
ssh-keygen -t ed25519 -f ~/.ssh/id_ed25519 -N ""
cat ~/.ssh/id_ed25519.pub   # add under repo → Settings → Deploy keys
git clone git@github.com:bkemmer/zotero_telegram_integration.git
```

Then continue with README steps 2–5.

## Migrating from an existing host

Instead of re-creating the five secrets, copy `/etc/paperbot/env` over from the old
host — run this from a machine that can reach both, and never commit the file:

```sh
ssh <old-host> 'sudo cat /etc/paperbot/env' > paperbot.env
scp paperbot.env ubuntu@<oci-ip>:/tmp/ && rm paperbot.env
ssh ubuntu@<oci-ip> 'sudo install -m 600 /tmp/paperbot.env /etc/paperbot/env && rm /tmp/paperbot.env'
```

`~/.config/rclone/rclone.conf` can move the same way, skipping `rclone authorize`.
Copy it **before** `sudo ./setup.sh`, which installs it into `/opt/paperbot`.

**Stop paperbot on the old host first.** Two instances long-polling the same bot
token conflict and Telegram returns HTTP 409:

```sh
ssh <old-host> 'sudo systemctl disable --now paperbot'
```

## After install

```sh
systemctl is-enabled unattended-upgrades     # security updates
sudo reboot
systemctl is-active translation-server paperbot   # both "active" after reboot
```

Note that Always Free instances can be **reclaimed when idle**; upgrading the
tenancy to Pay-As-You-Go exempts them (the Always Free allowance still costs
nothing).
