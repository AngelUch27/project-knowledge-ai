import pytest
from fastapi.testclient import TestClient

from backend import main
from backend.ratelimit import SlidingWindowLimiter, Window


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def limiter(clock, **policies):
    return SlidingWindowLimiter(policies, clock=clock)


def test_allows_up_to_the_limit_then_blocks_with_retry_after():
    clock = Clock()
    lim = limiter(clock, ask=(Window(3, 60),))
    assert [lim.check("ask").allowed for _ in range(3)] == [True, True, True]
    blocked = lim.check("ask")
    assert blocked.allowed is False and blocked.used == 3 and blocked.limit == 3 and blocked.window == "minute"
    assert 0 < blocked.retry_after <= 61
    assert blocked.headers()["Retry-After"] == str(blocked.retry_after)
    clock.t += 61
    assert lim.check("ask").allowed is True  # the window slid


def test_warns_from_eighty_percent_before_blocking():
    clock = Clock()
    lim = limiter(clock, ask=(Window(10, 60),))
    decisions = [lim.check("ask") for _ in range(8)]
    assert [d.warning for d in decisions[:7]] == [False] * 7
    assert decisions[7].warning is True and decisions[7].allowed is True
    assert decisions[7].headers()["X-RateLimit-Warning"] == "8/10 per minute"
    assert decisions[7].remaining == 2


def test_the_tightest_window_decides_the_usage_report():
    clock = Clock()
    lim = limiter(clock, ask=(Window(5, 60), Window(100, 3600)))
    for _ in range(4):
        lim.check("ask")
    snap = lim.check("ask", record=False)
    assert (snap.used, snap.limit, snap.window) == (4, 5, "minute")
    clock.t += 61
    snap = lim.check("ask", record=False)
    assert (snap.used, snap.limit, snap.window) == (4, 100, "hour")  # minute window emptied, hour still counts


def test_keys_are_independent_and_snapshot_does_not_record():
    lim = limiter(Clock(), ask=(Window(1, 60),))
    assert lim.check("ask", "a").allowed and not lim.check("ask", "a").allowed
    assert lim.check("ask", "b").allowed
    before = lim.snapshot("b")[0]["used"]
    lim.snapshot("b")
    assert lim.snapshot("b")[0]["used"] == before


def test_default_policies_are_generous_but_bounded():
    lim = SlidingWindowLimiter()
    for name in ("ask", "search", "import", "upload"):
        windows = lim.policy(name)
        assert windows and all(w.limit >= 10 for w in windows)
    assert lim.policy("ask")[0].limit >= 10 and lim.policy("ask")[1].limit >= 100


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("GCP_PROJECT_ID", "p")
    monkeypatch.setenv("GCS_BUCKET", "b")
    return TestClient(main.app, base_url="http://localhost")


def test_ask_returns_429_with_headers_when_the_budget_is_spent(client, monkeypatch):
    monkeypatch.setattr(main, "LIMITER", SlidingWindowLimiter({"ask": (Window(1, 60),)}, clock=Clock()))
    monkeypatch.setattr(main, "run_agent", lambda *a, **k: type("R", (), {"answer": "ok", "steps": [], "sources": [], "pending_action": None, "pending_description": None})())
    monkeypatch.setattr(main.genai, "Client", lambda **k: type("C", (), {"close": lambda self: None})())

    first = client.post("/ask", json={"question": "hello there"})
    assert first.status_code == 200
    assert first.headers["X-RateLimit-Remaining"] == "0"
    assert first.json()["usage"] == {"policy": "ask", "used": 1, "limit": 1, "window": "minute", "warning": True}

    second = client.post("/ask", json={"question": "hello again"})
    assert second.status_code == 429
    assert "Retry-After" in second.headers
    assert "Rate limit reached" in second.json()["detail"]


def test_usage_endpoint_reports_every_policy_for_the_calling_client(client, monkeypatch):
    monkeypatch.setattr(main, "LIMITER", SlidingWindowLimiter({"ask": (Window(5, 60),), "upload": (Window(5, 60),)}, clock=Clock()))
    monkeypatch.setattr(main, "run_agent", lambda *a, **k: type("R", (), {"answer": "ok", "steps": [], "sources": [], "pending_action": None, "pending_description": None})())
    monkeypatch.setattr(main.genai, "Client", lambda **k: type("C", (), {"close": lambda self: None})())
    client.post("/ask", json={"question": "hello there"})
    body = client.get("/usage").json()
    assert {p["policy"]: p["used"] for p in body["policies"]} == {"ask": 1, "upload": 0}
    # reading usage does not consume anything
    assert client.get("/usage").json()["policies"][0]["used"] == 1
