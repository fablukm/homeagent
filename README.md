# homeagent

Personal home-automation agent: mail triage, calendar rules, tasks, Telegram.

## Run on the Pi

1. `git clone` this repo to the stack folder `/mnt/nvme/docker/homeagent`.
2. In that folder create:
   - `.env` from `.env.example`
   - `config/` with every `config/*.toml`; `sources.toml` and `telegram.toml` come from their `.example.toml`
   - `rules/` from `rules.example/`
   - `models/laya/<name>/` with a fine-tuned Laya model; the compose file names the one laya-serve loads
3. `docker compose up -d --build`

Update: `scripts/deploy.sh`.

## Google login

Run `uv run python scripts/google_auth.py login` on a machine with a browser, copy the refresh token into the Pi's `.env`, then `uv run python scripts/google_auth.py check`.
