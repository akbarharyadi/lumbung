# Lumbung — agent instructions

Read `CLAUDE.md` first for the deploy configuration and the secrets policy.
Server connection details (address, user, key) live in `deploy.local.md`,
which is gitignored — never copy them into committed files.

- Run tests before pushing: `python -m pytest tests -q`
- `lumbung-engine` on the server runs in **live mode** — treat deploys and
  restarts as money-touching operations, not routine chores.
