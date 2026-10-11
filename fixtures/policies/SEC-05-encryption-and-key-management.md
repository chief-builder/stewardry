---
policy_id: SEC-05
title: Encryption and Key Management
version: 3
status: current
classification: internal
owner: Security Engineering Lead
effective_date: "2026-08-15"
review_date: "2027-08-15"
confluence_page_id: "105"
---

# SEC-05 Encryption and Key Management

## Encryption

5.1 Customer data is encrypted at rest with AES-256 in all production databases, object storage, and backups.

5.2 Data in transit over public networks is encrypted with TLS 1.2 or higher. TLS 1.0 and 1.1 are disabled.

5.3 Service-to-service traffic inside the production network is encrypted with mutual TLS.

## Key management

5.4 Encryption keys are managed in the cloud provider's key management service and are rotated at least every 12 months.

5.5 Access to key management functions is restricted to the Platform Security team, and every use of a key is logged.

5.6 Customer-managed encryption keys are not offered.
