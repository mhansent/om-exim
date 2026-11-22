import json

import pytest
import requests

from om_exim import client
from om_exim.client import ReadClient, ReadError, RequestPacer
from om_exim.settings import Connection, HttpSettings
from om_exim.target import TargetClient


def response(status, payload, headers=None):
    result = requests.Response()
    result.status_code = status
    result._content = json.dumps(payload).encode()
    result._content_consumed = True
    result.headers.update(headers or {})
    return result


class Session:
    def __init__(self, *results):
        self.results = iter(results)
        self.requests = []

    def get(self, url, **kwargs):
        return self.request("GET", url, **kwargs)

    def request(self, method, url, **kwargs):
        self.requests.append((method, url, kwargs))
        result = next(self.results)
        if isinstance(result, Exception):
            raise result
        return result


@pytest.fixture
def timer(monkeypatch):
    elapsed = 0
    sleeps = []

    def pause(seconds):
        nonlocal elapsed
        elapsed += seconds
        sleeps.append(seconds)

    monkeypatch.setattr(client, "sleep", pause)
    monkeypatch.setattr(client, "monotonic", lambda: elapsed)
    return sleeps


def test_reads_retry_timeouts_and_honor_retry_after(timer):
    session = Session(
        requests.Timeout("do not expose"),
        response(429, {}, {"Retry-After": "3"}),
        response(200, {"version": "2.0.8"}),
    )
    service = ReadClient(
        Connection("https://source.example/api", "unused"), "Source", session
    )

    assert service.version() == "2.0.8"
    assert timer == [0.5, 3]
    assert len(session.requests) == 3


@pytest.mark.parametrize(
    "failure",
    [
        response(503, {}, {"Retry-After": "31"}),
        response(403, {}),
        requests.exceptions.SSLError("do not expose"),
    ],
)
def test_reads_do_not_retry_long_waits_permission_denials_or_tls_failures(
    timer, failure
):
    session = Session(failure)
    service = ReadClient(
        Connection("https://source.example/api", "unused"), "Source", session
    )
    with pytest.raises(ReadError, match="Source: GET system"):
        service.version()
    assert not timer
    assert len(session.requests) == 1


def test_reads_stop_after_configured_retries(timer):
    session = Session(*(response(503, {}) for _ in range(3)))
    service = ReadClient(
        Connection("https://source.example/api", "unused"), "Source", session
    )
    with pytest.raises(ReadError, match="HTTP 503"):
        service.version()
    assert timer == [0.5, 1]
    assert len(session.requests) == 3


def test_reads_and_writes_share_rate_limit_and_writes_are_not_replayed(timer):
    pacer = RequestPacer(HttpSettings(timeout_seconds=12, requests_per_second=4))
    source_session = Session(response(200, {"version": "2.0.8"}))
    target_session = Session(response(503, {"secret": "do not expose"}))
    source = ReadClient(
        Connection("https://source.example/api", "unused"),
        "Source",
        source_session,
        pacer=pacer,
    )
    target = TargetClient(
        Connection("https://target.example/api", "unused"),
        "Target",
        target_session,
        pacer=pacer,
    )
    source.version()
    with pytest.raises(ReadError, match="HTTP 503.*checkpoint") as error:
        target.create("domain", {"name": "Scoped"})
    assert "do not expose" not in str(error.value)
    assert timer == [0.25]
    assert len(target_session.requests) == 1
    assert (
        source_session.requests[0][2]["timeout"]
        == target_session.requests[0][2]["timeout"]
        == 12
    )


def test_user_patch_permission_denial_reports_membership_access():
    session = Session(response(403, {"message": "private account detail"}))
    target = TargetClient(
        Connection("https://target.example/api", "unused"), "Target", session
    )

    with pytest.raises(ReadError, match="HTTP 403.*administrator.*checkpoint") as error:
        target.patch(
            "user",
            "scoped-user",
            [{"op": "add", "path": "/teams", "value": []}],
            etag=None,
        )

    assert len(session.requests) == 1
    assert "private account detail" not in str(error.value)
