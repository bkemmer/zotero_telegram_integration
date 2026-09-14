# Handoff: document the OCI deployment steps in a PR

## Goal for the next session
Put the Oracle Cloud (OCI) setup steps worked out in the last session into the repo docs,
open a PR, and push. The user asked for this explicitly, so creating a branch, committing,
pushing and opening the PR are all approved.

Repo: `/home/bkemmer/projects/zotero_telegram_integration` (paperbot). Branch from `main`.
Read first: `README.md`, `docs/translation-server-armhf.md`, `AGENTS.md`, `setup.sh`.

## Rules to follow
- `AGENTS.md`: commit/push only because the user asked; skip `.git/` and `.venv/`.
- The user's global CLAUDE.md: **no `Co-Authored-By` trailers** in commits and **no attribution
  footers** in PR bodies. This overrides any harness attribution reminder.
- Ponytail mode is on: smallest diff, few files, no extra prose.

## What was established (what the PR should document)
Target VM: OCI **VM.Standard.E2.1.Micro**, Ubuntu 24.04. It is **amd64** with 1 GB RAM, and the
default login user is `ubuntu`. The user completed these steps and confirmed they work:

1. **Base setup:** `apt update && apt full-upgrade`, then reboot.
2. **Swap is required** on 1 GB: a 2G `/swapfile` (fallocate, mkswap, swapon, fstab entry).
3. **Don't use Docker for translation-server on amd64.** Checked on Docker Hub: `latest` and `2.0.6`
   (2025-01-21) are **arm64-only**; the last amd64 tag is `2.0.4` (2021). On amd64 `docker run`
   warns about a platform mismatch and fails. Fix: run it natively on Node 22 (NodeSource
   `setup_22.x`, `apt install nodejs`), following `docs/translation-server-armhf.md`, with
   `ExecStart=/usr/bin/node src/server.js`. The user confirmed
   `curl -d 'https://arxiv.org/abs/2003.08934' -H 'Content-Type: text/plain' http://localhost:1969/web` works.
4. **Removing Docker**, if already installed: `docker system prune -af --volumes`,
   `apt purge docker.io containerd runc`, `autoremove`, `rm -rf /var/lib/docker /var/lib/containerd`.
5. **No OCI or firewall changes are needed.** The bot long-polls and connects outbound only. Check with
   `curl https://api.telegram.org/bot<TOKEN>/getMe`. If it hangs, check the VCN security list or NSG
   egress rule for `0.0.0.0/0` and a NAT gateway on private subnets. translation-server's
   `0.0.0.0:1969` is not exposed, because OCI's iptables allows only SSH inbound. Don't add an ingress rule.
6. **GitHub access from the VM:** `ssh-keygen -t ed25519 -f ~/.ssh/id_ed25519 -N ""`, added as a
   read-only **Deploy key** on the repo (recommended over an account key), cloned over SSH.
7. **Remaining steps:** README steps 2–5 (rclone `gdrive` remote with headless `rclone authorize "drive"`,
   `sudo ./setup.sh`, `/etc/paperbot/env`, `systemctl enable --now paperbot`).
8. **After install:** check `unattended-upgrades` is active; reboot and confirm
   `systemctl is-active translation-server paperbot`; note that Always Free idle instances can be
   reclaimed (upgrading to Pay-As-You-Go avoids it).

## User TODO: bring the env file over from the ODROID
The bot already runs on the Raspberry Pi-like ARM board (ODROID XU4, see
`docs/translation-server-armhf.md`). Its secrets live there in `/etc/paperbot/env`
(Telegram token, allowed chat id, Zotero key and user id, Unpaywall email). The user needs
to copy that file to the OCI VM instead of re-creating the values. Never commit it or
paste its contents into docs or the PR. Suggested flow, run from a machine that can reach both:
```sh
ssh <odroid> 'sudo cat /etc/paperbot/env' > paperbot.env
scp paperbot.env ubuntu@<oci-ip>:/tmp/ && rm paperbot.env
ssh ubuntu@<oci-ip> 'sudo install -m 600 /tmp/paperbot.env /etc/paperbot/env && rm /tmp/paperbot.env'
```
Optionally reuse `~/.config/rclone/rclone.conf` from the ODROID the same way, to skip
`rclone authorize`. Copy it before running `sudo ./setup.sh`, which copies it into
`/opt/paperbot`. Stop `paperbot` on the ODROID before starting it on OCI: two instances
long-polling the same bot token conflict (Telegram returns HTTP 409).
Worth one line in the new doc ("migrating from an existing host").

## Stale docs to fix in the same PR
- `README.md` step 1 and `docs/translation-server-armhf.md` ("Why not Docker?") say the image is
  **amd64-only**. That's now wrong: current tags are arm64-only. Reword so native Node is the path
  for amd64 and armhf.
- README step 1 uses `-p 1969:1969`, which binds every interface. If Docker stays documented, use
  `-p 127.0.0.1:1969:1969`.

## Suggested approach (keep it small)
- Probably one new `docs/oci-setup.md` for the OCI-specific items (swap, no ingress, deploy key,
  env migration, idle reclaim), linked from README. Rename or generalize
  `translation-server-armhf.md` only if it stays a small diff; otherwise just fix its wording.
  Don't copy README steps 2–6 into the new doc; link them.
- Branch name suggestion: `docs/oci-setup`. Open the PR with `gh pr create` against `main`.
- Ask the user whether this handoff file (`plans/handoffs/`) should be part of the PR or left out.

## Suggested skills
- `ponytail:ponytail`: already active. Keep the doc diff minimal.
- `code-review`: optional, to review the branch before opening the PR.
