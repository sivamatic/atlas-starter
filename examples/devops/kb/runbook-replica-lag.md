---
title: Runbook - Postgres replica lag
acl: oncall
owner: data-platform
updated: 2026-09-28
---
## Symptoms
The replica lag alert fires when orders-db replicas are more than 30 seconds behind the primary. Reports and search show stale data.

## Fix
Check for a long-running query on the replica and cancel it. If lag keeps growing, remove the replica from the read pool with: dbctl pool remove orders-db-replica-2. Never promote a lagging replica.