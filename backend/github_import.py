"""Stage public GitHub repository documentation in the existing GCS bucket.

The FastAPI layer can call :func:`import_repository` with the submitted URL and
the configured bucket name. This module intentionally does not define routes or
change the existing document-upload flow. Once uploaded, each Markdown object
uses the existing bucket-finalize event path into Agent Search.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath
import re
from typing import Any
from urllib.parse import quote, unquote, urlsplit

import requests
from google.cloud import storage


GITHUB_API_ROOT = "https://api.github.com"
GITHUB_RAW_ROOT = "https://raw.githubusercontent.com"
REQUEST_TIMEOUT = (5, 30)
MAX_FILES = 100
MAX_FILE_BYTES = 1 * 1024 * 1024
MAX_TOTAL_BYTES = 10 * 1024 * 1024
_OWNER_PATTERN = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?$")
_REPOSITORY_PATTERN = re.compile(r"^[A-Za-z0-9_.-]{1,100}$")


class GitHubImportError(Exception):
    """An import failure with an HTTP status suitable for the API layer."""

    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


@dataclass(frozen=True)
class RepositoryImportResult:
    """Summary of documents staged; this does not imply indexing has finished."""

    repository: str
    commit_sha: str
    staged_documents: int
    total_bytes: int
    missing_paths: tuple[str, ...]
    gcs_uris: tuple[str, ...]


@dataclass(frozen=True)
class _Repository:
    owner: str
    name: str
    canonical_name: str
    default_branch: str


@dataclass(frozen=True)
class _SelectedFile:
    path: str
    content: bytes


def _parse_repository_url(repository_url: str) -> tuple[str, str]:
    """Accept only a bare HTTPS github.com owner/repository URL.

    Keeping the accepted shape narrow means later HTTP requests can be built
    against fixed GitHub hosts, rather than making requests to a user-supplied
    host or following arbitrary URL paths.
    """
    try:
        parsed = urlsplit(repository_url.strip())
        port = parsed.port
    except (AttributeError, ValueError):
        raise GitHubImportError(400, "Enter a valid public GitHub repository URL.") from None

    if (
        parsed.scheme.lower() != "https"
        or (parsed.hostname or "").lower() != "github.com"
        or parsed.username is not None
        or parsed.password is not None
        or port is not None
        or parsed.query
        or parsed.fragment
    ):
        raise GitHubImportError(400, "Only HTTPS URLs for public repositories on github.com are supported.")

    # Allow one optional trailing slash and the conventional .git suffix, but
    # reject nested pages such as /tree/main or /issues/1.
    path = unquote(parsed.path).strip("/")
    components = path.split("/") if path else []
    if len(components) != 2:
        raise GitHubImportError(400, "Use a repository URL in the form https://github.com/owner/repository.")

    owner, repository = components
    if repository.lower().endswith(".git"):
        repository = repository[:-4]
    if (
        not _OWNER_PATTERN.fullmatch(owner)
        or not repository
        or repository in {".", ".."}
        or not _REPOSITORY_PATTERN.fullmatch(repository)
    ):
        raise GitHubImportError(400, "The GitHub owner or repository name is invalid.")

    return owner, repository


def _new_session() -> requests.Session:
    """Create an anonymous session with GitHub's recommended API headers."""
    session = requests.Session()
    session.headers.update(
        {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "project-knowledge-ai-public-doc-importer",
        }
    )
    return session


def _raise_for_github_response(response: requests.Response, *, repository_lookup: bool = False) -> None:
    """Translate GitHub statuses into useful, non-sensitive API errors."""
    if response.status_code in {403, 429}:
        remaining = response.headers.get("X-RateLimit-Remaining")
        if (
            response.status_code == 429
            or remaining == "0"
            or response.headers.get("Retry-After")
            or "rate limit" in response.text.lower()
        ):
            raise GitHubImportError(429, "GitHub's unauthenticated API rate limit was reached. Try again later.")

    if repository_lookup and response.status_code in {403, 404}:
        # GitHub can conceal private repositories as 404. Do not reveal whether
        # a similarly named private repository exists.
        raise GitHubImportError(404, "The repository is unavailable or is not public.")
    if response.status_code == 404:
        raise GitHubImportError(404, "The public repository or requested documentation was not found.")
    if response.status_code >= 400:
        raise GitHubImportError(502, f"GitHub returned an unexpected HTTP {response.status_code} response.")
    if 300 <= response.status_code < 400:
        raise GitHubImportError(502, "GitHub redirected the request unexpectedly; import was stopped.")


def _get_json(
    session: requests.Session,
    url: str,
    *,
    repository_lookup: bool = False,
) -> dict[str, Any]:
    """Fetch one GitHub API JSON resource without following redirects."""
    try:
        response = session.get(url, timeout=REQUEST_TIMEOUT, allow_redirects=False)
    except requests.Timeout as exc:
        raise GitHubImportError(504, "GitHub did not respond before the request timed out.") from exc
    except requests.RequestException as exc:
        raise GitHubImportError(502, "Could not connect to the GitHub API.") from exc

    try:
        _raise_for_github_response(response, repository_lookup=repository_lookup)
        try:
            payload = response.json()
        except ValueError as exc:
            raise GitHubImportError(502, "GitHub returned an invalid JSON response.") from exc
    finally:
        response.close()
    if not isinstance(payload, dict):
        raise GitHubImportError(502, "GitHub returned an unexpected API response.")
    return payload


