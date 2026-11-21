#!/usr/bin/env bash
set -Eeuo pipefail
[[ $EUID == 0 && $(uname -m) == x86_64 ]] || exit 1
cd /opt/vibe-zone-runner
[[ -f /etc/vibe-zone-runner.env ]] || { echo 'Configure /etc/vibe-zone-runner.env first'; exit 1; }
[[ $(stat -c %a /etc/vibe-zone-runner.env) == 600 ]] || exit 1
mountpoint -q /srv/vibe-zone-runner
[[ -x /usr/local/lib/vibe-zone/bounded-volume.sh ]]
docker info --format '{{json .Runtimes}}' | python3 -c 'import sys,json; assert "runsc" in json.load(sys.stdin)'
docker image inspect vibezone-runner-tools:local >/dev/null
install -m 0644 deploy/runner/vibe-zone-runner.service /etc/systemd/system/vibe-zone-runner.service
systemctl daemon-reload
systemctl enable --now vibe-zone-runner
systemctl is-active vibe-zone-runner
