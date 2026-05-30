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

GitHub Actions may be run manually when maintainer quota and timing allow. Do
not assume that every pull request will receive automatic CI.

## Pull Request Guidelines

This repository is maintained as an upstream reference implementation. Official
branches are maintained by the original author. Issues and Discussions are the
preferred collaboration path for problems, scenarios, and design tradeoffs.
Pull requests are welcome as bug reports, design references, or candidate
patches, but they are not guaranteed to be reviewed, responded to, accepted, or
merged. The maintainer may reimplement, adapt, or decline a contribution to keep
the official branch stable and maintainable.

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

## Forks and Third-Party Versions

Forks and long-lived downstream branches are welcome. Modified or redistributed
versions must continue to comply with GNU GPLv3 and preserve the original
license, source attribution, and third-party notices.

Third-party versions must not present themselves as the official ai-phone
release or imply maintenance, publication, or endorsement by the original
maintainer.

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