def _resolve_repository(session: requests.Session, owner: str, name: str) -> tuple[_Repository, str, str]:
    """Resolve the public repository, default branch, and immutable commit.

    The commit response supplies both the commit SHA and its tree SHA. The
    latter is required by GitHub's Git Trees API; using both keeps enumeration
    and subsequent raw-file downloads pinned to the same repository snapshot.
    """
    api_repository = f"{GITHUB_API_ROOT}/repos/{quote(owner, safe='')}/{quote(name, safe='')}"
    details = _get_json(session, api_repository, repository_lookup=True)
    if details.get("private") is True:
        raise GitHubImportError(404, "The repository is unavailable or is not public.")

    canonical_name = details.get("full_name")
    default_branch = details.get("default_branch")
    if not isinstance(canonical_name, str) or not isinstance(default_branch, str) or not default_branch:
        raise GitHubImportError(502, "GitHub did not provide the repository's canonical name and default branch.")
    canonical_parts = canonical_name.split("/")
    if len(canonical_parts) != 2 or any(not part for part in canonical_parts):
        raise GitHubImportError(502, "GitHub returned an invalid canonical repository name.")

    repository = _Repository(
        owner=canonical_parts[0],
        name=canonical_parts[1],
        canonical_name=canonical_name,
        default_branch=default_branch,
    )
    commit_url = (
        f"{GITHUB_API_ROOT}/repos/{quote(repository.owner, safe='')}/"
        f"{quote(repository.name, safe='')}/commits/{quote(default_branch, safe='')}"
    )
    commit = _get_json(session, commit_url, repository_lookup=True)
    commit_sha = commit.get("sha")
    tree_sha = (commit.get("commit") or {}).get("tree", {}).get("sha")
    if not isinstance(commit_sha, str) or not commit_sha or not isinstance(tree_sha, str) or not tree_sha:
        raise GitHubImportError(502, "GitHub did not provide a valid commit and tree SHA.")

    tree_url = (
        f"{GITHUB_API_ROOT}/repos/{quote(repository.owner, safe='')}/"
        f"{quote(repository.name, safe='')}/git/trees/{quote(tree_sha, safe='')}?recursive=1"
    )
    return repository, commit_sha, tree_url


def _select_document_paths(tree_payload: dict[str, Any]) -> tuple[list[str], tuple[str, ...]]:
    """Select only root README.md and Markdown files recursively under docs/."""
    if tree_payload.get("truncated") is True:
        raise GitHubImportError(422, "GitHub truncated the repository tree; refusing to import an incomplete document set.")

    entries = tree_payload.get("tree")
    if not isinstance(entries, list):
        raise GitHubImportError(502, "GitHub returned an invalid repository tree.")

    selected: list[str] = []
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("type") != "blob":
            continue
        path = entry.get("path")
        if not isinstance(path, str):
            continue

        # Git symlinks are represented as blobs; exclude them so a documentation
        # path cannot unexpectedly point outside the repository snapshot.
        if entry.get("mode") == "120000":
            continue

        is_root_readme = path == "README.md"
        is_docs_markdown = path.startswith("docs/") and PurePosixPath(path).suffix.lower() == ".md"
        path_parts = PurePosixPath(path).parts
        safe_repository_path = (
            not path.startswith("/")
            and "\\" not in path
            and all(part not in {"", ".", ".."} for part in path_parts)
        )
        if safe_repository_path and (is_root_readme or is_docs_markdown):
            selected.append(path)

    selected.sort()
    if len(selected) > MAX_FILES:
        raise GitHubImportError(413, f"The repository has more than the {MAX_FILES}-file documentation limit.")
    if not selected:
        raise GitHubImportError(422, "No root README.md or Markdown files under docs/ were found.")

    missing_paths: list[str] = []
    if "README.md" not in selected:
        missing_paths.append("README.md")
    if not any(path.startswith("docs/") and PurePosixPath(path).suffix.lower() == ".md" for path in selected):
        missing_paths.append("docs/**/*.md")
    return selected, tuple(missing_paths)


