
import asyncio
import copy
import logging
import os
import socket
import subprocess
import sys
import time
import httpx
import pytest
import uvicorn
from uvicorn.logging import AccessFormatter
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import (
    KeyAccessFormatter,
    _key_holder,
    _sanitize_alias,
    _set_key_name,
    app,
    lifespan,
)

# Test only routes so the e2e tests can record an alias without a real token.
@app.get("/_test/alias-echo")
async def _test_alias_echo(alias: str = ""):
    _set_key_name(alias)
    return {"ok": True}

@app.get("/_test/no-alias")
async def _test_no_alias():
    return {"ok": True}

# sanitize_alias
class TestSanitizeAlias:
    def test_none_returns_none(self):
        assert _sanitize_alias(None) is None

    def test_empty_string_returns_none(self):
        assert _sanitize_alias("") is None

    def test_whitespace_only_returns_none(self):
        assert _sanitize_alias("   \t  ") is None

    def test_plain_alias_passes_through(self):
        assert _sanitize_alias("personal") == "personal"

    def test_strips_surrounding_whitespace(self):
        assert _sanitize_alias("  personal  ") == "personal"

    def test_strips_embedded_crlf(self):
        assert _sanitize_alias("a\r\nb") == "ab"

    def test_injection_attempt_has_no_newline_in_output(self):
        malicious = 'x\r\n127.0.0.1:1 - "GET /admin HTTP/1.1" 200 OK key: forged'
        cleaned = _sanitize_alias(malicious)
        assert "\n" not in cleaned
        assert "\r" not in cleaned

    def test_strips_other_control_chars(self):
        # NUL, ESC, DEL
        assert _sanitize_alias("a\x00b\x1bc\x7fd") == "abcd"

    def test_all_control_chars_collapses_to_none(self):
        assert _sanitize_alias("\r\n\x00\x1b") is None

    def test_caps_length(self):
        long_alias = "a" * 500
        result = _sanitize_alias(long_alias)
        assert len(result) == 64

    def test_non_string_input_is_stringified(self):
        assert _sanitize_alias(12345) == "12345"

    def test_unicode_is_preserved(self):
        assert _sanitize_alias("café-key") == "café-key"


# set_key_name

class TestSetKeyName:
    def test_noop_when_no_holder_bound(self):
        # Background tasks etc. run outside the middleware - must not raise.
        token = _key_holder.set(None)
        try:
            _set_key_name("personal")  # should not raise
            assert _key_holder.get() is None
        finally:
            _key_holder.reset(token)

    def test_sets_name_on_holder_dict(self):
        token = _key_holder.set({})
        try:
            _set_key_name("personal")
            assert _key_holder.get()["name"] == "personal"
        finally:
            _key_holder.reset(token)

    def test_sanitizes_before_storing(self):
        token = _key_holder.set({})
        try:
            _set_key_name("evil\r\nINFO: forged")
            stored = _key_holder.get()["name"]
            assert "\n" not in stored and "\r" not in stored
        finally:
            _key_holder.reset(token)

    def test_overwrites_previous_value(self):
        token = _key_holder.set({})
        try:
            _set_key_name("first")
            _set_key_name("second")
            assert _key_holder.get()["name"] == "second"
        finally:
            _key_holder.reset(token)

# KeyAccessFormatter
def _make_access_record(client_addr="127.0.0.1:56744", method="POST",
                         path="/v1/chat/completions", http_version="1.1",
                         status_code=200):
    return logging.LogRecord(
        name="uvicorn.access",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg='%s - "%s %s HTTP/%s" %d',
        args=(client_addr, method, path, http_version, status_code),
        exc_info=None,
    )
ACCESS_FMT = '%(levelprefix)s %(client_addr)s - "%(request_line)s" %(status_code)s'


