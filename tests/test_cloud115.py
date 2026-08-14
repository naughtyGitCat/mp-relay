"""Tests for app/cloud115.py — token persistence + PKCE helpers + auth flow.

The actual p115client HTTP calls are mocked; we never hit 115's real API from
CI. Only the mp-relay integration logic is exercised.
"""
from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch, AsyncMock

sys.path.insert(0, str(Path(__file__).parent.parent))


# ---------------------------------------------------------------------------
# PKCE helpers
# ---------------------------------------------------------------------------

def test_code_verifier_is_url_safe_random():
    from app.cloud115 import _gen_code_verifier
    v1 = _gen_code_verifier()
    v2 = _gen_code_verifier()
    assert v1 != v2
    # RFC 7636 says verifier is 43-128 chars, [A-Z][a-z][0-9]-._~
    assert 43 <= len(v1) <= 128
    assert all(c.isalnum() or c in "-._~" for c in v1)


def test_code_challenge_is_sha256_b64():
    """115 docs require base64(sha256(verifier)) — NOT base64url."""
    import base64, hashlib
    from app.cloud115 import _gen_code_challenge
    verifier = "abc123"
    expected = base64.b64encode(hashlib.sha256(b"abc123").digest()).decode()
    assert _gen_code_challenge(verifier) == expected


# ---------------------------------------------------------------------------
# Token persistence
# ---------------------------------------------------------------------------

def _isolated_db(monkeypatch) -> str:
    """Point settings.state_db at a fresh tmpfile so tests don't touch each other."""
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    from app.config import settings
    monkeypatch.setattr(settings, "state_db", tmp.name)
    from app import cloud115
    cloud115.init_token_table()
    return tmp.name


def test_save_and_load_tokens(monkeypatch):
    _isolated_db(monkeypatch)
    from app import cloud115
    assert cloud115.load_tokens() is None
    assert not cloud115.is_authorized()
    cloud115.save_tokens("at-1", "rt-1", expires_in=3600)
    assert cloud115.load_tokens() == ("at-1", "rt-1")
    assert cloud115.is_authorized()


def test_save_tokens_overwrites_in_place(monkeypatch):
    _isolated_db(monkeypatch)
    from app import cloud115
    cloud115.save_tokens("at-1", "rt-1")
    cloud115.save_tokens("at-2", "rt-2")
    assert cloud115.load_tokens() == ("at-2", "rt-2")


def test_clear_tokens_removes_row(monkeypatch):
    _isolated_db(monkeypatch)
    from app import cloud115
    cloud115.save_tokens("at", "rt")
    cloud115.clear_tokens()
    assert cloud115.load_tokens() is None
    assert not cloud115.is_authorized()


def test_init_token_table_idempotent(monkeypatch):
    """Calling init_token_table twice must not error or wipe data."""
    _isolated_db(monkeypatch)
    from app import cloud115
    cloud115.save_tokens("at", "rt")
    cloud115.init_token_table()  # should not drop existing row
    assert cloud115.load_tokens() == ("at", "rt")


# ---------------------------------------------------------------------------
# Device-code auth flow (mocked p115client)
# ---------------------------------------------------------------------------

def test_start_auth_returns_qr_handle_and_stores_verifier(monkeypatch):
    _isolated_db(monkeypatch)
    from app import cloud115

    fake_resp = {"data": {"uid": "abc-uid-123", "time": 1234567, "sign": "fakesig", "qrcode": "115://qr-payload-x"}}

    async def fake_token_open(payload, **kw):
        # Verify we sent the right shape
        assert payload["client_id"] == cloud115._APP_ID
        assert "code_challenge" in payload
        assert payload["code_challenge_method"] == "sha256"
        return fake_resp

    monkeypatch.setattr(
        "app.cloud115.P115OpenClient.login_qrcode_token_open",
        AsyncMock(side_effect=fake_token_open),
    )

    out = asyncio.run(cloud115.start_auth())
    assert out == {"uid": "abc-uid-123", "time": 1234567, "sign": "fakesig", "qrcode": "115://qr-payload-x"}
    # verifier registered against uid for later exchange
    assert cloud115._pending_auth.get("abc-uid-123")
    cloud115._pending_auth.pop("abc-uid-123", None)