def _download_file(
    session: requests.Session,
    repository: _Repository,
    commit_sha: str,
    path: str,
) -> bytes:
    """Download a single selected file from the immutable commit snapshot."""
    encoded_path = quote(path, safe="/")
    url = (
        f"{GITHUB_RAW_ROOT}/{quote(repository.owner, safe='')}/"
        f"{quote(repository.name, safe='')}/{quote(commit_sha, safe='')}/{encoded_path}"
    )
    try:
        response = session.get(
            url,
            timeout=REQUEST_TIMEOUT,
            allow_redirects=False,
            stream=True,
        )
    except requests.Timeout as exc:
        raise GitHubImportError(504, f"GitHub timed out while downloading {path}.") from exc
    except requests.RequestException as exc:
        raise GitHubImportError(502, f"Could not download {path} from GitHub.") from exc

    content_parts: list[bytes] = []
    content_length = 0
    try:
        _raise_for_github_response(response)
        for chunk in response.iter_content(chunk_size=64 * 1024):
            if not chunk:
                continue
            content_length += len(chunk)
            if content_length > MAX_FILE_BYTES:
                raise GitHubImportError(413, f"Documentation file {path} exceeds the 1 MB per-file limit.")
            content_parts.append(chunk)
    finally:
        response.close()
    content = b"".join(content_parts)
    try:
        content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise GitHubImportError(422, f"Documentation file {path} is not valid UTF-8 Markdown.") from exc
    return content


def _gcs_object_name(repository: _Repository, path: str) -> str:
    """Build a stable key; repeated imports replace the same repository file."""
    # GitHub repository names are case-insensitive. Normalize only the owner
    # and repository components while preserving the actual document path.
    return f"github/raw/{repository.owner.lower()}/{repository.name.lower()}/{path}"


def import_repository(
    repository_url: str,
    bucket_name: str,
    *,
    session: requests.Session | None = None,
    storage_client: storage.Client | None = None,
) -> RepositoryImportResult:
    """Fetch and stage public repository documentation into an existing bucket.

    Only the repository root's exact ``README.md`` and Markdown files anywhere
    below ``docs/`` are included. Content is fully fetched and checked against
    all limits before the first GCS write, preventing a known oversized
    repository from being only partly staged. GCS writes are intentionally
    idempotent by object name; the existing finalize-triggered function then
    submits each new object to Agent Search asynchronously.

    ``session`` and ``storage_client`` are injectable for isolated tests. When
    omitted, this function creates and closes its own clients. A failure during
    the GCS write phase can still leave already-written objects in place; the
    raised error reports the number staged so a caller can present that partial
    result and safely retry using the same stable object names.
    """
    if not isinstance(repository_url, str) or not repository_url.strip():
        raise GitHubImportError(400, "A GitHub repository URL is required.")
    if not isinstance(bucket_name, str) or not bucket_name.strip():
        raise GitHubImportError(503, "The GCS bucket is not configured.")

    owner, name = _parse_repository_url(repository_url)
    owns_session = session is None
    http = session or _new_session()
    owns_storage_client = storage_client is None
    gcs = storage_client
    staged_uris: list[str] = []

    try:
        repository, commit_sha, tree_url = _resolve_repository(http, owner, name)
        tree_payload = _get_json(http, tree_url)
        paths, missing_paths = _select_document_paths(tree_payload)

        # Download everything before writing anything so count, per-file, and
        # aggregate limits are validated against a complete selected snapshot.
        files: list[_SelectedFile] = []
        total_bytes = 0
        for path in paths:
            content = _download_file(http, repository, commit_sha, path)
            total_bytes += len(content)
            if total_bytes > MAX_TOTAL_BYTES:
                raise GitHubImportError(413, "The selected repository documentation exceeds the 10 MB total limit.")
            files.append(_SelectedFile(path=path, content=content))

        try:
            if gcs is None:
                gcs = storage.Client()
            bucket = gcs.bucket(bucket_name)
            for document in files:
                object_name = _gcs_object_name(repository, document.path)
                blob = bucket.blob(object_name)
                blob.metadata = {
                    "source_type": "github",
                    "repository": repository.canonical_name,
                    "github_commit_sha": commit_sha,
                    "github_path": document.path,
                    "github_url": (
                        f"https://github.com/{repository.owner}/{repository.name}/"
                        f"blob/{commit_sha}/{quote(document.path, safe='/')}"
                    ),
                    "original_filename": PurePosixPath(document.path).name,
                    "ingestion_status": "pending",
                    "ingestion_message": "Waiting for the Cloud Storage ingestion event.",
                }
                # A stable object name makes repeated imports update the same
                # GCS object; the default upload behavior creates a new object
                # generation and causes the existing finalize trigger to run.
                blob.upload_from_string(document.content, content_type="text/markdown; charset=utf-8")
                staged_uris.append(f"gs://{bucket_name}/{object_name}")
        except GitHubImportError:
            raise
        except Exception as exc:
            raise GitHubImportError(
                502,
                f"Could not stage repository documentation in Cloud Storage; {len(staged_uris)} file(s) were staged before the failure.",
            ) from exc

        return RepositoryImportResult(
            repository=repository.canonical_name,
            commit_sha=commit_sha,
            staged_documents=len(files),
            total_bytes=total_bytes,
            missing_paths=missing_paths,
            gcs_uris=tuple(staged_uris),
        )
    finally:
        if owns_session:
            http.close()
        if owns_storage_client and gcs is not None:
            gcs.close()