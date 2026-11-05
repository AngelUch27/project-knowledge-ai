from dataclasses import dataclass, field

import pytest

from backend import sharepoint_import as sp
from backend.github_import import content_md5_base64

SETTINGS = sp.SharePointSettings(tenant_id="tenant", client_id="client", client_secret="secret")


# --- URL parsing -----------------------------------------------------------------

@pytest.mark.parametrize("url,expected", [
    ("https://contoso.sharepoint.com/sites/Ops", ("contoso.sharepoint.com", "sites/Ops")),
    ("https://contoso.sharepoint.com/sites/Ops/Shared%20Documents/Forms/AllItems.aspx", ("contoso.sharepoint.com", "sites/Ops")),
    ("https://Contoso.SharePoint.com/teams/plant-a/", ("contoso.sharepoint.com", "teams/plant-a")),
])
def test_parse_site_url_accepts_sharepoint_sites(url, expected):
    assert sp.parse_site_url(url) == expected


@pytest.mark.parametrize("url", [
    "http://contoso.sharepoint.com/sites/Ops",
    "https://contoso.example.com/sites/Ops",
    "https://contoso.sharepoint.com/",
    "https://contoso.sharepoint.com/personal/someone",
    "https://user:pw@contoso.sharepoint.com/sites/Ops",
])
def test_parse_site_url_rejects_everything_else(url):
    with pytest.raises(sp.SharePointImportError) as info:
        sp.parse_site_url(url)
    assert info.value.status_code == 400


def test_import_requires_configuration():
    with pytest.raises(sp.SharePointImportError) as info:
        sp.import_site("https://contoso.sharepoint.com/sites/Ops", "b", sp.SharePointSettings("", "", ""))
    assert info.value.status_code == 503


# --- a fake Graph + bucket ---------------------------------------------------------

class FakeResponse:
    def __init__(self, status, payload=None, body=b""):
        self.status_code, self._payload, self._body = status, payload, body

    def json(self):
        return self._payload

    def iter_content(self, chunk_size):
        yield self._body

    def close(self):
        pass


class FakeGraph:
    """Answers the token request, the site/drive/children listing and raw downloads."""

    def __init__(self):
        self.requests = []
        self.files = {"Procedures/SOP.pdf": b"%PDF-1.4 sop", "Readme.md": b"# readme", "image.png": b"\x89PNG"}

    def post(self, url, data, timeout):
        self.requests.append(("POST", url))
        assert data["grant_type"] == "client_credentials" and data["client_secret"] == "secret"
        return FakeResponse(200, {"access_token": "tok"})

    def get(self, url, headers=None, timeout=None, stream=False):
        self.requests.append(("GET", url))
        if url.startswith("https://contoso.sharepoint.com/dl/"):
            return FakeResponse(200, body=self.files[url[len("https://contoso.sharepoint.com/dl/"):]])
        if url.startswith("https://evil.example/"):
            raise AssertionError("a non-SharePoint download URL must never be fetched")
        assert headers["Authorization"] == "Bearer tok"
        if url.endswith("/sites/contoso.sharepoint.com:/sites/Ops"):
            return FakeResponse(200, {"id": "site-1"})
        if url.endswith("/sites/site-1/drives"):
            return FakeResponse(200, {"value": [{"id": "drive-1", "name": "Documents"}]})
        if url.endswith("/drives/drive-1/root/children"):
            return FakeResponse(200, {"value": [
                {"id": "f1", "name": "Procedures", "folder": {}},
                {"id": "i2", "name": "Readme.md", "size": 8, "webUrl": "https://contoso.sharepoint.com/sites/Ops/Readme.md",
                 "lastModifiedDateTime": "2026-11-01T00:00:00Z", "@microsoft.graph.downloadUrl": "https://contoso.sharepoint.com/dl/Readme.md"},
                {"id": "i3", "name": "image.png", "size": 4, "@microsoft.graph.downloadUrl": "https://contoso.sharepoint.com/dl/image.png"},
                {"id": "i5", "name": "relay.txt", "size": 5, "@microsoft.graph.downloadUrl": "https://evil.example/relay.txt"},
            ]})
        if url.endswith("/drives/drive-1/items/f1/children"):
            return FakeResponse(200, {"value": [
                {"id": "i4", "name": "SOP.pdf", "size": 12, "webUrl": "https://contoso.sharepoint.com/sites/Ops/Procedures/SOP.pdf",
                 "@microsoft.graph.downloadUrl": "https://contoso.sharepoint.com/dl/Procedures/SOP.pdf"},
            ]})
        return FakeResponse(404, {})

    def close(self):
        pass


