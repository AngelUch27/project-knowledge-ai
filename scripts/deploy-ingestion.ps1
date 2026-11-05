param(
    [string]$ProjectId = "geglobal-level-program",
    [string]$Region = "us-central1",
    [string]$TriggerLocation = "us",
    [string]$Bucket = "knowledge-agent-cap",
    [string]$DataStoreId = "knowledge-agent-rag_1792366120415",
    [string]$SearchLocation = "global",
    [string]$SyncSchedule = "0 6 * * *",
    [string]$SyncTimeZone = "America/Mexico_City"
)

$ErrorActionPreference = "Stop"
$serviceAccountName = "knowledge-agent-ingest"
$serviceAccount = "$serviceAccountName@$ProjectId.iam.gserviceaccount.com"
$bucketRoleId = "knowledgeAgentBucketMetadata"
$importRoleId = "knowledgeAgentDocumentImporter"
$storageServiceAgent = (& gcloud storage service-agent --project $ProjectId | Where-Object { $_ -match 'service-\d+@gs-project-accounts\.iam\.gserviceaccount\.com' } | ForEach-Object { $_.Trim() } | Select-Object -First 1)
if (-not $storageServiceAgent) { throw "Could not determine the Cloud Storage service agent for project $ProjectId" }

function Test-GcloudResource([scriptblock]$Command) {
    try {
        & $Command *> $null
        return $LASTEXITCODE -eq 0
    } catch {
        return $false
    }
}

function Invoke-Gcloud([string[]]$Arguments) {
    try {
        & gcloud @Arguments
        if ($LASTEXITCODE -ne 0) {
            throw "gcloud command returned exit code $LASTEXITCODE"
        }
    } catch {
        throw "Deployment stopped: $($_.Exception.Message)"
    }
}

if (-not (Test-GcloudResource { gcloud iam service-accounts describe $serviceAccount --project $ProjectId })) {
    Invoke-Gcloud @("iam", "service-accounts", "create", $serviceAccountName, "--project", $ProjectId, "--display-name", "Knowledge Agent document ingestion")
}

if (-not (Test-GcloudResource { gcloud iam roles describe $bucketRoleId --project $ProjectId })) {
    Invoke-Gcloud @("iam", "roles", "create", $bucketRoleId, "--project", $ProjectId, "--title", "Knowledge Agent bucket metadata updater", "--description", "Read and update ingestion status metadata on source objects", "--permissions", "storage.objects.get,storage.objects.update", "--stage", "GA")
}

# Agent Search (Discovery Engine) does not evaluate IAM conditions, so a conditional
# roles/discoveryengine.editor binding is ignored and imports fail with PERMISSION_DENIED.
# Grant a custom role limited to the two permissions the function needs, without a condition.
$importPermissions = "discoveryengine.documents.import,discoveryengine.operations.get,discoveryengine.documents.list,discoveryengine.documents.delete"
if (-not (Test-GcloudResource { gcloud iam roles describe $importRoleId --project $ProjectId })) {
    Invoke-Gcloud @("iam", "roles", "create", $importRoleId, "--project", $ProjectId, "--title", "Knowledge Agent document importer", "--description", "Import, list, and delete Agent Search documents and read import operations", "--permissions", $importPermissions, "--stage", "GA")
} else {
    # Keep the permission set current for roles created by earlier versions of this script.
    Invoke-Gcloud @("iam", "roles", "update", $importRoleId, "--project", $ProjectId, "--permissions", $importPermissions, "--quiet")
}
Invoke-Gcloud @("projects", "add-iam-policy-binding", $ProjectId, "--member", "serviceAccount:$serviceAccount", "--role", "projects/$ProjectId/roles/$importRoleId", "--condition=None")
Invoke-Gcloud @("projects", "add-iam-policy-binding", $ProjectId, "--member", "serviceAccount:$serviceAccount", "--role", "projects/$ProjectId/roles/$bucketRoleId", "--condition", "expression=resource.name.startsWith('projects/_/buckets/$Bucket/objects/'),title=knowledge-agent-bucket-only,description=Allow ingestion metadata access only to objects in the configured bucket")
Invoke-Gcloud @("projects", "add-iam-policy-binding", $ProjectId, "--member", "serviceAccount:$serviceAccount", "--role", "roles/logging.logWriter", "--condition=None")
Invoke-Gcloud @("projects", "add-iam-policy-binding", $ProjectId, "--member", "serviceAccount:$serviceAccount", "--role", "roles/eventarc.eventReceiver", "--condition=None")
Invoke-Gcloud @("storage", "buckets", "add-iam-policy-binding", "gs://$Bucket", "--project", $ProjectId, "--member", "serviceAccount:$serviceAccount", "--role", "roles/storage.bucketViewer")
# The scheduled GitHub sync uploads changed files and deletes removed ones, so it needs object
# create/delete on this bucket only (the metadata role above is read/update only).
Invoke-Gcloud @("storage", "buckets", "add-iam-policy-binding", "gs://$Bucket", "--project", $ProjectId, "--member", "serviceAccount:$serviceAccount", "--role", "roles/storage.objectUser")
Invoke-Gcloud @("projects", "add-iam-policy-binding", $ProjectId, "--member", "serviceAccount:$storageServiceAgent", "--role", "roles/pubsub.publisher", "--condition=None")

