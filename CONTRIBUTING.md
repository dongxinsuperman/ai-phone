# Contributing to ai-phone

Thanks for helping improve ai-phone. This project spans Python backend/agent
code, a Vue web console, a Midscene bridge, and mobile-device integrations, so
small, well-scoped changes are easiest to review.

## Development Setup

Backend:

```bash
cd backend
python3.11 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env
```

Web:

```bash
cd web
npm install
```

Midscene bridge:

```bash
cd midscene-bridge
npm install
cp .env.midscene.example .env.midscene
```

Do not commit real `.env` files, generated reports, screenshots, `.data/`,
`node_modules/`, `dist/`, or local virtual environments.

## Checks Before a Pull Request

Run the checks that match the files you changed:

```bash
cd backend && python -m pytest -q
cd web && npm run build
cd midscene-bridge && npm run build
```

The GitHub Actions workflow runs the same baseline checks for pull requests.

## Pull Request Guidelines

- Keep changes focused on one behavior or one document area.
- Include tests for backend scheduling, locking, API, runner, or protocol
  changes whenever practical.
- Update README or docs when changing setup steps, environment variables, API
  payloads, or deployment assumptions.
- Keep third-party binaries and generated assets out of changes unless the PR is
  specifically about updating that component.
- Explain device/platform assumptions for iOS, Android, or HarmonyOS changes.
- Avoid logging tokens, provider API keys, device identifiers from private
  fleets, or report URLs that contain sensitive data.

## Issue Reports

Useful bug reports include:

- commit hash or release;
- backend, web, and bridge versions if they differ;
- platform: iOS / Android / HarmonyOS;
- device model and OS version when relevant;
- exact goal or submission payload;
- sanitized logs and screenshots.

For security issues, follow `SECURITY.md` and avoid public disclosure until a fix
is available.
