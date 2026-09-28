#!/usr/bin/env bash
set -euo pipefail

dir="$(cd "$(dirname "$0")" && pwd)"
sudo cp "$dir/closed-loop-ran.service" /etc/systemd/system/
sudo cp "$dir/closed-loop-ran.timer" /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now closed-loop-ran.timer
