"""Stage the documents of a SharePoint Online site in the bucket via Microsoft Graph.

Same pattern as :mod:`backend.github_import`: the site's document libraries are
listed through Graph with an Entra ID application (client credentials), supported
files are downloaded, and only new or changed files are written to the bucket
under ``sharepoint/raw/<host>/<site>/<library>/<path>``. Files removed from the
site are deleted from the bucket; the bucket's finalize/delete triggers keep the
search index in step.

Configuration (environment): ``SHAREPOINT_TENANT_ID``, ``SHAREPOINT_CLIENT_ID``,
``SHAREPOINT_CLIENT_SECRET``. The application needs the *application* permission
``Sites.Read.All`` (or ``Sites.Selected`` granted on the specific sites).
"""

from __future__ import annotations

from dataclasses import dataclass
import logging
import os
from pathlib import PurePosixPath
import re
from typing import Any
from urllib.parse import quote, urlsplit

import requests
from google.cloud import storage

from backend.github_import import ExistingObject, plan_changes

logger = logging.getLogger(__name__)

GRAPH_ROOT = "https://graph.microsoft.com/v1.0"
LOGIN_ROOT = "https://login.microsoftonline.com"
SHAREPOINT_PREFIX = "sharepoint/raw/"
REQUEST_TIMEOUT = (5, 60)
MAX_FILES = 200
MAX_FILE_BYTES = 15 * 1024 * 1024
MAX_TOTAL_BYTES = 100 * 1024 * 1024
SUPPORTED_SUFFIXES = {".pdf", ".docx", ".pptx", ".xlsx", ".xlsm", ".txt", ".md", ".html", ".json"}
CONTENT_TYPES = {
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".xlsm": "application/vnd.ms-excel.sheet.macroenabled.12",
    ".txt": "text/plain; charset=utf-8",
    ".md": "text/markdown; charset=utf-8",
    ".html": "text/html; charset=utf-8",
    ".json": "application/json",
}
_SEGMENT = re.compile(r"^[A-Za-z0-9._ -]{1,128}$")


class SharePointImportError(Exception):
    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


@dataclass(frozen=True)
class SharePointSettings:
    tenant_id: str
    client_id: str
    client_secret: str

    @property
    def configured(self) -> bool:
        return bool(self.tenant_id and self.client_id and self.client_secret)

    @classmethod
    def from_env(cls) -> "SharePointSettings":
        return cls(
            tenant_id=os.getenv("SHAREPOINT_TENANT_ID", "").strip(),
            client_id=os.getenv("SHAREPOINT_CLIENT_ID", "").strip(),
            client_secret=os.getenv("SHAREPOINT_CLIENT_SECRET", "").strip(),
        )


@dataclass(frozen=True)
class SiteImportResult:
    site: str
    staged_documents: int
    unchanged_documents: int
    removed_documents: int
    skipped_files: int
    total_bytes: int
    gcs_uris: tuple[str, ...]


@dataclass(frozen=True)
class _RemoteFile:
    library: str
    path: str  # path inside the library, "/" separated
    size: int
    download_url: str
    web_url: str
    modified: str
    etag: str


def parse_site_url(site_url: str) -> tuple[str, str]:
    """Accept ``https://<tenant>.sharepoint.com/sites/<name>`` (or ``/teams/<name>``)."""
    try:
        parsed = urlsplit(site_url.strip())
    except (AttributeError, ValueError):
        raise SharePointImportError(400, "Enter a valid SharePoint site URL.") from None
    host = (parsed.hostname or "").lower()
    if parsed.scheme.lower() != "https" or not host.endswith(".sharepoint.com") or parsed.username or parsed.port or parsed.query or parsed.fragment:
        raise SharePointImportError(400, "Only HTTPS URLs of a *.sharepoint.com site are supported.")
    parts = [p for p in parsed.path.split("/") if p]
    if len(parts) < 2 or parts[0].lower() not in {"sites", "teams"} or not _SEGMENT.fullmatch(parts[1]):
        raise SharePointImportError(400, "Use a site URL in the form https://<tenant>.sharepoint.com/sites/<site>.")
    return host, f"{parts[0].lower()}/{parts[1]}"


