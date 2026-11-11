#!/usr/bin/env bash
set -euo pipefail
cd /mnt/nvme/src/homeagent && git pull --ff-only
cd /mnt/nvme/docker/homeagent && docker compose build homeagent && docker compose up -d homeagent
docker compose logs --tail 5 homeagent
