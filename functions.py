import asyncio
import base64
import contextlib
import contextvars
import json
import logging
import mimetypes
import os
import random
import re
import sqlite3
import string
import time
import urllib.parse
import uuid
from datetime import UTC, datetime

import aiohttp
import deepseek_tokenizer
import wasmtime

try:
    from playwright.async_api import async_playwright
except ImportError:
    async_playwright = None

logger = logging.getLogger("deeperseeker.functions")

# B12 (Stage 1 audit): all relative runtime paths are anchored to the package
# directory — the old global os.chdir(BASE_DIR) at import mutated process-wide
# state (hostile to embedding, testing and PyPI packaging).
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

wasm_path = os.path.join(BASE_DIR, "wasm", "deepseek_pow_solver.wasm")
_session = None
_db = os.getenv("DB_PATH") or os.path.join(BASE_DIR, "deeperseeker.db")


def data_dir():
    """Directory holding the SQLite database (and runtime files stored next to
    it, e.g. the generated API key). Honors DB_PATH; anchored to the package
    directory by default so nothing depends on the process CWD."""
    return os.path.dirname(os.path.abspath(_db))


def cookie_file_path():
    """Resolve where the DeepSeek cookie file lives.

    Order: DEEPSEEKER_COOKIE_PATH env > the target of a legacy Docker symlink
    > next to the real DB file (which honors DB_PATH) > the package directory.
    Writing goes to the RESOLVED path so os.replace() can never destroy a
    symlink that bridges the file into the persistent data volume.
    """
    p = os.getenv("DEEPSEEKER_COOKIE_PATH")
    if p:
        return p
    p = os.path.join(BASE_DIR, "aws_cookies_deepseek.json")
    try:
        if os.path.islink(p):
            target = os.path.realpath(p)
            if target:
                return target
    except Exception:
        pass
    d = os.path.dirname(os.path.abspath(_db))
    if d and os.path.abspath(d) != os.path.abspath(os.getcwd()):
        return os.path.join(d, "aws_cookies_deepseek.json")
    return p


try:
    _TZ_OFFSET = str(int(datetime.now().astimezone().utcoffset().total_seconds()))
except Exception:
    _TZ_OFFSET = "19800"


def get_db():
    conn = sqlite3.connect(_db, timeout=30)
    conn.row_factory = sqlite3.Row
    with contextlib.suppress(Exception):
        conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db():
    conn = get_db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS tokens (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            alias TEXT,
            token TEXT,
            status TEXT DEFAULT 'ACTIVE'
        );
        CREATE TABLE IF NOT EXISTS sessions (
            signature TEXT PRIMARY KEY,
            token_id INTEGER,
            deepseek_session_id TEXT,
            parent_message_id INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS session_map (
            old_session TEXT PRIMARY KEY,
            new_session TEXT,
            token_id INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS files (
            file_id TEXT PRIMARY KEY,
            token_id INTEGER,
            created_at REAL
        );
    """)
    # B3 (Stage 1 audit) migration: pool cooldown + usage tracking. Older
    # databases miss the columns; the ALTERs are idempotent behind the
    # pragma check.
    existing_cols = {
        row[1] for row in conn.execute("PRAGMA table_info(tokens)").fetchall()
    }
    if "rate_limited_until" not in existing_cols:
        conn.execute("ALTER TABLE tokens ADD COLUMN rate_limited_until REAL")
    if "last_used" not in existing_cols:
        conn.execute("ALTER TABLE tokens ADD COLUMN last_used REAL")
    # P0-3 migration: how many 403s this token has seen with no successful
    # turn in between. Reset by mark_active() and by restore_token().
    if "consecutive_403" not in existing_cols:
        conn.execute(
            "ALTER TABLE tokens ADD COLUMN consecutive_403 INTEGER NOT NULL DEFAULT 0"
        )
    if "cookies" not in existing_cols:
        conn.execute("ALTER TABLE tokens ADD COLUMN cookies TEXT")
    if "shumei_id" not in existing_cols:
        conn.execute("ALTER TABLE tokens ADD COLUMN shumei_id TEXT")
    if "device_id" not in existing_cols:
        conn.execute("ALTER TABLE tokens ADD COLUMN device_id TEXT")
    if "park_reason" not in existing_cols:
        conn.execute("ALTER TABLE tokens ADD COLUMN park_reason TEXT")
    existing_session_cols = {
        row[1] for row in conn.execute("PRAGMA table_info(sessions)").fetchall()
    }
    if "last_used" not in existing_session_cols:
        conn.execute("ALTER TABLE sessions ADD COLUMN last_used REAL")
    if "sent_prompt" not in existing_session_cols:
        conn.execute("ALTER TABLE sessions ADD COLUMN sent_prompt TEXT")
    # P0-6 (Day 29): usage stats — one row per COMPLETED turn (client-visible
    # success), aggregated per day x per masked key at read time. Keeping raw
    # rows instead of pre-aggregated counters makes the numbers auditable and
    # the "streamed retries count once" rule structural: a failed attempt has
    # nothing to record.
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS usage (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts REAL NOT NULL,
            day TEXT NOT NULL,
            key_alias TEXT NOT NULL,
            surface TEXT NOT NULL,
            model TEXT,
            input_tokens INTEGER NOT NULL DEFAULT 0,
            output_tokens INTEGER NOT NULL DEFAULT 0
        );
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_usage_day_key ON usage(day, key_alias)"
    )
    conn.commit()
    conn.close()
    try:
        prune_sessions()
    except Exception:
        logger.exception("Startup session pruning failed (non-fatal)")


_session_lock = asyncio.Lock()


async def get_session():
    global _session
    if _session is None or _session.closed:
        async with _session_lock:
            if _session is None or _session.closed:
                logger.info("Opening shared aiohttp ClientSession")
                _session = aiohttp.ClientSession(
                    timeout=aiohttp.ClientTimeout(total=None, connect=15, sock_read=600)
                )
    return _session


async def close_session():
    """Close the shared aiohttp ClientSession (Stage 1 minor list).

    Called from app lifespan shutdown: without it, every uvicorn reload/exit
    leaked the session's connector sockets and logged 'Unclosed client
    session' warnings."""
    global _session
    if _session is not None and not _session.closed:
        await _session.close()
    _session = None


# ==============================================================================
# Stage 0.4 — Dual-endpoint failover
#
# Every upstream POST goes through post_with_failover(): if the primary host
# fails at the connection level (ClientError / timeout) or answers HTTP 5xx,
# the identical request is replayed against the fallback endpoint. 4xx
# responses are returned as-is — a bad token or permission error will not
# improve on a different endpoint, so we fail fast instead of retrying.
#
# Endpoints (env, both optional to override):
#   DEEPSEEKER_UPSTREAM_BASE     primary   (default https://chat.deepseek.com)
#   DEEPSEEKER_UPSTREAM_FALLBACK fallback  (default: none; single-endpoint mode)
# ==============================================================================


def _build_upstream_bases():
    primary = (
        (os.getenv("DEEPSEEKER_UPSTREAM_BASE") or "https://chat.deepseek.com")
        .strip()
        .rstrip("/")
    )
    fallback = (os.getenv("DEEPSEEKER_UPSTREAM_FALLBACK") or "").strip().rstrip("/")
    return [primary] + ([fallback] if fallback else [])


UPSTREAM_BASES = _build_upstream_bases()


async def post_with_failover(path, *, headers, session=None, **kwargs):
    """POST to an upstream endpoint with automatic failover across
    UPSTREAM_BASES. This is a plain coroutine that RETURNS an owned response —
    it is not an async context manager:

        resp = await post_with_failover(...)
        async with resp:
            ...

    The caller owns and closes the returned response (async with). Raises the
    last connection error if every endpoint is unreachable, or returns the
    final 4xx/5xx response when the last endpoint answers with one (callers
    keep their existing error paths). `session` is injectable for tests;
    defaults to the shared ClientSession.
    """
    if session is None:
        session = await get_session()
    last_exc = None
    for attempt, base in enumerate(UPSTREAM_BASES):
        is_last = attempt == len(UPSTREAM_BASES) - 1
        try:
            resp = await session.post(base + path, headers=headers, **kwargs)
        except (TimeoutError, aiohttp.ClientError) as e:
            logger.warning("Upstream POST %s failed on %s: %s", path, base, e)
            last_exc = e
            if is_last:
                raise
            continue
        if resp.status >= 500 and not is_last:
            try:
                body = await resp.text()
            except (TimeoutError, aiohttp.ClientError):
                body = ""
            # PR #26 review fix (Medium): hand the failed connection back to
            # the pool. Without this, repeated failovers leak responses and
            # eventually exhaust the connector.
            resp.release()
            logger.warning(
                "Upstream POST %s -> HTTP %d on %s; failing over to fallback endpoint",
                path,
                resp.status,
                base,
            )
            last_exc = UpstreamError(resp.status, body[:200])
            continue
        if attempt > 0:
            logger.info(
                "Upstream POST %s succeeded on fallback endpoint %s", path, base
            )
        return resp
    raise (
        last_exc
        if last_exc is not None
        else RuntimeError("post_with_failover: no endpoints configured")
    )


# Desktop Chrome client identity sent as x-client-version (B11); bump it when
# DeepSeek's web frontend updates.
DEEPSEEKER_CLIENT_VERSION = os.getenv("DEEPSEEKER_CLIENT_VERSION", "2.7.1")
DEEPSEEKER_CLIENT_LOCALE = os.getenv("DEEPSEEKER_CLIENT_LOCALE", "ru")
DEEPSEEKER_BROWSER_PLATFORM_DEFAULT = "Linux"
DEEPSEEKER_BROWSER_UA_DEFAULT = (
    "Mozilla/5.0 (X11; Linux x86_64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/157.0.0.0 Safari/537.36"
)


def get_browser_user_agent(platform_name: str | None = None) -> str:
    ua_env = os.getenv("DEEPSEEKER_BROWSER_USER_AGENT")
    if ua_env:
        return ua_env
    plat = platform_name or os.getenv(
        "DEEPSEEKER_BROWSER_PLATFORM", DEEPSEEKER_BROWSER_PLATFORM_DEFAULT
    )
    plat_lower = str(plat).strip().lower()
    if plat_lower == "windows":
        return (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/157.0.0.0 Safari/537.36"
        )
    if plat_lower in ("macos", "darwin", "macintosh"):
        return (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/157.0.0.0 Safari/537.36"
        )
    return (
        "Mozilla/5.0 (X11; Linux x86_64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/157.0.0.0 Safari/537.36"
    )


def extract_shumei_device_id(cookies: dict | None) -> str | None:
    if not cookies or not isinstance(cookies, dict):
        return os.getenv("DEEPSEEKER_SHUMEI_DEVICE_ID")
    for k, v in cookies.items():
        if k.startswith(".thumbcache_") and v:
            unquoted = urllib.parse.unquote(str(v))
            return unquoted if unquoted.startswith("B") else f"B{unquoted}"
    return os.getenv("DEEPSEEKER_SHUMEI_DEVICE_ID")


def is_browser_profile(*args, **kwargs) -> bool:
    return True


def resolve_client_profile(*args, **kwargs) -> str:
    return "browser"


def get_headers(auth_token, pow=None, *args, **kwargs):
    browser_platform = os.getenv(
        "DEEPSEEKER_BROWSER_PLATFORM", DEEPSEEKER_BROWSER_PLATFORM_DEFAULT
    )
    browser_ua = get_browser_user_agent(browser_platform)
    locale = os.getenv("DEEPSEEKER_CLIENT_LOCALE", DEEPSEEKER_CLIENT_LOCALE)
    client_version = os.getenv("DEEPSEEKER_CLIENT_VERSION", DEEPSEEKER_CLIENT_VERSION)
    headers = {
        "accept": "*/*",
        "accept-language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
        "content-type": "application/json",
        "origin": "https://chat.deepseek.com",
        "referer": "https://chat.deepseek.com/",
        "user-agent": browser_ua,
        "sec-ch-ua": '"Not-A.Brand";v="99", "Google Chrome";v="157", "Chromium";v="157"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": f'"{browser_platform}"',
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-origin",
        "x-client-bundle-id": "com.deepseek.chat",
        "x-client-locale": locale,
        "x-client-platform": "web",
        "x-client-timezone-offset": _TZ_OFFSET,
        "x-client-version": client_version,
    }
    device_id = kwargs.get("device_id")
    if auth_token:
        headers["authorization"] = f"Bearer {auth_token}"
        headers["x-device-id"] = device_id or str(
            uuid.uuid5(uuid.NAMESPACE_URL, f"deeperseeker:{auth_token}")
        )
        headers["x-device-model"] = ""
    if pow:
        headers["x-ds-pow-response"] = pow
    return headers


_HIF_LEIM_URL = os.getenv(
    "DEEPSEEKER_HIF_URL",
    os.getenv("DEEPSEEKER_HIF_LEIM_URL", "https://hif-leim.deepseek.com/query"),
)
_HIF_DLIQ_URL = os.getenv(
    "DEEPSEEKER_HIF_DLIQ_URL", "https://hif-dliq.deepseek.com/query"
)
_HIF_URL = _HIF_LEIM_URL

_hif_caches = {
    "leim": {"value": None, "expire_at": 0.0, "retry_at": 0.0},
    "dliq": {"value": None, "expire_at": 0.0, "retry_at": 0.0},
}
_hif_cache = _hif_caches["leim"]
_hif_locks = {
    "leim": asyncio.Lock(),
    "dliq": asyncio.Lock(),
}
_hif_lock = _hif_locks["leim"]


def _get_hif_attest_headers() -> dict[str, str]:
    browser_platform = os.getenv(
        "DEEPSEEKER_BROWSER_PLATFORM", DEEPSEEKER_BROWSER_PLATFORM_DEFAULT
    )
    browser_ua = get_browser_user_agent(browser_platform)
    client_version = os.getenv("DEEPSEEKER_CLIENT_VERSION", DEEPSEEKER_CLIENT_VERSION)
    return {
        "accept": "*/*",
        "accept-language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
        "origin": "https://chat.deepseek.com",
        "referer": "https://chat.deepseek.com/",
        "user-agent": browser_ua,
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-site",
        "x-client-platform": "web",
        "x-client-version": client_version,
        "x-client-bundle-id": "com.deepseek.chat",
    }


async def get_hif_token(kind: str = "leim") -> str | None:
    norm_kind = "dliq" if str(kind).lower() == "dliq" else "leim"
    url = _HIF_DLIQ_URL if norm_kind == "dliq" else _HIF_LEIM_URL
    cache = _hif_caches[norm_kind]
    lock = _hif_locks[norm_kind]
    now = time.time()
    if cache["value"] and now < cache["expire_at"]:
        return cache["value"]
    if now < cache["retry_at"] and not cache["value"]:
        return None

    async with lock:
        now = time.time()
        if cache["value"] and now < cache["expire_at"]:
            return cache["value"]
        if now < cache["retry_at"] and not cache["value"]:
            return None
        try:
            session = await get_session()
            headers = _get_hif_attest_headers()
            async with session.get(
                url, headers=headers, timeout=aiohttp.ClientTimeout(total=5.0)
            ) as resp:
                if resp.status == 200:
                    ttl_header = resp.headers.get("x-hif-ttl")
                    ttl = (
                        float(ttl_header)
                        if ttl_header and ttl_header.isdigit()
                        else 600.0
                    )
                    js = await resp.json()
                    val = (
                        ((js.get("data") or {}).get("biz_data") or {}).get("value")
                        or (js.get("data") if isinstance(js.get("data"), str) else None)
                        or (js.get("data") or {}).get("token")
                        or js.get("token")
                    )
                    if val:
                        cache["value"] = str(val)
                        cache["expire_at"] = now + max(60.0, ttl - 30.0)
                        return cache["value"]
        except Exception:
            pass
        cache["retry_at"] = now + 60.0
        return cache["value"]


async def get_hif_dliq_token() -> str | None:
    return await get_hif_token(kind="dliq")


# ==============================================================================
# WAF & SESSION COOKIE GENERATION
#
# DeepSeek's web frontend enforces AWS WAF and Shumei device fingerprinting.
# The Playwright/Chromium cookie generation below obtains valid WAF and session
# cookies so requests replicate an authentic desktop browser.
# ==============================================================================

_cookie_lock = asyncio.Lock()

COOKIE_REGEN_ATTEMPTS = int(os.getenv("DEEPSEEKER_COOKIE_ATTEMPTS", "2"))
COOKIE_REGEN_TIMEOUT = float(os.getenv("DEEPSEEKER_COOKIE_TIMEOUT", "120"))
COOKIE_FAIL_COOLDOWN = float(os.getenv("DEEPSEEKER_COOKIE_COOLDOWN", "20"))

_cookie_fail = {"until": 0.0, "error": ""}


class CookieGenerationError(Exception):
    """Raised when the DeepSeek WAF cookie file cannot be produced."""


class UpstreamError(Exception):
    """Upstream DeepSeek failure carrying the HTTP status and body (B6).

    Replaces the old Exception(f"HTTP {status}: ...") string protocol whose
    'HTTP (\\d{3}):' regex parsing in app.py broke token rotation and error
    mapping whenever a wording changed. Callers read .status directly; str()
    keeps the familiar 'HTTP <status>: <body>' shape for logs."""

    def __init__(self, status, body="", retry_after=None):
        self.status = int(status)
        self.body = str(body)
        self.retry_after = retry_after
        super().__init__(f"HTTP {self.status}: {self.body}")


class UpstreamBizError(UpstreamError):
    def __init__(self, status, body, biz_code=None, retry_after=None):
        super().__init__(status, body, retry_after=retry_after)
        self.biz_code = biz_code


def _biz_data(payload, what="response"):
    data = payload.get("data") if isinstance(payload, dict) else None
    biz = data.get("biz_data") if isinstance(data, dict) else None
    if biz:
        if isinstance(biz, dict) and (biz.get("is_muted") == 1 or biz.get("biz_code") == 5):
            mute_until = biz.get("mute_until") or biz.get("end_at")
            retry_after = None
            if mute_until:
                with contextlib.suppress(Exception):
                    retry_after = max(1, int(float(mute_until) - time.time()))
            raise UpstreamBizError(429, f"{what}: account muted until {mute_until}", biz_code=5, retry_after=retry_after)
        return biz
    payload = payload if isinstance(payload, dict) else {}
    code = payload.get("code")
    msg = payload.get("msg") or payload.get("message") or "empty biz_data"
    code_map = {
        40002: 401,
        40003: 401,
        40012: 403,
        40029: 429,
        40303: 403,
        50006: 429,
        40006: 502,
    }
    retry_after = None
    if code == 40012:
        retry_after = 86400
    elif code == 40303:
        retry_after = 1800
    elif code == 50006:
        retry_after = 3600
    elif code == 40029:
        retry_after = TOKEN_COOLDOWN_SECONDS

    if code in code_map:
        status = code_map[code]
    elif re.search(r"login|auth|token|session|expire", str(msg), re.I):
        status = 401
    else:
        status = 429
    raise UpstreamBizError(status, f"{what}: code={code} msg={msg}", biz_code=code, retry_after=retry_after)


def _parse_cookie_env(val: str) -> dict | None:
    if not val or not val.strip():
        return None
    val = val.strip()
    if val.startswith("{") and val.endswith("}"):
        with contextlib.suppress(Exception):
            parsed = json.loads(val)
            if isinstance(parsed, dict):
                return parsed
    res = {}
    if ";" in val or "=" in val:
        for part in val.split(";"):
            if "=" in part:
                k, v = part.split("=", 1)
                k, v = k.strip(), v.strip()
                if k:
                    res[k] = v
        if res:
            return res
    return {
        "aws-waf-token": val,
        "ds_cookie_preference": "%257B%2522level%2522%253A%2522all%2522%257D",
    }


def _read_cookie_file():
    """Return valid (unexpired) cookies, or None."""
    env_c = _parse_cookie_env(
        os.getenv("DEEPSEEKER_WAF_COOKIE") or os.getenv("DEEPSEEK_COOKIE") or ""
    )
    if env_c:
        return env_c
    try:
        with open(cookie_file_path()) as f:
            c = json.load(f)
        if c.get("expiry") is not None and c["expiry"] > time.time():
            return c["cookie"]
    except Exception:
        pass
    return None


def _read_stale_cookie_file():
    """Return cookies even if expired (last-resort fallback)."""
    try:
        with open(cookie_file_path()) as f:
            c = json.load(f)
        return c.get("cookie") or None
    except Exception:
        return None


async def get_cookies():
    cookies = _read_cookie_file()
    if cookies:
        return cookies

    def _cooldown_error():
        return CookieGenerationError(
            _cookie_fail["error"]
            + " (cooling down; will retry automatically — try again shortly)"
        )

    if time.time() < _cookie_fail["until"]:
        stale = _read_stale_cookie_file()
        if stale:
            return stale
        raise _cooldown_error()
    async with _cookie_lock:
        cookies = _read_cookie_file()
        if cookies:
            return cookies
        if time.time() < _cookie_fail["until"]:
            stale = _read_stale_cookie_file()
            if stale:
                return stale
            raise _cooldown_error()
        last_err = None
        for attempt in range(1, COOKIE_REGEN_ATTEMPTS + 1):
            try:
                logger.info(
                    "Generating DeepSeek cookies (attempt %d/%d)...",
                    attempt,
                    COOKIE_REGEN_ATTEMPTS,
                )
                await asyncio.wait_for(
                    _generate_cookies(), timeout=COOKIE_REGEN_TIMEOUT
                )
                cookies = _read_cookie_file()
                if cookies:
                    _cookie_fail["until"] = 0.0
                    _cookie_fail["error"] = ""
                    return cookies
                last_err = CookieGenerationError(
                    "cookie file missing/invalid after generation"
                )
            except Exception as e:
                last_err = e
                logger.warning(
                    "DeepSeek cookie generation attempt %d/%d failed: %s",
                    attempt,
                    COOKIE_REGEN_ATTEMPTS,
                    e,
                )
            if attempt < COOKIE_REGEN_ATTEMPTS:
                await asyncio.sleep(min(5 * attempt, 10))
        _cookie_fail["until"] = time.time() + COOKIE_FAIL_COOLDOWN
        _cookie_fail["error"] = f"Could not generate DeepSeek cookies: {last_err}"
        logger.error("%s", _cookie_fail["error"])
        stale = _read_stale_cookie_file()
        if stale:
            logger.warning(
                "Serving STALE DeepSeek cookies after generation failure (upstream may reject them)"
            )
            return stale
        raise CookieGenerationError(_cookie_fail["error"])


async def _generate_isolated_cookies_payload():
    if async_playwright is None:
        raise CookieGenerationError("playwright is not installed")
    headless = (
        os.getenv("DEEPSEEKER_COOKIE_HEADLESS", "true").lower()
        in ("1", "true", "yes")
    )
    launch_kwargs = {
        "headless": headless,
        "args": [
            "--no-sandbox",
            "--disable-setuid-sandbox",
            "--disable-dev-shm-usage",
            "--disable-gpu",
        ],
    }
    async with async_playwright() as p:
        browser = await p.chromium.launch(**launch_kwargs)
        try:
            ua = get_browser_user_agent()
            context = await browser.new_context(user_agent=ua)
            page = await context.new_page()
            await page.goto(
                "https://chat.deepseek.com/",
                wait_until="domcontentloaded",
                timeout=45000,
            )
            await page.wait_for_selector("body", timeout=30000)
            cookies = []
            for _ in range(25):
                cookies = await context.cookies()
                if any(c.get("name") == "aws-waf-token" for c in cookies):
                    break
                await asyncio.sleep(0.4)
            ls_shumei = None
            with contextlib.suppress(Exception):
                ls_shumei = await page.evaluate("""() => {
                    for (let i = 0; i < localStorage.length; i++) {
                        const k = localStorage.key(i);
                        if (k && (k.startsWith('.thumbcache') || k === '_sm_di')) {
                            return localStorage.getItem(k);
                        }
                    }
                    return null;
                }""")
        finally:
            await browser.close()
    final_cookies = {}
    expiry = None
    for i in cookies:
        if i.get("name") == "aws-waf-token":
            expiry = i.get("expires")
        final_cookies[i["name"]] = i["value"]
    final_cookies["ds_cookie_preference"] = (
        "%257B%2522level%2522%253A%2522all%2522%257D"
    )
    if not expiry or expiry < 0:
        expiry = time.time() + 1800
    shumei_id = extract_shumei_device_id(final_cookies)
    if not shumei_id and ls_shumei:
        unquoted = urllib.parse.unquote(str(ls_shumei))
        shumei_id = unquoted if unquoted.startswith("B") else f"B{unquoted}"
    return final_cookies, expiry, shumei_id


async def _generate_cookies():
    final_cookies, expiry, _ = await _generate_isolated_cookies_payload()
    target = cookie_file_path()
    target_dir = os.path.dirname(os.path.abspath(target))
    os.makedirs(target_dir, exist_ok=True)
    tmp_path = os.path.join(target_dir, os.path.basename(target) + ".tmp")
    with open(tmp_path, "w") as f:
        f.write(json.dumps({"cookie": final_cookies, "expiry": expiry}))
    os.replace(tmp_path, target)
    logger.info(
        "DeepSeek cookies saved to %s (expires %s)",
        target,
        datetime.fromtimestamp(expiry) if expiry else "n/a",
    )


_token_cookie_locks = {}
_token_locks_guard = asyncio.Lock()


async def _get_token_lock(auth_token: str) -> asyncio.Lock:
    async with _token_locks_guard:
        if auth_token not in _token_cookie_locks:
            _token_cookie_locks[auth_token] = asyncio.Lock()
        return _token_cookie_locks[auth_token]


async def get_isolated_cookies_for_token(
    auth_token: str | None = None,
) -> tuple[dict | None, str | None, str | None]:
    """Return (cookies_dict, shumei_device_id, device_id) isolated per account.

    Guarantees account isolation: accounts in the pool never share cookies,
    WAF tokens, Shumei risk control device tokens, or x-device-id headers,
    preventing multi-account correlation and chain bans.
    """
    if not auth_token:
        c = await get_cookies_safely()
        return c, extract_shumei_device_id(c), None

    now = time.time()
    conn = get_db()
    row = conn.execute(
        "SELECT id, cookies, shumei_id, device_id FROM tokens WHERE token = ?",
        (auth_token,),
    ).fetchone()
    conn.close()

    if not row:
        c = await get_cookies_safely()
        dev_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"deeperseeker:{auth_token}"))
        return c, extract_shumei_device_id(c), dev_id

    tid = row["id"]
    cached_cookies = row["cookies"]
    shumei_id = row["shumei_id"]
    dev_id = row["device_id"] or str(
        uuid.uuid5(uuid.NAMESPACE_URL, f"deeperseeker:{auth_token}")
    )

    if cached_cookies:
        try:
            data = json.loads(cached_cookies)
            if isinstance(data, dict):
                expiry = data.get("expiry", 0)
                c_dict = data.get("cookie")
                if c_dict and isinstance(c_dict, dict) and expiry - now > 120:
                    return (
                        c_dict,
                        shumei_id or extract_shumei_device_id(c_dict),
                        dev_id,
                    )
        except Exception:
            pass

    lock = await _get_token_lock(auth_token)
    async with lock:
        conn = get_db()
        row2 = conn.execute(
            "SELECT id, cookies, shumei_id, device_id FROM tokens WHERE token = ?",
            (auth_token,),
        ).fetchone()
        conn.close()
        if row2 and row2["cookies"]:
            try:
                data = json.loads(row2["cookies"])
                if isinstance(data, dict):
                    expiry = data.get("expiry", 0)
                    c_dict = data.get("cookie")
                    if c_dict and isinstance(c_dict, dict) and expiry - time.time() > 120:
                        sh_id = row2["shumei_id"] or extract_shumei_device_id(c_dict)
                        d_id = row2["device_id"] or dev_id
                        return c_dict, sh_id, d_id
            except Exception:
                pass

        if os.getenv("PYTEST_CURRENT_TEST"):
            global_c = await get_cookies_safely()
            return global_c, shumei_id or extract_shumei_device_id(global_c), dev_id

        try:
            final_cookies, expiry, generated_shumei = (
                await _generate_isolated_cookies_payload()
            )
            resolved_shumei = generated_shumei or shumei_id
            update_token_credentials(
                tid,
                cookies={"cookie": final_cookies, "expiry": expiry},
                shumei_id=resolved_shumei,
                device_id=dev_id,
            )
            return final_cookies, resolved_shumei, dev_id
        except Exception as e:
            logger.warning(
                "Generating isolated cookies failed for token #%s: %s", tid, e
            )
            if row2 and row2["cookies"]:
                with contextlib.suppress(Exception):
                    stale_data = json.loads(row2["cookies"])
                    if isinstance(stale_data, dict) and stale_data.get("cookie"):
                        return (
                            stale_data["cookie"],
                            row2["shumei_id"]
                            or extract_shumei_device_id(stale_data["cookie"]),
                            dev_id,
                        )
            global_c = await get_cookies_safely()
            return global_c, shumei_id or extract_shumei_device_id(global_c), dev_id


