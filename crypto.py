"""Stage 1.6 — Encrypted credentials at rest (Fernet).

Ported from the web2api-free/crypto.py pattern to this project's SQLite token
table. Passwords enter storage in Stage 1 (login-by-email/mobile), so secrets
are encrypted from day one instead of being retrofitted late.

Schema generations:
  v1 (legacy)  tokens(token) holds plaintext; no credential columns.
  v2 (current) tokens gained email/mobile/area_code/password_enc/kind/
               error_count/last_probe_at/last_login_at/last_error, and
               credential values are stored as "enc:v1:<fernet-ciphertext>".

Key material, in order:
  1. DEEPSEEKER_FERNET_KEY env var (base64 Fernet key) — for deployments that
     manage secrets externally (Docker secrets, K8s, systemd).
  2. Key file next to the DB (<db>.key, chmod 600), auto-generated on first
     use and reused forever after.
  3. If the `cryptography` package is missing, the system degrades to v1
     (plaintext) with a loud one-time warning and meta.crypto='off'. A
     self-hosted proxy must keep serving even when optional crypto deps are
     broken; the degraded state is explicit in the meta table and in logs.

Migration (migrate_credentials_at_rest, called from functions.init_db):
  - ALTERs any missing v2 columns in.
  - Encrypts legacy plaintext token values in place (passwords cannot exist
    in v1, so there is nothing legacy to encrypt there).
  - Before touching anything it snapshots the DB file to <db>.bak (WAL is
    checkpointed first so the copy is complete). An existing .bak is never
    overwritten — the first, riskiest migration's snapshot is preserved.
    Rollback = stop the process and copy <db>.bak back over the DB file.
"""

import base64
import logging
import os
import shutil
import sqlite3

logger = logging.getLogger("deeperseeker.crypto")

ENC_PREFIX = "enc:v1:"
_DB_PATH = os.getenv("DB_PATH", "deeperseeker.db")

_fernet = None
_fernet_initialized = False
_degraded = False


class CryptoUnavailable(Exception):
    """The `cryptography` package is not importable; secrets stay plaintext."""


class CredentialDecryptError(Exception):
    """A stored credential could not be decrypted (wrong/rotated key)."""


def _db_path():
    return _DB_PATH


def _key_file():
    return _db_path() + ".key"


def crypto_available():
    try:
        import cryptography  # noqa: F401
    except ImportError:
        return False
    return True


