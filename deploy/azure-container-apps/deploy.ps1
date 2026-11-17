<#
.SYNOPSIS
  Deploy Atlas to Azure Container Apps, with Azure Database for PostgreSQL Flexible Server + pgvector.

.DESCRIPTION
  Creates (or reuses) a resource group, a Postgres flexible server with the "vector" extension
  allow-listed, and a container app built from this repo's Dockerfile. Costs money while the
  resources exist: run the teardown in README.md when you are done.

  -Plan prints every command and runs nothing. Start there.

.EXAMPLE
  .\deploy.ps1 -Plan
  .\deploy.ps1 -ResourceGroup rg-atlas-dev -Location westeurope -Solved
  .\deploy.ps1 -ResourceGroup rg-atlas-dev -SkipPostgres          # in-memory store, no database
#>
[CmdletBinding()]
param(
    [string]$ResourceGroup = "rg-atlas-dev",
    [string]$Location = "westeurope",
    [string]$AppName = "atlas",
    [string]$Environment = "atlas-env",
    [string]$PgServer = "",                 # must be globally unique; default: atlas-pg-<random>
    [string]$PgAdmin = "atlasadmin",
    [string]$DataDir = "data",              # or examples/bank, examples/healthcare, examples/devops
    [string]$LlmMode = "replay",            # live needs -LlmBaseUrl and an API key secret
    [string]$LlmBaseUrl = "",
    [switch]$Solved,                        # deploy the worked solutions (for demos), not your atlas/
    [switch]$SkipPostgres,
    [switch]$Plan
)
$ErrorActionPreference = "Stop"
$repo = Resolve-Path (Join-Path $PSScriptRoot "..\..")

function Run([string[]]$cmd, [switch]$Secret) {
    $shown = if ($Secret) { ($cmd -join " ") -replace "(password|secrets)\s+\S+", '$1 ***' } else { $cmd -join " " }
    Write-Host "> $shown" -ForegroundColor Cyan
    if ($Plan) { return "" }
    $out = & $cmd[0] $cmd[1..($cmd.Length - 1)]
    if ($LASTEXITCODE -ne 0) { throw "failed: $shown" }
    return $out
}

if (-not $Plan) {
    az account show --query name -o tsv | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "Run 'az login' first." }
}

# 1. Source to build. -Solved builds a copy with the worked solutions applied.
$source = $repo
if ($Solved) {
    $source = Join-Path $repo "var\deploy-build"
    Run @("python", (Join-Path $repo "tools\apply_solutions.py"), $source) | Out-Null
}

Run @("az", "group", "create", "--name", $ResourceGroup, "--location", $Location, "-o", "none") | Out-Null

# 2. Postgres flexible server, the pgvector allow-list, and the database.
$dbUrl = ""
if (-not $SkipPostgres) {
    if (-not $PgServer) { $PgServer = "atlas-pg-" + -join ((97..122) | Get-Random -Count 6 | ForEach-Object { [char]$_ }) }
    # Upper, lower, digit and symbol, URL-safe so it can sit in the connection string as is.
    $pgPassword = "Aa1-" + ([Convert]::ToBase64String((1..24 | ForEach-Object { [byte](Get-Random -Maximum 256) })) -replace "[+/=]", "x")
    Run -Secret @("az", "postgres", "flexible-server", "create", "--resource-group", $ResourceGroup, "--name", $PgServer,
        "--location", $Location, "--tier", "Burstable", "--sku-name", "Standard_B1ms", "--storage-size", "32",
        "--version", "16", "--admin-user", $PgAdmin, "--admin-password", $pgPassword,
        "--public-access", "None", "--yes", "-o", "none") | Out-Null
    # Let Azure services (the container app) reach the server. Tighter: VNet integration, see README.
    Run @("az", "postgres", "flexible-server", "firewall-rule", "create", "--resource-group", $ResourceGroup,
        "--server-name", $PgServer, "--name", "allowazureservices", "--start-ip-address", "0.0.0.0",
        "--end-ip-address", "0.0.0.0", "-o", "none") | Out-Null
    # pgvector must be allow-listed before Atlas can run CREATE EXTENSION vector.
    Run @("az", "postgres", "flexible-server", "parameter", "set", "--resource-group", $ResourceGroup,
        "--server-name", $PgServer, "--name", "azure.extensions", "--value", "vector", "-o", "none") | Out-Null
    Run @("az", "postgres", "flexible-server", "db", "create", "--resource-group", $ResourceGroup,
        "--server-name", $PgServer, "--name", "atlas", "-o", "none") | Out-Null
    $dbUrl = "postgresql://${PgAdmin}:${pgPassword}@${PgServer}.postgres.database.azure.com:5432/atlas?sslmode=require"
}

# 3. The container app: build from the Dockerfile, external HTTPS ingress on port 8000.
Run @("az", "containerapp", "up", "--name", $AppName, "--resource-group", $ResourceGroup, "--location", $Location,
    "--environment", $Environment, "--source", "$source", "--ingress", "external", "--target-port", "8000",
    "--env-vars", "LLM_MODE=$LlmMode", "LLM_BASE_URL=$LlmBaseUrl", "ATLAS_DATA_DIR=$DataDir", "ATLAS_RERANK=overlap") | Out-Null

# 4. Secrets are stored on the app and referenced with secretref:, never put in plain env vars.
if ($dbUrl) {
    Run -Secret @("az", "containerapp", "secret", "set", "--name", $AppName, "--resource-group", $ResourceGroup,
        "--secrets", "atlas-db-url=$dbUrl", "-o", "none") | Out-Null
    Run @("az", "containerapp", "update", "--name", $AppName, "--resource-group", $ResourceGroup,
        "--set-env-vars", "ATLAS_DB_URL=secretref:atlas-db-url", "-o", "none") | Out-Null
}

$fqdn = Run @("az", "containerapp", "show", "--name", $AppName, "--resource-group", $ResourceGroup,
    "--query", "properties.configuration.ingress.fqdn", "-o", "tsv")
if (-not $Plan) {
    Write-Host "Atlas: https://$fqdn/v1/health  and  https://$fqdn/v1/ready" -ForegroundColor Green
    if ($Solved) { Remove-Item -Recurse -Force $source }
}
Write-Host "Teardown: az group delete --name $ResourceGroup --yes --no-wait"
