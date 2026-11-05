from dataclasses import dataclass

import pytest

from backend import github_import as gi


# --- URL parsing -----------------------------------------------------------

@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/AngelUch27/project-knowledge-ai",
        "https://github.com/AngelUch27/project-knowledge-ai/",
        "https://github.com/AngelUch27/project-knowledge-ai.git",
        "  https://GitHub.com/AngelUch27/project-knowledge-ai  ",
    ],
)
def test_parse_accepts_bare_repository_urls(url):
    assert gi._parse_repository_url(url) == ("AngelUch27", "project-knowledge-ai")


@pytest.mark.parametrize(
    "url",
    [
        "http://github.com/owner/repo",
        "https://gitlab.com/owner/repo",
        "https://github.com/owner",
        "https://github.com/owner/repo/tree/main",
        "https://github.com/owner/repo?x=1",
        "https://user:pw@github.com/owner/repo",
        "https://github.com/owner/..",
    ],
)
def test_parse_rejects_anything_but_public_github_repository_urls(url):
    with pytest.raises(gi.GitHubImportError) as info:
        gi._parse_repository_url(url)
    assert info.value.status_code == 400


# --- change planning for re-sync -------------------------------------------

def existing(name, content=None, status="imported", md5=None):
    return gi.ExistingObject(
        name=name,
        md5_base64=md5 if md5 is not None else (gi.content_md5_base64(content) if content is not None else None),
        ingestion_status=status,
    )


def test_plan_uploads_new_files_and_deletes_removed_ones():
    desired = {"github/raw/o/r/README.md": b"new", "github/raw/o/r/docs/a.md": b"a"}
    current = {"github/raw/o/r/docs/old.md": existing("github/raw/o/r/docs/old.md", b"old")}
    plan = gi.plan_changes(desired, current)
    assert plan.upload == ("github/raw/o/r/README.md", "github/raw/o/r/docs/a.md")
    assert plan.unchanged == ()
    assert plan.delete == ("github/raw/o/r/docs/old.md",)


def test_plan_leaves_identical_imported_files_alone():
    desired = {"github/raw/o/r/README.md": b"same"}
    current = {"github/raw/o/r/README.md": existing("github/raw/o/r/README.md", b"same")}
    plan = gi.plan_changes(desired, current)
    assert plan.unchanged == ("github/raw/o/r/README.md",)
    assert plan.upload == () and plan.delete == ()


def test_plan_reuploads_changed_content():
    desired = {"github/raw/o/r/README.md": b"v2"}
    current = {"github/raw/o/r/README.md": existing("github/raw/o/r/README.md", b"v1")}
    assert gi.plan_changes(desired, current).upload == ("github/raw/o/r/README.md",)


@pytest.mark.parametrize("status", ["pending", "processing", "failed", None])
def test_plan_reuploads_identical_files_whose_import_never_succeeded(status):
    desired = {"github/raw/o/r/README.md": b"same"}
    current = {"github/raw/o/r/README.md": existing("github/raw/o/r/README.md", b"same", status=status)}
    assert gi.plan_changes(desired, current).upload == ("github/raw/o/r/README.md",)


def test_content_md5_matches_cloud_storage_base64_form():
    # echo -n "hello" | openssl md5 -binary | base64
    assert gi.content_md5_base64(b"hello") == "XUFAKrxLKna5cZ2REBfFkg=="


# --- staged repository discovery -------------------------------------------

@dataclass
class FakeBlob:
    name: str
    metadata: dict | None = None
    md5_hash: str | None = None


class FakeStorageClient:
    def __init__(self, blobs):
        self._blobs = blobs
        self.closed = False

    def list_blobs(self, bucket_name, prefix=""):
        return [b for b in self._blobs if b.name.startswith(prefix)]

    def close(self):
        self.closed = True


def test_list_staged_repositories_dedupes_and_prefers_canonical_metadata():
    client = FakeStorageClient(
        [
            FakeBlob("github/raw/angeluch27/project-knowledge-ai/README.md", {"repository": "AngelUch27/project-knowledge-ai"}),
            FakeBlob("github/raw/angeluch27/project-knowledge-ai/docs/x.md", {"repository": "AngelUch27/project-knowledge-ai"}),
            FakeBlob("github/raw/buildkite/agent/README.md"),
            FakeBlob("documents/raw/other.pdf"),
            FakeBlob("github/raw/incomplete"),
        ]
    )
    assert gi.list_staged_repositories("bucket", storage_client=client) == [
        "AngelUch27/project-knowledge-ai",
        "buildkite/agent",
    ]
    assert client.closed is False  # injected clients are not closed


def test_sync_all_isolates_failures_per_repository(monkeypatch):
    client = FakeStorageClient([FakeBlob("github/raw/a/ok/README.md"), FakeBlob("github/raw/b/bad/README.md")])
    calls = []

    def fake_import(url, bucket_name, *, session, storage_client):
        calls.append(url)
        if "bad" in url:
            raise gi.GitHubImportError(404, "The repository is unavailable or is not public.")
        return gi.RepositoryImportResult(
            repository="a/ok", commit_sha="abc", staged_documents=1, total_bytes=1, missing_paths=(), gcs_uris=()
        )

    monkeypatch.setattr(gi, "import_repository", fake_import)
    outcome = gi.sync_all_repositories("bucket", session=object(), storage_client=client)
    assert calls == ["https://github.com/a/ok", "https://github.com/b/bad"]
    assert [r.repository for r in outcome.results] == ["a/ok"]
    assert outcome.failures == (gi.SyncFailure("b/bad", "The repository is unavailable or is not public."),)
