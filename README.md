# homeagent

Personal home-automation agent: mail triage, calendar rules, tasks, Telegram.

## Run on the Pi

1. `git clone` this repo to the source folder named in `homeagent.docker-compose.yml`.
2. In the stack folder, next to the compose file, create:
   - `.env` from `.env.example`
   - `config/` with every `config/*.toml`; `sources.toml` and `telegram.toml` come from their `.example.toml`
   - `rules/` from `rules.example/`
3. `docker compose up -d --build homeagent`

Update: `scripts/deploy.sh`.

## Google login

Run `uv run python scripts/google_auth.py login` on a machine with a browser, copy the refresh token into the Pi's `.env`, then `uv run python scripts/google_auth.py check`.