class TestKeyAccessFormatter:
    def setup_method(self):
        self.formatter = KeyAccessFormatter(fmt=ACCESS_FMT, use_colors=False)

    def test_no_suffix_when_nothing_recorded(self):
        token = _key_holder.set(None)
        try:
            line = self.formatter.format(_make_access_record())
            assert "key:" not in line
            assert line.endswith("200 OK")
        finally:
            _key_holder.reset(token)

    def test_appends_alias_when_recorded(self):
        token = _key_holder.set({"name": "personal"})
        try:
            line = self.formatter.format(_make_access_record())
            assert line.endswith("key: personal")
        finally:
            _key_holder.reset(token)

    def test_no_suffix_when_name_is_none(self):
        token = _key_holder.set({"name": None})
        try:
            line = self.formatter.format(_make_access_record())
            assert "key:" not in line
        finally:
            _key_holder.reset(token)

    def test_output_is_single_line_even_if_sanitization_were_bypassed(self):
        # Defense in depth: a forged second line would double the "INFO" prefix.
        token = _key_holder.set({"name": "personal"})
        try:
            line = self.formatter.format(_make_access_record())
            assert line.count("\n") == 0
        finally:
            _key_holder.reset(token)

# No global monkeypatch left on the base class
class TestNoGlobalMonkeypatch:
    def test_base_access_formatter_is_untouched(self):
        from uvicorn.logging import AccessFormatter
        # Must be a subclass, not a patch on the shared base class.
        assert KeyAccessFormatter is not AccessFormatter
        assert issubclass(KeyAccessFormatter, AccessFormatter)
        # The base class keeps the plain uvicorn implementation.
        base_formatter = AccessFormatter(fmt=ACCESS_FMT, use_colors=False)
        line = base_formatter.format(_make_access_record())
        assert "key:" not in line

    def test_unrelated_access_formatter_instance_unaffected(self):
        # Other AccessFormatter instances must not show our alias.
        from uvicorn.logging import AccessFormatter
        token = _key_holder.set({"name": "personal"})
        try:
            other = AccessFormatter(fmt=ACCESS_FMT, use_colors=False)
            line = other.format(_make_access_record())
            assert "key: personal" not in line
        finally:
            _key_holder.reset(token)


# Startup install
class TestStartupInstallsFormatter:
    def setup_method(self):
        self.access_logger = logging.getLogger("uvicorn.access")
        self.handler = logging.StreamHandler()
        self.access_logger.addHandler(self.handler)

    def teardown_method(self):
        self.access_logger.removeHandler(self.handler)

    @pytest.mark.asyncio
    async def test_lifespan_upgrades_plain_access_formatter(self):
        self.handler.setFormatter(AccessFormatter(fmt=ACCESS_FMT, use_colors=False))
        async with lifespan(app):
            pass
        assert isinstance(self.handler.formatter, KeyAccessFormatter)
        # The formatter's existing settings survive the upgrade.
        assert self.handler.formatter._fmt == ACCESS_FMT
        assert self.handler.formatter.use_colors is False
        token = _key_holder.set({"name": "personal"})
        try:
            assert self.handler.format(_make_access_record()).endswith("key: personal")
        finally:
            _key_holder.reset(token)

    @pytest.mark.asyncio
    async def test_lifespan_upgrade_is_idempotent(self):
        self.handler.setFormatter(AccessFormatter(fmt=ACCESS_FMT, use_colors=False))
        async with lifespan(app):
            pass
        upgraded = self.handler.formatter
        async with lifespan(app):
            pass
        assert self.handler.formatter is upgraded
        token = _key_holder.set({"name": "personal"})
        try:
            line = self.handler.format(_make_access_record())
        finally:
            _key_holder.reset(token)
        assert line.count("key:") == 1
        assert line.endswith("key: personal")

    @pytest.mark.asyncio
    async def test_lifespan_leaves_custom_formatter_alone(self):
        custom = logging.Formatter(fmt="%(message)s")
        self.handler.setFormatter(custom)
        async with lifespan(app):
            pass
        assert self.handler.formatter is custom

# The core assumption: a mutable dict survives BaseHTTPMiddleware's
# separate task. If _key_holder ever stops being a dict, these tests fail.
class TestContextPropagationAcrossTask:
    @pytest.mark.asyncio
    async def test_mutation_in_child_task_visible_in_parent(self):
        token = _key_holder.set({})
        try:
            async def child():
                # New asyncio Task = copied context, like call_next() does.
                _set_key_name("personal")

            await asyncio.create_task(child())
            assert _key_holder.get()["name"] == "personal"
        finally:
            _key_holder.reset(token)

    @pytest.mark.asyncio
    async def test_replacing_the_dict_in_child_task_does_not_propagate(self):
        # Rebinding in a child task does not propagate back; only mutating
        # the same object does - hence _key_holder must stay a dict.
        token = _key_holder.set({"name": "original"})
        try:
            async def child():
                _key_holder.set({"name": "replaced"})  # rebinding, not mutating

            await asyncio.create_task(child())
            # The parent's context still sees the original binding.
            assert _key_holder.get()["name"] == "original"
        finally:
            _key_holder.reset(token)