Invoke-Gcloud @("services", "enable", "cloudfunctions.googleapis.com", "run.googleapis.com", "eventarc.googleapis.com", "cloudbuild.googleapis.com", "artifactregistry.googleapis.com", "cloudscheduler.googleapis.com", "--project", $ProjectId)

# The sync function reuses the backend's GitHub importer; Cloud Functions only uploads the
# --source folder, so copy the module in (the copy is git-ignored).
Copy-Item (Join-Path $PSScriptRoot "..\backend\github_import.py") (Join-Path $PSScriptRoot "..\ingestion\github_import.py") -Force

$functionEnv = "GCP_PROJECT_ID=$ProjectId,AGENT_SEARCH_LOCATION=$SearchLocation,AGENT_SEARCH_DATA_STORE_ID=$DataStoreId,GCS_BUCKET=$Bucket"
$commonDeployArgs = @("--gen2", "--project", $ProjectId, "--region", $Region, "--runtime", "python312", "--source", "ingestion", "--service-account", $serviceAccount, "--timeout", "540s", "--set-env-vars", $functionEnv)
$eventDeployArgs = @("--trigger-location", $TriggerLocation, "--trigger-event-filters", "bucket=$Bucket", "--trigger-service-account", $serviceAccount, "--retry", "--max-instances", "10")

# object.finalized -> incremental import into Agent Search
Invoke-Gcloud (@("functions", "deploy", "knowledge-agent-gcs-import", "--entry-point", "import_gcs_object", "--trigger-event-filters", "type=google.cloud.storage.object.v1.finalized") + $commonDeployArgs + $eventDeployArgs)
# object.deleted -> remove the document from the index (overwrites are detected and skipped)
Invoke-Gcloud (@("functions", "deploy", "knowledge-agent-gcs-delete", "--entry-point", "delete_gcs_object", "--trigger-event-filters", "type=google.cloud.storage.object.v1.deleted") + $commonDeployArgs + $eventDeployArgs)
# HTTP, private -> re-import every staged GitHub repository; invoked daily by Cloud Scheduler
Invoke-Gcloud (@("functions", "deploy", "knowledge-agent-github-sync", "--entry-point", "sync_github_repositories", "--trigger-http", "--no-allow-unauthenticated", "--max-instances", "1") + $commonDeployArgs)

# Eventarc and Cloud Scheduler call the functions' Cloud Run services with an OIDC token for
# $serviceAccount. Grant invoker on each service itself: a project-level roles/run.invoker binding
# conditioned on resource.name does not match Cloud Run services, so deliveries are rejected with 401.
foreach ($service in @("knowledge-agent-gcs-import", "knowledge-agent-gcs-delete", "knowledge-agent-github-sync")) {
    Invoke-Gcloud @("run", "services", "add-iam-policy-binding", $service, "--project", $ProjectId, "--region", $Region, "--member", "serviceAccount:$serviceAccount", "--role", "roles/run.invoker")
}

$syncUri = (& gcloud functions describe knowledge-agent-github-sync --gen2 --project $ProjectId --region $Region --format "value(serviceConfig.uri)").Trim()
if (-not $syncUri) { throw "Could not read the URL of knowledge-agent-github-sync" }
$schedulerArgs = @("--project", $ProjectId, "--location", $Region, "--schedule", $SyncSchedule, "--time-zone", $SyncTimeZone, "--uri", $syncUri, "--http-method", "POST", "--oidc-service-account-email", $serviceAccount, "--oidc-token-audience", $syncUri, "--attempt-deadline", "540s")
if (Test-GcloudResource { gcloud scheduler jobs describe knowledge-agent-github-sync --project $ProjectId --location $Region }) {
    Invoke-Gcloud (@("scheduler", "jobs", "update", "http", "knowledge-agent-github-sync") + $schedulerArgs)
} else {
    Invoke-Gcloud (@("scheduler", "jobs", "create", "http", "knowledge-agent-github-sync") + $schedulerArgs)
}
