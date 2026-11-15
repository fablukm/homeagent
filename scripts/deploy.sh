#!/usr/bin/env bash
set -euo pipefail
cd /mnt/nvme/docker/homeagent && git pull --ff-only
docker compose build && docker compose up -d
docker compose logs --tail 5 homeagent laya-serve
