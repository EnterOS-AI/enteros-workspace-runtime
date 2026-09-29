"""Presigned-URL redaction for runtime logs (molecule_runtime/log_redaction.py).

The config-relay prelude fetches its bundle from a SigV4 presigned URL whose
query string (X-Amz-Credential, X-Amz-Signature) is the read capability for
the bundle. These tests pin that the redactor, the logging filter and the
installer keep that query out of every rendered log line while the scheme,
host and path — what a failure line needs to name — stay visible.

All values below are synthetic.
"""
from __future__ import annotations

import io
import logging
import sys
from pathlib import Path

import httpx
import pytest

_THIS = Path(__file__).resolve()
sys.path.insert(0, str(_THIS.parent.parent))

from molecule_runtime.log_redaction import (  # noqa: E402
    REDACTED,
    PresignedUrlRedactionFilter,
    describe_presigned_url,
    install_log_redaction,
    redact_presigned_urls,
)

SIGNATURE = "f00d" * 16  # 64 hex chars, the SigV4 signature shape
ACCESS_KEY_ID = "SYNTHETICKEYIDFORTESTS0000000000"
SECURITY_TOKEN = "SYNTHETICSESSIONTOKEN0000000000000000000000"
HOST = "bucket.example-account.r2.cloudflarestorage.com"
PATH = "/relay/ws-1/n0nce.json"
PRESIGNED = (
    f"https://{HOST}{PATH}"
    "?X-Amz-Algorithm=AWS4-HMAC-SHA256"
    f"&X-Amz-Credential={ACCESS_KEY_ID}%2F20260929%2Fauto%2Fs3%2Faws4_request"
    "&X-Amz-Date=20260929T180747Z&X-Amz-Expires=600&X-Amz-SignedHeaders=host"
    f"&x-id=GetObject&X-Amz-Signature={SIGNATURE}"
)


def _assert_no_capability(text: str) -> None:
    assert SIGNATURE not in text
    assert ACCESS_KEY_ID not in text
    assert SECURITY_TOKEN not in text


# --------------------------------------------------------------------------- #
# redact_presigned_urls
# --------------------------------------------------------------------------- #
def test_httpx_request_line_keeps_host_path_status_drops_query():
    line = f'HTTP Request: GET {PRESIGNED} "HTTP/1.1 403 Forbidden"'
    out = redact_presigned_urls(line)
    _assert_no_capability(out)
    assert out == f'HTTP Request: GET https://{HOST}{PATH}?{REDACTED} "HTTP/1.1 403 Forbidden"'


def test_url_quoted_in_exception_text_ends_at_the_quote():
    # httpx.HTTPStatusError renders as: Client error '403 Forbidden' for url '<url>'
    text = f"Client error '403 Forbidden' for url '{PRESIGNED}'\nFor more information check: x"
    out = redact_presigned_urls(text)
    _assert_no_capability(out)
    assert f"for url 'https://{HOST}{PATH}?{REDACTED}'\nFor more information check: x" in out


def test_repr_and_fragment_boundaries():
    text = f"<Request('GET', '{PRESIGNED}#frag')> and URL({PRESIGNED!r})"
    out = redact_presigned_urls(text)
    _assert_no_capability(out)
    assert out.count(f"https://{HOST}{PATH}?{REDACTED}") == 2


def test_session_token_url_and_mixed_case_param_names():
    url = f"HTTPS://{HOST}{PATH}?x-amz-security-token={SECURITY_TOKEN}&X-AMZ-SIGNATURE={SIGNATURE}"
    out = redact_presigned_urls(f"GET {url} done")
    _assert_no_capability(out)
    assert out == f"GET HTTPS://{HOST}{PATH}?{REDACTED} done"


def test_bare_credential_params_without_url_are_redacted():
    text = (
        f"query was X-Amz-Credential={ACCESS_KEY_ID}%2F20260929%2Fauto%2Fs3%2Faws4_request"
        f"&X-Amz-Date=20260929T180747Z&X-Amz-Signature={SIGNATURE} "
        f"and X-Amz-Security-Token%3D{SECURITY_TOKEN}"
    )
    out = redact_presigned_urls(text)
    _assert_no_capability(out)
    assert f"X-Amz-Credential={REDACTED}" in out
    assert f"X-Amz-Signature={REDACTED}" in out
    assert f"X-Amz-Security-Token%3D{REDACTED}" in out
    assert "X-Amz-Date=20260929T180747Z" in out  # non-secret timing kept


