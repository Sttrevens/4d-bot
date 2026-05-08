import logging

import httpx


class _DummyResponse:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


def _configure_redis(monkeypatch, redis):
    monkeypatch.setattr(redis, "_REDIS_URL", "https://redis.example.test")
    monkeypatch.setattr(redis, "_REDIS_TOKEN", "token")
    monkeypatch.setattr(redis, "_cb_open_until", 0.0)
    monkeypatch.setattr(redis, "_shared_client", None)
    monkeypatch.setattr(redis.time, "sleep", lambda _seconds: None)
    if hasattr(redis, "_transient_cb_open_until"):
        monkeypatch.setattr(redis, "_transient_cb_open_until", 0.0)
    if hasattr(redis, "_consecutive_transient_failures"):
        monkeypatch.setattr(redis, "_consecutive_transient_failures", 0)


def test_post_with_retry_treats_write_timeout_as_transient_and_resets_client(monkeypatch):
    from app.services import redis_client as redis

    _configure_redis(monkeypatch, redis)
    calls = []
    closed = []

    class FakeClient:
        def __init__(self, name):
            self.name = name
            self.is_closed = False

        def post(self, url, json, timeout=None):
            calls.append((self.name, url, json, timeout))
            if len(calls) == 1:
                raise httpx.WriteTimeout("write timed out")
            return _DummyResponse({"result": "OK"})

        def close(self):
            closed.append(self.name)
            self.is_closed = True

    clients = [FakeClient("first"), FakeClient("second")]

    def fake_get_client():
        if redis._shared_client is None:
            redis._shared_client = clients.pop(0)
        return redis._shared_client

    monkeypatch.setattr(redis, "_get_client", fake_get_client)

    resp = redis._post_with_retry("https://redis.example.test/pipeline", [["PING"]], timeout=2.5)

    assert resp.json() == {"result": "OK"}
    assert [call[0] for call in calls] == ["first", "second"]
    assert calls[1][3] == 2.5
    assert closed == ["first"]


def test_post_with_retry_passes_timeout_to_httpx_client(monkeypatch):
    from app.services import redis_client as redis

    _configure_redis(monkeypatch, redis)
    seen = {}

    class FakeClient:
        is_closed = False

        def post(self, url, json, timeout=None):
            seen["timeout"] = timeout
            return _DummyResponse({"result": "PONG"})

    monkeypatch.setattr(redis, "_get_client", lambda: FakeClient())

    resp = redis._post_with_retry("https://redis.example.test", ["PING"], timeout=4.25)

    assert resp.json() == {"result": "PONG"}
    assert seen["timeout"] == 4.25


def test_pipeline_write_timeout_fail_opens_short_circuit_and_throttles_tracebacks(
    monkeypatch,
    caplog,
):
    from app.services import redis_client as redis

    _configure_redis(monkeypatch, redis)
    monkeypatch.setattr(redis, "_TRANSIENT_CB_THRESHOLD", 2, raising=False)
    monkeypatch.setattr(redis, "_TRANSIENT_CB_COOLDOWN", 60.0, raising=False)
    monkeypatch.setattr(redis, "_ERROR_LOG_COOLDOWN", 60.0, raising=False)
    monkeypatch.setattr(redis, "_post_with_retry", lambda *_args, **_kwargs: (_ for _ in ()).throw(httpx.WriteTimeout("boom")))
    caplog.set_level(logging.WARNING, logger=redis.logger.name)

    assert redis.pipeline([["GET", "a"]]) == [None]
    assert redis.pipeline([["GET", "b"]]) == [None]

    diag = redis.diagnostics()
    assert diag["last_error_type"] == "WriteTimeout"
    assert diag["consecutive_transient_failures"] >= 2
    assert diag["transient_circuit_open"] is True
    assert sum(1 for record in caplog.records if record.exc_info) == 1