def _new_session() -> requests.Session:
    session = requests.Session()
    session.headers.update({"Accept": "application/json", "User-Agent": "project-knowledge-ai-sharepoint-importer"})
    return session


def _get_token(session: requests.Session, settings: SharePointSettings) -> str:
    url = f"{LOGIN_ROOT}/{quote(settings.tenant_id, safe='')}/oauth2/v2.0/token"
    try:
        response = session.post(
            url,
            data={
                "client_id": settings.client_id,
                "client_secret": settings.client_secret,
                "grant_type": "client_credentials",
                "scope": "https://graph.microsoft.com/.default",
            },
            timeout=REQUEST_TIMEOUT,
        )
    except requests.RequestException as exc:
        raise SharePointImportError(502, "Could not reach the Microsoft identity platform.") from exc
    if response.status_code != 200:
        logger.warning("Entra ID token request failed with HTTP %s", response.status_code)
        raise SharePointImportError(502, "Entra ID rejected the application credentials; check SHAREPOINT_* settings.")
    token = response.json().get("access_token")
    if not token:
        raise SharePointImportError(502, "Entra ID did not return an access token.")
    return token


def _graph_get(session: requests.Session, url: str, token: str) -> dict[str, Any]:
    try:
        response = session.get(url, headers={"Authorization": f"Bearer {token}"}, timeout=REQUEST_TIMEOUT)
    except requests.Timeout as exc:
        raise SharePointImportError(504, "Microsoft Graph did not respond in time.") from exc
    except requests.RequestException as exc:
        raise SharePointImportError(502, "Could not connect to Microsoft Graph.") from exc
    if response.status_code in {401, 403}:
        raise SharePointImportError(403, "The application is not allowed to read this site (grant Sites.Read.All or Sites.Selected).")
    if response.status_code == 404:
        raise SharePointImportError(404, "The SharePoint site or library was not found.")
    if response.status_code == 429:
        raise SharePointImportError(429, "Microsoft Graph is throttling requests; try again later.")
    if response.status_code >= 400:
        raise SharePointImportError(502, f"Microsoft Graph returned HTTP {response.status_code}.")
    payload = response.json()
    if not isinstance(payload, dict):
        raise SharePointImportError(502, "Microsoft Graph returned an unexpected response.")
    return payload


def _paged(session: requests.Session, url: str, token: str) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    while url:
        payload = _graph_get(session, url, token)
        items.extend(item for item in payload.get("value", []) if isinstance(item, dict))
        url = payload.get("@odata.nextLink", "")
    return items


def _list_files(session: requests.Session, token: str, host: str, site_path: str) -> list[_RemoteFile]:
    site = _graph_get(session, f"{GRAPH_ROOT}/sites/{host}:/{site_path}", token)
    site_id = site.get("id")
    if not isinstance(site_id, str) or not site_id:
        raise SharePointImportError(502, "Microsoft Graph did not return the site id.")

    files: list[_RemoteFile] = []
    for drive in _paged(session, f"{GRAPH_ROOT}/sites/{quote(site_id, safe='')}/drives", token):
        drive_id, library = drive.get("id"), drive.get("name") or "Documents"
        if not isinstance(drive_id, str):
            continue
        pending = [(f"{GRAPH_ROOT}/drives/{quote(drive_id, safe='')}/root/children", "")]
        while pending:
            url, folder = pending.pop()
            for item in _paged(session, url, token):
                name = item.get("name")
                if not isinstance(name, str) or not _SEGMENT.fullmatch(name):
                    continue
                path = f"{folder}/{name}" if folder else name
                if "folder" in item:
                    pending.append((f"{GRAPH_ROOT}/drives/{quote(drive_id, safe='')}/items/{quote(item['id'], safe='')}/children", path))
                    continue
                if PurePosixPath(name).suffix.lower() not in SUPPORTED_SUFFIXES:
                    continue
                download = item.get("@microsoft.graph.downloadUrl")
                if not isinstance(download, str) or not download.startswith("https://"):
                    continue
                files.append(
                    _RemoteFile(
                        library=library,
                        path=path,
                        size=int(item.get("size") or 0),
                        download_url=download,
                        web_url=str(item.get("webUrl") or ""),
                        modified=str(item.get("lastModifiedDateTime") or ""),
                        etag=str(item.get("eTag") or ""),
                    )
                )
                if len(files) > MAX_FILES:
                    raise SharePointImportError(413, f"The site has more than the {MAX_FILES}-file limit for one import.")
    return sorted(files, key=lambda f: (f.library, f.path))


