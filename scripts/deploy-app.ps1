param(
    [string]$ProjectId = "geglobal-level-program",
    [string]$Region = "us-central1",
    [string]$ServiceName = "knowledge-agent",
    [string]$Bucket = "knowledge-agent-cap",
    [string]$DataStoreId = "knowledge-agent-rag_1792366120415",
    [string]$SearchAppId = "knowledge-agent-search_1792367963658",
    [string]$SearchLocation = "global",
    [string]$GeminiModel = "gemini-2.5-flash",
    # Who may open the app through Identity-Aware Proxy, e.g. "domain:example.com" or "user:someone@example.com".
    [string[]]$AllowedMembers = @("domain:capgemini.com")
)

# Publishes the web app + API on Cloud Run behind Identity-Aware Proxy (IAP):
#   - the service does NOT allow unauthenticated calls; IAP is the only way in
#   - IAP asks for a Google sign-in and admits only $AllowedMembers
#   - the app runs as a key-less service account limited to search, Gemini and the bucket
#   - min instances 0: costs nothing while nobody uses it
# Idempotent; run from the repository root. Requires permissions to deploy Cloud Run from source,
# manage IAM and enable services (a project administrator can run it).

$ErrorActionPreference = "Stop"
$serviceAccountName = "knowledge-agent-app"
$serviceAccount = "$serviceAccountName@$ProjectId.iam.gserviceaccount.com"

function Test-GcloudResource([scriptblock]$Command) {
    $previous = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try { & $Command *> $null; return $LASTEXITCODE -eq 0 } catch { return $false } finally { $ErrorActionPreference = $previous }
}

function Invoke-Gcloud([string[]]$Arguments) {
    # gcloud reports progress on stderr; judge by exit code only (see deploy-ingestion.ps1).
    $previous = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        & gcloud @Arguments 2>&1 | ForEach-Object { if ($_ -is [System.Management.Automation.ErrorRecord]) { $_.Exception.Message } else { $_ } }
        $exitCode = $LASTEXITCODE
    } finally { $ErrorActionPreference = $previous }
    if ($exitCode -ne 0) { throw "Deployment stopped: gcloud $($Arguments[0..1] -join ' ') returned exit code $exitCode" }
}

Invoke-Gcloud @("services", "enable", "run.googleapis.com", "iap.googleapis.com", "cloudbuild.googleapis.com", "artifactregistry.googleapis.com", "aiplatform.googleapis.com", "discoveryengine.googleapis.com", "--project", $ProjectId)

if (-not (Test-GcloudResource { gcloud iam service-accounts describe $serviceAccount --project $ProjectId })) {
    Invoke-Gcloud @("iam", "service-accounts", "create", $serviceAccountName, "--project", $ProjectId, "--display-name", "Knowledge Agent web app runtime")
}
# Same roles the README asks for a human user, nothing more.
Invoke-Gcloud @("projects", "add-iam-policy-binding", $ProjectId, "--member", "serviceAccount:$serviceAccount", "--role", "roles/discoveryengine.user", "--condition=None")
Invoke-Gcloud @("projects", "add-iam-policy-binding", $ProjectId, "--member", "serviceAccount:$serviceAccount", "--role", "roles/aiplatform.user", "--condition=None")
Invoke-Gcloud @("projects", "add-iam-policy-binding", $ProjectId, "--member", "serviceAccount:$serviceAccount", "--role", "roles/logging.logWriter", "--condition=None")
Invoke-Gcloud @("storage", "buckets", "add-iam-policy-binding", "gs://$Bucket", "--project", $ProjectId, "--member", "serviceAccount:$serviceAccount", "--role", "roles/storage.objectUser")

$appEnv = "GCP_PROJECT_ID=$ProjectId,GCP_LOCATION=$Region,GCS_BUCKET=$Bucket,GEMINI_MODEL=$GeminiModel,AGENT_SEARCH_LOCATION=$SearchLocation,AGENT_SEARCH_APP_ID=$SearchAppId,AGENT_SEARCH_SERVING_CONFIG=default_search,AGENT_SEARCH_DATA_STORE_ID=$DataStoreId,ALLOWED_HOSTS=*.run.app"
Invoke-Gcloud @("run", "deploy", $ServiceName, "--source", ".", "--project", $ProjectId, "--region", $Region, "--service-account", $serviceAccount, "--no-allow-unauthenticated", "--iap", "--ingress", "all", "--min-instances", "0", "--max-instances", "3", "--memory", "1Gi", "--cpu", "1", "--timeout", "300", "--concurrency", "20", "--set-env-vars", $appEnv)

# IAP's service agent calls the service on behalf of signed-in users.
$projectNumber = (& gcloud projects describe $ProjectId --format "value(projectNumber)").Trim()
Invoke-Gcloud @("run", "services", "add-iam-policy-binding", $ServiceName, "--project", $ProjectId, "--region", $Region, "--member", "serviceAccount:service-$projectNumber@gcp-sa-iap.iam.gserviceaccount.com", "--role", "roles/run.invoker")

# Who may sign in.
foreach ($member in $AllowedMembers) {
    Invoke-Gcloud @("beta", "iap", "web", "add-iam-policy-binding", "--project", $ProjectId, "--resource-type", "cloud-run", "--service", $ServiceName, "--region", $Region, "--member", $member, "--role", "roles/iap.httpsResourceAccessor")
}

$url = (& gcloud run services describe $ServiceName --project $ProjectId --region $Region --format "value(status.url)").Trim()
Write-Host ""
Write-Host "Knowledge Agent is published at: $url"
Write-Host "Sign-in is required; allowed: $($AllowedMembers -join ', ')"
