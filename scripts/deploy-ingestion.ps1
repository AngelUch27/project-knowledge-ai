param(
    [string]$ProjectId = "geglobal-level-program",
    [string]$Region = "us-central1",
    [string]$TriggerLocation = "us",
    [string]$Bucket = "knowledge-agent-cap",
    [string]$DataStoreId = "knowledge-agent-rag_1792366120415",
    [string]$SearchLocation = "global"
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
if (-not (Test-GcloudResource { gcloud iam roles describe $importRoleId --project $ProjectId })) {
    Invoke-Gcloud @("iam", "roles", "create", $importRoleId, "--project", $ProjectId, "--title", "Knowledge Agent document importer", "--description", "Import documents into Agent Search and read import operations", "--permissions", "discoveryengine.documents.import,discoveryengine.operations.get", "--stage", "GA")
}
Invoke-Gcloud @("projects", "add-iam-policy-binding", $ProjectId, "--member", "serviceAccount:$serviceAccount", "--role", "projects/$ProjectId/roles/$importRoleId", "--condition=None")
Invoke-Gcloud @("projects", "add-iam-policy-binding", $ProjectId, "--member", "serviceAccount:$serviceAccount", "--role", "projects/$ProjectId/roles/$bucketRoleId", "--condition", "expression=resource.name.startsWith('projects/_/buckets/$Bucket/objects/'),title=knowledge-agent-bucket-only,description=Allow ingestion metadata access only to objects in the configured bucket")
Invoke-Gcloud @("projects", "add-iam-policy-binding", $ProjectId, "--member", "serviceAccount:$serviceAccount", "--role", "roles/logging.logWriter", "--condition=None")
Invoke-Gcloud @("projects", "add-iam-policy-binding", $ProjectId, "--member", "serviceAccount:$serviceAccount", "--role", "roles/eventarc.eventReceiver", "--condition=None")
Invoke-Gcloud @("storage", "buckets", "add-iam-policy-binding", "gs://$Bucket", "--project", $ProjectId, "--member", "serviceAccount:$serviceAccount", "--role", "roles/storage.bucketViewer")
Invoke-Gcloud @("projects", "add-iam-policy-binding", $ProjectId, "--member", "serviceAccount:$storageServiceAgent", "--role", "roles/pubsub.publisher", "--condition=None")

Invoke-Gcloud @("services", "enable", "cloudfunctions.googleapis.com", "run.googleapis.com", "eventarc.googleapis.com", "cloudbuild.googleapis.com", "artifactregistry.googleapis.com", "--project", $ProjectId)

Invoke-Gcloud @("functions", "deploy", "knowledge-agent-gcs-import", "--gen2", "--project", $ProjectId, "--region", $Region, "--runtime", "python312", "--source", "ingestion", "--entry-point", "import_gcs_object", "--trigger-location", $TriggerLocation, "--trigger-event-filters", "type=google.cloud.storage.object.v1.finalized", "--trigger-event-filters", "bucket=$Bucket", "--trigger-service-account", $serviceAccount, "--service-account", $serviceAccount, "--retry", "--timeout", "540s", "--max-instances", "3", "--set-env-vars", "GCP_PROJECT_ID=$ProjectId,AGENT_SEARCH_LOCATION=$SearchLocation,AGENT_SEARCH_DATA_STORE_ID=$DataStoreId")

# Eventarc pushes events to the function's Cloud Run service with an OIDC token for $serviceAccount.
# Grant invoker on the service itself: a project-level roles/run.invoker binding conditioned on
# resource.name does not match Cloud Run services, so deliveries are rejected with 401.
Invoke-Gcloud @("run", "services", "add-iam-policy-binding", "knowledge-agent-gcs-import", "--project", $ProjectId, "--region", $Region, "--member", "serviceAccount:$serviceAccount", "--role", "roles/run.invoker")