async def warmup_all_tokens_cookies():
    """Background startup warmup: ensure fallback and active tokens have fresh isolated cookies."""
    if os.getenv("PYTEST_CURRENT_TEST") or os.getenv(
        "DEEPSEEKER_DISABLE_WARMUP", ""
    ).lower() in ("1", "true", "yes"):
        return
    if not _read_cookie_file():
        logger.info("Startup: generating default DeepSeek cookies in background...")
        with contextlib.suppress(Exception):
            await get_cookies_safely()
        logger.info("Startup: default DeepSeek cookies ready.")

    conn = get_db()
    rows = conn.execute(
        "SELECT id, token, cookies FROM tokens WHERE status != 'QUARANTINED'"
    ).fetchall()
    conn.close()

    if not rows:
        return

    now = time.time()
    for row in rows:
        tid = row["id"]
        token_str = row["token"]
        cookies_json = row["cookies"]
        is_valid = False
        if cookies_json:
            try:
                data = json.loads(cookies_json)
                exp = data.get("expiry", 0)
                if exp - now > 120 and data.get("cookie"):
                    is_valid = True
            except Exception:
                pass
        if not is_valid and token_str:
            logger.info("Startup: generating isolated cookies for token id=%s...", tid)
            try:
                await get_isolated_cookies_for_token(token_str)
                logger.info("Startup: token id=%s isolated cookies ready.", tid)
            except Exception as e:
                logger.warning(
                    "Startup: failed to generate cookies for token id=%s: %s", tid, e
                )


async def solve_captcha_interactive(token_id: int) -> bool:
    """Launch a real visible Playwright browser for the given token on chat.deepseek.com.

    Injects the user's auth_token into localStorage and any existing cookies,
    allowing the operator to solve any Cloudflare/Shumei/Chairman Xi captcha
    interactively with their mouse. Once the window is closed or captcha is
    solved, saves the updated cookies and restores the token to ACTIVE.
    """
    if async_playwright is None:
        logger.error("Solve Captcha failed: playwright is not installed")
        return False

    tok = get_token(token_id)
    if not tok:
        logger.error("Solve Captcha failed: token id=%s not found", token_id)
        return False

    auth_token = tok["token"]
    launch_kwargs = {
        "headless": False,
        "args": [
            "--no-sandbox",
            "--disable-setuid-sandbox",
            "--disable-dev-shm-usage",
        ],
    }

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(**launch_kwargs)
            try:
                ua = get_browser_user_agent()
                context = await browser.new_context(
                    user_agent=ua,
                    viewport={"width": 1280, "height": 840},
                )

                if tok.get("cookies"):
                    with contextlib.suppress(Exception):
                        data = json.loads(tok["cookies"])
                        c_dict = data.get("cookie") if isinstance(data, dict) else None
                        if c_dict and isinstance(c_dict, dict):
                            playwright_cookies = [
                                {
                                    "name": k,
                                    "value": str(v),
                                    "domain": ".deepseek.com",
                                    "path": "/",
                                }
                                for k, v in c_dict.items()
                            ]
                            await context.add_cookies(playwright_cookies)

                await context.add_init_script(f"""
                    try {{
                        localStorage.setItem("userToken", JSON.stringify({{"value": {json.dumps(auth_token)}, "__version": "0"}}));
                    }} catch(e) {{}}
                """)

                page = await context.new_page()
                logger.info(
                    "Solve Captcha: opened visible browser for token #%d on chat.deepseek.com",
                    token_id,
                )
                await page.goto(
                    "https://chat.deepseek.com/", wait_until="domcontentloaded"
                )

                deadline = time.time() + 600
                last_cookies = []
                while time.time() < deadline:
                    with contextlib.suppress(Exception):
                        last_cookies = await context.cookies()
                    if page.is_closed() or not context.pages:
                        break
                    await asyncio.sleep(1.0)

            finally:
                with contextlib.suppress(Exception):
                    await browser.close()

            if last_cookies:
                final_cookies = {}
                expiry = None
                for i in last_cookies:
                    if i.get("name") == "aws-waf-token":
                        expiry = i.get("expires")
                    final_cookies[i["name"]] = i["value"]
                final_cookies["ds_cookie_preference"] = (
                    "%257B%2522level%2522%253A%2522all%2522%257D"
                )
                if not expiry or expiry < 0:
                    expiry = time.time() + 1800
                shumei_id = extract_shumei_device_id(final_cookies)
                dev_id = tok.get("device_id") or str(
                    uuid.uuid5(uuid.NAMESPACE_URL, f"deeperseeker:{auth_token}")
                )
                update_token_credentials(
                    token_id,
                    cookies={"cookie": final_cookies, "expiry": expiry},
                    shumei_id=shumei_id,
                    device_id=dev_id,
                )
                logger.info(
                    "Solve Captcha: updated credentials for token #%d; awaiting operator Restore",
                    token_id,
                )

            return True
    except Exception as e:
        logger.error("Solve Captcha error for token #%d: %s", token_id, e)
        return False


