---
title: Postmortem - search outage on 9 September
acl: engineering
owner: discovery-team
updated: 2026-09-16
---
## Summary
Search was down for 47 minutes on 9 September. The TLS certificate on search-gateway expired because the renewal job had been failing silently for three weeks.

## Root cause and actions
Root cause: the certificate renewal job had no alert on failure. Actions: alert when any certificate expires within 14 days, and add the renewal job to the weekly on-call checklist.