def test_poll_auth_status_0_or_1_returns_unauthorized(monkeypatch):
    _isolated_db(monkeypatch)
    from app import cloud115

    monkeypatch.setattr(
        "app.cloud115.P115OpenClient.login_qrcode_scan_status",
        AsyncMock(return_value={"data": {"status": 1, "msg": "已扫描"}}),
    )

    out = asyncio.run(cloud115.poll_auth("abc", "1234", "sig"))
    assert out == {"status": 1, "msg": "已扫描", "authorized": False}


def test_poll_auth_status_2_exchanges_token_and_persists(monkeypatch):
    """Happy path: scan complete → token exchange → save → return authorized."""
    _isolated_db(monkeypatch)
    from app import cloud115

    cloud115._pending_auth["my-uid"] = "test-verifier"

    monkeypatch.setattr(
        "app.cloud115.P115OpenClient.login_qrcode_scan_status",
        AsyncMock(return_value={"data": {"status": 2}}),
    )
    monkeypatch.setattr(
        "app.cloud115.P115OpenClient.login_qrcode_access_token_open",
        AsyncMock(return_value={
            "data": {"access_token": "fresh-at", "refresh_token": "fresh-rt", "expires_in": 3600},
        }),
    )

    out = asyncio.run(cloud115.poll_auth("my-uid", "1234", "sig"))
    assert out["authorized"] is True
    assert cloud115.load_tokens() == ("fresh-at", "fresh-rt")
    # Verifier should be consumed (popped) after exchange
    assert "my-uid" not in cloud115._pending_auth


def test_poll_auth_status_2_without_pending_verifier_fails(monkeypatch):
    """If service restarted between start_auth and poll_auth, the in-memory
    verifier is gone — we must not crash, just tell the user to retry."""
    _isolated_db(monkeypatch)
    from app import cloud115

    cloud115._pending_auth.clear()

    monkeypatch.setattr(
        "app.cloud115.P115OpenClient.login_qrcode_scan_status",
        AsyncMock(return_value={"data": {"status": 2}}),
    )

    out = asyncio.run(cloud115.poll_auth("missing-uid", "1234", "sig"))
    assert out["authorized"] is False
    assert "code_verifier" in out["msg"]


# ---------------------------------------------------------------------------
# Offline ops + token refresh
# ---------------------------------------------------------------------------

def test_call_without_tokens_raises(monkeypatch):
    _isolated_db(monkeypatch)
    from app import cloud115
    try:
        asyncio.run(cloud115.add_offline_url("magnet:?xt=urn:btih:abc"))
        assert False, "should have raised"
    except RuntimeError as e:
        assert "未授权" in str(e)


def test_client_method_resolves_legacy_and_renamed_helpers():
    """p115client 0.0.9 renamed offline_*_open to clouddownload_*."""
    from app.cloud115 import _client_method

    class Legacy:
        def offline_add_urls_open(self) -> str:
            return "legacy"

        def offline_quota_info_open(self) -> str:
            return "legacy-quota"

    class Renamed:
        def clouddownload_task_add_urls(self) -> str:
            return "new"

        def clouddownload_quota_info(self) -> str:
            return "new-quota"

    assert _client_method(Legacy(), "offline_add_urls_open")() == "legacy"
    assert _client_method(Renamed(), "offline_add_urls_open")() == "new"
    assert _client_method(Legacy(), "offline_quota_info_open")() == "legacy-quota"
    assert _client_method(Renamed(), "offline_quota_info_open")() == "new-quota"