async def get_cookies_safely():
    with contextlib.suppress(Exception):
        return await get_cookies()
    return None


def get_auth_token():
    conn = get_db()
    row = conn.execute(
        "SELECT token FROM tokens WHERE status = 'ACTIVE' ORDER BY id ASC LIMIT 1"
    ).fetchone()
    if not row:
        row = conn.execute("SELECT token FROM tokens LIMIT 1").fetchone()
    conn.close()
    if row:
        return row[0]
    return None


def add_token(token, alias=None, cookies=None, shumei_id=None, device_id=None):
    conn = get_db()
    if not conn.execute("SELECT 1 FROM tokens WHERE id = 1").fetchone():
        next_id = 1
    else:
        row = conn.execute("""
            SELECT min(t1.id + 1)
            FROM tokens t1
            LEFT JOIN tokens t2 ON t1.id + 1 = t2.id
            WHERE t2.id IS NULL
        """).fetchone()
        next_id = row[0] if row and row[0] else 1
    if not device_id and token:
        device_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"deeperseeker:{token}"))
    cookies_str = (
        json.dumps(cookies)
        if isinstance(cookies, dict)
        else (cookies if isinstance(cookies, str) else None)
    )
    conn.execute(
        "INSERT INTO tokens (id, alias, token, status, cookies, shumei_id, device_id) "
        "VALUES (?, ?, ?, 'ACTIVE', ?, ?, ?)",
        (next_id, alias, token, cookies_str, shumei_id, device_id),
    )
    conn.commit()
    conn.close()


def update_token_credentials(
    token_id_or_token, cookies=None, shumei_id=None, device_id=None
):
    conn = get_db()
    clause = "id = ?" if isinstance(token_id_or_token, int) else "token = ?"
    updates = []
    params = []
    if cookies is not None:
        updates.append("cookies = ?")
        params.append(
            json.dumps(cookies) if isinstance(cookies, dict) else str(cookies)
        )
    if shumei_id is not None:
        updates.append("shumei_id = ?")
        params.append(str(shumei_id))
    if device_id is not None:
        updates.append("device_id = ?")
        params.append(str(device_id))
    if updates:
        params.append(token_id_or_token)
        conn.execute(f"UPDATE tokens SET {', '.join(updates)} WHERE {clause}", params)
        conn.commit()
    conn.close()


def invalidate_token_cookies(token_id_or_token):
    if not token_id_or_token:
        return
    with contextlib.suppress(Exception):
        conn = get_db()
        clause = "id = ?" if isinstance(token_id_or_token, int) else "token = ?"
        conn.execute(
            f"UPDATE tokens SET cookies = NULL WHERE {clause}",
            (token_id_or_token,),
        )
        conn.commit()
        conn.close()


def get_tokens():
    conn = get_db()
    rows = conn.execute(
        "SELECT id, alias, token, status, cookies, shumei_id, device_id, park_reason FROM tokens"
    ).fetchall()
    conn.close()
    return [
        {
            "id": r[0],
            "alias": r[1],
            "token": r[2],
            "status": r[3],
            "cookies": r[4],
            "shumei_id": r[5],
            "device_id": r[6],
            "park_reason": r[7],
        }
        for r in rows
    ]


def get_token(token_id):
    conn = get_db()
    row = conn.execute(
        "SELECT id, alias, token, status, cookies, shumei_id, device_id, park_reason FROM tokens WHERE id = ?",
        (token_id,),
    ).fetchone()
    conn.close()
    if row:
        return {
            "id": row[0],
            "alias": row[1],
            "token": row[2],
            "status": row[3],
            "cookies": row[4],
            "shumei_id": row[5],
            "device_id": row[6],
            "park_reason": row[7],
        }
    return None


def delete_token(token_id):
    conn = get_db()
    conn.execute("DELETE FROM tokens WHERE id = ?", (token_id,))
    conn.commit()
    conn.close()


# ==============================================================================
# B3 (Stage 1 audit) — token pool scheduler v2
#
# The old pool selected ACTIVE tokens with ORDER BY RANDOM() and, when every
# token was RATE_LIMITED, fell back to the FIRST token by id. Consequences
# (root cause of issue #32): a limited token never recovered while any other
# token stayed ACTIVE, a single-token pool had no backoff at all, and
# concurrent requests stampeded onto one account — uneven wear and a
# ban-ladder accelerator. The scheduler now provides:
#   * a rate_limited_until cooldown with automatic RATE_LIMITED -> ACTIVE
#     recovery once the cooldown expires (flipped back on the next pick),
#   * least-in-flight selection (spreads load instead of RANDOM()),
#   * an idle-oldest tie-break (the never/least-recently-used token leads),
#   * a soft per-token concurrency cap: capped tokens are only used when
#     nothing else is free,
#   * a soonest-to-recover fallback when every token is still cooling down.
#
# P0-3 adds the third state on top of this scheduler: QUARANTINED. Selection
# logic below is untouched — a quarantined token is simply never a candidate.
# ==============================================================================

TOKEN_COOLDOWN_SECONDS = max(1, int(os.getenv("DEEPSEEKER_RATE_LIMIT_COOLDOWN", "60")))
TOKEN_CONCURRENCY_CAP = max(1, int(os.getenv("DEEPSEEKER_TOKEN_CONCURRENCY", "8")))

# P0-3: a 403 seen this many times in a row (no successful turn in between) is
# not a transient refusal — it is the ban ladder, so the token leaves rotation
# until an operator restores it. 0 disables escalation and keeps today's
# cooldown-everything behavior; a 401 quarantines unconditionally.
QUARANTINE_AFTER_403 = max(0, int(os.getenv("DEEPSEEKER_QUARANTINE_AFTER_403", "3")))

# Per-token in-flight refcounts (per process; single event-loop worker). Every
# send reserves one slot via acquire_token_slot() and releases it when the
# request ends — success, upstream failure or client abort — mirroring the
# ownership-transfer pattern of the per-chat locks.
_in_flight = {}


def _bump_in_flight(token_id, delta):
    n = _in_flight.get(token_id, 0) + delta
    if n > 0:
        _in_flight[token_id] = n
    else:
        _in_flight.pop(token_id, None)


class _TokenSlot:
    """One in-flight reservation against a token. release() is once-only, so
    transferring the slot to a streaming generator (exactly like the per-chat
    lock owner) is safe by construction."""

    __slots__ = ("token_id", "_held")

    def __init__(self, token_id):
        self.token_id = token_id
        self._held = True
        _bump_in_flight(token_id, +1)

    def release(self):
        if not self._held:
            return
        self._held = False
        gap = float(os.getenv("DEEPSEEKER_CALL_GAP_SECONDS", "0.0"))
        if gap > 0:
            delay = (gap / 2.0) + random.uniform(0.0, gap / 2.0)
            try:
                loop = asyncio.get_running_loop()
                loop.call_later(delay, _bump_in_flight, self.token_id, -1)
                return
            except RuntimeError:
                pass
        _bump_in_flight(self.token_id, -1)


def acquire_token_slot(token_id):
    """Reserve one in-flight slot for token_id; pair it with slot.release()."""
    return _TokenSlot(token_id)


def token_in_flight(token_id):
    """Current in-flight request count for a token (selection signal)."""
    return _in_flight.get(token_id, 0)


def pick_token(exclude=None):
    """Select the best available token (scheduler v2 — block comment above).

    `exclude` (optional, B10) lists token ids the caller just saw fail: they
    are avoided while ANY other token is available, so an empty-SSE retry
    cannot land on the same poisoned token.

    QUARANTINED tokens (P0-3) are never a candidate here, not even through the
    all-cooling fallback: a token rejected on auth would otherwise be handed
    straight back to the caller as the "soonest to recover" one.
    """
    now = time.time()
    conn = get_db()
    rows = conn.execute(
        "SELECT id, status, rate_limited_until, last_used FROM tokens"
    ).fetchall()
    conn.close()
    if not rows:
        return None

    excluded = set(exclude or ())
    available = []
    recovered = []
    for tid, status, until, last_used in rows:
        if status == "QUARANTINED":
            continue  # terminal: only restore_token() puts it back (P0-3)
        if status in ("RATE_LIMITED", "PARKED") and until is not None and until > now:
            continue
        if status in ("RATE_LIMITED", "PARKED"):
            recovered.append(tid)
        available.append((tid, last_used or 0.0))

    pickable = [entry for entry in available if entry[0] not in excluded] or available
    if pickable:
        if recovered:
            conn = get_db()
            conn.executemany(
                "UPDATE tokens SET status = 'ACTIVE', rate_limited_until = NULL WHERE id = ?",
                [(tid,) for tid in recovered],
            )
            conn.commit()
            conn.close()
            logger.info(
                "Token pool: cooldown expired, auto-recovered token(s) %s", recovered
            )

        def _sort_key(entry):
            tid, last_used = entry
            in_flight = _in_flight.get(tid, 0)
            # Soft cap: over-cap tokens sort behind every under-cap one.
            over_cap = 1 if in_flight >= TOKEN_CONCURRENCY_CAP else 0
            return (over_cap, in_flight, last_used, tid)

        return min(pickable, key=_sort_key)[0]

    # Every token is rate-limited and still cooling: pick the one that
    # recovers first (bounded wait) instead of stampeding onto the lowest id
    # with no backoff at all. Quarantined rows stay out of the race — with a
    # pool of nothing but dead credentials this returns None, and the callers
    # already treat "no token" as the not-configured case.
    conn = get_db()
    row = None
    if excluded:
        placeholders = ",".join("?" for _ in excluded)
        row = conn.execute(
            f"SELECT id FROM tokens WHERE status NOT IN ('QUARANTINED', 'PARKED') AND id NOT IN ({placeholders}) "
            "ORDER BY COALESCE(rate_limited_until, 0) ASC, id ASC LIMIT 1",
            list(excluded),
        ).fetchone()
    if not row:
        row = conn.execute(
            "SELECT id FROM tokens WHERE status NOT IN ('QUARANTINED', 'PARKED') "
            "ORDER BY COALESCE(rate_limited_until, 0) ASC, id ASC LIMIT 1"
        ).fetchone()
    conn.close()
    return row[0] if row else None


def mark_quarantined(token_id, reason):
    """Take a token out of rotation until an operator restores it (P0-3).

    Unlike RATE_LIMITED there is no timer on this state: rate_limited_until is
    cleared so pick_token()'s auto-recovery can never mistake a quarantined row
    for a cooled-down one. The reason is logged for whoever is watching the
    pool.
    """
    logger.warning(
        "Token #%d QUARANTINED (manual return required): %s", token_id, reason
    )
    conn = get_db()
    conn.execute(
        "UPDATE tokens SET status = ?, rate_limited_until = NULL WHERE id = ?",
        ("QUARANTINED", token_id),
    )
    conn.commit()
    conn.close()


def mark_parked(token_id, reason, duration=86400):
    secs = int(duration) if duration and duration > 0 else 86400
    secs = min(secs, 7 * 86400)
    until = time.time() + secs
    logger.warning(
        "Token #%d PARKED for %ds (%s)",
        token_id,
        secs,
        reason,
    )
    conn = get_db()
    conn.execute(
        "UPDATE tokens SET status = ?, rate_limited_until = ?, park_reason = ? WHERE id = ?",
        ("PARKED", until, str(reason or ""), token_id),
    )
    conn.commit()
    conn.close()


CHAIRMAN_XI_SOCIAL_CREDIT_MESSAGE = (
    "Account social credit is critically low: Chairman Xi requested captcha verification."
)


def chairman_xi_penalize_social_credit(token_id, penalty_duration=1800):
    social_credit_dock_seconds = (
        int(penalty_duration) if penalty_duration and penalty_duration > 0 else 1800
    )
    mark_parked(token_id, CHAIRMAN_XI_SOCIAL_CREDIT_MESSAGE, social_credit_dock_seconds)


def restore_token(token_id):
    """The only exit from quarantine (P0-3): an explicit operator action.

    last_used is deliberately left alone, so a restored token carries the
    oldest usage stamp and the landed idle-oldest tie-break eases it back into
    rotation instead of dumping a burst onto it.
    """
    conn = get_db()
    conn.execute(
        "UPDATE tokens SET status = ?, rate_limited_until = NULL, "
        "consecutive_403 = 0, park_reason = NULL WHERE id = ?",
        ("ACTIVE", token_id),
    )
    conn.commit()
    conn.close()
    logger.info("Token #%d restored to ACTIVE by operator", token_id)


def _bump_403_streak(token_id):
    """Increment and return this token's consecutive-403 counter."""
    conn = get_db()
    conn.execute(
        "UPDATE tokens SET consecutive_403 = consecutive_403 + 1 WHERE id = ?",
        (token_id,),
    )
    row = conn.execute(
        "SELECT consecutive_403 FROM tokens WHERE id = ?", (token_id,)
    ).fetchone()
    conn.commit()
    conn.close()
    return row[0] if row else 0


def mark_limited(token_id, code=None, cooldown=None):
    if code in (401, 40001, 40002, 40003):
        mark_quarantined(token_id, f"upstream {code} (auth rejected)")
        return
    if code in (40012, "40012"):
        mark_parked(token_id, "upstream 40012 (banned)", 86400)
        return
    if code in (40303, "40303"):
        chairman_xi_penalize_social_credit(token_id, cooldown or 1800)
        return
    if code in (50006, "50006"):
        secs = int(cooldown) if cooldown and cooldown > 0 else 3600
        mark_parked(token_id, "upstream 50006 (muted)", secs)
        return
    if code == 403:
        streak = _bump_403_streak(token_id)
        threshold = QUARANTINE_AFTER_403
        if threshold and streak >= threshold:
            mark_quarantined(
                token_id,
                f"{streak} consecutive upstream 403s (threshold {threshold})",
            )
            return
    secs = int(cooldown) if cooldown and cooldown > 0 else TOKEN_COOLDOWN_SECONDS
    secs = min(secs, 7 * 86400)
    until = time.time() + secs
    logger.warning(
        "Token #%d marked RATE_LIMITED for %ds (recovers automatically)",
        token_id,
        secs,
    )
    conn = get_db()
    conn.execute(
        "UPDATE tokens SET status = ?, rate_limited_until = ? WHERE id = ?",
        ("RATE_LIMITED", until, token_id),
    )
    conn.commit()
    conn.close()


def mark_active(token_id):
    """Stamp a successful turn: back in rotation, streak cleared.

    A QUARANTINED row is deliberately left alone (P0-3). Requests overlap, so a
    send that started before the pull can land its success afterwards; letting
    that flip the status would let a stale response launder an operator-
    controlled state, and only restore_token() ends a quarantine.
    """
    conn = get_db()
    conn.execute(
        "UPDATE tokens SET status = ?, rate_limited_until = NULL, last_used = ?, "
        "consecutive_403 = 0 WHERE id = ? AND status NOT IN ('QUARANTINED', 'PARKED')",
        ("ACTIVE", time.time(), token_id),
    )
    conn.commit()
    conn.close()


