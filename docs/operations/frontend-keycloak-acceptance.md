# Frontend Keycloak acceptance evidence

## Automated evidence

- Web TypeScript and ESLint: `npm.cmd --prefix web run check`
- Web unit and component tests: `npm.cmd --prefix web test`
- Production bundle: `npm.cmd --prefix web run build`
- Browser test discovery: `npm.cmd --prefix web run test:e2e:keycloak -- --list`
- Compose validation: `docker compose config --quiet`
- OpenSpec strict validation: `openspec validate add-frontend-keycloak-login --type change --strict`

Local Docker acceptance on 2026-08-26 completed with 24 Web unit/component tests, one real
Keycloak browser scenario, and a production Web build. The browser scenario exercised session
recovery, protected JSON and streaming requests, preview/download, natural token expiry and
renewal, logout, a second subject, cross-tenant 403, and immediate membership revocation. Direct
and Web-proxied readiness were healthy, while an anonymous tenant request returned 401.

The browser suite requires two Keycloak subjects, expected tenant names, an existing protected
document, and a streaming question through `E2E_*` secret environment variables. Its reporter is
line-only and trace, screenshot, and video capture are disabled so credentials and authorization
callbacks are not retained as test artifacts.

## Runtime image evidence

The Web image is built once. Two non-root temporary containers are then started from that same
image with different public issuer, redirect, and renewal-window values. Acceptance requires:

- each `/runtime-config.js` response contains only its own public values;
- each response sends `Cache-Control: no-store, max-age=0`;
- `/runtime-config.js` appears before the bundled module in built `index.html`;
- the document response includes the hardened CSP and browser security headers;
- both temporary containers are stopped and removed after verification.

## Known browser limits

- Third-party Cookie restrictions may disable silent iframe SSO. Keycloak's normal `check-sso`
  fallback or explicit login remains the supported path.
- Cross-tab logout is eventually consistent with Keycloak session checks and the next token
  refresh; application safety does not depend on immediate iframe notification.
- Access and refresh tokens remain in Keycloak adapter memory. XSS can still read process memory,
  so CSP, dependency pinning, safe rendering, and credential-free diagnostics remain required.
- Provider outage and invalid callback handling are deterministic adapter/component tests; the
  local same-site `localhost` topology cannot reproduce every third-party Cookie policy used by
  managed browsers.

## Rollback

Restore the previous Web image and runtime configuration, then verify API and Web health plus an
interactive login. Exact Keycloak callbacks introduced for the new Web can remain during the
rollback window. This frontend change has no database migration. Never attach browser traces,
screenshots, tokens, callback URLs, or test passwords to rollback evidence.