@pytest.mark.parametrize(
    "text",
    [
        "",
        "config-relay: fetch attempt 1/6 failed (HTTP 403); retrying",
        'HTTP Request: GET http://tenant.svc:8080/workspaces/ws-1/activity?type=a2a_receive "HTTP/1.1 200 OK"',
        "https://cp.example/cp/workspaces/ws-1/relay-ack",
    ],
)
def test_text_without_presigned_urls_is_unchanged(text):
    assert redact_presigned_urls(text) == text


# --------------------------------------------------------------------------- #
# describe_presigned_url
# --------------------------------------------------------------------------- #
def test_describe_keeps_host_path_and_presign_timing_only():
    out = describe_presigned_url(PRESIGNED)
    _assert_no_capability(out)
    assert out == f"https://{HOST}{PATH} (X-Amz-Date=20260929T180747Z, X-Amz-Expires=600)"


def test_describe_drops_userinfo_and_keeps_port():
    out = describe_presigned_url(f"https://user:pw@{HOST}:8443{PATH}?X-Amz-Signature={SIGNATURE}")
    _assert_no_capability(out)
    assert out == f"https://{HOST}:8443{PATH}"


def test_describe_ignores_malformed_timing_values():
    url = f"https://{HOST}{PATH}?X-Amz-Date=X-Amz-Signature%3D{SIGNATURE}&X-Amz-Expires=600"
    out = describe_presigned_url(url)
    _assert_no_capability(out)
    assert out == f"https://{HOST}{PATH} (X-Amz-Expires=600)"


@pytest.mark.parametrize("bad", ["", "not a url", "https://", "https://host:notaport/x"])
def test_describe_unparseable(bad):
    assert describe_presigned_url(bad) == "<unparseable URL>"


# --------------------------------------------------------------------------- #
# PresignedUrlRedactionFilter
# --------------------------------------------------------------------------- #
@pytest.fixture
def filtered_logger():
    """A private logger -> StringIO handler carrying the filter, like a root
    handler after install_log_redaction()."""
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
    handler.addFilter(PresignedUrlRedactionFilter())
    logger = logging.getLogger("test_log_redaction.private")
    logger.handlers[:] = [handler]
    logger.propagate = False
    logger.setLevel(logging.DEBUG)
    yield logger, stream
    logger.handlers[:] = []


def test_filter_redacts_message_built_from_args(filtered_logger):
    logger, stream = filtered_logger
    # Same call shape as httpx._client: the URL arrives as an httpx.URL arg.
    logger.info('HTTP Request: %s %s "%s %d %s"', "GET", httpx.URL(PRESIGNED), "HTTP/1.1", 403, "Forbidden")
    out = stream.getvalue()
    _assert_no_capability(out)
    assert f'GET https://{HOST}{PATH}?{REDACTED} "HTTP/1.1 403 Forbidden"' in out


def test_filter_redacts_exception_text(filtered_logger):
    logger, stream = filtered_logger
    try:
        raise RuntimeError(f"fetch failed for url '{PRESIGNED}'")
    except RuntimeError:
        logger.exception("relay fetch crashed")
    out = stream.getvalue()
    _assert_no_capability(out)
    assert "Traceback (most recent call last)" in out
    assert f"RuntimeError: fetch failed for url 'https://{HOST}{PATH}?{REDACTED}'" in out


def test_filter_redacts_stack_info_and_precomputed_exc_text(filtered_logger):
    logger, stream = filtered_logger
    record = logger.makeRecord(
        logger.name, logging.ERROR, __file__, 1, "plain message", None, None,
        sinfo=f"Stack (most recent call last):\n  url={PRESIGNED}",
    )
    record.exc_text = f"Traceback:\nValueError: {PRESIGNED}"
    logger.handle(record)
    out = stream.getvalue()
    _assert_no_capability(out)
    assert f"url=https://{HOST}{PATH}?{REDACTED}" in out
    assert f"ValueError: https://{HOST}{PATH}?{REDACTED}" in out