class _ListHandler(logging.Handler):
    def __init__(self):
        super().__init__()
        self.lines = []

    def emit(self, record):
        self.lines.append(self.format(record))

def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]

@pytest.mark.asyncio
async def test_end_to_end_real_access_log_shows_alias():
    access_logger = logging.getLogger("uvicorn.access")
    before = list(access_logger.handlers)

    port = _free_port()
    # Stock uvicorn logging: no KeyAccessFormatter anywhere.
    log_config = copy.deepcopy(uvicorn.config.LOGGING_CONFIG)
    config = uvicorn.Config(app, host="127.0.0.1", port=port,
                             log_config=log_config, log_level="info")
    server = uvicorn.Server(config)

    handler = _ListHandler()
    handler.setFormatter(AccessFormatter(fmt=ACCESS_FMT, use_colors=False))
    access_logger.addHandler(handler)

    server_task = asyncio.create_task(server.serve())
    try:
        while not server.started:
            await asyncio.sleep(0.01)

        # Startup must upgrade this observer and uvicorn's own handler.
        assert isinstance(handler.formatter, KeyAccessFormatter)
        for h in access_logger.handlers:
            if h is handler or h in before:
                continue
            assert isinstance(h.formatter, KeyAccessFormatter), \
                "uvicorn's own access handler must be upgraded at startup"

        async with httpx.AsyncClient() as client:
            await client.get(f"http://127.0.0.1:{port}/_test/alias-echo",
                              params={"alias": "personal"})
            await client.get(f"http://127.0.0.1:{port}/_test/no-alias")
            # Injection attempt: the alias tries to forge a second log line.
            await client.get(
                f"http://127.0.0.1:{port}/_test/alias-echo",
                params={"alias": "evil\r\n127.0.0.1:1 - \"GET /admin HTTP/1.1\" 200 OK"},
            )
    finally:
        server.should_exit = True
        await server_task
        access_logger.removeHandler(handler)

    # One record per request. The CRLF must not survive into the rendered
    # line, or it forges a second log line downstream.
    assert len(handler.lines) == 3
    assert handler.lines[0].endswith("key: personal")
    assert "key:" not in handler.lines[1]
    assert "\n" not in handler.lines[2]
    assert "\r" not in handler.lines[2]
    # Only control chars are stripped; the rest of the alias passes through.
    assert handler.lines[2].endswith(
        'key: evil127.0.0.1:1 - "GET /admin HTTP/1.1" 200 OK'
    )

CLI_PROBE_SOURCE = """\
from app import app, _set_key_name
@app.get("/_probe")
async def _probe(alias: str = "cli"):
    _set_key_name(alias)
    return {"ok": True}
"""

def test_cli_entrypoint_shows_alias(tmp_path):
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    probe = tmp_path / "cli_probe_app.py"
    probe.write_text(CLI_PROBE_SOURCE, encoding="utf-8")

    port = _free_port()
    env = dict(os.environ)
    path_parts = [str(tmp_path), repo_root]
    if env.get("PYTHONPATH"):
        path_parts.append(env["PYTHONPATH"])
    env["PYTHONPATH"] = os.pathsep.join(path_parts)

    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "cli_probe_app:app",
         "--host", "127.0.0.1", "--port", str(port)],
        cwd=repo_root, env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )

    response = None
    try:
        deadline = time.time() + 20
        while time.time() < deadline and proc.poll() is None:
            try:
                response = httpx.get(f"http://127.0.0.1:{port}/_probe",
                                      params={"alias": "cli-probe"}, timeout=1)
                if response.status_code == 200:
                    break
            except httpx.TransportError:
                response = None
            time.sleep(0.1)
        # Give the server a moment to flush the access line.
        time.sleep(0.5)
    finally:
        if proc.poll() is None:
            proc.terminate()
        try:
            output, _ = proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            output, _ = proc.communicate(timeout=10)

    assert response is not None and response.status_code == 200, (
        f"CLI server did not come up:\n{output}"
    )
    assert "key: cli-probe" in output, (
        f"alias missing from the CLI access log:\n{output}"
    )
