#!/usr/bin/env bash
set -euo pipefail

SRC="${INFRA_CONTROL_SNIPE_OPS_SOURCE:-/root/snipe-it/frontend-next/var/ops}"
DEST="${INFRA_CONTROL_SNIPE_OPS_DIR:-/opt/infra-asset-manager/var/imports/snipe-ops}"

mkdir -p "$DEST"
sudo find "$SRC" -maxdepth 1 -type f \( -name '*.json' -o -name '*.csv' \) -print0 \
  | sudo xargs -0 -I{} cp -p {} "$DEST"/
sudo chown -R "$(id -u):$(id -g)" "$DEST"
find "$DEST" -maxdepth 1 -type f | wc -l