def test_make_client_uses_from_token_when_present(monkeypatch):
    from app import cloud115

    class Dummy:
        @classmethod
        def from_token(cls, access_token: str, refresh_token: str) -> "Dummy":
            self = object.__new__(cls)
            self.access_token = access_token
            self.refresh_token = refresh_token
            return self

        def __init__(self, *args: object, **kwargs: object) -> None:
            raise AssertionError("constructor should not run when from_token exists")

    monkeypatch.setattr(cloud115, "P115OpenClient", Dummy)
    client = cloud115._make_client("at", "rt")
    assert client.access_token == "at"
    assert client.refresh_token == "rt"


def test_make_client_falls_back_to_constructor(monkeypatch):
    from app import cloud115

    class Dummy:
        def __init__(self, access_token: str, refresh_token: str) -> None:
            self.access_token = access_token
            self.refresh_token = refresh_token

    monkeypatch.setattr(cloud115, "P115OpenClient", Dummy)
    client = cloud115._make_client("at", "rt")
    assert client.access_token == "at"
    assert client.refresh_token == "rt"


def test_installed_p115client_can_build_and_resolve_offline_helpers():
    """Fail CI if a future p115client drop/rename breaks the aliases."""
    from app.cloud115 import _client_method, _make_client

    client = _make_client("at", "rt")
    assert client.access_token == "at"
    assert client.refresh_token == "rt"
    for name in (
        "offline_add_urls_open",
        "offline_quota_info_open",
        "offline_list_open",
        "download_url_info_open",
    ):
        assert callable(_client_method(client, name)), name


def test_add_offline_url_invokes_correct_endpoint(monkeypatch):
    _isolated_db(monkeypatch)
    from app import cloud115
    cloud115.save_tokens("at", "rt")

    captured: dict = {}

    async def fake_add(self, payload, **kw):
        captured["payload"] = payload
        return {"state": True, "data": [{"info_hash": "abc", "name": "MyFile.mp4"}]}

    monkeypatch.setattr(
        "app.cloud115.P115OpenClient.offline_add_urls_open",
        fake_add,
        raising=False,
    )

    out = asyncio.run(cloud115.add_offline_url("magnet:?xt=urn:btih:abc"))
    assert captured["payload"]["urls"] == "magnet:?xt=urn:btih:abc"
    assert out["state"] is True


def test_call_refreshes_on_token_expired(monkeypatch):
    """First call raises 40140116, second succeeds — verify refresh happened."""
    _isolated_db(monkeypatch)
    from app import cloud115
    cloud115.save_tokens("old-at", "old-rt")

    call_count = {"add": 0, "refresh": 0}

    async def fake_add(self, payload, **kw):
        call_count["add"] += 1
        if call_count["add"] == 1:
            raise RuntimeError("HTTP 401 / 40140116 access_token expired")
        return {"state": True, "data": []}

    async def fake_refresh(payload, **kw):
        call_count["refresh"] += 1
        return {"data": {"access_token": "new-at", "refresh_token": "new-rt", "expires_in": 7200}}

    monkeypatch.setattr("app.cloud115.P115OpenClient.offline_add_urls_open", fake_add, raising=False)
    monkeypatch.setattr("app.cloud115.P115OpenClient.login_refresh_token_open",
                        AsyncMock(side_effect=fake_refresh))

    asyncio.run(cloud115.add_offline_url("magnet:?xt=urn:btih:xyz"))

    assert call_count["add"] == 2          # called twice (initial + retry)
    assert call_count["refresh"] == 1
    # Tokens must have been persisted
    assert cloud115.load_tokens() == ("new-at", "new-rt")


