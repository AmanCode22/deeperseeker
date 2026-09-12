"""Stage 1 — Account lifecycle tests (login, refresh, heal, probe, crypto).

Covers the roadmap #1 Definition of Done:
- account added by email/mobile via API — no manual token extraction
- dead token -> upstream 401 -> exactly one re-login -> request succeeds
- login flood rate-limited with per-identifier backoff + global window
- probe error accrual, threshold auto-recovery, dead-token parking
- credentials encrypted at rest; v1->v2 migration leaves a .bak; rollback works
"""

import asyncio
import base64
import contextlib
import json
import os
import shutil
import sqlite3
import time
import urllib.parse

import pytest

import accounts
import app
import crypto
import functions
from accounts import (
    LoginError,
    LoginLimiter,
    build_login_payload,
    identifier_for,
    login_user,
    probe_account,
    probe_stale_accounts,
    refresh_account_token,
    re_login_single,
)
from crypto import CredentialDecryptError, decrypt_value, encrypt_value

# Fixed 32-byte Fernet key for deterministic at-rest assertions.
TEST_KEY = base64.urlsafe_b64encode(b"stage1-test-key-0123456789abcdef").decode()
TEST_KEY_2 = base64.urlsafe_b64encode(b"stage1-test-key-fedcba9876543210").decode()


# ------------------------------------------------------------------------------
# Helpers

@pytest.fixture()
def fresh_db(tmp_path, monkeypatch):
    """Isolated v2 database + deterministic Fernet key."""
    db = str(tmp_path / "test.db")
    monkeypatch.setattr(functions, "_db", db)
    monkeypatch.setattr(crypto, "_DB_PATH", db)
    monkeypatch.setenv("DEEPSEEKER_FERNET_KEY", TEST_KEY)
    crypto.reset_cache_for_tests()
    functions.init_db()
    yield db
    crypto.reset_cache_for_tests()


