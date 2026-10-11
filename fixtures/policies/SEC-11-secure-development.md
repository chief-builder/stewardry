---
policy_id: SEC-11
title: Secure Development
version: 1
status: current
classification: internal
owner: Security Engineering Lead
effective_date: "2026-08-01"
review_date: "2027-08-01"
confluence_page_id: "111"
---

# SEC-11 Secure Development

## Code changes

11.1 All code changes require peer review before they are merged.

11.2 Automated static analysis and dependency scanning run on every change.

11.3 Secrets must not be stored in source code. Secret scanning blocks pushes that contain credentials.

## Deployment and training

11.4 Production deployments are performed only through the automated deployment pipeline.

11.5 Developers complete secure coding training every year.
