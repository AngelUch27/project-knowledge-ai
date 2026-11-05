import logging

import pytest
from fastapi.testclient import TestClient
from google.api_core.exceptions import GoogleAPIError

from backend import main
from backend import github_import as gi


@pytest.fixture
def client():
    # The app only trusts 127.0.0.1/localhost, so the test client must present that host.
    return TestClient(main.app, base_url="http://localhost")


def test_answers_follow_the_language_of_the_question():
    assert "same language as the user's latest message" in main.SYSTEM_INSTRUCTION
    assert "Answer in English" not in main.SYSTEM_INSTRUCTION


def test_health_needs_no_gcp(client):
    assert client.get("/health").json() == {"status": "ok", "service": "project-knowledge-ai-api"}


def test_sync_endpoint_reports_results_and_failures(client, monkeypatch):
    monkeypatch.setenv("GCP_PROJECT_ID", "p")
    monkeypatch.setenv("GCS_BUCKET", "b")
    outcome = gi.SyncOutcome(
        results=(
            gi.RepositoryImportResult(
                repository="a/ok", commit_sha="abc1234", staged_documents=2, total_bytes=10,
                missing_paths=(), gcs_uris=(), unchanged_documents=5, removed_documents=1,
            ),
        ),
        failures=(gi.SyncFailure("b/bad", "GitHub's unauthenticated API rate limit was reached. Try again later."),),
    )
    monkeypatch.setattr(main, "sync_all_repositories", lambda bucket: outcome)

    response = client.post("/github/repositories/sync")

    assert response.status_code == 200
    body = response.json()
    assert body["synced"] == [
        {
            "repository": "a/ok", "commit_sha": "abc1234", "staged_documents": 2,
            "unchanged_documents": 5, "removed_documents": 1, "status": "staged",
        }
    ]
    assert body["failed"] == [{"repository": "b/bad", "detail": "GitHub's unauthenticated API rate limit was reached. Try again later."}]


def test_sync_endpoint_logs_the_real_error_behind_a_generic_502(client, monkeypatch, caplog):
    monkeypatch.setenv("GCP_PROJECT_ID", "p")
    monkeypatch.setenv("GCS_BUCKET", "b")

    def boom(bucket):
        raise GoogleAPIError("Permission 'storage.objects.list' denied on bucket b")

    monkeypatch.setattr(main, "sync_all_repositories", boom)
    with caplog.at_level(logging.ERROR, logger="backend.main"):
        response = client.post("/github/repositories/sync")

    assert response.status_code == 502
    assert response.json()["detail"] == "Could not list the staged repositories in the bucket."
    assert "storage.objects.list" in caplog.text  # the cause is in the server log, not in the response


def test_requests_from_other_origins_are_rejected(client):
    response = client.get("/health", headers={"Origin": "https://evil.example"})
    assert response.status_code == 403


def test_same_origin_behind_a_tls_proxy_is_accepted(client):
    # Cloud Run terminates TLS: the browser's Origin is https while the app sees http.
    response = client.get("/health", headers={"Origin": "https://localhost", "X-Forwarded-Proto": "https"})
    assert response.status_code == 200
    assert client.get("/health", headers={"Origin": "https://localhost"}).status_code == 403


def test_me_reports_the_iap_identity_or_local_mode(client):
    assert client.get("/me").json() == {"user": None, "mode": "local"}
    body = client.get("/me", headers={"X-Goog-Authenticated-User-Email": "accounts.google.com:ana@example.com"}).json()
    assert body == {"user": "ana@example.com", "mode": "cloud"}


def test_rate_limits_are_per_iap_user_when_signed_in(client, monkeypatch):
    from backend.ratelimit import SlidingWindowLimiter, Window

    monkeypatch.setenv("GCP_PROJECT_ID", "p")
    monkeypatch.setenv("GCS_BUCKET", "b")
    monkeypatch.setattr(main, "LIMITER", SlidingWindowLimiter({"ask": (Window(1, 60),)}))
    monkeypatch.setattr(main, "run_agent", lambda *a, **k: type("R", (), {"answer": "ok", "steps": [], "sources": [], "pending_action": None, "pending_description": None})())
    monkeypatch.setattr(main.genai, "Client", lambda **k: type("C", (), {"close": lambda self: None})())
    ana = {"X-Goog-Authenticated-User-Email": "accounts.google.com:ana@example.com"}
    luis = {"X-Goog-Authenticated-User-Email": "accounts.google.com:luis@example.com"}
    assert client.post("/ask", json={"question": "hello there"}, headers=ana).status_code == 200
    assert client.post("/ask", json={"question": "hello there"}, headers=ana).status_code == 429
    assert client.post("/ask", json={"question": "hello there"}, headers=luis).status_code == 200  # own budget
