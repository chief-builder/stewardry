# Security policy

## Reporting a vulnerability

Please report vulnerabilities privately through GitHub:
**[Report a vulnerability](https://github.com/chief-builder/stewardry/security/advisories/new)**
(Security tab → Advisories → Report a vulnerability). Do not open a public
issue, pull request, or discussion for a suspected vulnerability.

Include the affected component, the commit, steps to reproduce, and the
impact. Never include real tokens, API keys, or client secrets. Stewardry
uses synthetic data, and the offline tier reproduces most application
behavior without any credentials.

You can expect an acknowledgement within 7 days.

## Supported versions

Only `main` is supported. Stewardry is a portfolio project in active
development (Milestone 1). It is not production software and makes no
availability claims.

## Scope notes

- Stewardry depends on
  [vendor-token-broker](https://github.com/chief-builder/vendor-token-broker)
  at a pinned commit. Report issues in the broker or its MCP gateway to that
  repository. Report how Stewardry *uses* them here.
- Stewardry does not inherit any security claim from the broker or any other
  project. Its own trust boundaries and known limitations are in
  [THREAT_MODEL.md](THREAT_MODEL.md).