def make_v1_db(path, tokens=("tok-aaa", "tok-bbb")):
    """A pristine v1 database: old schema, plaintext tokens, no meta table."""
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE tokens (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            alias TEXT,
            token TEXT,
            status TEXT DEFAULT 'ACTIVE'
        );
        CREATE TABLE sessions (
            signature TEXT PRIMARY KEY,
            token_id INTEGER,
            deepseek_session_id TEXT,
            parent_message_id INTEGER DEFAULT 0
        );
        CREATE TABLE session_map (
            old_session TEXT PRIMARY KEY,
            new_session TEXT,
            token_id INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    """)
    for i, t in enumerate(tokens, 1):
        conn.execute("INSERT INTO tokens (id, alias, token) VALUES (?, ?, ?)", (i, f"alias{i}", t))
    conn.commit()
    conn.close()


@pytest.fixture()
def v1_db(tmp_path, monkeypatch):
    """Isolated v1 database (paths wired but schema migration NOT yet run)."""
    db = str(tmp_path / "legacy.db")
    monkeypatch.setattr(functions, "_db", db)
    monkeypatch.setattr(crypto, "_DB_PATH", db)
    monkeypatch.setenv("DEEPSEEKER_FERNET_KEY", TEST_KEY)
    crypto.reset_cache_for_tests()
    make_v1_db(db)
    yield db
    crypto.reset_cache_for_tests()


@pytest.fixture()
def clean_global_limiter():
    lim = app.login_limiter
    lim._failures.clear(); lim._until.clear(); lim._window.clear()
    yield lim
    lim._failures.clear(); lim._until.clear(); lim._window.clear()


@contextlib.contextmanager
def stub_module(module, **swaps):
    saved = {k: getattr(module, k) for k in swaps}
    for k, v in swaps.items():
        setattr(module, k, v)
    try:
        yield
    finally:
        for k, v in saved.items():
            setattr(module, k, v)


class FakeClock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def advance(self, dt):
        self.t += dt


class FakeResp:
    """Minimal aiohttp response stand-in for post_with_failover consumers."""

    def __init__(self, status=200, payload=None, text_body=""):
        self.status = status
        self._payload = payload
        self._text = text_body
        self.released = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        self.released = True
        return False

    async def json(self):
        if self._payload is None:
            raise ValueError("response is not JSON")
        return self._payload

    async def text(self):
        return self._text

    def release(self):
        self.released = True


async def call_endpoint(method, path, *, query="", json_body=None, form=None, admin=False):
    """Drive the real FastAPI app over raw ASGI (no TestClient/httpx dep)."""
    if json_body is not None:
        body = json.dumps(json_body).encode()
        ctype = "application/json"
    elif form is not None:
        body = urllib.parse.urlencode(form).encode()
        ctype = "application/x-www-form-urlencoded"
    else:
        body, ctype = b"", None
    headers = []
    if ctype:
        headers.append((b"content-type", ctype.encode()))
    if admin:
        app.SESSIONS["stage1-test-sid"] = time.time() + 3600
        headers.append((b"cookie", b"session_id=stage1-test-sid"))
    if body:
        headers.append((b"content-length", str(len(body)).encode()))
    scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
        "method": method, "scheme": "http", "path": path,
        "raw_path": (path + ("?" + query if query else "")).encode(),
        "query_string": query.encode(), "root_path": "",
        "server": ("testserver", 80), "client": ("127.0.0.1", 123),
        "headers": headers,
    }
    sent = []
    first = {"done": False}

    async def receive():
        if not first["done"]:
            first["done"] = True
            return {"type": "http.request", "body": body, "more_body": False}
        return {"type": "http.disconnect"}

    async def send(message):
        sent.append(message)

    await app.app(scope, receive, send)  # app = module; app.app = FastAPI ASGI callable
    status = sent[0]["status"]
    resp_headers = {k.decode(): v.decode() for k, v in sent[0].get("headers", [])}
    resp_body = b"".join(m.get("body", b"") for m in sent[1:] if m["type"] == "http.response.body")
    return status, resp_headers, resp_body


def raw_token_value(db, token_id=1):
    conn = sqlite3.connect(db)
    try:
        return conn.execute("SELECT token FROM tokens WHERE id = ?", (token_id,)).fetchone()[0]
    finally:
        conn.close()


def raw_row(db, token_id=1):
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    try:
        return dict(conn.execute("SELECT * FROM tokens WHERE id = ?", (token_id,)).fetchone())
    finally:
        conn.close()


# ------------------------------------------------------------------------------
# 1.6 — crypto: at-rest encryption

def test_encrypt_decrypt_roundtrip_and_idempotence():
    stored = encrypt_value("s3cret-password")
    assert stored.startswith("enc:v1:")
    assert "s3cret-password" not in stored
    assert decrypt_value(stored) == "s3cret-password"
    assert encrypt_value(stored) == stored  # never double-wrap


def test_decrypt_passthrough_legacy_plaintext():
    assert decrypt_value("raw-pasted-token") == "raw-pasted-token"
    assert decrypt_value(None) is None


def test_tampered_ciphertext_raises_credential_decrypt_error():
    stored = encrypt_value("s3cret")
    broken = "enc:v1:" + stored[len("enc:v1:"):][:-4] + "AAAA"
    with pytest.raises(CredentialDecryptError):
        decrypt_value(broken)


def test_key_rotation_makes_stored_values_unreadable(monkeypatch):
    stored = encrypt_value("s3cret")
    monkeypatch.setenv("DEEPSEEKER_FERNET_KEY", TEST_KEY_2)
    crypto.reset_cache_for_tests()
    with pytest.raises(CredentialDecryptError):
        decrypt_value(stored)


def test_add_token_encrypts_at_rest_and_reads_decrypt(fresh_db):
    functions.add_token("plain-pasted-token")
    assert raw_token_value(fresh_db).startswith("enc:v1:")
    assert functions.get_auth_token() == "plain-pasted-token"
    assert functions.get_token(1)["token"] == "plain-pasted-token"
    listing = functions.get_tokens()[0]
    assert listing["token"] == "plain-pasted-token"
    assert "password" not in listing and "password_enc" not in listing


def test_v1_to_v2_migration_encrypts_and_leaves_bak(v1_db):
    functions.init_db()  # runs migrate_credentials_at_rest()
    assert raw_token_value(v1_db, 1).startswith("enc:v1:")
    assert raw_token_value(v1_db, 2).startswith("enc:v1:")
    assert functions.get_token(1)["token"] == "tok-aaa"
    conn = sqlite3.connect(v1_db)
    version = conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()[0]
    conn.close()
    assert version == "2"
    bak = v1_db + ".bak"
    assert os.path.exists(bak), "migration must leave a .bak snapshot"
    bak_conn = sqlite3.connect(bak)
    bak_rows = bak_conn.execute("SELECT token FROM tokens ORDER BY id").fetchall()
    bak_conn.close()
    assert [r[0] for r in bak_rows] == ["tok-aaa", "tok-bbb"], "backup keeps v1 plaintext"


def test_migration_rollback_bak_restores_v1_plaintext(v1_db):
    functions.init_db()
    shutil.copy2(v1_db + ".bak", v1_db)
    conn = sqlite3.connect(v1_db)
    rows = conn.execute("SELECT token FROM tokens ORDER BY id").fetchall()
    cols = {r[1] for r in conn.execute("PRAGMA table_info(tokens)").fetchall()}
    conn.close()
    assert [r[0] for r in rows] == ["tok-aaa", "tok-bbb"]
    assert "password_enc" not in cols, "rollback returns the pristine v1 schema"


def test_migration_is_idempotent(v1_db):
    functions.init_db()
    functions.init_db()
    functions.init_db()
    assert functions.get_token(1)["token"] == "tok-aaa"
    assert raw_token_value(v1_db, 1).startswith("enc:v1:")


def test_init_db_fresh_has_v2_columns(fresh_db):
    conn = sqlite3.connect(fresh_db)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(tokens)").fetchall()}
    conn.close()
    assert {"email", "mobile", "area_code", "password_enc", "kind",
            "error_count", "last_probe_at", "last_login_at", "last_error"} <= cols


# ------------------------------------------------------------------------------
# 1.1 — normalization + pure-HTTP login

def test_normalize_mobile_variants():
    assert functions.normalize_mobile("+86 138-0013-8000", "86") == "13800138000"
    assert functions.normalize_mobile("0138-0013-8000") == "13800138000"
    assert functions.normalize_mobile("13800138000") == "13800138000"
    assert functions.normalize_mobile("+8613800138000", "86") == "13800138000"
    with pytest.raises(ValueError):
        functions.normalize_mobile("no-digits-here")


def test_normalize_area_code_defaults_86():
    assert functions.normalize_area_code(None) == "86"
    assert functions.normalize_area_code("+86") == "86"
    assert functions.normalize_area_code("1") == "1"


def test_login_payload_email_shape():
    p = build_login_payload("pw", email="a@b.c")
    assert p == {"password": "pw", "device_id": "deepseek_to_api", "os": "android",
                 "email": "a@b.c"}
    assert "mobile" not in p and "area_code" not in p


def test_login_payload_mobile_shape():
    p = build_login_payload("pw", mobile="+86 138-0013-8000")
    assert p["mobile"] == "13800138000"
    assert p["area_code"] == "86"
    assert p["device_id"] == "deepseek_to_api"
    assert p["os"] == "android"
    assert "email" not in p


def test_login_payload_requires_exactly_one_identifier():
    with pytest.raises(ValueError):
        build_login_payload("pw", email="a@b.c", mobile="13800138000")
    with pytest.raises(ValueError):
        build_login_payload("pw")
    with pytest.raises(ValueError):
        build_login_payload("", email="a@b.c")


LOGIN_OK = {"code": 0, "data": {"biz_code": 0, "biz_msg": "",
                                "biz_data": {"user": {"token": "TOK", "id": "u1"}}}}


def test_login_user_success_unwraps_nested_token():
    calls = []

    async def fake_pwf(path, *, headers, session=None, **kwargs):
        calls.append({"path": path, "json": kwargs.get("json")})
        return FakeResp(200, payload=LOGIN_OK)

    with stub_module(accounts, post_with_failover=fake_pwf):
        result = asyncio.run(login_user("pw", email="a@b.c"))
    assert result == {"token": "TOK", "user_id": "u1"}
    assert calls[0]["path"] == "/api/v0/users/login"
    assert calls[0]["json"]["email"] == "a@b.c"
    assert calls[0]["json"]["device_id"] == "deepseek_to_api"


def test_login_user_bad_credentials_maps_401():
    async def fake_pwf(path, *, headers, session=None, **kwargs):
        return FakeResp(401, text_body="unauthorized")

    with stub_module(accounts, post_with_failover=fake_pwf):
        with pytest.raises(LoginError) as ei:
            asyncio.run(login_user("wrong", email="a@b.c"))
    assert ei.value.code == "bad_credentials"


def test_login_user_5xx_maps_upstream_error():
    async def fake_pwf(path, *, headers, session=None, **kwargs):
        return FakeResp(500, text_body="boom")

    with stub_module(accounts, post_with_failover=fake_pwf):
        with pytest.raises(LoginError) as ei:
            asyncio.run(login_user("pw", email="a@b.c"))
    assert ei.value.code == "upstream_error"


def test_login_user_rejects_wrong_unwrap_shapes():
    bad_payloads = [
        {"code": 1, "msg": "throttled"},
        {"code": 0, "data": {"biz_code": 4001, "biz_msg": "bad password"}},
        {"code": 0, "data": {"biz_code": 0, "biz_data": {"user": {}}}},
        {"code": 0, "data": {"biz_code": 0}},  # no biz_data
    ]
    for payload in bad_payloads:
        async def fake_pwf(path, *, headers, session=None, **kwargs):
            return FakeResp(200, payload=payload)

        with stub_module(accounts, post_with_failover=fake_pwf):
            with pytest.raises(LoginError) as ei:
                asyncio.run(login_user("pw", email="a@b.c"))
        assert ei.value.code == "invalid_response"


def test_login_user_non_json_body_is_invalid_response():
    async def fake_pwf(path, *, headers, session=None, **kwargs):
        return FakeResp(200, payload=None, text_body="<html>waf</html>")

    with stub_module(accounts, post_with_failover=fake_pwf):
        with pytest.raises(LoginError) as ei:
            asyncio.run(login_user("pw", email="a@b.c"))
    assert ei.value.code == "invalid_response"


# ------------------------------------------------------------------------------
# 1.3 — login cooldown limiter

def test_limiter_global_window_flood_is_blocked():
    clock = FakeClock()
    lim = LoginLimiter(global_max=3, global_window=60, now_fn=clock)
    lim.acquire("a"); lim.acquire("b"); lim.acquire("c")
    with pytest.raises(LoginError) as ei:
        lim.acquire("d")
    assert ei.value.code == "cooldown"
    assert ei.value.retry_after > 55
    clock.advance(61)
    lim.acquire("d")  # window slid — allowed again


def test_limiter_per_identifier_backoff_doubles_and_caps():
    clock = FakeClock()
    lim = LoginLimiter(base=5, max_cooldown=300, global_max=1000,
                       global_window=60, now_fn=clock)
    # failure streaks 1..7 -> cooldown 5, 10, 20, 40, 80, 160, capped 300
    expected = [5, 10, 20, 40, 80, 160, 300]
    for streak, exp in enumerate(expected, start=1):
        clock.advance(1000)  # past any previous cooldown
        lim.report_failure("x")
        with pytest.raises(LoginError) as ei:
            lim.acquire("x")
        assert ei.value.code == "cooldown"
        assert exp - 1 <= ei.value.retry_after <= exp + 1


def test_limiter_success_resets_streak():
    clock = FakeClock()
    lim = LoginLimiter(base=5, global_max=1000, global_window=60, now_fn=clock)
    lim.report_failure("x")
    clock.advance(6)
    lim.acquire("x")
    lim.report_success("x")
    lim.acquire("x")  # no cooldown after success
    lim.report_failure("x")
    with pytest.raises(LoginError):
        lim.acquire("x")  # streak restarted from 1 (5s), not from 2 (10s)
    assert lim.cooldown_remaining("x") <= 5.5


# ------------------------------------------------------------------------------
# 1.1 via API — adding accounts by email/mobile, encrypted at rest

def test_add_account_via_api_stores_encrypted_password(fresh_db, clean_global_limiter):
    async def fake_login(password, email=None, mobile=None, area_code=None, session=None):
        assert password == "s3cret"
        return {"token": "fresh-bearer", "user_id": "u9"}

    with stub_module(app, login_user=fake_login):
        status, headers, body = asyncio.run(call_endpoint(
            "POST", "/accounts/add", admin=True,
            json_body={"email": "a@b.c", "password": "s3cret", "alias": "main"},
        ))
    assert status == 201, body
    assert json.loads(body) == {"ok": True, "token_id": 1}
    row = raw_row(fresh_db)
    assert row["kind"] == "login"
    assert row["email"] == "a@b.c"
    assert row["token"].startswith("enc:v1:"), "bearer token must be encrypted at rest"
    assert row["password_enc"].startswith("enc:v1:"), "password must be encrypted at rest"
    assert "s3cret" not in json.dumps(row), "plaintext password must never hit the DB"
    assert functions.get_account_credentials(1)["password"] == "s3cret"
    assert functions.get_token(1)["token"] == "fresh-bearer"


def test_add_account_via_api_mobile_normalizes(fresh_db, clean_global_limiter):
    async def fake_login(password, email=None, mobile=None, area_code=None, session=None):
        return {"token": "t", "user_id": "u"}

    with stub_module(app, login_user=fake_login):
        status, _, body = asyncio.run(call_endpoint(
            "POST", "/accounts/add", admin=True,
            json_body={"mobile": "+86 138-0013-8000", "password": "s3cret"},
        ))
    assert status == 201, body
    row = raw_row(fresh_db)
    assert row["mobile"] == "13800138000"
    assert row["area_code"] == "86"


def test_add_account_validation_errors(fresh_db, clean_global_limiter):
    for payload in (
        {"email": "a@b.c"},                                  # no password
        {"email": "a@b.c", "mobile": "138", "password": "x"},  # both identifiers
        {"password": "x"},                                    # no identifier
    ):
        status, _, _ = asyncio.run(call_endpoint(
            "POST", "/accounts/add", admin=True, json_body=payload))
        assert status == 422, payload


def test_add_account_requires_admin(fresh_db):
    status, _, _ = asyncio.run(call_endpoint(
        "POST", "/accounts/add", json_body={"email": "a@b.c", "password": "x"}))
    assert status == 200  # HTML redirect to /login, not a JSON mutation


def test_add_account_bad_credentials_maps_401(fresh_db, clean_global_limiter):
    async def fake_login(*a, **k):
        raise LoginError("bad_credentials", "Upstream rejected credentials (HTTP 401)")

    with stub_module(app, login_user=fake_login):
        status, _, body = asyncio.run(call_endpoint(
            "POST", "/accounts/add", admin=True,
            json_body={"email": "a@b.c", "password": "wrong"}))
    assert status == 401
    assert json.loads(body)["error"]["code"] == "bad_credentials"
    assert functions.get_tokens() == [], "failed login must not store anything"


def test_add_account_cooldown_maps_429(fresh_db, clean_global_limiter):
    clean_global_limiter.report_failure("a@b.c")
    status, headers, body = asyncio.run(call_endpoint(
        "POST", "/accounts/add", admin=True,
        json_body={"email": "a@b.c", "password": "x"}))
    assert status == 429
    err = json.loads(body)["error"]
    assert err["code"] == "cooldown"
    assert err["retry_after"] > 0
    assert headers.get("retry-after") == "1" or int(headers.get("retry-after", "0")) >= 1


def test_get_tokens_exposes_identifier_but_never_password(fresh_db):
    functions.add_account("tok", password="s3cret", email="a@b.c", alias="main")
    (row,) = functions.get_tokens()
    assert row["identifier"] == "a@b.c"
    assert row["kind"] == "login"
    assert "password" not in row and "password_enc" not in row


# ------------------------------------------------------------------------------
# 1.2 + 1.4 — refresh on 401 and re-login by identifier

def test_refresh_account_token_success_updates_db(fresh_db):
    functions.add_account("old-token", password="pw", email="a@b.c")
    lim = LoginLimiter(global_max=100, global_window=60)

    async def fake_login(password, email=None, mobile=None, area_code=None, session=None):
        assert password == "pw"
        return {"token": "fresh-token", "user_id": "u"}

    with stub_module(accounts, login_user=fake_login):
        assert asyncio.run(refresh_account_token(1, lim)) is True
    row = raw_row(fresh_db)
    assert row["token"].startswith("enc:v1:")
    assert functions.get_token(1)["token"] == "fresh-token"
    assert row["status"] == "ACTIVE"
    assert row["last_login_at"]
    assert lim._failures.get("a@b.c", 0) == 0


def test_refresh_account_token_without_credentials_is_false(fresh_db):
    functions.add_token("pasted-token")
    assert asyncio.run(refresh_account_token(1)) is False


def test_refresh_account_failure_returns_false_and_accrues(fresh_db):
    functions.add_account("old", password="pw", email="a@b.c")
    lim = LoginLimiter(global_max=100, global_window=60)

    async def fake_login(*a, **k):
        raise LoginError("bad_credentials", "nope")

    with stub_module(accounts, login_user=fake_login):
        assert asyncio.run(refresh_account_token(1, lim)) is False
    assert lim._failures.get("a@b.c") == 1
    assert raw_row(fresh_db)["token"].startswith("enc:v1:old") or \
        decrypt_value(raw_row(fresh_db)["token"]) == "old"


def test_re_login_single_by_email_and_mobile(fresh_db):
    functions.add_account("old1", password="pw", email="a@b.c")
    functions.add_account("old2", password="pw", mobile="+86 139-0000-0001")

    async def fake_login(password, email=None, mobile=None, area_code=None, session=None):
        return {"token": f"fresh-for-{email or mobile}", "user_id": "u"}

    with stub_module(accounts, login_user=fake_login):
        by_email = asyncio.run(re_login_single("a@b.c"))
        by_mobile = asyncio.run(re_login_single("0139-0000-0001"))  # trunk zero tolerated
    assert by_email == {"token_id": 1, "user_id": "u"}
    assert by_mobile == {"token_id": 2, "user_id": "u"}
    assert functions.get_token(1)["token"] == "fresh-for-a@b.c"
    assert functions.get_token(2)["token"] == "fresh-for-13900000001"


def test_re_login_single_unknown_identifier_raises_not_found(fresh_db):
    with pytest.raises(LoginError) as ei:
        asyncio.run(re_login_single("nobody@nowhere.io"))
    assert ei.value.code == "not_found"


def test_re_login_single_manual_token_has_no_credentials(fresh_db):
    functions.add_token("pasted")
    # A manual row that DOES match the identifier (e.g. mobile recorded on a
    # pasted token) — heal must refuse because there is no stored password.
    conn = sqlite3.connect(fresh_db)
    conn.execute("UPDATE tokens SET mobile = '13800138000', area_code = '86' WHERE id = 1")
    conn.commit()
    conn.close()
    with pytest.raises(LoginError) as ei:
        asyncio.run(re_login_single("13800138000"))
    assert ei.value.code == "no_credentials"


# ------------------------------------------------------------------------------
# 1.2 wired into handle_chat — the DoD flow: 401 -> exactly one re-login -> success

def make_fake_send(calls, script):
    """send_message stand-in: an async-generator function (called un-awaited)
    whose n-th call raises script[n] when it is an Exception, else yields it."""
    def fake_send(chat_id, auth_token, message, parent_message_id,
                  thinking=False, search=False, file_ids_=None):
        calls.append({"chat_id": chat_id, "token": auth_token})
        idx = len(calls) - 1
        outcome = script[idx] if idx < len(script) else script[-1]

        async def gen():
            if isinstance(outcome, Exception):
                raise outcome
            for chunk in outcome:
                yield chunk
        return gen()
    return fake_send


def stub_handle_chat(script, send_calls, refresh_calls=None, refresh_result=True):
    async def fake_create_new_chat(token):
        return "sess-1"

    async def fake_sig(messages, model, scope=""):
        return "sig-1"

    async def fake_build_prompt(messages, tools, model, is_first, rollover_summary=None):
        return "PROMPT"

    async def fake_extract(messages, token, last_user_only=False):
        return []

    state = {"refreshes": 0}

    async def fake_refresh(token_id, limiter=None):
        state["refreshes"] += 1
        state["token_id"] = token_id
        if refresh_calls is not None:
            refresh_calls.append(token_id)
        if refresh_result:
            functions.update_token_value(token_id, "fresh-token")
        return refresh_result

    swaps = {
        "send_message": make_fake_send(send_calls, script),
        "create_new_chat": fake_create_new_chat,
        "generate_signature": fake_sig,
        "build_prompt": fake_build_prompt,
        "extract_and_upload_files": fake_extract,
        "refresh_account_token": fake_refresh,
    }
    return stub_module(app, **swaps), state


def test_handle_chat_401_relogins_exactly_once_and_succeeds(fresh_db):
    tok_id = functions.add_account("dead-token", password="pw", email="a@b.c")
    script = [Exception("HTTP 401: token expired"), ["hello"]]
    send_calls, refresh_calls = [], []
    ctx, state = stub_handle_chat(script, send_calls, refresh_calls=refresh_calls)

    with ctx:
        resp = asyncio.run(app.handle_chat(
            [{"role": "user", "content": "hi"}], "v4.1flash"))
    # Success returns format_response's plain dict payload (FastAPI serializes
    # it in the route); failures return JSONResponse objects.
    body = json.dumps(resp) if isinstance(resp, dict) else resp.body.decode()
    if not isinstance(resp, dict):
        assert resp.status_code == 200
    assert "hello" in body
    assert state["refreshes"] == 1, "exactly one re-login on 401"
    assert refresh_calls == [tok_id]
    assert len(send_calls) == 2
    assert send_calls[0]["token"] == "dead-token"
    assert send_calls[1]["token"] == "fresh-token", "retry must use the refreshed token"
    assert functions.get_token(tok_id)["status"] == "ACTIVE"


def test_handle_chat_401_refresh_failure_parks_token_and_errors_out(fresh_db):
    tok_id = functions.add_account("dead-token", password="pw", email="a@b.c")
    # Both attempts fail: refresh cannot heal (wrong password), rotation has
    # no better token either -> client gets the upstream error, token parked.
    script = [Exception("HTTP 401: token expired"), Exception("HTTP 401: still dead")]
    send_calls = []
    ctx, state = stub_handle_chat(script, send_calls, refresh_result=False)

    with ctx:
        resp = asyncio.run(app.handle_chat(
            [{"role": "user", "content": "hi"}], "v4.1flash"))
    assert resp.status_code == 401
    assert state["refreshes"] == 1, "refresh attempted once, never in a loop"
    assert functions.get_token(tok_id)["status"] == "RATE_LIMITED", \
        "unhealable token must be parked for pick_token"


def test_handle_chat_401_with_manual_token_parks_without_login(fresh_db):
    functions.add_token("pasted-token")  # manual: no stored credentials
    script = [Exception("HTTP 401: token expired"), Exception("HTTP 401: still dead")]
    send_calls = []

    async def fake_create_new_chat(token):
        return "sess-1"

    # The REAL refresh_account_token — it must return False for manual tokens
    # without ever calling the login endpoint.
    login_attempts = []

    async def spy_login(*a, **k):
        login_attempts.append(a)
        return {"token": "should-not-happen", "user_id": None}

    with stub_module(accounts, login_user=spy_login):
        with stub_module(app,
                         send_message=make_fake_send(send_calls, script),
                         create_new_chat=fake_create_new_chat,
                         refresh_account_token=accounts.refresh_account_token):
            resp = asyncio.run(app.handle_chat(
                [{"role": "user", "content": "hi"}], "v4.1flash"))
    assert resp.status_code == 401
    assert login_attempts == [], "manual tokens must never trigger logins"
    assert functions.get_token(1)["status"] == "RATE_LIMITED"


# ------------------------------------------------------------------------------
# 1.5 — health probe: real create_session attempt, error accrual, auto-recovery

def test_probe_success_resets_error_tally(fresh_db):
    functions.add_token("healthy")
    functions.mark_probe_fail(1, "HTTP 401: x")  # error_count = 1
    async def fake_create_new_chat(token):
        assert token == "healthy"
        return "sess-probe"
    with stub_module(accounts, create_new_chat=fake_create_new_chat):
        assert asyncio.run(probe_account(1)) == "recovered"
    row = raw_row(fresh_db)
    assert row["status"] == "ACTIVE"
    assert row["error_count"] == 0
    assert row["last_probe_at"]
    assert row["last_error"] is None


def test_probe_first_clean_pass_is_ok(fresh_db):
    functions.add_token("healthy")
    async def fake_create_new_chat(token):
        return "sess"
    with stub_module(accounts, create_new_chat=fake_create_new_chat):
        assert asyncio.run(probe_account(1)) == "ok"


def test_probe_401_accrues_then_auto_recovers_via_relogin(fresh_db):
    functions.add_account("dead", password="pw", email="a@b.c")
    lim = LoginLimiter(global_max=100, global_window=60)

    async def failing_create_new_chat(token):
        raise Exception("HTTP 401: token expired")

    async def fake_login(password, email=None, mobile=None, area_code=None, session=None):
        return {"token": "fresh-via-probe", "user_id": "u"}

    with stub_module(accounts, create_new_chat=failing_create_new_chat,
                     login_user=fake_login):
        assert asyncio.run(probe_account(1, lim)) == "degraded"
        assert asyncio.run(probe_account(1, lim)) == "degraded"
        assert asyncio.run(probe_account(1, lim)) == "recovered"
    assert raw_row(fresh_db)["error_count"] == 0, "recovery resets the tally"
    assert functions.get_token(1)["token"] == "fresh-via-probe"
    assert functions.get_token(1)["status"] == "ACTIVE"


def test_probe_401_without_credentials_parks_account_dead(fresh_db):
    functions.add_token("pasted")
    async def failing_create_new_chat(token):
        raise Exception("HTTP 401: token expired")
    with stub_module(accounts, create_new_chat=failing_create_new_chat):
        assert asyncio.run(probe_account(1)) == "degraded"
        assert asyncio.run(probe_account(1)) == "degraded"
        assert asyncio.run(probe_account(1)) == "dead"
    assert functions.get_token(1)["status"] == "RATE_LIMITED"


def test_probe_network_errors_degrade_but_never_park(fresh_db):
    import aiohttp
    functions.add_token("flaky")
    async def flaky_create_new_chat(token):
        raise aiohttp.ClientError("connection reset")
    with stub_module(accounts, create_new_chat=flaky_create_new_chat):
        for _ in range(5):
            assert asyncio.run(probe_account(1)) == "degraded"
    assert functions.get_token(1)["status"] == "ACTIVE"


def test_probe_stale_accounts_skips_fresh_probes(fresh_db):
    functions.add_token("a")
    functions.add_token("b")
    functions.mark_probe_ok(2)  # just probed -> fresh
    probed = []

    async def fake_probe(token_id, limiter=None):
        probed.append(token_id)
        return "ok"

    with stub_module(accounts, probe_account=fake_probe):
        results = asyncio.run(probe_stale_accounts())
    assert probed == [1]
    assert results == [(1, "ok")]


# ------------------------------------------------------------------------------
# 1.7 — dashboard heal button + relogin endpoint

def test_heal_endpoint_relogins_account_by_identifier(fresh_db):
    functions.add_account("dead", password="pw", email="a@b.c")
    seen = []

    async def fake_re_login(identifier, limiter=None):
        seen.append(identifier)
        return {"token_id": 1, "user_id": "u"}

    with stub_module(app, re_login_single=fake_re_login):
        status, headers, body = asyncio.run(call_endpoint(
            "POST", "/tokens/1/heal", admin=True))
    assert status == 200
    assert seen == ["a@b.c"], "heal must target the row's own identifier"
    assert "heal=1" in body.decode() and "result=ok" in body.decode()


def test_heal_endpoint_probes_manual_token(fresh_db):
    functions.add_token("pasted")
    seen = []

    async def fake_probe(token_id, limiter=None):
        seen.append(token_id)
        return "recovered"

    with stub_module(app, probe_account=fake_probe):
        status, _, body = asyncio.run(call_endpoint(
            "POST", "/tokens/1/heal", admin=True))
    assert seen == [1]
    assert "result=recovered" in body.decode()


def test_heal_endpoint_reports_login_failure_code(fresh_db):
    functions.add_account("dead", password="pw", email="a@b.c")

    async def failing(identifier, limiter=None):
        raise LoginError("bad_credentials", "nope")

    with stub_module(app, re_login_single=failing):
        status, _, body = asyncio.run(call_endpoint(
            "POST", "/tokens/1/heal", admin=True))
    assert "result=error-bad_credentials" in body.decode()


def test_relogin_endpoint_accepts_identifier(fresh_db):
    functions.add_account("dead", password="pw", email="a@b.c")

    async def fake_re_login(identifier, limiter=None):
        return {"token_id": 1, "user_id": "u"}

    with stub_module(app, re_login_single=fake_re_login):
        status, _, body = asyncio.run(call_endpoint(
            "POST", "/accounts/relogin", admin=True,
            json_body={"identifier": "a@b.c"}))
    assert status == 200
    assert json.loads(body) == {"ok": True, "token_id": 1}


def test_relogin_endpoint_unknown_identifier_is_404(fresh_db):
    async def failing(identifier, limiter=None):
        raise LoginError("not_found", "no account")

    with stub_module(app, re_login_single=failing):
        status, _, body = asyncio.run(call_endpoint(
            "POST", "/accounts/relogin", admin=True,
            json_body={"identifier": "ghost@x.io"}))
    assert status == 404
    assert json.loads(body)["error"]["code"] == "not_found"


def test_dashboard_renders_account_columns(fresh_db):
    functions.add_account("tok", password="pw", email="a@b.c", alias="main")
    functions.add_token("manual")
    status, _, body = asyncio.run(call_endpoint("GET", "/dashboard", admin=True))
    html = body.decode()
    assert status == 200
    assert "a@b.c" in html, "account identifier visible"
    assert "login" in html and "manual" in html
    assert "s3cret" not in html and "pw<" not in html, "no secrets in the page"