def test_call_refreshes_on_state_false_token_message(monkeypatch):
    """The 2026-05-01 case: 115 returns HTTP 200 + {state: false, message:
    "access_token 无效"} instead of raising. The original exception-only
    handler missed this and surfaced the failure to /health."""
    _isolated_db(monkeypatch)
    from app import cloud115
    cloud115.save_tokens("old-at", "old-rt")

    call_count = {"quota": 0, "refresh": 0}

    async def fake_quota(self, **kw):
        call_count["quota"] += 1
        if call_count["quota"] == 1:
            return {"state": False, "code": 40140116,
                    "message": "access_token 无效，请刷新后重试"}
        return {"state": True, "data": {"quota": 1000, "used": 5}}

    async def fake_refresh(payload, **kw):
        call_count["refresh"] += 1
        return {"data": {"access_token": "new-at", "refresh_token": "new-rt"}}

    monkeypatch.setattr("app.cloud115.P115OpenClient.offline_quota_info_open", fake_quota, raising=False)
    monkeypatch.setattr("app.cloud115.P115OpenClient.login_refresh_token_open",
                        AsyncMock(side_effect=fake_refresh))

    out = asyncio.run(cloud115.quota_info())
    assert call_count["quota"] == 2     # called twice (initial state=false + retry)
    assert call_count["refresh"] == 1
    assert out["state"] is True
    assert cloud115.load_tokens() == ("new-at", "new-rt")


def test_call_does_not_loop_on_persistent_state_false(monkeypatch):
    """If refresh-and-retry STILL gets state=false, return the response —
    don't infinite-loop refreshing. (Refresh token might be the actually-
    expired one.)"""
    _isolated_db(monkeypatch)
    from app import cloud115
    cloud115.save_tokens("old-at", "old-rt")

    call_count = {"quota": 0, "refresh": 0}

    async def fake_quota(self, **kw):
        call_count["quota"] += 1
        return {"state": False, "code": 40140116, "message": "access_token expired"}

    async def fake_refresh(payload, **kw):
        call_count["refresh"] += 1
        return {"data": {"access_token": "new-at", "refresh_token": "new-rt"}}

    monkeypatch.setattr("app.cloud115.P115OpenClient.offline_quota_info_open", fake_quota, raising=False)
    monkeypatch.setattr("app.cloud115.P115OpenClient.login_refresh_token_open",
                        AsyncMock(side_effect=fake_refresh))

    out = asyncio.run(cloud115.quota_info())
    assert call_count["quota"] == 2      # initial + 1 retry; NOT infinite
    assert call_count["refresh"] == 1
    assert out["state"] is False         # caller sees the persistent failure


def test_call_does_not_refresh_on_unrelated_state_false(monkeypatch):
    """state=false WITHOUT a token-related message → not a token issue,
    don't burn a refresh on it."""
    _isolated_db(monkeypatch)
    from app import cloud115
    cloud115.save_tokens("at", "rt")

    call_count = {"quota": 0, "refresh": 0}

    async def fake_quota(self, **kw):
        call_count["quota"] += 1
        return {"state": False, "code": 99999, "message": "out of quota for today"}

    async def fake_refresh(payload, **kw):
        call_count["refresh"] += 1
        return {"data": {"access_token": "x", "refresh_token": "y"}}

    monkeypatch.setattr("app.cloud115.P115OpenClient.offline_quota_info_open", fake_quota, raising=False)
    monkeypatch.setattr("app.cloud115.P115OpenClient.login_refresh_token_open",
                        AsyncMock(side_effect=fake_refresh))

    asyncio.run(cloud115.quota_info())
    assert call_count["quota"] == 1
    assert call_count["refresh"] == 0    # token wasn't the issue, no refresh attempted


def test_looks_like_expired_token_response_classification():
    from app.cloud115 import _looks_like_expired_token_response
    # Token-related state=false → True
    assert _looks_like_expired_token_response(
        {"state": False, "message": "access_token 无效"}
    )
    assert _looks_like_expired_token_response(
        {"state": False, "message": "Token expired, please refresh"}
    )
    # Unrelated state=false → False
    assert not _looks_like_expired_token_response(
        {"state": False, "message": "quota exceeded"}
    )
    # state=true → False (not an error at all)
    assert not _looks_like_expired_token_response(
        {"state": True, "data": {}}
    )
    # Non-dict → False
    assert not _looks_like_expired_token_response(None)
    assert not _looks_like_expired_token_response([])