@dataclass
class FakeBlob:
    name: str
    bucket: "FakeBucket"
    metadata: dict = field(default_factory=dict)
    md5_hash: str | None = None

    def upload_from_string(self, data, content_type):
        self.bucket.uploaded[self.name] = (data, content_type, dict(self.metadata))

    def delete(self):
        self.bucket.deleted.append(self.name)


class FakeBucket:
    def __init__(self):
        self.uploaded, self.deleted = {}, []

    def blob(self, name):
        return FakeBlob(name, self)


class FakeStorage:
    def __init__(self, existing):
        self._existing, self._bucket = existing, FakeBucket()

    def bucket(self, name):
        return self._bucket

    def list_blobs(self, bucket_name, prefix=""):
        return [b for b in self._existing if b.name.startswith(prefix)]


def existing_blob(name, content, status="imported"):
    return FakeBlob(name, None, {"ingestion_status": status}, content_md5_base64(content))


def test_import_site_stages_supported_files_and_mirrors_removals():
    graph, storage = FakeGraph(), FakeStorage([
        existing_blob("sharepoint/raw/contoso.sharepoint.com/sites/Ops/Documents/Readme.md", b"# readme"),  # unchanged
        existing_blob("sharepoint/raw/contoso.sharepoint.com/sites/Ops/Documents/Old/gone.docx", b"old"),     # removed upstream
    ])
    result = sp.import_site("https://contoso.sharepoint.com/sites/Ops", "b", SETTINGS, session=graph, storage_client=storage)

    assert result.site == "contoso.sharepoint.com/sites/Ops"
    assert (result.staged_documents, result.unchanged_documents, result.removed_documents) == (1, 1, 1)
    assert list(storage._bucket.uploaded) == ["sharepoint/raw/contoso.sharepoint.com/sites/Ops/Documents/Procedures/SOP.pdf"]
    data, content_type, metadata = storage._bucket.uploaded["sharepoint/raw/contoso.sharepoint.com/sites/Ops/Documents/Procedures/SOP.pdf"]
    assert data == b"%PDF-1.4 sop" and content_type == "application/pdf"
    assert metadata["source_type"] == "sharepoint" and metadata["original_filename"] == "SOP.pdf"
    assert metadata["sharepoint_url"] == "https://contoso.sharepoint.com/sites/Ops/Procedures/SOP.pdf"
    assert metadata["ingestion_status"] == "pending"
    assert storage._bucket.deleted == ["sharepoint/raw/contoso.sharepoint.com/sites/Ops/Documents/Old/gone.docx"]
    # the PNG and the non-SharePoint download URL were never fetched
    fetched = [u for m, u in graph.requests if m == "GET"]
    assert "https://contoso.sharepoint.com/dl/image.png" not in fetched
    assert not any(u.startswith("https://evil.example/") for u in fetched)
    assert "sharepoint/raw/contoso.sharepoint.com/sites/Ops/Documents/relay.txt" not in storage._bucket.uploaded


def test_forbidden_site_gives_a_clear_permission_error():
    class Forbidden(FakeGraph):
        def get(self, url, headers=None, timeout=None, stream=False):
            return FakeResponse(403, {})

    with pytest.raises(sp.SharePointImportError) as info:
        sp.import_site("https://contoso.sharepoint.com/sites/Ops", "b", SETTINGS, session=Forbidden(), storage_client=FakeStorage([]))
    assert info.value.status_code == 403 and "Sites.Read.All" in info.value.detail


def test_bad_credentials_do_not_leak_into_the_error():
    class BadToken(FakeGraph):
        def post(self, url, data, timeout):
            return FakeResponse(401, {"error": "invalid_client"})

    with pytest.raises(sp.SharePointImportError) as info:
        sp.import_site("https://contoso.sharepoint.com/sites/Ops", "b", SETTINGS, session=BadToken(), storage_client=FakeStorage([]))
    assert info.value.status_code == 502 and "secret" not in info.value.detail.lower().replace("sharepoint_*", "")
