---
title: Runbook - checkout-api 5xx errors
acl: oncall
owner: payments-team
updated: 2026-10-20
---
## Triage
Open the checkout-api dashboard and compare the 5xx rate with the deploy markers. If the errors started within 30 minutes of a deploy, treat the deploy as the cause.

## Roll back
Roll back the last checkout-api deploy with: deployctl rollback checkout-api --to previous. Watch the 5xx rate for 10 minutes after the rollback finishes.

## Escalate
If the 5xx rate is still above 2 percent after 15 minutes, page the payments team secondary and declare a SEV2 incident.