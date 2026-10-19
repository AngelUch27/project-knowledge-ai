"""CloudEvent function: import finalized GCS documents into Agent Search."""

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
COLLECTION = "default_collection"
SUPPORTED_SUFFIXES = {".pdf", ".docx", ".pptx", ".xlsx", ".xlsm", ".txt", ".md", ".html", ".json"}


def _set_metadata(bucket_name: str, object_name: str, **values: str) -> None:
    blob = storage.Client().bucket(bucket_name).blob(object_name)
    blob.reload()
    metadata = dict(blob.metadata or {})
    metadata.update(values)
    blob.metadata = metadata
    blob.patch()


@functions_framework.cloud_event
def import_gcs_object(cloud_event) -> None:
    data = cloud_event.data
    bucket = data.get("bucket", "")
    object_name = data.get("name", "")
    generation = str(data.get("generation", ""))
    if not bucket or not object_name:
        raise ValueError("Cloud Storage finalize event is missing bucket or object name")

    if PurePosixPath(object_name).suffix.lower() not in SUPPORTED_SUFFIXES:
        logging.info("Ignoring unsupported object: gs://%s/%s", bucket, object_name)
        return

    logging.info("Starting Agent Search import for gs://%s/%s", bucket, object_name)
    _set_metadata(
        bucket,
        object_name,
        ingestion_status="processing",
        ingestion_message="Importación incremental iniciada.",
        ingestion_generation=generation,
    )

    endpoint = f"{LOCATION}-discoveryengine.googleapis.com" if LOCATION != "global" else None
    client = discoveryengine.DocumentServiceClient(
        client_options=ClientOptions(api_endpoint=endpoint) if endpoint else None
    )
    parent = (
        f"projects/{PROJECT_ID}/locations/{LOCATION}/collections/{COLLECTION}"
        f"/dataStores/{DATA_STORE_ID}/branches/default_branch"
    )
    request = discoveryengine.ImportDocumentsRequest(
        parent=parent,
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
            ingestion_message="Importación aceptada por Agent Search; indexación asíncrona.",
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
                ingestion_message=f"Falló la importación; se reintentará. ({type(exc).__name__})",
                ingestion_generation=generation,
            )
        except Exception:
            logging.exception("Could not write import failure status to the source object")
        raise
    finally:
        client.transport.close()
