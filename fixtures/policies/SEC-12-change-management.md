---
policy_id: SEC-12
title: Change Management
version: 1
status: current
classification: internal
owner: Security Engineering Lead
effective_date: "2026-08-01"
review_date: "2027-08-01"
confluence_page_id: "112"
---

# SEC-12 Change Management

## Standard changes

12.1 Production changes are tracked in the change system with an owner, a description, and a rollback plan.

12.2 Changes are tested in a staging environment before they reach production.

12.3 Infrastructure is defined as code, and infrastructure changes go through the same review as application code.

## Emergency changes

12.4 Emergency changes may skip staging with approval from the on-call lead and are reviewed within 2 business days.
