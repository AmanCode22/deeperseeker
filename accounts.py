"""Stage 1 — Account lifecycle: login, refresh, heal (roadmap #1).

Everything a token needs to stay alive without a human re-pasting it:

  1.1  login_user()           pure-HTTP login against DeepSeek's mobile API
                              (email OR mobile + area code + password).
  1.2  refresh_account_token() upstream 401 -> one re-login, caller retries
                              once (wired in app.handle_chat's 401 branch).
  1.3  LoginLimiter           per-identifier exponential backoff + a global
                              sliding window so a flood of logins can never
                              hammer the auth endpoint.
  1.4  re_login_single()      heal one account by email/mobile identifier
                              (dashboard heal button + /accounts/relogin).
  1.5  probe_account()        health probe = a real create_session attempt;
                              errors accrue and at the threshold the account
                              auto-recovers via re-login (or is parked as
                              RATE_LIMITED when it has no credentials).
       auto_heal_loop()       background sweeper started from app lifespan.

Login payload/unwrap reference: ds2api internal/deepseek/client/client_auth.go
(POST /api/v0/users/login; code==0 -> data.biz_code==0 ->
data.biz_data.user.token; device_id "deepseek_to_api", os "android").
"""

import asyncio
import logging
import os
import time
from collections import deque

import aiohttp

from functions import (
    add_account,
    create_new_chat,
    find_account_by_identifier,
    get_account_credentials,
    get_headers,
    get_probe_candidates,
    mark_limited,
    mark_probe_fail,
    mark_probe_ok,
    normalize_area_code,
    normalize_mobile,
    post_with_failover,
    update_token_value,
)

logger = logging.getLogger("deeperseeker.accounts")

LOGIN_PATH = "/api/v0/users/login"
LOGIN_DEVICE_ID = "deepseek_to_api"
LOGIN_OS = "android"

LOGIN_TIMEOUT = aiohttp.ClientTimeout(total=20)


class LoginError(Exception):
    """Login/refresh failure with a machine-readable code.

    Codes: bad_credentials (upstream 401/400), upstream_error (5xx / network),
    invalid_response (unexpected payload shape), cooldown (limiter refused),
    not_found (no account for identifier), no_credentials (manual token)."""

    def __init__(self, code, message=None, retry_after=None):
        super().__init__(message or code)
        self.code = code
        self.retry_after = retry_after


# ==============================================================================
# 1.1 — Pure-HTTP login
# ==============================================================================

def build_login_payload(password, email=None, mobile=None, area_code=None):
    """Payload per ds2api client_auth.go: exactly one identifier plus the
    fixed android-app identity. Mobile is normalized to bare digits."""
    if not password:
        raise ValueError("password is required")
    if bool(email) == bool(mobile):
        raise ValueError("exactly one of email / mobile is required")
    payload = {
        "password": password,
        "device_id": LOGIN_DEVICE_ID,
        "os": LOGIN_OS,
    }
    if email:
        payload["email"] = str(email).strip()
    else:
        area = normalize_area_code(area_code)
        payload["mobile"] = normalize_mobile(mobile, area)
        payload["area_code"] = area
    return payload


async def login_user(password, email=None, mobile=None, area_code=None, session=None):
    """Login and return {'token': ..., 'user_id': ...}. Raises LoginError with
    a stable .code so callers (API endpoints, refresh paths) can map it to an
    HTTP status without string-matching."""
    payload = build_login_payload(password, email, mobile, area_code)
    ident = payload.get("email") or f"+{payload.get('area_code', '86')} {payload.get('mobile')}"
    headers = get_headers(None)  # android spoof headers, no bearer yet
    resp = await post_with_failover(
        LOGIN_PATH, headers=headers, json=payload, session=session, timeout=LOGIN_TIMEOUT,
    )
    async with resp:
        if resp.status in (400, 401, 403):
            logger.warning("Login failed for %s: HTTP %d (bad credentials)", ident, resp.status)
            raise LoginError("bad_credentials", f"Upstream rejected credentials (HTTP {resp.status})")
        if resp.status != 200:
            body = ""
            try:
                body = (await resp.text())[:200]
            except (aiohttp.ClientError, asyncio.TimeoutError):
                pass
            logger.warning("Login failed for %s: HTTP %d %s", ident, resp.status, body)
            raise LoginError("upstream_error", f"Login endpoint returned HTTP {resp.status}")
        try:
            data = await resp.json()
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as e:
            raise LoginError("invalid_response", f"Login response is not JSON: {e}") from e

    # Unwrap chain: code==0 -> data.biz_code==0 -> data.biz_data.user.token
    if not isinstance(data, dict) or data.get("code") != 0:
        msg = (data or {}).get("msg") if isinstance(data, dict) else str(data)[:200]
        raise LoginError("invalid_response", f"Login envelope code != 0: {msg}")
    inner = data.get("data") or {}
    if inner.get("biz_code") != 0:
        raise LoginError(
            "invalid_response",
            f"Login biz_code={inner.get('biz_code')}: {inner.get('biz_msg')}",
        )
    biz = inner.get("biz_data") or {}
    user = biz.get("user") or {}
    token = user.get("token")
    if not token:
        raise LoginError("invalid_response", "Login response missing data.biz_data.user.token")
    logger.info("Login OK for %s (user_id=%s)", ident, user.get("id"))
    return {"token": token, "user_id": user.get("id")}