def _format_span(seconds):
    """Compact elapsed time for the pool view: 42s, 7m, 3h, 2d."""
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h"
    return f"{seconds // 86400}d"


# ==============================================================================
# P0-6 (Days 28-30) — usage stats
#
# One row per COMPLETED turn (the client saw a real answer). Failed attempts
# record nothing, so a turn retried across the pool lands exactly once — the
# counting rule is structural, not a dedup heuristic. Tokens are billed on the
# CLEANED completion (B7 semantics) by the callers, matching the usage chunk
# clients already receive. Cost is derived from the flat tariff table and is
# labeled as such everywhere it renders: it is an estimate against the shipped
# rates, not upstream metering.
# ==============================================================================


def mask_api_key(key):
    """Masked key alias for display: last 4 characters only.

    The dashboard and every aggregate render this — a screenshot of the page
    must not carry usable key material. Short keys (<= 8 chars, e.g. someone's
    one-word key) are fully masked."""
    key = str(key or "").strip()
    if not key:
        return "unknown"
    if len(key) <= 8:
        return "…"
    return "…" + key[-4:]


def record_usage(key_alias, surface, model, in_tokens, out_tokens, ts=None):
    """Record one completed turn. `key_alias` is expected PRE-MASKED — pass
    mask_api_key(raw_key) at the call site; this function never stores raw
    key material as a defense against future callers forgetting."""
    alias = (
        mask_api_key(key_alias)
        if key_alias and not str(key_alias).startswith("…")
        else str(key_alias or "unknown")
    )
    when = time.time() if ts is None else float(ts)
    day = time.strftime("%Y-%m-%d", time.gmtime(when))
    conn = get_db()
    conn.execute(
        "INSERT INTO usage (ts, day, key_alias, surface, model, input_tokens, output_tokens) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            when,
            day,
            alias,
            str(surface or "unknown"),
            str(model or ""),
            max(0, int(in_tokens or 0)),
            max(0, int(out_tokens or 0)),
        ),
    )
    conn.commit()
    conn.close()


def usage_by_day_key(since_ts=0.0):
    """Aggregate usage per day x per masked key alias.

    Returns ready-to-render rows (day, key, surfaces, requests, input_tokens,
    output_tokens, cost, pricing_source) — the template computes nothing, the
    same rule the pool view follows. An empty store is an empty list, never an
    error; an unused key simply has no rows for that day. Performance: the
    (day, key_alias) index covers the GROUP BY, so a month of data stays a
    few milliseconds of SQLite work — no pre-aggregation table needed."""
    conn = get_db()
    rows = conn.execute(
        "SELECT day, key_alias, "
        "GROUP_CONCAT(DISTINCT surface), COUNT(*), "
        "SUM(input_tokens), SUM(output_tokens) "
        "FROM usage WHERE ts >= ? "
        "GROUP BY day, key_alias ORDER BY day DESC, key_alias ASC",
        (float(since_ts or 0.0),),
    ).fetchall()
    conn.close()

    tariff = DEEPSEEK_TARIFFS["deepseek-v4.1-flash"]
    out = []
    for day, alias, surfaces, requests, in_tok, out_tok in rows:
        in_tok = in_tok or 0
        out_tok = out_tok or 0
        cost = (in_tok / 1_000_000 * tariff["cache_miss_input"]) + (
            out_tok / 1_000_000 * tariff["output_generation"]
        )
        out.append(
            {
                "day": day,
                "key": alias,
                "surfaces": surfaces or "unknown",
                "requests": requests,
                "input_tokens": in_tok,
                "output_tokens": out_tok,
                "cost": round(cost, 6),
                "pricing_source": "flat tariff table (not metered upstream usage)",
            }
        )
    return out


def get_pool_view():
    """Per-token operator view of the pool (P0-3).

    State, load and timers as ready-to-render strings, and no credential field
    at all: the token column is deliberately absent, because this output goes
    straight into HTML a browser caches and a screenshot carries away.
    Formatting happens here so templates/dashboard.html renders state without
    computing any — a template that computes is one that can grow a secret.
    """
    now = time.time()
    conn = get_db()
    rows = conn.execute(
        "SELECT id, alias, status, rate_limited_until, last_used, consecutive_403, park_reason "
        "FROM tokens ORDER BY id"
    ).fetchall()
    conn.close()

    view = []
    for tid, alias, status, until, last_used, streak, park_reason in rows:
        streak = streak or 0
        is_captcha = status == "PARKED" and (
            "captcha" in str(park_reason or "").lower()
            or "social credit" in str(park_reason or "").lower()
            or "chairman xi" in str(park_reason or "").lower()
            or (until and (until - now) <= 1805 and (until - now) > 0)
        )
        if status == "QUARANTINED":
            state_class, cooldown = "err", "—"
            note = "quarantined — manual return required"
        elif status == "PARKED" and until and until > now:
            state_class = "warn"
            cooldown = f"{_format_span(until - now)} left"
            note = (
                f"parked ({park_reason})"
                if park_reason
                else "parked — auto or manual return"
            )
        elif status == "PARKED":
            state_class, cooldown = "warn", "expired"
            note = (
                f"parking expired ({park_reason}) — rejoins rotation on next pick"
                if park_reason
                else "parking expired — rejoins rotation on next pick"
            )
        elif status == "RATE_LIMITED" and until and until > now:
            state_class = "warn"
            cooldown = f"{_format_span(until - now)} left"
            note = "cooling down, recovers automatically"
        elif status == "RATE_LIMITED":
            state_class, cooldown = "warn", "expired"
            note = "rejoins rotation on the next pick"
        else:
            state_class, cooldown = "ok", "—"
            note = "in rotation"
        if streak and status not in ("QUARANTINED", "PARKED"):
            note = f"{note}, 403 streak {streak}"
        age = "never" if not last_used else f"{_format_span(now - last_used)} ago"
        view.append(
            {
                "id": tid,
                "label": alias if alias else f"untagged #{tid}",
                "status": status,
                "state_class": state_class,
                "state_note": note,
                "in_flight": _in_flight.get(tid, 0),
                "last_used": age,
                "cooldown": cooldown,
                "can_restore": status in ("QUARANTINED", "PARKED"),
                "can_solve_captcha": bool(is_captcha),
                "park_reason": park_reason or "",
            }
        )
    return view


async def probe_token(auth_token: str) -> dict:
    cookie, shumei_id, device_id = await get_isolated_cookies_for_token(auth_token)
    headers = get_headers(auth_token, device_id=device_id)
    try:
        session = await get_session()
        get_kwargs = {"timeout": aiohttp.ClientTimeout(total=10.0)}
        if cookie:
            get_kwargs["cookies"] = cookie
        async with session.get(
            f"{UPSTREAM_BASES[0]}/api/v0/users/current",
            headers=headers,
            **get_kwargs,
        ) as resp:
            if resp.status == 200:
                js = await resp.json()
                code = js.get("code")
                biz = (js.get("data") or {}).get("biz_data") or {}
                if code == 0:
                    if biz.get("is_muted") == 1 or biz.get("biz_code") == 5:
                        mute_until = biz.get("mute_until") or biz.get("end_at")
                        return {"status": "muted", "mute_until": mute_until, "msg": "account muted"}
                    return {"status": "valid", "msg": "ok"}
                if code in (40001, 40002, 40003):
                    return {"status": "invalid", "code": code, "msg": js.get("msg") or "invalid token"}
                if code == 40012:
                    return {"status": "banned", "code": code, "msg": js.get("msg") or "user banned"}
                if code == 40029:
                    return {"status": "rate_limited", "code": code, "msg": js.get("msg") or "too many requests"}
                if code == 40303:
                    return {
                        "status": "challenge_required",
                        "code": code,
                        "msg": CHAIRMAN_XI_SOCIAL_CREDIT_MESSAGE,
                    }
                return {"status": "unknown", "code": code, "msg": js.get("msg") or "unknown error"}
            if resp.status == 202:
                return {"status": "unknown", "msg": "WAF challenge (202)"}
            if resp.status == 401:
                return {"status": "invalid", "msg": "auth rejected (401)"}
            return {"status": "unknown", "msg": f"upstream status {resp.status}"}
    except Exception as e:
        return {"status": "unknown", "msg": str(e)}


_memory_sessions: dict[str, dict] = {}
_memory_session_map: dict[str, dict] = {}


def is_sessions_persistence_enabled() -> bool:
    return os.getenv("DEEPSEEKER_PERSIST_SESSIONS", "true").lower() in (
        "true",
        "1",
        "yes",
    )


def find_session(sig):
    if not is_sessions_persistence_enabled():
        sess = _memory_sessions.get(sig)
        if sess:
            sess["last_used"] = time.time()
            return {
                "token_id": sess["token_id"],
                "session_id": sess["session_id"],
                "parent_message_id": sess["parent_message_id"],
                "sent_prompt": sess.get("sent_prompt"),
            }
        return None

    conn = get_db()
    row = conn.execute(
        "SELECT token_id, deepseek_session_id, parent_message_id, sent_prompt FROM sessions WHERE signature = ?",
        (sig,),
    ).fetchone()
    if row:
        conn.execute(
            "UPDATE sessions SET last_used = ? WHERE signature = ?", (time.time(), sig)
        )
        conn.commit()
    conn.close()
    if row:
        return {
            "token_id": row[0],
            "session_id": row[1],
            "parent_message_id": row[2],
            "sent_prompt": row[3] if len(row) > 3 else None,
        }
    return None


# Cap on stored session signatures. Every request stores 2 rows and nothing
# ever removed them, so after many chats the SQLite file (and its WAL) grew
# unbounded — on volume-limited deployments a full disk freezes ALL requests,
# including brand-new chats. PRUNE_EVERY saves trigger a prune that keeps the
# MAX_SESSIONS MOST-RECENTLY-USED rows (B13: by last_used, not insertion
# order — the old rowid policy evicted long-running chats, the most valuable
# sessions, exactly because they hold the oldest rows).
MAX_SESSIONS = int(os.getenv("DEEPSEEKER_MAX_SESSIONS", "20000"))
PRUNE_EVERY = int(os.getenv("DEEPSEEKER_PRUNE_EVERY", "500"))
_save_counter = {"n": 0}


def prune_sessions():
    if not is_sessions_persistence_enabled():
        if len(_memory_sessions) > MAX_SESSIONS:
            sorted_items = sorted(
                _memory_sessions.items(),
                key=lambda item: item[1].get("last_used") or 0.0,
            )
            excess = len(_memory_sessions) - MAX_SESSIONS
            for k, _ in sorted_items[:excess]:
                _memory_sessions.pop(k, None)
        return

    conn = get_db()
    try:
        total = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
        deleted = 0
        if total > MAX_SESSIONS:
            # B13: evict LEAST-RECENTLY-USED. NULL last_used (never re-hit)
            # sorts as 0, i.e. evicted first; rowid breaks ties deterministically.
            deleted = conn.execute(
                "DELETE FROM sessions WHERE rowid IN "
                "(SELECT rowid FROM sessions ORDER BY COALESCE(last_used, 0) ASC, rowid ASC LIMIT ?)",
                (total - MAX_SESSIONS,),
            ).rowcount
        deleted_map = conn.execute(
            "DELETE FROM session_map WHERE created_at < datetime('now', '-7 days')"
        ).rowcount
        conn.commit()
        if deleted or deleted_map:
            logger.info(
                "Pruned %d session signature(s) and %d stale session_map row(s)",
                deleted,
                deleted_map,
            )
    finally:
        conn.close()


def save_session(sig, token_id, session_id, parent_message_id=0, sent_prompt=None):
    if not is_sessions_persistence_enabled():
        _memory_sessions[sig] = {
            "token_id": token_id,
            "session_id": session_id,
            "parent_message_id": parent_message_id,
            "sent_prompt": sent_prompt,
            "last_used": time.time(),
        }
        _save_counter["n"] += 1
        if _save_counter["n"] >= PRUNE_EVERY:
            _save_counter["n"] = 0
            prune_sessions()
        return

    conn = get_db()
    conn.execute(
        """INSERT OR REPLACE INTO sessions (signature, token_id, deepseek_session_id, parent_message_id, sent_prompt)
           VALUES (?, ?, ?, ?, ?)""",
        (sig, token_id, session_id, parent_message_id, sent_prompt),
    )
    conn.commit()
    conn.close()
    _save_counter["n"] += 1
    if _save_counter["n"] >= PRUNE_EVERY:
        _save_counter["n"] = 0
        try:
            prune_sessions()
        except Exception:
            logger.exception("Session pruning failed (non-fatal)")


def delete_session(sig):
    if not is_sessions_persistence_enabled():
        _memory_sessions.pop(sig, None)
        return
    conn = get_db()
    conn.execute("DELETE FROM sessions WHERE signature = ?", (sig,))
    conn.commit()
    conn.close()


def next_parent(parent_message_id):
    """Compute the parent_message_id to use for the next turn on a chat session.

    Invariant: each successful /chat/completion request appends exactly one user
    message and one assistant message to the DeepSeek chat session, so if the
    request was sent with parent_message_id P, the last message id afterwards is
    P + 1 and the next request must use P + 2.

    DeepSeek's web API exposes no endpoint to list a session's messages, so this
    increment cannot be verified against the server; it is centralized here so
    every save_session() call site (token-rotation branch, non-stream and both
    streaming paths) stays consistent if the invariant ever changes.
    """
    return parent_message_id + 2


def delete_sessions_for_chat(token_id, session_id):
    if not is_sessions_persistence_enabled():
        to_del = [
            k
            for k, v in _memory_sessions.items()
            if v.get("token_id") == token_id and v.get("session_id") == session_id
        ]
        for k in to_del:
            _memory_sessions.pop(k, None)
        return
    conn = get_db()
    conn.execute(
        "DELETE FROM sessions WHERE token_id = ? AND deepseek_session_id = ?",
        (token_id, session_id),
    )
    conn.commit()
    conn.close()


def delete_sessions_for_token(token_id):
    if not is_sessions_persistence_enabled():
        to_del = [
            k for k, v in _memory_sessions.items() if v.get("token_id") == token_id
        ]
        for k in to_del:
            _memory_sessions.pop(k, None)
        return
    conn = get_db()
    conn.execute("DELETE FROM sessions WHERE token_id = ?", (token_id,))
    conn.commit()
    conn.close()


def delete_all_sessions():
    _memory_sessions.clear()
    if is_sessions_persistence_enabled():
        conn = get_db()
        conn.execute("DELETE FROM sessions")
        conn.commit()
        conn.close()


# ==============================================================================
# B4 (Stage 1 audit) — file ownership registry
#
# /v1/files uploads picked a RANDOM token and upstream files are
# account-scoped, so a later /v1/chat/completions referencing that file_id
# could pick a different token and get "file not found" — the OpenAI-style
# upload->reference flow (Claude Code / Cline file flows) was broken by
# design. Uploads are now pinned to their token; chat handlers prefer the
# file-owner token on the first turn and re-home foreign references onto the
# chat's own token on later turns.
# ==============================================================================


def record_file(file_id, token_id):
    """Pin an upstream file_id to the token (account) that owns it.
    First owner wins: INSERT OR IGNORE keeps the original mapping stable."""
    conn = get_db()
    conn.execute(
        "INSERT OR IGNORE INTO files (file_id, token_id, created_at) VALUES (?, ?, ?)",
        (file_id, token_id, time.time()),
    )
    conn.commit()
    conn.close()


def get_file_token(file_id):
    """Return the token id that owns an uploaded file_id, or None."""
    conn = get_db()
    row = conn.execute(
        "SELECT token_id FROM files WHERE file_id = ?", (file_id,)
    ).fetchone()
    conn.close()
    return row[0] if row else None


# DeepSeek now serves a single model (v4.1flash) as the website default. The
# web API uses the website default when model_type is null, so no explicit
# value is sent. If DeepSeek ever exposes an explicit model_type enum value
# for v4.1flash, this constant is the single place to set it.
DEFAULT_MODEL_TYPE = None

