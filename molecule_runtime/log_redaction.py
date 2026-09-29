"""Keep presigned object-store URLs out of runtime logs.

The config-relay prelude (``config_relay.py``) GETs the workspace config
bundle — config.yaml, prompts and secret config files — from a short-TTL
presigned R2/S3 URL. A SigV4 presigned URL carries its authorization in the
query string (``X-Amz-Credential``, ``X-Amz-Signature``, and for session
credentials ``X-Amz-Security-Token``): anyone holding the full URL can read the
bundle until ``X-Amz-Date + X-Amz-Expires``.

httpx logs every request at INFO as ``HTTP Request: GET <full URL> "HTTP/1.1
<status> <reason>"``, and the logging bootstrap in ``main.py`` sets the root
logger to ``LOG_LEVEL`` (default INFO) on stdout, which the cluster log
collector ships to Loki. Every relay fetch attempt therefore wrote the full
presigned URL, signature and credential included, to the log store.

This module is the guard, in two layers:

* ``install_log_redaction()`` raises the ``httpx`` and ``httpcore`` loggers to
  WARNING, so per-request lines are not emitted at the default level.
* ``PresignedUrlRedactionFilter`` rewrites any log record whose message,
  formatted exception or stack text contains a presigned URL: the query string
  of a URL carrying ``X-Amz-*`` parameters becomes ``<redacted>`` while scheme,
  host and path stay, so a failure line still names what was fetched. It is
  attached to the root logger's handlers and to the ``httpx`` logger, so it
  still applies when a level is lowered for debugging.

Error text built outside logging (the relay's own boot lines and its
``SystemExit`` message) uses ``redact_presigned_urls`` and
``describe_presigned_url`` directly.
"""
from __future__ import annotations

import logging
import re
from urllib.parse import parse_qsl, urlsplit

REDACTED = "<redacted>"

# An http(s) URL that has a query string. Group 1 = scheme://authority/path,
# group 2 = the query. Both stop at whitespace, quotes and angle brackets, so a
# URL quoted inside exception text (httpx: "... for url 'https://…'") ends at
# its closing quote; the query also stops at the fragment marker.
_URL_WITH_QUERY_RE = re.compile(r"""(?i)\b(https?://[^\s?#'"<>]+)\?([^\s#'"<>]*)""")

# A credential-bearing SigV4 parameter that appears without its URL (a query
# string logged on its own, or a URL whose scheme was cut off). Matches the
# plain and the percent-encoded ("%3D") form of "=".
_AMZ_SECRET_PARAM_RE = re.compile(
    r"(?i)\b(x-amz-(?:signature|credential|security-token)(?:=|%3d))[^&\s'\"<>]+"
)

# Presign parameters that are not secret and date the URL, with the value shape
# each must have to be printed. describe_presigned_url keeps them so an
# expired-presign 403 can be told apart from any other 403.
_PRESIGN_TIMING_PARAMS = {
    "x-amz-date": re.compile(r"\d{8}T\d{6}Z"),
    "x-amz-expires": re.compile(r"\d{1,7}"),
}

# Loggers whose per-request INFO lines print the full request URL.
_QUIET_LOGGERS = ("httpx", "httpcore")

_EXC_FORMATTER = logging.Formatter()


def redact_presigned_urls(text: str) -> str:
    """Return ``text`` with every presigned URL's query string redacted.

    A URL counts as presigned when its query contains an ``X-Amz-`` parameter
    (case-insensitive); its whole query is replaced by ``<redacted>`` and the
    scheme, host and path are kept. Any ``X-Amz-Signature``,
    ``X-Amz-Credential`` or ``X-Amz-Security-Token`` value left outside such a
    URL is replaced as well. URLs without ``X-Amz-`` parameters are unchanged.
    """
    if not text or "x-amz-" not in text.lower():
        return text

    def _strip_query(match: re.Match[str]) -> str:
        if "x-amz-" in match.group(2).lower():
            return f"{match.group(1)}?{REDACTED}"
        return match.group(0)

    text = _URL_WITH_QUERY_RE.sub(_strip_query, text)
    return _AMZ_SECRET_PARAM_RE.sub(lambda m: m.group(1) + REDACTED, text)


def describe_presigned_url(url: str) -> str:
    """Return a log-safe description of ``url``: ``scheme://host[:port]/path``.

    Userinfo, query and fragment are dropped. ``X-Amz-Date`` and
    ``X-Amz-Expires`` are appended when present and well-formed, e.g.
    ``https://bucket.example/relay/ws/n.json (X-Amz-Date=20260929T180747Z,
    X-Amz-Expires=600)``.
    """
    try:
        parts = urlsplit(url)
        host = parts.hostname or ""
        port = parts.port
    except (TypeError, ValueError):
        return "<unparseable URL>"
    if not parts.scheme or not host:
        return "<unparseable URL>"
    if ":" in host:  # IPv6 literal
        host = f"[{host}]"
    netloc = f"{host}:{port}" if port is not None else host
    label = f"{parts.scheme}://{netloc}{parts.path}"
    timing = []
    for name, value in parse_qsl(parts.query, keep_blank_values=True):
        shape = _PRESIGN_TIMING_PARAMS.get(name.lower())
        if shape is not None and shape.fullmatch(value):
            timing.append(f"{name}={value}")
    return f"{label} ({', '.join(timing)})" if timing else label


class PresignedUrlRedactionFilter(logging.Filter):
    """Redact presigned-URL query strings from a record before it is emitted.

    Rewrites ``record.msg``/``record.args`` (as the merged message),
    ``record.exc_text`` and ``record.stack_info`` only when redaction changes
    them. Never drops a record.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001 — a malformed record is the handler's to report
            return True
        redacted = redact_presigned_urls(message)
        if redacted != message:
            record.msg = redacted
            record.args = None

        if record.exc_text:
            record.exc_text = redact_presigned_urls(record.exc_text)
        elif record.exc_info:
            # Formatter.format() renders exc_info only when exc_text is unset,
            # and caches the result there. Pre-render with the default
            # formatting and keep it only when it needed redaction, so a
            # handler's own formatException still runs for every other record.
            formatted = _EXC_FORMATTER.formatException(record.exc_info)
            redacted_exc = redact_presigned_urls(formatted)
            if redacted_exc != formatted:
                record.exc_text = redacted_exc

        if record.stack_info:
            record.stack_info = redact_presigned_urls(record.stack_info)
        return True


def _attach_filter(target: logging.Filterer) -> None:
    if not any(isinstance(f, PresignedUrlRedactionFilter) for f in target.filters):
        target.addFilter(PresignedUrlRedactionFilter())


def install_log_redaction() -> None:
    """Apply the presigned-URL guard to this process's logging. Idempotent.

    * Sets the ``httpx`` and ``httpcore`` loggers to WARNING.
    * Adds ``PresignedUrlRedactionFilter`` to every handler currently on the
      root logger, and to the ``httpx`` logger itself so its records are
      redacted before they reach any handler, including one added later.
    """
    for name in _QUIET_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)
    _attach_filter(logging.getLogger("httpx"))
    for handler in logging.getLogger().handlers:
        _attach_filter(handler)
