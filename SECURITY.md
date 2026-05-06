# Security Policy

ai-phone controls real mobile devices and can execute actions inside apps. Treat the
server, agent, database, reports, and device network as trusted infrastructure.

## Supported Versions

Security fixes are handled on the latest `main` branch until the project starts
publishing versioned releases. After tagged releases are introduced, this section
will list the supported release line explicitly.

## Deployment Guidance

- Do not expose the backend directly to the public internet.
- Put the service behind VPN, private network routing, a zero-trust gateway, or
  your own reverse proxy authentication in production.
- Change `AI_PHONE_AGENT_TOKEN` and `AI_PHONE_SUBMISSION_INTERNAL_TOKEN` before
  any shared or production deployment. The default `dev` token is for local
  development only.
- Keep `backend/.env`, `midscene-bridge/.env.midscene`, database backups, HTML
  reports, screenshots, and `.data/` out of public artifacts.
- Use least-privilege API keys for VLM providers and rotate keys immediately if
  they are exposed in logs, reports, screenshots, or issue attachments.
- The external submission and report APIs are designed for controlled networks.
  If you expose them across network boundaries, add gateway-level authentication,
  rate limiting, and access logs.
- Webhook callbacks currently do not include a built-in HMAC signature. Use a
  private callback URL, gateway authentication, IP allowlists, or add your own
  signature verification before using callbacks in sensitive environments.
- Agent WebSocket authentication uses a shared token. Prefer TLS termination and
  avoid logging full WebSocket URLs because query strings may contain tokens.
- The web UI is an internal operations console, not a multi-tenant user system.
  Do not rely on it as an internet-facing admin portal without additional auth.

## Reporting a Vulnerability

Please report security issues privately instead of opening a public GitHub issue.
If GitHub private vulnerability reporting is enabled for the repository, use it.
Otherwise, contact the maintainer listed on the GitHub repository profile.

When reporting, include:

- affected commit or release;
- deployment mode and exposed endpoints;
- clear reproduction steps;
- impact assessment;
- any logs or screenshots with secrets redacted.

We will acknowledge valid reports as soon as possible and coordinate a fix before
public disclosure.
