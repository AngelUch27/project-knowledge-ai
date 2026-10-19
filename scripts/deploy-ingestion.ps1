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

Invoke-Gcloud @("projects", "add-iam-policy-binding", $ProjectId, "--member", "serviceAccount:$serviceAccount", "--role", "roles/discoveryengine.editor", "--condition", "expression=resource.name.startsWith('projects/$ProjectId/locations/$SearchLocation/collections/default_collection/dataStores/$DataStoreId'),title=knowledge-agent-datastore-only,description=Allow document imports only into the Knowledge Agent data store")
Invoke-Gcloud @("projects", "add-iam-policy-binding", $ProjectId, "--member", "serviceAccount:$serviceAccount", "--role", "projects/$ProjectId/roles/$bucketRoleId", "--condition", "expression=resource.name.startsWith('projects/_/buckets/$Bucket/objects/'),title=knowledge-agent-bucket-only,description=Allow ingestion metadata access only to objects in the configured bucket")
Invoke-Gcloud @("projects", "add-iam-policy-binding", $ProjectId, "--member", "serviceAccount:$serviceAccount", "--role", "roles/logging.logWriter", "--condition=None")
Invoke-Gcloud @("projects", "add-iam-policy-binding", $ProjectId, "--member", "serviceAccount:$serviceAccount", "--role", "roles/eventarc.eventReceiver", "--condition=None")
Invoke-Gcloud @("projects", "add-iam-policy-binding", $ProjectId, "--member", "serviceAccount:$serviceAccount", "--role", "roles/run.invoker", "--condition", "expression=resource.name == 'projects/$ProjectId/locations/$Region/services/knowledge-agent-gcs-import',title=knowledge-agent-function-only,description=Allow Eventarc to invoke only the ingestion function")
Invoke-Gcloud @("storage", "buckets", "add-iam-policy-binding", "gs://$Bucket", "--project", $ProjectId, "--member", "serviceAccount:$serviceAccount", "--role", "roles/storage.bucketViewer")

Invoke-Gcloud @("services", "enable", "cloudfunctions.googleapis.com", "run.googleapis.com", "eventarc.googleapis.com", "cloudbuild.googleapis.com", "artifactregistry.googleapis.com", "--project", $ProjectId)

Invoke-Gcloud @("functions", "deploy", "knowledge-agent-gcs-import", "--gen2", "--project", $ProjectId, "--region", $Region, "--runtime", "python312", "--source", "ingestion", "--entry-point", "import_gcs_object", "--trigger-location", $TriggerLocation, "--trigger-event-filters", "type=google.cloud.storage.object.v1.finalized", "--trigger-event-filters", "bucket=$Bucket", "--trigger-service-account", $serviceAccount, "--service-account", $serviceAccount, "--retry", "--timeout", "540s", "--max-instances", "3", "--set-env-vars", "GCP_PROJECT_ID=$ProjectId,AGENT_SEARCH_LOCATION=$SearchLocation,AGENT_SEARCH_DATA_STORE_ID=$DataStoreId")
