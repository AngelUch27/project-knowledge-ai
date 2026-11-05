"""Cloud Run functions that keep Agent Search in step with the bucket.

- ``import_gcs_object``: object.finalized → incremental ImportDocuments.
- ``delete_gcs_object``: object.deleted → delete the document from the index,
  unless the object still exists (an overwrite also emits a delete for the
  previous generation).
- ``sync_github_repositories``: HTTP, invoked by Cloud Scheduler → re-import
  every staged GitHub repository (changed files only, removed files deleted).
"""

import json
import logging
import os
from pathlib import PurePosixPath

import functions_framework
from google.api_core.client_options import ClientOptions
from google.cloud import discoveryengine_v1 as discoveryengine
from google.cloud import storage

PROJECT_ID = os.environ["GCP_PROJECT_ID"]
LOCATION = os.getenv("AGENT_SEARCH_LOCATION", "global")
DATA_STORE_ID = os.environ["AGENT_SEARCH_DATA_STORE_ID"]
GCS_BUCKET = os.getenv("GCS_BUCKET", "")
COLLECTION = "default_collection"
SUPPORTED_SUFFIXES = {".pdf", ".docx", ".pptx", ".xlsx", ".xlsm", ".txt", ".md", ".html", ".json"}
BRANCH = (
    f"projects/{PROJECT_ID}/locations/{LOCATION}/collections/{COLLECTION}"
    f"/dataStores/{DATA_STORE_ID}/branches/default_branch"
)


def _set_metadata(bucket_name: str, object_name: str, **values: str) -> None:
    blob = storage.Client().bucket(bucket_name).blob(object_name)
    blob.reload()
    metadata = dict(blob.metadata or {})
    metadata.update(values)
    blob.metadata = metadata
    blob.patch()


def _document_client() -> discoveryengine.DocumentServiceClient:
    endpoint = f"{LOCATION}-discoveryengine.googleapis.com" if LOCATION != "global" else None
    return discoveryengine.DocumentServiceClient(
        client_options=ClientOptions(api_endpoint=endpoint) if endpoint else None
    )


def _is_supported(object_name: str) -> bool:
    return PurePosixPath(object_name).suffix.lower() in SUPPORTED_SUFFIXES


@functions_framework.cloud_event
def import_gcs_object(cloud_event) -> None:
    data = cloud_event.data
    bucket = data.get("bucket", "")
    object_name = data.get("name", "")
    generation = str(data.get("generation", ""))
    if not bucket or not object_name:
        raise ValueError("Cloud Storage finalize event is missing bucket or object name")

    if not _is_supported(object_name):
        logging.info("Ignoring unsupported object: gs://%s/%s", bucket, object_name)
        return

    logging.info("Starting Agent Search import for gs://%s/%s", bucket, object_name)
    _set_metadata(
        bucket,
        object_name,
        ingestion_status="processing",
        ingestion_message="Incremental import started.",
        ingestion_generation=generation,
    )

    client = _document_client()
    request = discoveryengine.ImportDocumentsRequest(
        parent=BRANCH,
        gcs_source=discoveryengine.GcsSource(
            input_uris=[f"gs://{bucket}/{object_name}"],
            data_schema="content",
        ),
        reconciliation_mode=discoveryengine.ImportDocumentsRequest.ReconciliationMode.INCREMENTAL,
    )
    try:
        operation = client.import_documents(request=request)
        operation.result(timeout=540)
        _set_metadata(
            bucket,
            object_name,
            ingestion_status="imported",
            ingestion_message="Import accepted by Agent Search; indexing is asynchronous.",
            ingestion_operation=operation.operation.name,
            ingestion_generation=generation,
        )
        logging.info("Agent Search import completed: %s", operation.operation.name)
    except Exception as exc:
        try:
            _set_metadata(
                bucket,
                object_name,
                ingestion_status="failed",
                ingestion_message=f"Import failed and will be retried. ({type(exc).__name__})",
                ingestion_generation=generation,
            )
        except Exception:
            logging.exception("Could not write import failure status to the source object")
        raise
    finally:
        client.transport.close()


def find_document_names(document_client, branch: str, uri: str) -> list[str]:
    """Return the resource names of indexed documents whose content URI is ``uri``."""
    names = []
    for document in document_client.list_documents(parent=branch):
        if document.content and document.content.uri == uri:
            names.append(document.name)
    return names


def remove_from_index(
    bucket_name: str,
    object_name: str,
    *,
    storage_client,
    document_client,
    branch: str = BRANCH,
) -> int:
    """Delete the indexed document(s) for an object that no longer exists.

    Returns how many documents were deleted. When the object still exists the
    delete event belonged to an overwritten generation, so the index entry is
    kept: the finalize event for the new generation re-imports the same URI.
    """
    if not _is_supported(object_name):
        logging.info("Ignoring unsupported deleted object: gs://%s/%s", bucket_name, object_name)
        return 0
    if storage_client.bucket(bucket_name).get_blob(object_name) is not None:
        logging.info("gs://%s/%s still exists (overwrite); keeping its index entry", bucket_name, object_name)
        return 0

    uri = f"gs://{bucket_name}/{object_name}"
    names = find_document_names(document_client, branch, uri)
    for name in names:
        document_client.delete_document(name=name)
        logging.info("Deleted %s from the index for %s", name, uri)
    if not names:
        logging.info("No indexed document found for %s", uri)
    return len(names)


@functions_framework.cloud_event
def delete_gcs_object(cloud_event) -> None:
    data = cloud_event.data
    bucket = data.get("bucket", "")
    object_name = data.get("name", "")
    if not bucket or not object_name:
        raise ValueError("Cloud Storage delete event is missing bucket or object name")

    document_client = _document_client()
    try:
        remove_from_index(
            bucket, object_name, storage_client=storage.Client(), document_client=document_client
        )
    finally:
        document_client.transport.close()


@functions_framework.http
def sync_github_repositories(request):
    """Re-import every GitHub repository staged in the bucket (called by Cloud Scheduler)."""
    # github_import.py is copied from backend/ into this folder by the deploy script.
    from github_import import sync_all_repositories

    if not GCS_BUCKET:
        return (json.dumps({"detail": "GCS_BUCKET is not configured."}), 503, {"Content-Type": "application/json"})

    outcome = sync_all_repositories(GCS_BUCKET)
    body = {
        "synced": [
            {
                "repository": result.repository,
                "commit_sha": result.commit_sha,
                "staged_documents": result.staged_documents,
                "unchanged_documents": result.unchanged_documents,
                "removed_documents": result.removed_documents,
            }
            for result in outcome.results
        ],
        "failed": [{"repository": f.repository, "detail": f.detail} for f in outcome.failures],
    }
    logging.info("GitHub sync finished: %s", json.dumps(body))
    return (json.dumps(body), 200, {"Content-Type": "application/json"})
