# ADR 0005: Web UI and dashboard

Status: Accepted · 2026-10-10 (owner review of chief-builder/stewardry#1)

## Decision

- One server-rendered web app: FastAPI routes, Jinja2 templates, and HTMX
  for partial updates. HTMX is vendored at a pinned version and served
  locally, with no CDN.
- Sessions use an opaque server-side session ID in an HttpOnly, SameSite
  cookie, with CSRF tokens on every state-changing form. Hub tokens never
  reach the browser.
- There is one view per role: Sales status, GRC review, Legal review,
  platform admin, auditor, and owner. Each view is built from a view model
  that the server filters for that role. Templates never receive fields the
  role must not see, so a template mistake cannot leak hidden data.
- The dashboard lives in the same app. It is made of server-rendered tables
  and simple SVG charts generated on the server. Every figure shows its
  provenance label, sample size, and time window (ADR 0007).

**Reason:** this keeps authorization and filtering on the server, in one
codebase and one language. A role-restricted review surface is the main UI
requirement, and this is the shortest path to it.

## Rejected alternatives

- **A React or other single-page app:** it adds a build toolchain and a
  second language, and it pushes more state and authorization decisions to
  the browser.
- **Streamlit or Gradio:** weak per-role authorization and request control.
  They are not suitable for a restricted review surface.
- **Grafana or Metabase for the dashboard:** they add a separate
  authentication plane and direct database reads, which can bypass the
  role-filtered views. They also add another service.

## Consequences

- The UI looks plain. Visual identity uses neutral styling with no
  Anthropic branding.
- Leak tests target the rendered HTML, status messages, and exports per
  role, not only the API.
