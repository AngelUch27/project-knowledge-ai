# Project Knowledge AI

A private document Q&A demo built with FastAPI, Agent Search, and Gemini. The web interface is served by the API.

## Demo sections

- **Chat:** ask questions and receive Gemini answers with the titles of retrieved sources.
- **Documents:** upload supported files, view bucket contents, and check ingestion status.
- **Activity:** review recent uploads and import status messages.

## Set up a new computer

Requirements: Windows PowerShell, Git, Python 3.10 or later, and the Google Cloud CLI (`gcloud`). Users need access to this repository and the required permissions on the GCP resources. Repository or project read access alone does not grant those permissions.

Clone the repository and enter the project folder:

```powershell
git clone https://github.com/AngelUch27/project-knowledge-ai.git
Set-Location project-knowledge-ai
```

## Run the application locally

```powershell
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r backend\requirements.txt
Copy-Item .env.example .env
```

`.env.example` contains the demo project and resource IDs. `.env` stores local configuration and is excluded from Git. Do not add credentials or key files.

The GCP administrator must grant each app user permission to query Agent Search (`roles/discoveryengine.user`), use Vertex AI (`roles/aiplatform.user`), and read, list, upload, and update bucket objects (`roles/storage.objectUser`). Then authenticate with your own account without creating or downloading service account keys:

```powershell
gcloud auth login
gcloud config set project geglobal-level-program
gcloud auth application-default login
```

Start the local application:

```powershell
.\.venv\Scripts\python.exe -m uvicorn backend.main:app --host 127.0.0.1 --port 8000
```

Open `http://127.0.0.1:8000`. The server binds to `127.0.0.1`, so it accepts connections from this computer only. It also validates the host and rejects browser requests from other origins. The web page and API use the same origin; CORS is not enabled and the API is not exposed publicly. Do not change the host to `0.0.0.0` for this demo. If you publish the app later, use Cloud Run with IAM authentication and an attached runtime service account. Do not deploy a public API without authentication.

## Upload and automatic indexing

The Documents tab uploads files to `knowledge-agent-cap` under `documents/raw/`. Other bucket prefixes are watched as well. For each supported object that is finalized, an Eventarc-triggered Cloud Run function calls `ImportDocuments` with that object's exact URI and incremental mode. Supported formats are PDF, DOCX, PPTX, XLSX, XLSM, TXT, MD, HTML, and JSON. Ingestion status and messages are stored in object metadata and shown in Documents and Activity. Unsupported file types are ignored.

## Deploy automatic ingestion to GCP

Run this once per project from the repository root. The deploying account needs permissions to manage IAM, enable services, and deploy Cloud Run Functions and Eventarc. In particular, it needs `roles/run.sourceDeveloper`, `roles/serviceusage.serviceUsageConsumer`, and `roles/iam.serviceAccountUser` on the runtime service account. It must also be able to create service accounts and service agent roles and modify IAM policies. A GCP project administrator can run these steps using their own account. See Google's [Cloud Run functions deployment permissions](https://cloud.google.com/run/docs/deploy-functions).

In PowerShell, sign in, select the project, and grant the active account permission to use the runtime identity. This block creates the service account if it does not already exist:

```powershell
gcloud auth login
gcloud config set project geglobal-level-program
$project = "geglobal-level-program"
$serviceAccount = "knowledge-agent-ingest@$project.iam.gserviceaccount.com"
$activeAccount = (gcloud auth list --filter="status:ACTIVE" --format="value(account)").Trim()
gcloud iam service-accounts describe $serviceAccount --project $project *> $null
if ($LASTEXITCODE -ne 0) {
    gcloud iam service-accounts create knowledge-agent-ingest --project $project --display-name "Knowledge Agent document ingestion"
}
gcloud iam service-accounts add-iam-policy-binding $serviceAccount --member="user:$activeAccount" --role="roles/iam.serviceAccountUser" --project $project
.\scripts\deploy-ingestion.ps1
gcloud functions describe knowledge-agent-gcs-import --gen2 --region us-central1 --project $project --format="value(state)"
```

The final command should show `ACTIVE`. The script enables APIs and deploys `knowledge-agent-gcs-import` using a service account **without a key**. It limits Agent Search imports to the configured data store and metadata updates to the bucket. If required by your organization policy, a project administrator must also grant `roles/run.builder` to the Cloud Build service account specified by Google Cloud.

If these commands return `PERMISSION_DENIED`, the active account does not have enough permissions; ask a project administrator to grant them. Users who only run the local app do not need deployment permissions.

Uploads create Cloud Storage events. Eventarc and Cloud Run Functions retry failed deliveries. Imports are incremental and requested per file. Agent Search processes and indexes files asynchronously, so a document might take several minutes to become searchable. The UI shows “Sent to Agent Search” when the import operation finishes; this does not guarantee that search indexing has already propagated.

Objects that existed before the trigger was created do not produce retroactive events. If you need to index existing files, run a separate initial import. This is not done automatically to avoid re-importing existing content.

## API routes and configuration

- `GET /health`: local server health; it does not check GCP connectivity.
- `GET /documents`: lists up to 1,000 supported bucket files and their ingestion metadata. This limit only applies to the UI list; questions search the corpus indexed by Agent Search.
- `POST /documents`: uploads one file to the bucket. The UI accepts files up to 15 MB each.
- `POST /search`: queries the configured Search App.
- `POST /ask`: retrieves the five most relevant results from the indexed corpus and generates an answer with Gemini. Gemini does not read all 1,000 documents for every question; Agent Search retrieves the most relevant content.
- `/docs`: interactive FastAPI documentation, accessible locally only.

The `.env` variables are `GCP_PROJECT_ID`, `GCP_LOCATION`, `GCS_BUCKET`, `GEMINI_MODEL`, `AGENT_SEARCH_LOCATION`, `AGENT_SEARCH_APP_ID`, `AGENT_SEARCH_SERVING_CONFIG`, and `AGENT_SEARCH_DATA_STORE_ID`. Application Default Credentials provide authentication. Never commit tokens, keys, or documents to Git.

Search, ingestion, and Gemini requests can incur charges in the shared project.