def identifier_for(account):
    """Canonical limiter/log identifier for an account row dict."""
    if account.get("email"):
        return account["email"]
    if account.get("mobile"):
        return f"+{account.get('area_code') or '86'} {account['mobile']}"
    return f"token#{account.get('id')}"


# ==============================================================================
# 1.3 — Login cooldown limiter (per-identifier backoff + global window)
# ==============================================================================

class LoginLimiter:
    """Rate-limits login attempts.

    Per identifier: after each failure the next attempt is blocked for
    min(base * 2^(failures-1), max) seconds — 5s, 10s, 20s, ... capped.
    Globally: at most global_max attempts per global_window seconds across
    ALL identifiers, so a pool-wide credential storm can't hammer the auth
    endpoint. In-memory only: a restart clears cooldowns (fail-safe — a
    rebooted proxy is not mid-flood).
    """

    def __init__(self, base=None, max_cooldown=None, global_max=None,
                 global_window=None, now_fn=time.monotonic):
        self.base = float(base if base is not None else os.getenv("DEEPSEEKER_LOGIN_COOLDOWN_BASE", "5"))
        self.max_cooldown = float(max_cooldown if max_cooldown is not None else os.getenv("DEEPSEEKER_LOGIN_COOLDOWN_MAX", "300"))
        self.global_max = int(global_max if global_max is not None else os.getenv("DEEPSEEKER_LOGIN_GLOBAL_MAX", "10"))
        self.global_window = float(global_window if global_window is not None else os.getenv("DEEPSEEKER_LOGIN_GLOBAL_WINDOW", "60"))
        self._now = now_fn
        self._failures = {}
        self._until = {}
        self._window = deque()

    def _global_slot(self, now):
        while self._window and now - self._window[0] >= self.global_window:
            self._window.popleft()
        if len(self._window) >= self.global_max:
            retry_after = max(0.0, self.global_window - (now - self._window[0]))
            raise LoginError(
                "cooldown",
                f"Global login rate limit ({self.global_max}/{self.global_window:.0f}s) reached",
                retry_after=retry_after,
            )
        self._window.append(now)

    def acquire(self, identifier):
        """Reserve one login attempt or raise LoginError('cooldown')."""
        now = self._now()
        until = self._until.get(identifier, 0.0)
        if now < until:
            raise LoginError(
                "cooldown",
                f"Identifier {identifier} is in login cooldown",
                retry_after=until - now,
            )
        self._global_slot(now)

    def report_success(self, identifier):
        self._failures[identifier] = 0
        self._until[identifier] = 0.0

    def report_failure(self, identifier):
        n = self._failures.get(identifier, 0) + 1
        self._failures[identifier] = n
        cooldown = min(self.base * (2 ** (n - 1)), self.max_cooldown)
        self._until[identifier] = self._now() + cooldown
        logger.warning(
            "Login failed for %s (streak %d) — cooldown %.0fs", identifier, n, cooldown,
        )

    def cooldown_remaining(self, identifier):
        return max(0.0, self._until.get(identifier, 0.0) - self._now())


login_limiter = LoginLimiter()


# ==============================================================================
# 1.2 + 1.4 — Re-login: refresh-on-401 and heal-by-identifier
# ==============================================================================

async def _login_and_update(account, limiter):
    """Shared re-login core: acquire limiter slot, login with stored
    credentials, persist the fresh token. Raises LoginError on failure."""
    ident = identifier_for(account)
    limiter.acquire(ident)
    try:
        result = await login_user(
            account["password"],
            email=account.get("email"),
            mobile=account.get("mobile"),
            area_code=account.get("area_code"),
        )
    except LoginError as e:
        if e.code != "cooldown":
            limiter.report_failure(ident)
        raise
    limiter.report_success(ident)
    update_token_value(account["id"], result["token"])
    logger.info("Token #%d re-logged-in via %s", account["id"], ident)
    return result


async def refresh_account_token(token_id, limiter=None):
    """Stage 1.2: one re-login for a dead bearer token. Returns True when the
    token row now holds a fresh token (caller may retry the request once);
    False when there are no stored credentials, the limiter refused, or the
    login failed (caller falls back to mark_limited + rotate)."""
    limiter = limiter or login_limiter
    account = get_account_credentials(token_id)
    if not account or not account.get("password"):
        return False
    try:
        await _login_and_update(account, limiter)
        return True
    except LoginError as e:
        logger.warning("Refresh failed for token #%d: %s", token_id, e.code)
        return False
    except (aiohttp.ClientError, asyncio.TimeoutError) as e:
        logger.warning("Refresh network failure for token #%d: %s", token_id, e)
        return False