def _load_or_create_key():
    """Resolve the Fernet key: env > key file > generate. Returns None when
    the cryptography package is unavailable (degraded v1 mode)."""
    global _fernet, _fernet_initialized, _degraded
    if _fernet_initialized:
        return _fernet
    _fernet_initialized = True
    if not crypto_available():
        _degraded = True
        logger.warning(
            "cryptography package unavailable — credentials will be stored "
            "PLAINTEXT (v1 mode). Install it and restart to encrypt at rest."
        )
        return None
    from cryptography.fernet import Fernet

    key_b64 = (os.getenv("DEEPSEEKER_FERNET_KEY") or "").strip()
    if key_b64:
        logger.info("Using Fernet key from DEEPSEEKER_FERNET_KEY env")
    else:
        kf = _key_file()
        if os.path.exists(kf):
            with open(kf, "r") as f:
                key_b64 = f.read().strip()
            logger.info("Loaded Fernet key from %s", kf)
        else:
            key_b64 = Fernet.generate_key().decode()
            fd = os.open(kf, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as f:
                f.write(key_b64)
            logger.info("Generated new Fernet key at %s (chmod 600)", kf)
    _fernet = Fernet(key_b64)
    return _fernet


def reset_cache_for_tests():
    """Drop the memoized key so env/key-file changes take effect (tests only)."""
    global _fernet, _fernet_initialized, _degraded
    _fernet = None
    _fernet_initialized = False
    _degraded = False


def is_encrypted(stored):
    return isinstance(stored, str) and stored.startswith(ENC_PREFIX)


def encrypt_value(plaintext):
    """Encrypt a credential for storage. Passthrough when crypto is degraded."""
    if plaintext is None:
        return None
    f = _load_or_create_key()
    if f is None:
        return plaintext
    if is_encrypted(plaintext):  # idempotent — never double-wrap
        return plaintext
    token = f.encrypt(plaintext.encode("utf-8")).decode("ascii")
    return ENC_PREFIX + token


def decrypt_value(stored):
    """Decrypt a stored credential. Legacy plaintext (no prefix) passes
    through untouched so v1 rows keep working during/after migration."""
    if stored is None:
        return None
    if not is_encrypted(stored):
        return stored
    f = _load_or_create_key()
    if f is None:
        raise CredentialDecryptError(
            "Credential is encrypted but the cryptography package is missing"
        )
    try:
        return f.decrypt(stored[len(ENC_PREFIX):].encode("ascii")).decode("utf-8")
    except Exception as e:
        raise CredentialDecryptError(
            "Failed to decrypt stored credential — is DEEPSEEKER_FERNET_KEY "
            "the same key that encrypted it? Restore <db>.key or the .bak."
        ) from e


def _backup_db(conn):
    """Snapshot the DB file to <db>.bak (never overwrites an existing .bak).
    WAL is checkpointed first so the main file is self-contained."""
    db = _db_path()
    bak = db + ".bak"
    if os.path.exists(bak):
        logger.info("Backup %s already exists — keeping the original snapshot", bak)
        return bak
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    except sqlite3.Error:
        pass  # non-WAL or in-memory DB; copy what is there
    shutil.copy2(db, bak)
    logger.info("Pre-migration snapshot written to %s", bak)
    return bak


V2_COLUMNS = {
    "email": "TEXT",
    "mobile": "TEXT",
    "area_code": "TEXT",
    "password_enc": "TEXT",
    "kind": "TEXT DEFAULT 'manual'",
    "error_count": "INTEGER DEFAULT 0",
    "last_probe_at": "TEXT",
    "last_login_at": "TEXT",
    "last_error": "TEXT",
}


def migrate_credentials_at_rest():
    """Bring the tokens table from v1 to v2 (idempotent; runs on startup).

    1. ALTER in any missing v2 columns.
    2. Encrypt legacy plaintext token values in place (crypto permitting).
    3. Record the generation in the meta table.
    A <db>.bak snapshot is taken before the first modification. Failures are
    logged but never fatal — the proxy must start; decrypt_value() passes
    legacy plaintext through transparently.
    """
    db = _db_path()
    if not os.path.exists(db):
        return  # fresh install: init_db creates the v2 schema directly
    conn = sqlite3.connect(db, timeout=30)
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(tokens)").fetchall()}
        if not cols:
            return  # no tokens table yet (fresh DB created by init_db)
        missing = [c for c in V2_COLUMNS if c not in cols]
        plain_rows = conn.execute(
            "SELECT id FROM tokens WHERE token IS NOT NULL AND token != '' "
            "AND token NOT LIKE ?",
            (ENC_PREFIX + "%",),
        ).fetchall()
        encryptable = bool(plain_rows) and crypto_available() and bool(_load_or_create_key())

        if not missing and not encryptable:
            _record_meta(conn)
            return

        _backup_db(conn)
        for col in missing:
            conn.execute(f"ALTER TABLE tokens ADD COLUMN {col} {V2_COLUMNS[col]}")
            logger.info("Migration: added tokens.%s", col)
        if encryptable:
            for (row_id,) in plain_rows:
                stored = conn.execute(
                    "SELECT token FROM tokens WHERE id = ?", (row_id,)
                ).fetchone()[0]
                conn.execute(
                    "UPDATE tokens SET token = ? WHERE id = ?",
                    (encrypt_value(stored), row_id),
                )
            logger.info("Migration: encrypted %d token value(s) at rest", len(plain_rows))
        elif plain_rows:
            logger.warning(
                "Migration: %d plaintext token(s) left as-is (crypto unavailable "
                "or key creation failed)", len(plain_rows),
            )
        _record_meta(conn)
        conn.commit()
        logger.info("tokens table is at schema v2 (crypto=%s)", "off" if _degraded else "on")
    except Exception:
        logger.exception("Credential migration failed (continuing in degraded mode)")
        try:
            conn.rollback()
        except Exception:
            pass
    finally:
        conn.close()


def _record_meta(conn):
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)"
        )
        conn.execute(
            "INSERT INTO meta (key, value) VALUES ('schema_version', '2') "
            "ON CONFLICT(key) DO UPDATE SET value = '2'"
        )
        conn.execute(
            "INSERT INTO meta (key, value) VALUES ('crypto', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            ("off" if _degraded else "on",),
        )
        conn.commit()
    except sqlite3.Error:
        logger.exception("Could not record schema meta (non-fatal)")
