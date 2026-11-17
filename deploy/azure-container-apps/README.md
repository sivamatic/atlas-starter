# Deploy Atlas to Azure Container Apps

One real cloud path: Atlas runs as a container app, and its knowledge base and tickets live in
Azure Database for PostgreSQL Flexible Server with pgvector. `deploy.ps1` runs every step.
Nothing here was run against a real subscription while writing this course; the commands and
flags were checked against the Microsoft Learn pages listed at the end and against
`az ... --help` from Azure CLI 2.92.0.

## What gets created

| Resource | Why |
| --- | --- |
| Resource group (`rg-atlas-dev`) | One place to see the cost and delete everything |
| Postgres flexible server, Burstable `Standard_B1ms`, 32 GiB, version 16 | Knowledge base (pgvector) and tickets |
| Firewall rule `allowazureservices` (0.0.0.0) | Lets Azure services, including the app, reach the database |
| Server parameter `azure.extensions=vector` | pgvector must be allow-listed before `CREATE EXTENSION vector` |
| Container Apps environment and app, external HTTPS ingress, port 8000 | Atlas itself, built from the repo `Dockerfile` |
| App secret `atlas-db-url`, env `ATLAS_DB_URL=secretref:atlas-db-url` | The connection string never appears as a plain env var |

`az containerapp up --source` builds the image from the `Dockerfile` and pushes it to Azure
Container Registry. It creates the registry, the environment and a Log Analytics workspace if they
don't exist yet.

## Run it

One-time setup, from the `up` how-to page:

```powershell
az login
az extension add --name containerapp --upgrade
az provider register --namespace Microsoft.App
az provider register --namespace Microsoft.OperationalInsights
```

Then:

```powershell
cd deploy\azure-container-apps
.\deploy.ps1 -Plan                                  # print every command, run nothing
.\deploy.ps1 -ResourceGroup rg-atlas-dev -Location westeurope
.\deploy.ps1 -ResourceGroup rg-atlas-dev -Solved -DataDir examples/bank   # demo with the worked solutions
.\deploy.ps1 -ResourceGroup rg-atlas-dev -SkipPostgres                      # in-memory store, cheapest
```

The script prints the app URL. Check it:

```powershell
curl https://<fqdn>/v1/health
curl https://<fqdn>/v1/ready
curl -X POST https://<fqdn>/v1/ask -H "X-Atlas-User: priya" -H "Content-Type: application/json" -d '{"question":"My VPN keeps dropping"}'
az containerapp logs show --name atlas --resource-group rg-atlas-dev --follow
```

`LLM_MODE=replay` is the default, so the app only answers questions that are in
`recordings/tape.json`. For a real model, pass `-LlmMode live -LlmBaseUrl https://...` and store
the key as a secret:

```powershell
az containerapp secret set --name atlas --resource-group rg-atlas-dev --secrets llm-api-key=<key>
az containerapp update --name atlas --resource-group rg-atlas-dev --set-env-vars LLM_API_KEY=secretref:llm-api-key
```

A secret change alone does not reach running replicas. The `update` above creates a new revision
that reads it.

## Health probes

Atlas has `/v1/health` (liveness: the process answers) and `/v1/ready` (readiness: adapter,
budget and knowledge base). Container Apps probes are set in the app's YAML. Export it, add a
`probes` list under `properties.template.containers[0]`, and apply it:

```powershell
az containerapp show --name atlas --resource-group rg-atlas-dev -o yaml > app.yaml
# add under the container:
#   probes:
#     - type: Liveness
#       httpGet: { path: /v1/health, port: 8000 }
#     - type: Readiness
#       httpGet: { path: /v1/ready, port: 8000 }
az containerapp update --name atlas --resource-group rg-atlas-dev --yaml app.yaml
```

## Cost

You pay while the resources exist, not only while you use them. The Postgres server bills for
compute and storage every hour it exists, even when idle. Container Apps on the consumption plan
bill per use; scale to zero between sessions with
`az containerapp update --name atlas --resource-group rg-atlas-dev --min-replicas 0`.
Check the current prices for your region on the pricing pages below before you start.

Pause the database when you are not using it (`az postgres flexible-server stop --resource-group
rg-atlas-dev --name <server>`), and delete everything when you are done.

## Teardown

```powershell
az group delete --name rg-atlas-dev --yes --no-wait
```

This deletes the app, the environment, the registry, the database and its data. Check the portal
or `az group list -o table` a few minutes later to confirm it is gone.

## Production gaps (on purpose)

- The firewall rule admits any Azure service. Production uses VNet integration and private access.
- The admin user is the app user. Production creates a least-privilege database role.
- Images built by `up --source` are tagged by the tool. Production builds in CI, scans and pins a digest.
- Identity is the `X-Atlas-User` header. Production puts Entra ID authentication in front of the app.

## Sources

- `az containerapp up` reference: https://learn.microsoft.com/en-us/cli/azure/containerapp?view=azure-cli-latest#az-containerapp-up
- `az containerapp up` how-to (setup, what it creates): https://learn.microsoft.com/en-us/azure/container-apps/containerapp-up
- Secrets and `secretref:`: https://learn.microsoft.com/en-us/azure/container-apps/manage-secrets
- Health probes: https://learn.microsoft.com/en-us/azure/container-apps/health-probes
- `az postgres flexible-server create`: https://learn.microsoft.com/en-us/cli/azure/postgres/flexible-server?view=azure-cli-latest#az-postgres-flexible-server-create
- Allow-list extensions (`azure.extensions`): https://learn.microsoft.com/en-us/azure/postgresql/extensions/how-to-allow-extensions
- Extension versions (pgvector): https://learn.microsoft.com/en-us/azure/postgresql/extensions/concepts-extensions-versions
- Firewall rules: https://learn.microsoft.com/en-us/cli/azure/postgres/flexible-server/firewall-rule?view=azure-cli-latest
- TLS on by default: https://learn.microsoft.com/en-us/azure/postgresql/flexible-server/concepts-networking-ssl-tls
- Postgres pricing: https://azure.microsoft.com/en-us/pricing/details/postgresql/flexible-server/
- Container Apps pricing: https://azure.microsoft.com/en-us/pricing/details/container-apps/