def _download(session: requests.Session, remote: _RemoteFile) -> bytes:
    try:
        response = session.get(remote.download_url, timeout=REQUEST_TIMEOUT, stream=True)
    except requests.RequestException as exc:
        raise SharePointImportError(502, f"Could not download {remote.path} from SharePoint.") from exc
    chunks, size = [], 0
    try:
        if response.status_code >= 400:
            raise SharePointImportError(502, f"SharePoint returned HTTP {response.status_code} for {remote.path}.")
        for chunk in response.iter_content(chunk_size=256 * 1024):
            size += len(chunk)
            if size > MAX_FILE_BYTES:
                raise SharePointImportError(413, f"{remote.path} exceeds the 15 MB per-file limit.")
            chunks.append(chunk)
    finally:
        response.close()
    return b"".join(chunks)


def object_name(host: str, site_path: str, remote: _RemoteFile) -> str:
    return f"{SHAREPOINT_PREFIX}{host}/{site_path}/{remote.library}/{remote.path}"


def import_site(
    site_url: str,
    bucket_name: str,
    settings: SharePointSettings,
    *,
    session: requests.Session | None = None,
    storage_client: storage.Client | None = None,
) -> SiteImportResult:
    if not settings.configured:
        raise SharePointImportError(503, "SharePoint is not configured: set SHAREPOINT_TENANT_ID, SHAREPOINT_CLIENT_ID and SHAREPOINT_CLIENT_SECRET.")
    host, site_path = parse_site_url(site_url)
    owns_session, http = session is None, session or _new_session()
    owns_client, gcs = storage_client is None, storage_client
    try:
        token = _get_token(http, settings)
        remote_files = _list_files(http, token, host, site_path)
        skipped = [f for f in remote_files if f.size > MAX_FILE_BYTES]
        remote_files = [f for f in remote_files if f.size <= MAX_FILE_BYTES]
        if sum(f.size for f in remote_files) > MAX_TOTAL_BYTES:
            raise SharePointImportError(413, "The site's supported documents exceed the 100 MB total limit for one import.")

        if gcs is None:
            gcs = storage.Client()
        bucket = gcs.bucket(bucket_name)
        prefix = f"{SHAREPOINT_PREFIX}{host}/{site_path}/"
        existing = {
            blob.name: ExistingObject(blob.name, blob.md5_hash, (blob.metadata or {}).get("ingestion_status"))
            for blob in gcs.list_blobs(bucket_name, prefix=prefix)
        }
        # Download every candidate so content hashes decide what actually changed.
        contents = {object_name(host, site_path, f): (f, _download(http, f)) for f in remote_files}
        plan = plan_changes({name: data for name, (_, data) in contents.items()}, existing)

        staged: list[str] = []
        for name in plan.upload:
            remote, data = contents[name]
            blob = bucket.blob(name)
            blob.metadata = {
                "source_type": "sharepoint",
                "site": f"{host}/{site_path}",
                "library": remote.library,
                "sharepoint_path": remote.path,
                "sharepoint_url": remote.web_url,
                "sharepoint_modified": remote.modified,
                "original_filename": PurePosixPath(remote.path).name,
                "ingestion_status": "pending",
                "ingestion_message": "Waiting for the Cloud Storage ingestion event.",
            }
            blob.upload_from_string(data, content_type=CONTENT_TYPES[PurePosixPath(remote.path).suffix.lower()])
            staged.append(f"gs://{bucket_name}/{name}")
        for name in plan.delete:
            bucket.blob(name).delete()
        return SiteImportResult(
            site=f"{host}/{site_path}",
            staged_documents=len(staged),
            unchanged_documents=len(plan.unchanged),
            removed_documents=len(plan.delete),
            skipped_files=len(skipped),
            total_bytes=sum(len(data) for _, data in contents.values()),
            gcs_uris=tuple(staged),
        )
    finally:
        if owns_session:
            http.close()
        if owns_client and gcs is not None:
            gcs.close()
