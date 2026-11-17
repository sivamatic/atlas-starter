---
title: Service catalog
acl: engineering
owner: platform-team
updated: 2026-10-01
---
## checkout-api
Owner: payments team. Tier 1. Pager rotation: payments-primary. Depends on orders-db and the payment gateway.

## search-gateway
Owner: discovery team. Tier 2. Pager rotation: discovery-primary. Terminates TLS for search traffic.

## orders-db
Owner: data platform team. Tier 1. Postgres primary with two read replicas.