def test_filter_leaves_unrelated_records_untouched():
    record = logging.LogRecord("x", logging.INFO, __file__, 1, "value %s at %d%%", ("a", 5), None)
    assert PresignedUrlRedactionFilter().filter(record) is True
    assert record.msg == "value %s at %d%%"
    assert record.args == ("a", 5)
    assert record.exc_text is None


def test_filter_passes_malformed_record_through():
    record = logging.LogRecord("x", logging.INFO, __file__, 1, "%s %s", ("only-one",), None)
    assert PresignedUrlRedactionFilter().filter(record) is True


# --------------------------------------------------------------------------- #
# install_log_redaction
# --------------------------------------------------------------------------- #
@pytest.fixture
def isolated_logging_state():
    """Give the test a root handler at the main.py default level (INFO) and
    fresh httpx/httpcore loggers; restore what install_log_redaction mutates.

    Any test that imported molecule_runtime.main earlier in the session already
    ran install_log_redaction() on the root handlers present then. A handler
    filter rewrites the shared LogRecord for every handler after it, so those
    filters are lifted for the duration of the test and put back afterwards.
    """
    root = logging.getLogger()
    saved_root_level = root.level
    saved_handler_filters = {h: h.filters[:] for h in root.handlers}
    for h in root.handlers:
        h.filters[:] = [f for f in h.filters if not isinstance(f, PresignedUrlRedactionFilter)]
    saved = {
        name: (logging.getLogger(name).level, logging.getLogger(name).filters[:])
        for name in ("httpx", "httpcore")
    }
    for name in saved:
        logging.getLogger(name).setLevel(logging.NOTSET)
        logging.getLogger(name).filters[:] = []
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    yield handler, stream
    root.removeHandler(handler)
    root.setLevel(saved_root_level)
    for h, filters in saved_handler_filters.items():
        h.filters[:] = filters
    for name, (level, filters) in saved.items():
        logging.getLogger(name).setLevel(level)
        logging.getLogger(name).filters[:] = filters


def _presigned_fetch(status: int = 403) -> None:
    client = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(status)))
    with client:
        client.get(PRESIGNED)


def test_without_install_the_request_line_leaks(isolated_logging_state):
    """Pins the mechanism the guard exists for: at INFO, httpx itself writes the
    full presigned URL. If this stops holding, the other tests here prove less."""
    _handler, stream = isolated_logging_state
    _presigned_fetch()
    assert SIGNATURE in stream.getvalue()


def test_install_quiets_httpx_request_lines(isolated_logging_state):
    _handler, stream = isolated_logging_state
    install_log_redaction()
    assert logging.getLogger("httpx").level == logging.WARNING
    assert logging.getLogger("httpcore").level == logging.WARNING
    _presigned_fetch()
    assert "HTTP Request" not in stream.getvalue()


def test_install_redacts_when_httpx_is_turned_back_up(isolated_logging_state):
    _handler, stream = isolated_logging_state
    install_log_redaction()
    logging.getLogger("httpx").setLevel(logging.INFO)  # an operator debugging
    _presigned_fetch()
    out = stream.getvalue()
    _assert_no_capability(out)
    assert f"HTTP Request: GET https://{HOST}{PATH}?{REDACTED}" in out


def test_install_covers_a_root_handler_added_later_via_the_httpx_logger(isolated_logging_state):
    install_log_redaction()
    late_stream = io.StringIO()
    late = logging.StreamHandler(late_stream)
    # First in line, so no filtered root handler can have rewritten the shared
    # record before it: only the filter on the httpx logger itself protects it.
    logging.getLogger().handlers.insert(0, late)
    try:
        logging.getLogger("httpx").setLevel(logging.INFO)
        _presigned_fetch()
    finally:
        logging.getLogger().removeHandler(late)
    _assert_no_capability(late_stream.getvalue())
    assert f"https://{HOST}{PATH}?{REDACTED}" in late_stream.getvalue()


def test_install_is_idempotent(isolated_logging_state):
    handler, _stream = isolated_logging_state
    install_log_redaction()
    install_log_redaction()
    assert sum(isinstance(f, PresignedUrlRedactionFilter) for f in handler.filters) == 1
    assert sum(
        isinstance(f, PresignedUrlRedactionFilter) for f in logging.getLogger("httpx").filters
    ) == 1