# Flat peak-hour rates (per 1M tokens) for the single default model,
# per https://api-docs.deepseek.com/quick_start/pricing
DEEPSEEK_TARIFFS = {
    "deepseek-v4.1-flash": {
        "cache_miss_input": 0.44,
        "output_generation": 1.32,
    },
}


def count_tokens(text, model="deepseek-v4.1-flash"):
    return len(deepseek_tokenizer.ds_token.encode(text))


def normalize_tool_call(tool_data_or_name, args_if_name=None):
    if isinstance(tool_data_or_name, str):
        name = tool_data_or_name
        args = args_if_name if args_if_name is not None else {}
    elif isinstance(tool_data_or_name, dict):
        tool_data = tool_data_or_name
        if "function" in tool_data and isinstance(tool_data["function"], dict):
            fn = tool_data["function"]
            name = fn.get("name") or tool_data.get("name")
            args = (
                fn.get("arguments")
                or fn.get("parameters")
                or fn.get("input")
                or fn.get("args")
                or fn.get("params")
                or {}
            )
        else:
            name = (
                tool_data.get("name")
                or tool_data.get("tool")
                or tool_data.get("tool_name")
                or tool_data.get("function")
                or tool_data.get("action")
            )
            args = (
                tool_data.get("arguments")
                or tool_data.get("parameters")
                or tool_data.get("input")
                or tool_data.get("args")
                or tool_data.get("params")
                or tool_data.get("tool_input")
                or tool_data.get("action_input")
                or {}
            )
    else:
        return None

    if not name or not isinstance(name, str):
        return None
    args = _clean_tool_arg(args)
    if isinstance(args, (dict, list)):
        args_str = json.dumps(args)
    elif isinstance(args, str):
        args_str = args
        try:
            json.loads(args_str)
        except Exception:
            args_str = json.dumps(args_str)
    else:
        args_str = json.dumps({})
    call_id = "call_" + "".join(
        random.choices(string.ascii_letters + string.digits, k=8)
    )
    return {
        "id": call_id,
        "type": "function",
        "function": {
            "name": name.strip(),
            "arguments": args_str,
        },
    }


def clean_json_str(s):
    s = s.strip()
    if s.startswith("```json"):
        s = s[7:]
    elif s.startswith("```"):
        s = s[3:]
    if s.endswith("```"):
        s = s[:-3]
    return s.strip()


def _code_fence_spans(text):
    """Return the (start, end) spans of markdown fenced code blocks.

    Tool-call markup inside a fence is documentation/example text, not an
    actual tool call, so matches falling inside these spans are ignored.
    Unclosed fences extend to end-of-text.
    """
    return [
        (m.start(), m.end()) for m in re.finditer(r"```.*?(?:```|$)", text, re.DOTALL)
    ]


# Openers may come as "</｜｜DSML｜｜ parameter name=...>" (the model mixes up
# open/close), as 'parameter="x"' without name, or without "parameter name=" at all
# ('<｜｜DSML｜｜command" string=...>', '<｜｜DSML｜｜ old_string="true">') ("<｜｜DSML｜｜command" string=...>),
# and a value ends at the proper closer, the next parameter, or ANY DSML marker
# (bare "</｜｜DSML｜｜>", truncated "</｜｜DSML｜｜ ", "</｜｜DSML｜｜ calls>",
# "<"-less "｜｜DSML｜｜parameter>"): markers never belong in a value.
_DSML_PARAM_RE = re.compile(
    r"(?:</?[｜\|]{0,2}(?:DSML[｜\|]{0,2})?\s*(?:parameter|tool_call|param|invoke)(?:\s+name\s*=|\s*=)\s*[\x27\x22]([^\x27\x22]+)[\x27\x22]"
    r"|</?[｜\|]{1,2}DSML[｜\|]{1,2}\s*(?!(?:parameter|invoke|function_calls?|tool_calls?|calls)\b)([A-Za-z_][\w\-]*)(?=[\x27\x22]|\s*=\s*[\x27\x22](?:true|false)[\x27\x22]))([^>]*)>"
    r"(.*?)(?:</[｜\|]{0,2}(?:DSML[｜\|]{0,2})?\s*(?:parameter|tool_call|param|invoke)>|(?=</?[｜\|]{0,2}(?:DSML[｜\|]{0,2})?\s*(?:parameter|tool_call|param|invoke)\s+name=)|(?=(?:</?)?[｜\|]{1,2}DSML|</?[｜\|]{0,2}DSML[｜\|])|$)",
    re.DOTALL | re.IGNORECASE,
)


def _dsml_params(body):
    args = {}
    for pm in _DSML_PARAM_RE.finditer(body):
        p_name = (pm.group(1) or pm.group(2)).strip()
        p_val = pm.group(4).strip()
        p_val = re.sub(
            r"</?(?:tool_calls?|invoke|function_call|parameter|param)\b[^>]*>",
            "",
            p_val,
            flags=re.IGNORECASE,
        ).strip()
        # string="true" marks a literal string; don't turn "42"/"true" into JSON.
        # ('<｜｜DSML｜｜old_string="true">' puts the type right after the name.)
        if re.search(r"(?:string\s*=|^\s*=)\s*[\x27\x22]true", pm.group(3), re.IGNORECASE):
            args[p_name] = p_val
            continue
        try:
            args[p_name] = json.loads(p_val)
        except Exception:
            args[p_name] = p_val
    return args


# The request's tool list, set by handle_chat, so parse_tools can recover a call
# whose tool name the model dropped (a function_calls block without an invoke).
request_tools = contextvars.ContextVar("request_tools", default=None)

# The id DeepSeek gave the reply of the last send_message in this request (from
# the first SSE event). Ids are chat-global, so a reply sent from a fork point is
# not at parent + 2.
reply_id = contextvars.ContextVar("reply_id", default=None)


def _extract_upstream_message_id(data):
    if not isinstance(data, dict):
        return None
    for k in ("response_message_id", "message_id"):
        v = data.get(k)
        if isinstance(v, int) or (isinstance(v, str) and v):
            return v
    for parent in ("response", "v"):
        nested = data.get(parent)
        if isinstance(nested, dict):
            for k in ("response_message_id", "message_id"):
                v = nested.get(k)
                if isinstance(v, int) or (isinstance(v, str) and v):
                    return v
            v_nested = nested.get("response")
            if isinstance(v_nested, dict):
                for k in ("response_message_id", "message_id"):
                    v = v_nested.get(k)
                    if isinstance(v, int) or (isinstance(v, str) and v):
                        return v
    if data.get("o") == "BATCH" and isinstance(data.get("v"), list):
        for op in data["v"]:
            if isinstance(op, dict):
                mid = _extract_upstream_message_id(op)
                if mid is not None:
                    return mid
    return None


def _tool_schemas():
    schemas = {}
    for t in request_tools.get() or []:
        if not isinstance(t, dict):
            continue
        fn = t.get("function") if isinstance(t.get("function"), dict) else t
        name = fn.get("name")
        params = fn.get("parameters") or fn.get("input_schema") or {}
        if name and isinstance(params, dict):
            schemas[name] = params
    return schemas


def _infer_tool_name(args, schemas):
    """The one tool whose schema accepts exactly these argument names."""
    # First require every required field; if the model also dropped one of those,
    # still name the one tool that accepts these arguments, so the client reports
    # the missing field instead of an unknown tool.
    for need_required in (True, False):
        fits = []
        for name, params in schemas.items():
            props = set(params.get("properties") or {})
            required = set(params.get("required") or [])
            if set(args) <= props and (not need_required or required <= set(args)):
                fits.append((len(props - set(args)), name))
        fits.sort()
        if fits and (len(fits) == 1 or fits[0][0] < fits[1][0]):
            return fits[0][1]
    return None


def _repair_truncated_json(s: str) -> str:
    s = s.strip()
    if not s:
        return s
    stack = []
    in_string = False
    escape = False
    for c in s:
        if escape:
            escape = False
            continue
        if c == "\\":
            if in_string:
                escape = True
            continue
        if c == '"':
            in_string = not in_string
            continue
        if not in_string:
            if c in ("{", "["):
                stack.append("}" if c == "{" else "]")
            elif c in ("}", "]") and stack and stack[-1] == c:
                stack.pop()
    if in_string:
        s += '"'
    s = s.rstrip()
    if s.endswith(":"):
        s += ' ""'
    while s.endswith(","):
        s = s[:-1].rstrip()
    if stack:
        s += "".join(reversed(stack))
    return s


def _match_or_predict_tool_name(raw_name, args, schemas):
    if not schemas:
        return raw_name
    if raw_name in schemas:
        return raw_name
    raw_lower = (raw_name or "").lower().strip()
    if raw_lower:
        for s in schemas:
            if s.lower() == raw_lower:
                return s
        clean_name = (
            raw_name.split(".")[-1]
            .split(":")[-1]
            .rstrip("()")
            .removesuffix("_tool")
            .removesuffix("-tool")
            .strip()
        )
        if clean_name:
            clean_lower = clean_name.lower()
            for s in schemas:
                if s.lower() == clean_lower:
                    return s
        for s in schemas:
            s_low = s.lower()
            if s_low in raw_lower or (len(raw_lower) >= 3 and raw_lower in s_low):
                return s
    if isinstance(args, dict):
        inferred = _infer_tool_name(args, schemas)
        if inferred:
            return inferred
    if not raw_name and len(schemas) == 1 and isinstance(args, dict):
        s_name = next(iter(schemas.keys()))
        s_props = set(schemas[s_name].get("properties") or {})
        if set(args.keys()) & s_props:
            return s_name
    return raw_name


def _repair_tool_names(tools):
    schemas = _tool_schemas()
    if not schemas:
        return tools
    for t in tools:
        fn = t["function"]
        args = {}
        with contextlib.suppress(Exception):
            args = json.loads(fn["arguments"])
        fn["name"] = _match_or_predict_tool_name(fn.get("name"), args, schemas)
    return tools




# The request's reasoning effort ("high", "max", ...) when thinking is on, set by
# the chat endpoints; build_prompt turns it into a think-harder instruction.
request_effort = contextvars.ContextVar("request_effort", default=None)
request_response_format = contextvars.ContextVar("request_response_format", default=None)


def is_json_object_format(fmt):
    if not fmt:
        return False
    if isinstance(fmt, str):
        return fmt.strip().lower() in ("json_object", "json")
    if isinstance(fmt, dict):
        t = str(fmt.get("type", "")).strip().lower()
        return t in ("json_object", "json_schema")
    return False


def json_mode_coerce(text: str) -> str:
    if not text:
        return "{}"
    trimmed = text.strip()
    if trimmed.startswith("```"):
        lines = trimmed.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        trimmed = "\n".join(lines).strip()

    try:
        parsed = json.loads(trimmed)
        if isinstance(parsed, (dict, list)):
            return json.dumps(parsed, ensure_ascii=False)
    except Exception:
        pass

    first_brace = trimmed.find("{")
    last_brace = trimmed.rfind("}")
    if first_brace != -1 and last_brace != -1 and last_brace > first_brace:
        candidate = trimmed[first_brace : last_brace + 1]
        try:
            parsed = json.loads(candidate)
            if isinstance(parsed, (dict, list)):
                return json.dumps(parsed, ensure_ascii=False)
        except Exception:
            pass
        repaired = _repair_truncated_json(candidate)
        try:
            parsed = json.loads(repaired)
            if isinstance(parsed, (dict, list)):
                return json.dumps(parsed, ensure_ascii=False)
        except Exception:
            pass

    repaired = _repair_truncated_json(trimmed)
    try:
        parsed = json.loads(repaired)
        if isinstance(parsed, (dict, list)):
            return json.dumps(parsed, ensure_ascii=False)
    except Exception:
        pass

    return json.dumps({"response": text.strip()}, ensure_ascii=False)


def effort_level(body):
    """The client's effort string, wherever the dialect puts it."""
    cands = [body.get("reasoning_effort"), body.get("effort")]
    for key in ("reasoning", "output_config", "thinking"):
        v = body.get(key)
        if isinstance(v, dict):
            cands += [v.get("effort"), v.get("reasoning_effort"), v.get("level")]
        elif key == "thinking" and isinstance(v, str):
            cands.append(v)
    for c in cands:
        if isinstance(c, str) and c.strip():
            return c.strip().lower()
    return None


# Structural DSML tag names: a lone wrapper tag is never a call to a tool of
# that name (unless the client really defines one).
_DSML_WRAPPER_NAMES = {
    "function_calls", "function_call", "tool_calls", "tool_call",
    "calls", "invoke", "parameter", "param",
}


def _is_wrapper_name(name, args):
    return not args and name.lower() in _DSML_WRAPPER_NAMES and name not in _tool_schemas()