async def re_login_single(identifier, limiter=None):
    """Stage 1.4: heal one account by email or mobile. Raises LoginError:
    not_found / no_credentials / cooldown / bad_credentials / upstream_error.
    Returns {'token_id': ..., 'user_id': ...} on success."""
    limiter = limiter or login_limiter
    row = find_account_by_identifier(identifier)
    if not row:
        raise LoginError("not_found", f"No account for identifier {identifier!r}")
    account = get_account_credentials(row[0])
    if not account or not account.get("password"):
        raise LoginError("no_credentials",
                         f"Token #{row[0]} was added manually — paste a new token or delete it")
    result = await _login_and_update(account, limiter)
    return {"token_id": account["id"], "user_id": result.get("user_id")}


# ==============================================================================
# 1.5 — Health probe: real create_session attempt + error accrual + auto-heal
# ==============================================================================

PROBE_INTERVAL = float(os.getenv("DEEPSEEKER_PROBE_INTERVAL", "300"))
PROBE_SWEEP = float(os.getenv("DEEPSEEKER_PROBE_SWEEP", "60"))
PROBE_ERROR_THRESHOLD = int(os.getenv("DEEPSEEKER_PROBE_ERROR_THRESHOLD", "3"))

_probe_lock = asyncio.Lock()


def _http_code(exc):
    import re
    m = re.match(r"HTTP (\d{3}):", str(exc))
    return int(m.group(1)) if m else None


async def probe_account(token_id, limiter=None):
    """Probe one account with a REAL create_session attempt (the same call a
    chat would make — anything weaker lies about health). Error counts accrue
    per failure; at PROBE_ERROR_THRESHOLD the account auto-recovers via
    re-login when it has credentials, or is parked RATE_LIMITED when it
    doesn't (pick_token skips parked rows). Returns a status string:
    ok / recovered / degraded / dead."""
    limiter = limiter or login_limiter
    account = get_account_credentials(token_id)
    if not account:
        return "gone"
    try:
        await create_new_chat(account["token"])
    except (aiohttp.ClientError, asyncio.TimeoutError) as e:
        mark_probe_fail(token_id, f"network: {e}")
        logger.warning("Probe of token #%d hit a network error: %s", token_id, e)
        return "degraded"
    except Exception as e:
        count = mark_probe_fail(token_id, str(e))
        code = _http_code(e)
        logger.warning("Probe of token #%d failed (HTTP %s, error %d/%d): %s",
                       token_id, code, count, PROBE_ERROR_THRESHOLD, str(e)[:120])
        if code in (401, 403) and count >= PROBE_ERROR_THRESHOLD:
            if account.get("password"):
                try:
                    await _login_and_update(account, limiter)
                    logger.info("Auto-recovery: token #%d re-logged-in after %d failed probes",
                                token_id, count)
                    return "recovered"
                except LoginError as le:
                    logger.warning("Auto-recovery failed for token #%d: %s", token_id, le.code)
            mark_limited(token_id)
            return "dead"
        return "degraded"
    was = account.get("status")
    was_errors = account.get("error_count") or 0
    mark_probe_ok(token_id)
    return "recovered" if (was == "RATE_LIMITED" or was_errors) else "ok"


async def probe_stale_accounts(limiter=None):
    """Probe every account whose last probe is older than PROBE_INTERVAL.
    Sequential by design — probes must never stampede the upstream."""
    import datetime as _dt
    cutoff = (_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(seconds=PROBE_INTERVAL)).isoformat()
    candidates = get_probe_candidates(cutoff)
    results = []
    for token_id in candidates:
        results.append((token_id, await probe_account(token_id, limiter)))
    return results


async def auto_heal_loop():
    """Background sweeper (started from app lifespan): every PROBE_SWEEP
    seconds, probe whatever is due. One sweep at a time (_probe_lock); any
    exception is logged and the loop keeps running — healing must never take
    the proxy down."""
    logger.info("Auto-heal loop started (sweep %.0fs, probe interval %.0fs, threshold %d)",
                PROBE_SWEEP, PROBE_INTERVAL, PROBE_ERROR_THRESHOLD)
    while True:
        await asyncio.sleep(PROBE_SWEEP)
        if _probe_lock.locked():
            continue
        async with _probe_lock:
            try:
                results = await probe_stale_accounts()
                if results:
                    summary = {}
                    for _, status in results:
                        summary[status] = summary.get(status, 0) + 1
                    logger.info("Health probe sweep: %d account(s), %s", len(results), summary)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Health probe sweep failed (non-fatal)")
