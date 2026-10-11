---
policy_id: SEC-17
title: Network Security
version: 2
status: current
classification: confidential
owner: Security Engineering Lead
effective_date: "2026-08-01"
review_date: "2027-08-01"
confluence_page_id: "117"
---

# SEC-17 Network Security

## Segmentation and access

17.1 Production networks are segmented from corporate networks, with no direct connectivity between them.

17.2 Administrative access to production networks is possible only through an identity-aware proxy. No VPN or bastion host with static credentials is used.

## Perimeter

17.3 Inbound traffic to production reaches only load balancers protected by a web application firewall.

17.4 Distributed denial-of-service protection is provided by the cloud provider's edge network.

17.5 Firewall and security group rules are reviewed every quarter.