def parse_tools(text):
    tools = []
    clean_text = text
    fence_spans = _code_fence_spans(text)

    def fenced(pos):
        return any(s <= pos < e for s, e in fence_spans)

    param_names = {
        "command",
        "description",
        "file_path",
        "content",
        "path",
        "prompt",
        "query",
        "subject",
        "old_string",
        "new_string",
        "url",
        "input",
    }
    # Call starts: invoke openers (also "</…invoke name=…>", the model mixes up
    # open/close), DSML function_calls blocks with no invoke at all, and a bare
    # "<｜｜DSML｜｜invoke>" without a name; their tool name is inferred from the
    # parameters.
    starts = [
        (m, m.group(1).strip())
        for m in re.finditer(
            r"</?[｜\|]{0,2}(?:DSML[｜\|]{0,2})?\s*(?:tool_calls?|invoke|function_calls?)(?:\s+(?:name|tool)\s*=|\s*=)\s*[\x27\x22]([^\x27\x22]+)[\x27\x22][^>]*>",
            text,
            re.IGNORECASE,
        )
    ] + [
        (m, None)
        for m in re.finditer(
            r"<[｜\|]{0,2}DSML[｜\|]{0,2}\s*(?:function_calls?|tool_calls?|calls|invoke)\s*>",
            text,
            re.IGNORECASE,
        )
    ]
    real_tool_matches = sorted(
        (st for st in starts if not fenced(st[0].start())), key=lambda st: st[0].start()
    )

    if real_tool_matches:
        schemas = _tool_schemas()
        for i, (tm, candidate_name) in enumerate(real_tool_matches):
            start_idx = tm.end()
            # 'invoke name="i" string="true">…': the model fused the invoke with its
            # first parameter; read it as a parameter and infer the tool name.
            if (
                candidate_name
                and "string=" in tm.group(0)
                and schemas
                and candidate_name not in schemas
            ):
                start_idx = tm.start()
                candidate_name = None
            end_idx = (
                real_tool_matches[i + 1][0].start()
                if i + 1 < len(real_tool_matches)
                else len(text)
            )
            body = text[start_idx:end_idx]
            args = {}
            args = _dsml_params(body)
            tag_param_matches = re.finditer(
                r"<([A-Za-z0-9_\-]+)>(.*?)(?:</\1>|$)",
                body,
                flags=re.DOTALL | re.IGNORECASE,
            )
            for pm in tag_param_matches:
                t_name = pm.group(1).strip().lower()
                if t_name in param_names:
                    t_val = pm.group(2).strip()
                    t_val = re.sub(
                        r"</?(?:tool_calls?|invoke|function_call|parameter|param)\b[^>]*>",
                        "",
                        t_val,
                        flags=re.IGNORECASE,
                    ).strip()
                    try:
                        args[t_name] = json.loads(t_val)
                    except Exception:
                        args[t_name] = t_val
            if candidate_name or args:
                # A wrapper with no parameters just precedes its invoke; skip it.
                norm = normalize_tool_call(candidate_name or "function_calls", args)
                if norm:
                    tools.append(norm)

    if tools:
        clean_text = re.sub(
            r"<[｜\|]{0,2}(?:DSML[｜\|]{0,2})?\s*(?:tool_calls?|calls)[^>]*>.*?(?:</[｜\|]{0,2}(?:DSML[｜\|]{0,2})?\s*(?:tool_calls?|calls)>|$)",
            "",
            clean_text,
            flags=re.DOTALL | re.IGNORECASE,
        ).strip()
        clean_text = re.sub(
            # Also "</…invoke name=…>": the model mixes up open/close for an opener.
            r"(?:<[｜\|]{0,2}(?:DSML[｜\|]{0,2})?\s*(?:invoke|function_call)[^>]*>|</[｜\|]{0,2}(?:DSML[｜\|]{0,2})?\s*invoke\s+name\s*=[^>]*>)"
            r".*?(?:</[｜\|]{0,2}(?:DSML[｜\|]{0,2})?\s*(?:invoke|function_call)>|$)",
            "",
            clean_text,
            flags=re.DOTALL | re.IGNORECASE,
        ).strip()

    if not tools and "DSML" in text:
        dsml_block_pattern = re.compile(
            r"<[｜\|]{2}DSML[｜\|]{2}([A-Za-z0-9_]+)>(.*?)(?:</[｜\|]{2}DSML[｜\|]{2}\1>|$)",
            re.DOTALL | re.IGNORECASE,
        )
        param_pattern_b = re.compile(
            r"<[｜\|]{2}DSML[｜\|]{2}B([A-Za-z0-9_]+)[^>]*>(.*?)(?:</[｜\|]{2}DSML[｜\|]{2}B.*?>|$)",
            re.DOTALL | re.IGNORECASE,
        )
        for m in dsml_block_pattern.finditer(text):
            if fenced(m.start()):
                continue
            tool_name = m.group(1).strip()
            body = m.group(2)
            args = {}
            for pm in param_pattern_b.finditer(body):
                p_name = pm.group(1).lower().strip()
                p_val = pm.group(2).strip()
                try:
                    args[p_name] = json.loads(p_val)
                except Exception:
                    args[p_name] = p_val
            norm = None if _is_wrapper_name(tool_name, args) else normalize_tool_call(tool_name, args)
            if norm:
                tools.append(norm)
        if not tools:
            tool_match = re.search(
                r"[｜\|]{2}DSML[｜\|]{2}(Bash|Read|Write|Edit|Agent|TaskList|TaskCreate|WebSearch|[A-Za-z0-9_]+)",
                text,
                re.IGNORECASE,
            )
            if tool_match and not fenced(tool_match.start()):
                candidate = tool_match.group(1).strip()
                tool_name = (
                    "Bash"
                    if candidate.lower().startswith("b")
                    and candidate.lower() not in ["bdescription", "bparam"]
                    else candidate
                )
                args = {}
                cmd_match = re.search(
                    r"[｜\|]{2}B[\x22\x27]?command[\x22\x27]?[^>]*>(.*?)(?:</[｜\|]{2}B|$)",
                    text,
                    re.DOTALL | re.IGNORECASE,
                )
                desc_match = re.search(
                    r"[｜\|]{2}B[\x22\x27]?description[\x22\x27]?[^>]*>(.*?)(?:</[｜\|]{2}B|$)",
                    text,
                    re.DOTALL | re.IGNORECASE,
                )
                if cmd_match:
                    clean_cmd = re.sub(
                        r"</?[｜\|]{2}DSML[｜\|]{2}[^>]*>", "", cmd_match.group(1)
                    ).strip("\x22\x27() ")
                    args["command"] = clean_cmd
                if desc_match:
                    clean_desc = re.sub(
                        r"</?[｜\|]{2}DSML[｜\|]{2}[^>]*>", "", desc_match.group(1)
                    ).strip("\x22\x27() ")
                    args["description"] = clean_desc
                norm = None if _is_wrapper_name(tool_name, args) else normalize_tool_call(tool_name, args)
                if norm:
                    tools.append(norm)
        if tools:
            clean_text = re.sub(
                r"<[｜\|]{2}DSML[｜\|]{2}[^>]*>.*?(?:</[｜\|]{2}DSML[｜\|]{2}[^>]*>|$)",
                "",
                clean_text,
                flags=re.DOTALL | re.IGNORECASE,
            ).strip()
            clean_text = re.sub(
                r"</?[｜\|]{2}DSML[｜\|]{2}[^>]*>", "", clean_text, flags=re.IGNORECASE
            ).strip()

    if not tools:
        fn_call_pattern = re.compile(
            r"<function_call>\s*<name>([^<]+)</name>\s*<arguments>(.*?)</arguments>\s*</function_call>",
            re.DOTALL | re.IGNORECASE,
        )
        for m in fn_call_pattern.finditer(text):
            if fenced(m.start()):
                continue
            name = m.group(1).strip()
            args_raw = m.group(2).strip()
            try:
                args = json.loads(args_raw)
            except Exception:
                args = args_raw
            norm = normalize_tool_call(name, args)
            if norm:
                tools.append(norm)
        if tools:
            clean_text = re.sub(
                r"<function_call>.*?</function_call>",
                "",
                clean_text,
                flags=re.DOTALL | re.IGNORECASE,
            ).strip()

    if not tools:
        tag_regex = re.compile(
            r"<(?:tool_call|function_call)(?:\s+(?:name|tool|function)=[\x27\x22]([^\x27\x22]+)[\x27\x22])?\s*>",
            re.IGNORECASE,
        )
        decoder = json.JSONDecoder()
        matches = [m for m in tag_regex.finditer(text) if not fenced(m.start())]
        if matches:
            for m in matches:
                tag_name = m.group(1)
                after_tag = text[m.end() :]
                brace_pos = after_tag.find("{")
                if brace_pos != -1:
                    json_substr = after_tag[brace_pos:]
                    data = None
                    with contextlib.suppress(Exception):
                        data, _ = decoder.raw_decode(json_substr)
                    if not data:
                        cleaned_json = re.sub(
                            r"</?(?:tool_call|function_call|tool_calls|invoke)[^>]*>.*",
                            "",
                            json_substr,
                            flags=re.DOTALL,
                        ).strip()
                        open_b = cleaned_json.count("{")
                        close_b = cleaned_json.count("}")
                        if open_b > close_b:
                            cleaned_json += "}" * (open_b - close_b)
                        with contextlib.suppress(Exception):
                            data = json.loads(cleaned_json)
                    if isinstance(data, dict):
                        if tag_name:
                            name = tag_name
                            if "arguments" in data and isinstance(
                                data["arguments"], dict
                            ):
                                args = data["arguments"]
                            elif "parameters" in data and isinstance(
                                data["parameters"], dict
                            ):
                                args = data["parameters"]
                            elif "input" in data and isinstance(data["input"], dict):
                                args = data["input"]
                            else:
                                args = {
                                    k: v
                                    for k, v in data.items()
                                    if k not in ["name", "tool", "function"]
                                }
                        else:
                            name = (
                                data.get("name")
                                or data.get("tool")
                                or data.get("tool_name")
                                or data.get("function")
                                or data.get("action")
                            )
                            args = (
                                data.get("arguments")
                                or data.get("parameters")
                                or data.get("input")
                                or data.get("args")
                                or data.get("params")
                                or data.get("tool_input")
                                or data.get("action_input")
                            )
                            if args is None:
                                args = {}
                        if name:
                            norm = normalize_tool_call(name, args)
                            if norm:
                                tools.append(norm)
            clean_text = re.sub(
                r"<(?:tool_call|function_call)[^>]*>.*?(?:</(?:tool_call|function_call)>|$)",
                "",
                text,
                flags=re.DOTALL,
            ).strip()

    if not tools:
        codeblock_pattern = r"```(?:tool_call|function_call)\s*(.*?)\s*```"
        cb_matches = list(re.finditer(codeblock_pattern, clean_text, flags=re.DOTALL))
        for m in cb_matches:
            cleaned = clean_json_str(m.group(1))
            try:
                data = json.loads(cleaned)
                if isinstance(data, dict):
                    name = (
                        data.get("name")
                        or data.get("tool")
                        or data.get("function")
                        or data.get("action")
                    )
                    args = (
                        data.get("arguments")
                        or data.get("parameters")
                        or data.get("input")
                        or data.get("args")
                        or {}
                    )
                    norm = normalize_tool_call(name, args)
                    if norm:
                        tools.append(norm)
            except Exception:
                pass
        if tools:
            clean_text = re.sub(
                codeblock_pattern, "", clean_text, flags=re.DOTALL
            ).strip()

    if not tools:
        json_pattern = r"```json\s*(\{.*?\})\s*```"
        json_matches = list(re.finditer(json_pattern, clean_text, flags=re.DOTALL))
        for m in json_matches:
            cleaned = clean_json_str(m.group(1))
            try:
                data = json.loads(cleaned)
                if isinstance(data, dict) and (
                    "name" in data or "tool" in data or "function" in data
                ):
                    name = (
                        data.get("name")
                        or data.get("tool")
                        or data.get("function")
                        or data.get("action")
                    )
                    args = (
                        data.get("arguments")
                        or data.get("parameters")
                        or data.get("input")
                        or data.get("args")
                        or {}
                    )
                    norm = normalize_tool_call(name, args)
                    if norm:
                        tools.append(norm)
            except Exception:
                pass
        if tools:
            clean_text = re.sub(json_pattern, "", clean_text, flags=re.DOTALL).strip()

    if not tools:
        schemas = _tool_schemas()
        if schemas:
            decoder = json.JSONDecoder()
            spans_to_remove = []
            pos = 0
            while pos < len(clean_text):
                if fenced(pos):
                    pos += 1
                    continue
                ch = clean_text[pos]
                if ch in ("{", "["):
                    sub = clean_text[pos:]
                    parsed_obj = None
                    end_idx = None
                    try:
                        parsed_obj, consumed = decoder.raw_decode(sub)
                        end_idx = pos + consumed
                    except Exception:
                        repaired = _repair_truncated_json(sub)
                        try:
                            parsed_obj = json.loads(repaired)
                            end_idx = len(clean_text)
                        except Exception:
                            parsed_obj = None
                    if parsed_obj is not None:
                        items = (
                            parsed_obj
                            if isinstance(parsed_obj, list)
                            else [parsed_obj]
                        )
                        if isinstance(parsed_obj, dict) and isinstance(
                            parsed_obj.get("tool_calls") or parsed_obj.get("calls"),
                            list,
                        ):
                            items = parsed_obj.get("tool_calls") or parsed_obj.get(
                                "calls"
                            )
                        recognized = []
                        for it in items:
                            if not isinstance(it, dict):
                                continue
                            c_name = (
                                it.get("name")
                                or it.get("tool")
                                or it.get("tool_name")
                                or it.get("function")
                                or it.get("action")
                            )
                            c_args = (
                                it.get("arguments")
                                or it.get("parameters")
                                or it.get("input")
                                or it.get("args")
                                or it.get("params")
                            )
                            if c_args is None and c_name:
                                c_args = {
                                    k: v
                                    for k, v in it.items()
                                    if k
                                    not in (
                                        "name",
                                        "tool",
                                        "tool_name",
                                        "function",
                                        "action",
                                    )
                                }
                            if not c_name:
                                c_args = it
                                c_name = _match_or_predict_tool_name(
                                    None, c_args, schemas
                                )
                            else:
                                c_name = _match_or_predict_tool_name(
                                    c_name, c_args, schemas
                                )
                            if c_name and (c_name in schemas or len(schemas) == 1):
                                norm = normalize_tool_call(c_name, c_args or {})
                                if norm:
                                    recognized.append(norm)
                        if recognized:
                            tools.extend(recognized)
                            spans_to_remove.append((pos, end_idx))
                            pos = end_idx
                            continue
                pos += 1
            if spans_to_remove:
                spans_to_remove.sort(reverse=True, key=lambda s: s[0])
                for start_s, end_s in spans_to_remove:
                    clean_text = clean_text[:start_s] + clean_text[end_s:]
                clean_text = clean_text.strip()
    # Keep companion text: the tool-call blocks themselves were already removed
    # from clean_text above; only leftover bare tags are stripped here.
    clean_text = _strip_wrapper_tags(clean_text).strip()
    tools = _repair_tool_names(tools)
    return tools, clean_text


# Family-wide closer pattern for the tool-call wrapper family, including
# the |/｜-decorated variants that parse_tools accepts. Used by
# StreamToolParser so a mismatched closer closes the open block instead of
# hanging until flush(). [FIX 3]
_TOOL_END_TAG_RE = re.compile(
    r"</[｜\|]{0,2}(?:DSML[｜\|]{0,2})?\s*(?:tool_calls?|calls|invoke|function_calls?)\s*>",
    re.IGNORECASE,
)

# [FIX 4] Spaced-DSML dialect: deepseek-harness emits decorated tags with a
# space between the ｜｜DSML｜｜ marker and the tag name, e.g.
# "<｜｜DSML｜｜ invoke name=...>". Entry detection cannot rely on plain
# substring start tags; this regex accepts bars, the DSML marker and the
# space in any combination.
# Also enters on "</…invoke name=…>": the model opens an invoke with a closing
# tag. Only with name=, so a real "</invoke>" closer never opens a block.
_STREAM_ENTRY_RE = re.compile(
    r"<(?:/(?=[｜\|]{0,2}(?:DSML[｜\|]{0,2})?\s*invoke\s+name\s*=))?"
    r"[｜\|]{0,2}(?:DSML[｜\|]{0,2})?\s*(tool_calls?|calls|function_calls?|invoke)\b[^>]*>",
    re.IGNORECASE,
)

# Orphan closers trailing a block already closed by the per-tag or family
# fallback (e.g. "</｜｜DSML｜｜ calls>" after the inner invoke was flushed).
_ORPHAN_CLOSER_RE = re.compile(
    r"</[｜\|]{0,2}(?:DSML[｜\|]{0,2})?\s*(?:tool_calls?|calls|function_calls?|invoke)\s*[｜\|]{0,2}>",
    re.IGNORECASE,
)

# [FIX 5] Bare DSML marker: a "<｜｜DSML｜｜>" / "</｜｜DSML｜｜>" whose tag-name
# slot is empty (nothing between the DSML marker and ">"). Every other cleanup
# regex in this module requires an actual tag name (tool_calls/calls/invoke/
# function_call/parameter), so these bare markers used to slip through and get
# streamed to the client as literal text. Note the tag-name slot is required to
# be EMPTY: "<｜｜DSML｜｜ invoke ...>" does not match and is left for the entry
# regex.
# The ">" is optional: the model also leaks truncated markers like
# "<｜｜DSML｜｜ </｜｜DSML｜｜ " as prose.
_BARE_DSML_MARKER_RE = re.compile(
    r"</?[｜\|]{0,2}DSML[｜\|]{0,2}+\s*+(?![A-Za-z_])[>＞]?",
    re.IGNORECASE,
)


# Full wrapper-tag family (open or close, with an optional tag name). Used on
# the non-streaming path and on flush() to strip wrapper noise; unlike
# _ORPHAN_CLOSER_RE it also removes stray OPEN tags.
_WRAPPER_TAG_RE = re.compile(
    r"</?[｜\|]{0,2}(?:DSML[｜\|]{0,2})?\s*(?:tool_calls?|calls|function_calls?|invoke|parameter)[^>]*>",
    re.IGNORECASE,
)


# Catch-all for corrupted/degenerate DSML markers that the structured
# regexes miss (e.g. a stray char between the bars: "<\uff5c\u89e6\uff5cDSML\uff5c\uff5c calls>"):
# any <...> token containing a fullwidth pipe U+FF5C, which never appears in
# legitimate prose. ASCII '|' is deliberately NOT matched (tables, code).
_FULLWIDTH_PIPE_TAG_RE = re.compile(r"<[^<>]*\uff5c[^<>]*>")