def test_healthcheck_unauthorized_returns_disabled_string(monkeypatch):
    """When 115 isn't authorized, healthcheck returns a ``"disabled (...)"``
    string — distinct from real errors so callers can treat it as opt-out
    rather than a failure. Users without a 115 membership shouldn't see
    "ok=false" on /health."""
    _isolated_db(monkeypatch)
    from app import cloud115
    err = asyncio.run(cloud115.healthcheck())
    assert err is not None
    assert err.startswith("disabled")
    # Mentions how to enable (so the user can act on it)
    assert "/auth/115" in err


def test_healthcheck_authorized_calls_quota(monkeypatch):
    _isolated_db(monkeypatch)
    from app import cloud115
    cloud115.save_tokens("at", "rt")

    async def fake_quota(self, **kw):
        return {"state": True, "data": {"quota": 100, "used": 10}}

    monkeypatch.setattr(
        "app.cloud115.P115OpenClient.offline_quota_info_open",
        fake_quota,
        raising=False,
    )

    err = asyncio.run(cloud115.healthcheck())
    assert err is None


def test_healthcheck_binary_oserror_does_not_leak_full_blob(monkeypatch):
    """Regression: p115client sometimes wraps an undecodable response body
    into ``OSError(errno, b'<binary blob>')``. Old code did
    ``f"cloud115 probe error: {e}"`` which let ``str(OSError)`` splatter
    hundreds of ``\\x``-escaped bytes into /health JSON. The fix should:

      - bound the returned string to something readable
      - surface the exception type so the user knows what kind of failure
      - point at the actionable fix (/auth/115 re-authorize)
    """
    _isolated_db(monkeypatch)
    from app import cloud115
    cloud115.save_tokens("at", "rt")

    # Simulate the exact wrapping p115client does on a sign-failure with
    # expired token — a long opaque byte blob as strerror.
    binary_blob = (b"\x81R8\xbf\xc5E\xde\xae\x1dS\x82#\x94\xdf\xa1\x1d0" * 50)
    assert len(binary_blob) > 500  # well over our truncation cap

    async def fake_quota(self, **kw):
        raise OSError(61, binary_blob)

    monkeypatch.setattr(
        "app.cloud115.P115OpenClient.offline_quota_info_open",
        fake_quota,
        raising=False,
    )

    err = asyncio.run(cloud115.healthcheck())
    assert err is not None
    # Bounded: even with a multi-KB binary payload, the returned string
    # must stay short enough that /health JSON remains usable.
    assert len(err) < 400, f"error string too long: {len(err)} chars"
    # Exception type surfaced so caller can distinguish OS-level failure
    # from app-level. Note Python auto-promotes ``OSError(61, ...)`` to
    # ``ConnectionRefusedError`` on POSIX hosts (errno-based subclass
    # dispatch). On Windows where errno 61 doesn't map to a known
    # subclass, it stays ``OSError``. Accept either.
    assert "OSError" in err or "ConnectionRefusedError" in err
    # Truncation marker present
    assert "truncated" in err
    # Actionable hint
    assert "/auth/115" in err


def test_healthcheck_truncates_long_quota_rejection(monkeypatch):
    """The state=False rejection path also goes through the truncator —
    defensive in case 115 ever returns a multi-KB ``message`` field."""
    _isolated_db(monkeypatch)
    from app import cloud115
    cloud115.save_tokens("at", "rt")

    long_msg = "rate-limited: " + ("x" * 1000)

    async def fake_quota(self, **kw):
        return {"state": False, "message": long_msg}

    monkeypatch.setattr(
        "app.cloud115.P115OpenClient.offline_quota_info_open",
        fake_quota,
        raising=False,
    )

    err = asyncio.run(cloud115.healthcheck())
    assert err is not None
    assert err.startswith("quota probe rejected:")
    assert len(err) < 400
    assert "truncated" in err
