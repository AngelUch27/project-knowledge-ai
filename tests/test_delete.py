import pytest
from fastapi.testclient import TestClient

from backend import main


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("GCP_PROJECT_ID", "p")
    monkeypatch.setenv("GCS_BUCKET", "knowledge-agent-cap")
    return TestClient(main.app, base_url="http://localhost")


class FakeBlob:
    def __init__(self, store, name):
        self.store, self.name = store, name
        self.metadata = {"original_filename": "Guide.pdf"}

    def delete(self):
        self.store.deleted.append(self.name)


class FakeBucket:
    def __init__(self, store):
        self.store = store

    def get_blob(self, name):
        return FakeBlob(self.store, name) if name in self.store.objects else None


class FakeStorageClient:
    def __init__(self, project=None):
        self.store = FakeStorageClient.store

    def bucket(self, name):
        return FakeBucket(self.store)

    def close(self):
        pass


class Store:
    def __init__(self, objects):
        self.objects, self.deleted = set(objects), []


@pytest.fixture
def store(monkeypatch):
    s = Store({"documents/raw/abc.pdf"})
    FakeStorageClient.store = s
    monkeypatch.setattr(main.storage, "Client", FakeStorageClient)
    return s


def test_delete_removes_the_object_and_reports_its_name(client, store):
    response = client.delete("/documents", params={"uri": "gs://knowledge-agent-cap/documents/raw/abc.pdf"})
    assert response.status_code == 200
    assert response.json() == {"deleted": "gs://knowledge-agent-cap/documents/raw/abc.pdf", "filename": "Guide.pdf", "status": "deleted"}
    assert store.deleted == ["documents/raw/abc.pdf"]
    assert response.headers["X-RateLimit-Policy"] == "delete"


def test_delete_refuses_other_buckets_unsupported_types_and_missing_objects(client, store):
    assert client.delete("/documents", params={"uri": "gs://other-bucket/documents/raw/abc.pdf"}).status_code == 400
    assert client.delete("/documents", params={"uri": "gs://knowledge-agent-cap/manifest/run.log"}).status_code == 415
    assert client.delete("/documents", params={"uri": "not a uri"}).status_code == 400
    assert client.delete("/documents", params={"uri": "gs://knowledge-agent-cap/documents/raw/missing.pdf"}).status_code == 404
    assert store.deleted == []


def test_agent_delete_tool_is_an_action_with_the_same_guards(store, monkeypatch):
    monkeypatch.setenv("GCP_PROJECT_ID", "p")
    monkeypatch.setenv("GCS_BUCKET", "knowledge-agent-cap")
    spec = main.build_toolbox(main.get_settings()).get("delete_document")
    assert spec.action
    assert "recoverable for 7 days" in spec.confirm_text({"uri": "gs://knowledge-agent-cap/documents/raw/abc.pdf"})
    refused = spec.run({"uri": "gs://other-bucket/x.pdf"})
    assert "error" in refused and store.deleted == []
    done = spec.run({"uri": "gs://knowledge-agent-cap/documents/raw/abc.pdf"})
    assert done["status"] == "deleted" and "Guide.pdf" in done["summary"]
    assert store.deleted == ["documents/raw/abc.pdf"]