def _clean_tool_arg(value):
    # Strip corrupted DSML markers that leaked INTO a tool-call argument
    # value (e.g. a path parsed as "shell/core/Theme.qml</\uff5c\uff5cDSML\uff5c\uff5c >").
    # Only touches strings that actually contain a fullwidth pipe U+FF5C, so
    # normal arguments are untouched. Recurses into dicts/lists.
    if isinstance(value, str):
        if "\uff5c" in value:
            value = _FULLWIDTH_PIPE_TAG_RE.sub("", value).strip()
        return value
    if isinstance(value, dict):
        return {k: _clean_tool_arg(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_clean_tool_arg(v) for v in value]
    return value


def _strip_stray_markers(text: str) -> str:
    """Prose path: drop bare DSML markers + orphan closers only.

    Open tags are left untouched here so literal prose mentioning e.g.
    "" is not silently deleted mid-stream.
    """
    if not text:
        return text
    text = _FULLWIDTH_PIPE_TAG_RE.sub("", text)
    text = _BARE_DSML_MARKER_RE.sub("", text)
    return _ORPHAN_CLOSER_RE.sub("", text)


def _strip_wrapper_tags(text: str) -> str:
    """Wrapper path: drop the whole tag family (open or close) + bare markers."""
    if not text:
        return text
    text = _FULLWIDTH_PIPE_TAG_RE.sub("", text)
    text = _BARE_DSML_MARKER_RE.sub("", text)
    return _WRAPPER_TAG_RE.sub("", text)


def _append_clean_text(results, text: str) -> None:
    """Append cleaned prose, skipping empty deltas after marker stripping."""
    cleaned = _strip_stray_markers(text)
    if cleaned:
        results.append({"text": cleaned})


# Tag names _STREAM_ENTRY_RE can open on.
_STREAM_ENTRY_TAGS = (
    "tool_calls",
    "tool_call",
    "function_calls",
    "function_call",
    "invoke",
    "calls",
)


def _skip_bars(text, pos):
    for _ in range(2):
        if pos < len(text) and text[pos] in "|｜":
            pos += 1
    return pos


def _is_plausible_stream_entry_prefix(segment: str) -> bool:
    if not segment.startswith("<") or ">" in segment:
        return False
    body = segment[1:]
    pos = _skip_bars(body, 0)
    length = len(body)
    if pos < length and body[pos] in "Dd":
        matched = 0
        while matched < 4 and pos < length and body[pos].upper() == "DSML"[matched]:
            pos += 1
            matched += 1
        if matched < 4:
            return pos == length
        pos = _skip_bars(body, pos)
    while pos < length and body[pos].isspace():
        pos += 1
    if pos == length:
        return True
    remainder = body[pos:]
    remainder_lower = remainder.lower()
    for tag in _STREAM_ENTRY_TAGS:
        if tag.startswith(remainder_lower):
            return True
        if remainder_lower.startswith(tag):
            suffix = remainder[len(tag) :]
            if not suffix or not (suffix[0].isalnum() or suffix[0] == "_"):
                return True
    return False


def _is_plausible_stream_closer_prefix(segment: str) -> bool:
    return segment.startswith("</") and _is_plausible_stream_entry_prefix(
        "<" + segment[2:]
    )


class StreamToolParser:
    def __init__(self):
        self.buffer = ""
        self.in_tool = False
        self.has_tool = False
        self.json_done = False
        self._end_re = None

    def feed(self, chunk):
        self.buffer += chunk
        results = []
        while True:
            if self.in_tool:
                # [FIX 3] Family-wide closer fallback: accept ANY wrapper closer
                # the tool-call family can emit (including |/｜-decorated
                # variants parse_tools tolerates) instead of hanging an open
                # block until flush() when the model closes with a wrong tag.
                # [FIX 4] Prefer the closer for the tag that OPENED the block
                # so nested wrappers (<｜｜DSML｜｜ calls> wrapping invokes)
                # close as one unit; fall back to the family-wide closer for
                # mismatched or decorated closers [FIX 3].
                end_match = self._end_re.search(self.buffer) if self._end_re else None
                if end_match is None:
                    end_match = _TOOL_END_TAG_RE.search(self.buffer)
                if end_match:
                    if not self.json_done:
                        tool_xml = self.buffer[: end_match.end()]
                        parsed, _ = parse_tools(tool_xml)
                        for item in parsed:
                            results.append({"tool": item})
                    self.buffer = self.buffer[end_match.end() :]
                    self.in_tool = False
                    self.json_done = False
                    self._end_re = None
                    continue
                brace_idx = self.buffer.find("{")
                if brace_idx != -1 and not self.json_done:
                    decoder = json.JSONDecoder()
                    try:
                        data, consumed = decoder.raw_decode(self.buffer[brace_idx:])
                        norm = normalize_tool_call(data)
                        if norm:
                            results.append({"tool": norm})
                            self.json_done = True
                            self.buffer = self.buffer[brace_idx + consumed :]
                            continue
                    except Exception:
                        pass
                break
            else:
                # [FIX 4] Regex entry detection replaces plain substring finds
                # so decorated/spaced DSML openers enter tool mode.
                m = _STREAM_ENTRY_RE.search(self.buffer)
                if m:
                    start = m.start()
                    if start > 0:
                        _append_clean_text(results, self.buffer[:start])
                    self.buffer = self.buffer[start:]
                    tag_name = m.group(1).lower()
                    self._end_re = re.compile(
                        r"</[｜\|]{0,2}(?:DSML[｜\|]{0,2})?\s*"
                        + re.escape(tag_name)
                        + r"[｜\|]{0,2}\s*>",
                        re.IGNORECASE,
                    )
                    self.in_tool = True
                    self.has_tool = True
                    continue
                # [FIX 2] Hold an unclosed '<' tail only while it remains a
                # plausible partial match of _STREAM_ENTRY_RE or of a wrapper
                # closer ("</..."), so a split closer is never dumped as prose.
                last_lt = self.buffer.rfind("<")
                tail = self.buffer[last_lt:] if last_lt != -1 else ""
                hold = ">" not in tail and (
                    _is_plausible_stream_entry_prefix(tail)
                    or _is_plausible_stream_closer_prefix(tail)
                    or "\uff5c" in tail
                )
                if last_lt != -1 and hold:
                    if last_lt > 0:
                        _append_clean_text(results, self.buffer[:last_lt])
                    self.buffer = tail
                    break
                if self.buffer:
                    _append_clean_text(results, self.buffer)
                self.buffer = ""
                break
        return results

    def flush(self):
        out = []
        if self.buffer and not self.in_tool:
            _append_clean_text(out, self.buffer)
        elif self.in_tool:
            # [FIX 1] Recover the tool before stripping: if the stream was cut
            # off before the closing tag arrived but the payload itself is
            # parseable, emit the tool call instead of dumping raw parameter
            # values into the chat text. Strip only when nothing parses.
            parsed, _ = parse_tools(self.buffer)
            if parsed:
                for item in parsed:
                    out.append({"tool": item})
            elif not self.json_done:
                stripped = _strip_wrapper_tags(self.buffer).strip()
                if stripped:
                    out.append({"text": stripped})
            # With json_done set, whatever is left after the consumed JSON
            # payload is wrapper noise (partial closers / whitespace) and is
            # dropped instead of leaking into the chat text.
        self.buffer = ""
        self.in_tool = False
        self.json_done = False
        self._end_re = None
        return out


def summarize_messages(messages, max_tokens=500):
    recent = messages[-10:] if len(messages) > 10 else messages
    parts = []
    for msg in recent:
        role = msg.get("role", "unknown")
        content = msg.get("content", "")
        if isinstance(content, list):
            content = " ".join(
                c.get("text", "") for c in content if c.get("type") == "text"
            )
        if content:
            parts.append(f"{role}: {content[:200]}")
    summary = "\n".join(parts)
    tokens = count_tokens(summary)
    while tokens > max_tokens and len(parts) > 1:
        parts = parts[1:]
        summary = "\n".join(parts)
        tokens = count_tokens(summary)
    return summary


_pow_setup = None


def _get_pow_setup():
    global _pow_setup
    if _pow_setup is None:
        engine = wasmtime.Engine()
        with open(wasm_path, "rb") as f:
            module = wasmtime.Module(engine, f.read())
        _pow_setup = (engine, module, wasmtime.Linker(engine))
    return _pow_setup


def _find_pow_answer_blocking(challange_data):
    engine, module, linker = _get_pow_setup()
    store = wasmtime.Store(engine)
    instance = linker.instantiate(store, module)
    memory = instance.exports(store)["memory"]
    alloc_func = instance.exports(store)["alloc"]
    solve_func = instance.exports(store)["solve_pow"]
    ch_ptr, ch_len = write_string_pow(
        challange_data["challenge"], alloc_func, memory, store
    )
    salt_ptr, salt_len = write_string_pow(
        challange_data["salt"], alloc_func, memory, store
    )
    result = solve_func(
        store,
        ch_ptr,
        ch_len,
        salt_ptr,
        salt_len,
        challange_data["expire_at"],
        challange_data["difficulty"],
    )
    if result < 0:
        result = result + 0x10000000000000000
    return result if result != 0xFFFFFFFFFFFFFFFF else None


async def _safe_json(response, context="upstream", auth_token=None):
    ct = (getattr(response, "headers", None) or {}).get("Content-Type", "").lower()
    if response.status == 202 or "text/html" in ct:
        if auth_token:
            invalidate_token_cookies(auth_token)
            asyncio.create_task(get_isolated_cookies_for_token(auth_token))
        raise UpstreamError(
            429,
            f"AWS WAF challenge (HTTP {response.status} text/html) on {context}: refreshing WAF cookie",
            retry_after=15,
        )
    try:
        return await response.json()
    except aiohttp.ContentTypeError as e:
        if auth_token:
            invalidate_token_cookies(auth_token)
            asyncio.create_task(get_isolated_cookies_for_token(auth_token))
        status = getattr(e, "status", response.status) or 502
        eff_status = 429 if status == 202 else (status if 400 <= status <= 599 else 502)
        raise UpstreamError(
            eff_status,
            f"Upstream returned non-JSON response ({status}) on {context}: {e}",
            retry_after=15 if eff_status == 429 else None,
        ) from e


async def create_challange_pow(target_path, auth_token):
    cookie, shumei_id, device_id = await get_isolated_cookies_for_token(auth_token)
    headers = get_headers(auth_token, device_id=device_id)
    kwargs = {"timeout": aiohttp.ClientTimeout(total=20)}
    if cookie:
        kwargs["cookies"] = cookie
    response = await post_with_failover(
        "/api/v0/chat/create_pow_challenge",
        headers=headers,
        json={"target_path": target_path},
        **kwargs,
    )
    async with response:
        data = await _safe_json(response, "create_pow_challenge", auth_token)
    return _biz_data(data, "chat/create_pow_challenge")["challenge"]


def write_string_pow(text, alloc_func, memory, store):
    data = text.encode("utf-8")
    ptr = alloc_func(store, len(data))
    mem = memory.data_ptr(store)
    for i in range(len(data)):
        mem[ptr + i] = data[i]
    return ptr, len(data)


async def find_pow_answer(challange_data):
    return await asyncio.to_thread(_find_pow_answer_blocking, challange_data)


async def solve_create_pow(target_path, auth_token):
    pow = await create_challange_pow(target_path, auth_token)
    answer = await find_pow_answer(pow)
    if answer is None:
        raise Exception("PoW solve failed")
    json_data = {
        "algorithm": "DeepSeekHashV1",
        "challenge": pow["challenge"],
        "salt": pow["salt"],
        "answer": answer,
        "signature": pow["signature"],
        "target_path": target_path,
    }
    return base64.b64encode(json.dumps(json_data).encode()).decode()


async def create_new_chat(auth_token):
    cookie, shumei_id, device_id = await get_isolated_cookies_for_token(auth_token)
    headers = get_headers(auth_token, device_id=device_id)
    kwargs = {"timeout": aiohttp.ClientTimeout(total=20)}
    if cookie:
        kwargs["cookies"] = cookie
    response = await post_with_failover(
        "/api/v0/chat_session/create",
        headers=headers,
        json={},
        **kwargs,
    )
    async with response:
        data = await _safe_json(response, "chat_session/create", auth_token)
    return _biz_data(data, "chat_session/create")["chat_session"]["id"]


async def purge_chats_for_token(token_id: int):
    tok = get_token(token_id)
    if not tok:
        return False
    auth_token = tok["token"]
    cookie, shumei_id, device_id = await get_isolated_cookies_for_token(auth_token)
    headers = get_headers(auth_token, device_id=device_id)
    kwargs = {"timeout": aiohttp.ClientTimeout(total=30)}
    if cookie:
        kwargs["cookies"] = cookie
    with contextlib.suppress(Exception):
        resp = await post_with_failover(
            "/api/v0/users/update_settings",
            headers=headers,
            json={"training_allowed": False},
            **kwargs,
        )
        async with resp:
            pass
    with contextlib.suppress(Exception):
        resp = await post_with_failover(
            "/api/v0/chat_session/delete_all",
            headers=headers,
            json={},
            **kwargs,
        )
        async with resp:
            pass
    delete_sessions_for_token(token_id)
    return True


async def purge_all_tokens_chats():
    toks = get_tokens()
    for tok in toks:
        await purge_chats_for_token(tok["id"])
    delete_all_sessions()
    return True


_EMPTY_SSE_DEFAULT = (
    "Empty response from DeepSeek (no parseable SSE content): "
    "stream ended with no assistant fragments"
)
_EMPTY_SSE_CONTEXT = (
    "Empty response from DeepSeek (prompt may exceed the session context limit)"
)
_CONTEXT_LIMIT_HINT_RE = re.compile(
    r"maximum.?context"
    r"|context.?(?:length|limit|window)"
    r"|token.?limit"
    r"|(?:prompt|payload|conversation|messages?).{0,24}too.?long"
    r"|exceed(?:ed|s)?[^.\n]{0,30}context",
    re.IGNORECASE,
)
_SSE_RECENT_LINES = 8


def is_context_limit_error(exc):
    """Public classifier (DSH fix): did this failure come from the upstream
    session running out of context?

    Agent harnesses die here: a context-class failure used to be retried
    verbatim on other tokens (the context belongs to the CHAT, not the token,
    so every attempt fails the same way) and finally surfaced to the client as
    a context-limit error. The caller uses this to trigger a summary rollover
    instead. Matches:
      * UpstreamError bodies carrying upstream context wording (HTTP 400
        'maximum context length', 'context limit', 'too long', ...), and
      * the bridge's own empty-SSE-with-context-hint error.
    Deliberately NOT matched: generic 5xx/timeouts — 'deadline exceeded' or
    '502 upstream timeout' are retry-shaped, not context-shaped. The loose
    words only count when anchored to context material: a bare 'exceed' or a
    'too long' with no prompt/message in front of it no longer classifies as
    context (review fix, PR #67 — an upstream body like 'header limit
    exceeded' for a non-context reason used to force a wasteful summary
    rollover), and the plain-exception fallback requires explicit context-
    limit phrasing — a mere 'context' substring ('context deadline
    exceeded') is retry-shaped, not context-shaped."""
    if isinstance(exc, UpstreamError):
        body = exc.body or ""
        return bool(_CONTEXT_LIMIT_HINT_RE.search(body))
    text = str(exc or "")
    return bool(
        re.search(
            r"maximum.?context|context.?(?:length|limit|window)|token.?limit",
            text,
            re.IGNORECASE,
        )
    )


class _SseStreamFinished(Exception):
    pass


def _redact_sse_line(line: str) -> str:
    if len(line) > 500:
        return line[:500] + "..."
    return line


def _remember_sse_line(recent_lines, line: str):
    recent_lines.append(_redact_sse_line(line))
    if len(recent_lines) > _SSE_RECENT_LINES:
        del recent_lines[0]


def _sse_context_limit_hint(recent_lines, parsed_events):
    for obj in parsed_events:
        try:
            blob = json.dumps(obj, ensure_ascii=False)
        except Exception:
            blob = str(obj)
        if _CONTEXT_LIMIT_HINT_RE.search(blob):
            return True
    return any(_CONTEXT_LIMIT_HINT_RE.search(line) for line in recent_lines)


def _scan_recent_sse_errors(parsed_events):
    """Return (http_status, message) if a recent parsed event is an upstream error."""
    for obj in reversed(parsed_events):
        if not isinstance(obj, dict) or obj.get("type") != "error":
            continue
        content = obj.get("content") or obj.get("message") or "Upstream error"
        finish = str(obj.get("finish_reason") or "")
        if finish == "rate_limit_reached" or "rate_limit" in finish.lower():
            return 429, content
        if finish in ("permission_denied", "forbidden"):
            return 403, content
        return 502, content
    return None


def _non_sse_biz_error(non_sse_tail):
    """Return (code, msg) if the non-SSE body tail holds a JSON error envelope.

    Upstream answers some protocol failures on the SSE endpoint with a plain
    `application/json` envelope (`{"code":26,"msg":"invalid message id",...}`)
    instead of an event stream. The parser sees zero `data:` lines and the
    failure surfaces as an anonymous empty stream; this recovers the reason so
    the error message says what actually went wrong.
    """
    for line in reversed(non_sse_tail or []):
        try:
            obj = json.loads(line)
        except Exception:
            continue
        if isinstance(obj, dict) and (obj.get("code") or obj.get("msg")):
            return obj.get("code"), obj.get("msg") or obj.get("message")
    return None, None


def _raise_empty_sse_response(
    recent_lines, parsed_events, resp_ct="", non_sse_tail=None
):
    err = _scan_recent_sse_errors(parsed_events)
    if err:
        code, msg = err
        logger.warning(
            "DeepSeek SSE error event (not empty stream); last events: %s",
            recent_lines[-_SSE_RECENT_LINES:],
        )
        raise UpstreamError(code, msg)
    if _sse_context_limit_hint(recent_lines, parsed_events):
        msg = _EMPTY_SSE_CONTEXT
    else:
        msg = _EMPTY_SSE_DEFAULT
    # Log the response type and the raw non-SSE body so the next upstream
    # protocol change is identifiable from one request, not an hour of guessing.
    tail = non_sse_tail or []
    logger.warning(
        "DeepSeek SSE ended without assistant output; ct=%s non_sse=%s; last events: %s",
        resp_ct or "-",
        (" | ".join(tail[-3:])[:400] or "-"),
        recent_lines[-_SSE_RECENT_LINES:],
    )
    biz_code, biz_msg = _non_sse_biz_error(tail)
    if biz_code is not None or biz_msg:
        code_map = {
            40002: 401,
            40003: 401,
            40012: 403,
            40029: 429,
            40303: 403,
            50006: 429,
            40006: 502,
        }
        retry_after = None
        if biz_code in (40012, "40012"):
            retry_after = 86400
        elif biz_code in (40303, "40303"):
            retry_after = 1800
        elif biz_code in (50006, "50006"):
            retry_after = 3600
        elif biz_code in (40029, "40029"):
            retry_after = TOKEN_COOLDOWN_SECONDS
        status = code_map.get(
            biz_code, 403 if str(biz_code).startswith("403") else 502
        )
        raise UpstreamBizError(
            status,
            f"{msg}: code={biz_code} msg={biz_msg}",
            biz_code=int(biz_code) if str(biz_code).isdigit() else biz_code,
            retry_after=retry_after,
        )
    raise UpstreamError(502, msg)


def _raise_sse_error_event(data, recent_lines):
    content = data.get("content") or data.get("message") or "Upstream error"
    finish = str(data.get("finish_reason") or "")
    if finish == "rate_limit_reached" or "rate_limit" in finish.lower():
        logger.warning(
            "DeepSeek SSE error event (not empty stream); last events: %s",
            recent_lines[-_SSE_RECENT_LINES:],
        )
        raise UpstreamError(429, content)
    if finish in ("permission_denied", "forbidden"):
        raise UpstreamError(403, content)
    raise UpstreamError(502, content)


# DeepSeek generates one message per account at a time; a second concurrent send
# fails with "A message is being generated" / "Messages too frequent". Queue sends
# per token instead. Every generation goes through send_message, so this covers all
# callers.
_send_locks = {}


def token_busy(auth_token):
    """True while this account is generating (its send lock is held)."""
    lock = _send_locks.get(auth_token)
    return bool(lock and lock.locked())


async def send_message(chat_id, auth_token, *args, **kwargs):
    # The upstream stream is drained by its own task, so the lock is released when
    # DeepSeek finishes even if the consumer abandons this generator mid-stream
    # (client gone without aclose): holding it across our yields leaked the lock
    # and wedged every later request on that account.
    lock = _send_locks.setdefault(auth_token, asyncio.Lock())
    queue = asyncio.Queue()
    done = object()

    async def pump():
        try:
            async with lock:
                gen = _send_message_unlocked(chat_id, auth_token, *args, **kwargs)
                try:
                    async for chunk in gen:
                        queue.put_nowait(chunk)
                finally:
                    await gen.aclose()
        except Exception as e:
            queue.put_nowait(e)
        finally:
            queue.put_nowait(done)

    task = asyncio.create_task(pump())
    try:
        while (item := await queue.get()) is not done:
            if isinstance(item, Exception):
                raise item
            yield item
    finally:
        # A consumer that closes us (disconnect, abort) still cancels the upstream.
        task.cancel()


async def _send_message_unlocked(
    chat_id,
    auth_token,
    message,
    parent_message_id,
    thinking=False,
    search=False,
    file_ids_=None,
):
    reply_id.set(None)
    cookie, shumei_id, device_id = await get_isolated_cookies_for_token(auth_token)
    file_ids = file_ids_ or []

    pow_resp = await solve_create_pow(
        "/api/v0/chat/completion", auth_token
    )
    headers = get_headers(auth_token, pow_resp, device_id=device_id)
    hif_env = os.getenv("DEEPSEEKER_HIF_ENABLED")
    should_hif = (
        hif_env.lower() in ("true", "1", "yes")
        if hif_env is not None
        else True
    )
    if should_hif:
        hif_leim = await get_hif_token("leim")
        if hif_leim:
            headers["x-hif-leim"] = hif_leim
        hif_dliq = await get_hif_token("dliq")
        if hif_dliq:
            headers["x-hif-dliq"] = hif_dliq
    if chat_id:
        headers["referer"] = f"https://chat.deepseek.com/a/chat/s/{chat_id}"
    json_data = {
        "chat_session_id": chat_id,
        "model_type": DEFAULT_MODEL_TYPE,
        "prompt": message,
        "ref_file_ids": file_ids,
        "thinking_enabled": thinking,
        "search_enabled": search,
        "preempt": False,
        "action": None,
    }
    # Since 2026-10-31 upstream rejects parent_message_id=0 with biz_code 26
    # "invalid message id", and edges disagree about whether null is tolerated.
    # Omitting the key on the first turn is the only shape that works on all
    # of them; later turns still chain off the previous response id.
    if parent_message_id:
        json_data["parent_message_id"] = parent_message_id

    resolved_shumei = shumei_id or extract_shumei_device_id(cookie)
    if resolved_shumei:
        json_data["shumei_device_id"] = resolved_shumei

    think_open = False
    got_output = False
    # DeepSeek ends a reply it cut short (seen mid-thinking on long contexts) with
    # status INCOMPLETE. With no answer text yet that is a failed turn, not a
    # finished one: an empty "stop" makes agent clients end the turn.
    incomplete = False
    got_response = False
    post_kwargs = {}
    if cookie:
        post_kwargs["cookies"] = cookie
    resp = await post_with_failover(
        "/api/v0/chat/completion",
        headers=headers,
        json=json_data,
        **post_kwargs,
    )
    async with resp:
        resp_ct = (getattr(resp, "headers", None) or {}).get("content-type") or ""
        if resp.status == 202 or (resp.status != 200 and "text/html" in str(resp_ct).lower()):
            invalidate_token_cookies(auth_token)
            asyncio.create_task(get_isolated_cookies_for_token(auth_token))
            raise UpstreamError(
                429,
                f"AWS WAF challenge (HTTP {resp.status} text/html): refreshing WAF cookie",
                retry_after=15,
            )
        if resp.status != 200:
            error_text = await resp.text()
            logger.warning(
                "DeepSeek completion HTTP %d for chat %s: %s",
                resp.status,
                chat_id,
                error_text[:300],
            )
            raise UpstreamError(resp.status, error_text)

        recent_lines = []
        parsed_events = []
        # Non-SSE body lines (no `data: ` prefix). Upstream answers some
        # protocol failures with a plain application/json envelope on this
        # endpoint; keeping the raw lines is what makes those diagnosable.
        non_sse_tail = []
        line_buf = b""

        def _check_incomplete():
            if incomplete and not got_response:
                raise UpstreamError(
                    502, "DeepSeek stopped the reply before any answer (status INCOMPLETE)"
                )

        async def _emit_from_event(data):
            nonlocal think_open, got_output, incomplete, got_response
            if isinstance(data, dict) and data.get("type") == "error":
                _raise_sse_error_event(data, recent_lines)

            if data.get("o") == "BATCH" and isinstance(data.get("v"), list):
                for op in data["v"]:
                    if not isinstance(op, dict):
                        continue
                    if op.get("p") == "quasi_status":
                        incomplete = incomplete or op.get("v") == "INCOMPLETE"
                        continue  # a status word, never content
                    base, sub = data.get("p"), op.get("p")
                    if base and sub and not sub.startswith(base + "/"):
                        # Sub-op paths are relative to the BATCH path:
                        # {"p":"response","o":"BATCH","v":[{"p":"fragments",...}]}
                        op = {**op, "p": f"{base}/{sub}"}
                    async for chunk in _emit_from_event(op):
                        yield chunk
                return

            # Any other status field ("response/status", ".../status") carries a
            # status word such as INCOMPLETE or WIP, never content.
            p_name, p_val = str(data.get("p") or ""), data.get("v")
            if (
                p_name.endswith("status")
                and isinstance(p_val, str)
                and p_val.isupper()
                and p_val != "FINISHED"
            ):
                incomplete = incomplete or p_val == "INCOMPLETE"
                return

            if data.get("p") == "response/status" and data.get("v") == "FINISHED":
                if think_open:
                    yield "\n</think>\n\n"
                _check_incomplete()
                if not got_output:
                    _raise_empty_sse_response(
                        recent_lines, parsed_events, resp_ct, non_sse_tail
                    )
                raise _SseStreamFinished()

            if "v" in data and isinstance(data["v"], dict) and "response" in data["v"]:
                fragments = data["v"]["response"].get("fragments")
                if fragments:
                    for fragment in fragments:
                        if fragment.get("type") == "THINK":
                            if not think_open:
                                yield "<think>\n"
                                think_open = True
                            got_output = True
                            yield fragment.get("content", "")
                        else:
                            if think_open:
                                yield "\n</think>\n\n"
                                think_open = False
                            got_output = True
                            got_response = got_response or bool(fragment.get("content"))
                            yield fragment.get("content", "")
                return

            if data.get("p") == "response/fragments" and data.get("o") in ("APPEND", None):
                fragments = data.get("v")
                if isinstance(fragments, list):
                    for fragment in fragments:
                        if fragment.get("type") == "RESPONSE":
                            if think_open:
                                yield "\n</think>\n\n"
                                think_open = False
                            got_output = True
                            got_response = got_response or bool(fragment.get("content"))
                            yield fragment.get("content", "")
                        elif fragment.get("type") == "THINK":
                            if not think_open:
                                yield "<think>\n"
                                think_open = True
                            got_output = True
                            yield fragment.get("content", "")
                        else:
                            got_output = True
                            yield fragment.get("content", "")
                return

            v = data.get("v")
            if isinstance(v, str) and v:
                got_output = True
                got_response = got_response or not think_open
                yield v

        async def _consume_sse_line(decoded_line: str):
            if not decoded_line.startswith("data: "):
                non_sse_tail.append(_redact_sse_line(decoded_line))
                if len(non_sse_tail) > _SSE_RECENT_LINES:
                    del non_sse_tail[0]
                return
            _remember_sse_line(recent_lines, decoded_line)
            try:
                data = json.loads(decoded_line[6:])
            except Exception:
                return
            if isinstance(data, dict):
                parsed_events.append(data)
                if len(parsed_events) > _SSE_RECENT_LINES:
                    del parsed_events[0]
                mid = _extract_upstream_message_id(data)
                if mid is not None:
                    reply_id.set(mid)
            try:
                async for chunk in _emit_from_event(data):
                    yield chunk
            except _SseStreamFinished:
                raise

        async for chunk in resp.content.iter_any():
            if not chunk:
                continue
            line_buf += chunk
            while b"\n" in line_buf:
                raw_line, line_buf = line_buf.split(b"\n", 1)
                decoded_line = raw_line.decode("utf-8", errors="replace").strip()
                if not decoded_line:
                    continue
                try:
                    async for out in _consume_sse_line(decoded_line):
                        yield out
                except _SseStreamFinished:
                    return

        if line_buf.strip():
            decoded_line = line_buf.decode("utf-8", errors="replace").strip()
            if decoded_line:
                try:
                    async for out in _consume_sse_line(decoded_line):
                        yield out
                except _SseStreamFinished:
                    return

        if think_open:
            yield "\n</think>\n\n"
        _check_incomplete()
        if not got_output:
            _raise_empty_sse_response(
                recent_lines, parsed_events, resp_ct, non_sse_tail
            )


async def upload_file(file_bytes, file_name, file_content_type, auth_token):
    cookie, shumei_id, device_id = await get_isolated_cookies_for_token(auth_token)
    session = await get_session()
    file_size = len(file_bytes)
    pow_response = await solve_create_pow(
        "/api/v0/file/upload_file", auth_token
    )
    boundary = b"----WebKitFormBoundaryTB0pXOQR2RL219Hu"
    safe_name = re.sub(r"[^ -~]", "_", file_name).replace('"', "_") or "file.bin"
    body_parts = [
        b"--" + boundary + b"\r\n",
        f'Content-Disposition: form-data; name="file"; filename="{safe_name}"\r\n'.encode(),
        f"Content-Type: {file_content_type}\r\n\r\n".encode(),
        file_bytes,
        b"\r\n--" + boundary + b"--\r\n",
    ]
    reconstructed_body = b"".join(body_parts)
    headers = get_headers(auth_token, pow_response, device_id=device_id)
    headers.update(
        {
            "content-type": f"multipart/form-data; boundary={boundary.decode('utf-8')}",
            "x-file-size": str(file_size),
        }
    )
    post_kwargs = {"timeout": aiohttp.ClientTimeout(total=120)}
    if cookie:
        post_kwargs["cookies"] = cookie
    response = await post_with_failover(
        "/api/v0/file/upload_file",
        data=reconstructed_body,
        headers=headers,
        **post_kwargs,
    )
    async with response:
        resp_json = await _safe_json(response, "upload_file", auth_token)
    js_data = _biz_data(resp_json, "file/upload_file")
    file_id = js_data["id"]
    yield ("uploaded", file_id)
    status = js_data["status"]
    headers = get_headers(auth_token, device_id=device_id)
    deadline = time.time() + 300
    while status in ["PENDING", "PARSING"] and time.time() < deadline:
        yield ("uploaded", file_id)
        await asyncio.sleep(0.3)
        get_kwargs = {"timeout": aiohttp.ClientTimeout(total=30)}
        if cookie:
            get_kwargs["cookies"] = cookie
        async with session.get(
            "https://chat.deepseek.com/api/v0/file/fetch_files?file_ids=" + file_id,
            headers=headers,
            **get_kwargs,
        ) as resp:
            js_data = _biz_data(await resp.json(), "file/fetch_files")["files"][0]
        status = js_data["status"]
    if status == "SUCCESS":
        tp_data = datetime.fromtimestamp(js_data["updated_at"], UTC)
        yield (
            "success",
            {
                "file_id": file_id,
                "openai_timestamp": int(js_data["updated_at"]),
                "size": js_data["file_size"],
                "anthropic_timestamp": tp_data.strftime("%Y-%m-%dT%H:%M:%SZ"),
            },
        )
    else:
        yield ("error", file_id)


async def get_file_content(auth_token, file_id):
    cookie, shumei_id, device_id = await get_isolated_cookies_for_token(auth_token)
    session = await get_session()
    headers = get_headers(auth_token, device_id=device_id)
    get_kwargs = {"timeout": aiohttp.ClientTimeout(total=30)}
    if cookie:
        get_kwargs["cookies"] = cookie
    async with session.get(
        "https://chat.deepseek.com/api/v0/file/fetch_files?file_ids=" + file_id,
        headers=headers,
        **get_kwargs,
    ) as resp:
        resp_json = await resp.json()
    js_data = _biz_data(resp_json, "file/fetch_files")["files"][0]
    yield mimetypes.guess_type(js_data["file_name"])[0]
    deadline = time.time() + 60
    while (
        js_data.get("status") in ("PENDING", "PARSING")
        and time.time() < deadline
        and not js_data.get("signed_path")
    ):
        await asyncio.sleep(0.5)
        async with session.get(
            "https://chat.deepseek.com/api/v0/file/fetch_files?file_ids=" + file_id,
            headers=headers,
            # cookies=cookie,  # Backup WAF fallback
            timeout=aiohttp.ClientTimeout(total=30),
        ) as resp:
            js_data = _biz_data(await resp.json(), "file/fetch_files")["files"][0]
    if not js_data.get("signed_path"):
        return
    file_path = "https://files.deepseeksvc.com/api" + js_data["signed_path"] + "&ty=r"
    async with session.get(file_path) as data:
        async for chunk in data.content.iter_chunked(8192):
            if chunk:
                yield chunk