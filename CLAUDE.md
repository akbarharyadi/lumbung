# Lumbung — personal finance agent

Dev happens on the Windows box; production runs on a private home server.
Connection details for the server live in `deploy.local.md`, which is
**gitignored on purpose** — never commit server addresses, usernames, or keys.

Tests: `python -m pytest tests -q` (475+ tests, run before every push).
Lint: `python -m ruff check src tests`.

## Deploy Configuration (configured by /setup-deploy)
- Platform: custom — private home server over SSH (see `deploy.local.md` for
  the address, user and key; deliberately not in this public repo)
- Production URL: https://lumbung.akbarharyadi.com (read-only dashboard behind
  Cloudflare Access, served from `127.0.0.1:8788` via `lumbung-tunnel.service`)
- App dir on server: `~/apps/lumbung` (NOT a git clone yet — code was copied
  there; runs under `uv run`, venv at `~/apps/lumbung/.venv`, editable install)
- Services (systemd, system-level units — restarting them needs sudo):
  - `lumbung-engine.service` — trading engine. **`TA_MODE=live` in the
    server-side `.env` — real money.**
  - `lumbung-agent.service` — chat/research answerer (opencode2 / GLM on 127.0.0.1:42778)
  - `lumbung-dashboard.service` — read-only dashboard, port 8788
  - `lumbung-tunnel.service` — cloudflared tunnel for lumbung.akbarharyadi.com
  - Timers: `lumbung-daily.timer` (16:15 WIB), `lumbung-research.timer` (07:30 WIB)
- Deploy status command: `ssh <server> "systemctl is-active lumbung-engine lumbung-agent lumbung-dashboard lumbung-tunnel"`
- Health check: `curl -sf https://lumbung.akbarharyadi.com/ -o /dev/null -w "%{http_code}"` (expects 200)
- Merge method: direct push to `main` (solo project); no PR gate
- Project type: self-hosted web app + always-on trading engine

### Custom deploy hooks
- Pre-merge: none (tests must pass locally before push)
- Deploy trigger: manual — sync code to `~/apps/lumbung` then restart the touched
  services (sudo required). **Caution: restarting `lumbung-engine` interrupts
  stop-loss monitoring (Indodax has no server-side stop); restart it promptly
  and never leave it down.**
- Deploy status: `systemctl is-active` on the four services (above)
- Health check: dashboard URL returns 200 through Cloudflare Access

## Secrets
- Never commit `.env`, `config/holdings.yaml`, any Indodax key, or any server
  address/credential. The server keeps its own `.env`; the Windows dev box
  keeps the repo root `.env`.
- `deploy.local.md` is the single allowed local home for connection details.
