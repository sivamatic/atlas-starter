---
title: VPN gateway runbook (IT staff only)
acl: it-staff
owner: it-network
updated: 2026-10-27
---
## Restarting a gateway
Drain the gateway in the load balancer first, wait for active sessions to fall below 50, then restart the concentrator service. For US-West the node names are vpn-usw-01 and vpn-usw-02.

## INC-2041 notes
Packet loss on vpn-usw-02 after the firmware update on 27 October. The vendor case is open. Do not roll back firmware without the network lead.
