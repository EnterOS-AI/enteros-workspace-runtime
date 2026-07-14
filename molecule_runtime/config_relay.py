"""Config-relay fetch-and-ack boot prelude (cf-r2-relay-config-secret-delivery).

When the control plane provisions a workspace with the R2 config relay ENABLED
(``MOLECULE_CONFIG_RELAY_ENABLE=1`` on the CP), it stages this workspace's
``{config.yaml + prompts/* + secret config files}`` as ONE transient Cloudflare
R2 object and injects three env vars onto the box:

    MOLECULE_CONFIG_RELAY_URI       short-TTL (~10m) single-object presigned HTTPS GET
    MOLECULE_CONFIG_RELAY_SHA256    sha256 hex of the bundle body (integrity, fail-closed)
    MOLECULE_CONFIG_RELAY_ACK_TOKEN one-time bearer for the ready-ack

This prelude, run once at the very start of boot (before anything reads
``/configs``), does exactly four things:

    1. FETCH the bundle over the presigned URL (the box holds NO durable cloud
       credential — only the minutes-long presigned URL is its secret-plane reach).
    2. VERIFY sha256(body) == MOLECULE_CONFIG_RELAY_SHA256.
    3. UNPACK the ``{path: base64(content)}`` JSON into ``config_path`` (``/configs``)
       — config.yaml + prompts/* + secret files, files 0600 / dirs 0700.
    4. ACK by POSTing ``/cp/workspaces/<id>/relay-ack`` with the bearer so the CP
       DELETES the transient object the moment we are ready.

INERT by default. When ``MOLECULE_CONFIG_RELAY_URI`` is absent the prelude is a
no-op, so a workspace provisioned with the relay DISABLED (the CP dark-ship
default) boots exactly as before on the legacy ``/configs`` delivery. The CP
injects the URI ONLY when its ``MOLECULE_CONFIG_RELAY_ENABLE`` flag is on, so the
runtime side carries NO separate flag — it stays dormant until the operator flips
that CP flag. Presence of the injected URI *is* the enable gate.

Fail-closed on delivery. A genuine fetch failure (after the retry ceiling) or a
sha256 mismatch ABORTS boot (``SystemExit``) rather than let the agent boot on a
partially-delivered or tampered config — a silently mis-configured agent is worse
than a visibly-aborted provision. Transient failures — a network blip, a 5xx, or
a *cold presign* (a 403/404 in the brief window right after the object is PUT) —
are retried with exponential backoff before that ceiling.

Best-effort on the ack. The ready-ack is retried on transient errors, but a
permanent ack failure only logs: the CP-side ReapAbandoned sweep and the R2
bucket lifecycle rule are the delete backstops, so a box that has ALREADY
unpacked a valid config must not refuse to boot over an un-acked object.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import posixpath
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Mapping

import httpx

# --- box-facing env contract (SSOT: CP internal/configrelay/relay.go consts) ---
RELAY_URI_ENV = "MOLECULE_CONFIG_RELAY_URI"
RELAY_SHA256_ENV = "MOLECULE_CONFIG_RELAY_SHA256"
RELAY_ACK_TOKEN_ENV = "MOLECULE_CONFIG_RELAY_ACK_TOKEN"
# The CP base URL the box already receives (workspace_provision.go injects it as
# MOLECULE_CP_URL from CP_BASE_URL). The ack endpoint lives on the CP, so the ack
# URL is derived from it — the relay env deliberately carries only the token, not
# a URL, keeping the box's trust surface minimal.
CP_URL_ENV = "MOLECULE_CP_URL"
WORKSPACE_ID_ENV = "WORKSPACE_ID"
WORKSPACE_CONFIG_PATH_ENV = "WORKSPACE_CONFIG_PATH"

DEFAULT_CONFIG_PATH = "/configs"

# Path charset SSOT — mirrors the CP-side workspaceConfigPathPattern
# (internal/provisioner/workspace_config_seed.go): ^[A-Za-z0-9._/-]+$. The CP
# validates on WRITE; we re-validate on UNPACK so a corrupt or hostile bundle can
# never escape config_path (defense-in-depth path-traversal guard — the same
# posture as the CP's ValidWorkspaceID key-prefix boundary).
_VALID_PATH = re.compile(r"^[A-Za-z0-9._/-]+$")

# File/dir perms. Bundle files include secret config, so every unpacked file is
# owner-only 0600 and every created dir is owner-only 0700 (never widen a secret
# via a world-readable parent).
_FILE_MODE = 0o600
_DIR_MODE = 0o700

# Retry envelope for the presigned fetch and the ack POST. 6 attempts with
# exponential backoff capped at 30s (1,2,4,8,16,30 -> ~61s ceiling) comfortably
# covers a cold-presign / clock-skew window without wedging boot forever. Mirrors
# the register_with_platform backoff shape already in main.py.
_MAX_ATTEMPTS = 6
_BASE_DELAY = 1.0
_MAX_DELAY = 30.0
_HTTP_TIMEOUT = 30.0


class RelayConfigError(RuntimeError):
    """A fail-closed config-relay condition (bad env contract, unreachable/invalid
    bundle after the retry ceiling, sha256 mismatch, or a malformed/traversing
    bundle). The prelude translates this into a ``SystemExit`` so boot aborts
    loudly instead of running a mis-configured agent."""


@dataclass
class RelayResult:
    """Outcome of a config-relay prelude run (returned for observability/tests)."""

    written: list[str] = field(default_factory=list)
    acked: bool = False


def _log(msg: str) -> None:
    # Boot-visible, line-buffered like the other main.py boot steps. NEVER logs
    # the presigned URI (carries a signature) or the ack token (CWE-532).
    print(f"config-relay: {msg}", flush=True)


def relay_env(env: Mapping[str, str]) -> tuple[str, str, str] | None:
    """Read the box-facing relay env contract.

    Returns ``(uri, sha256, ack_token)`` when the relay is active, or ``None``
    when ``MOLECULE_CONFIG_RELAY_URI`` is absent (the inert/disabled case — the
    caller no-ops and keeps the legacy /configs delivery).

    Raises ``RelayConfigError`` when the URI is present but the integrity digest
    or ack token is missing: a half-delivered contract is a provisioning bug and
    must fail closed, never boot on an unverifiable bundle.
    """
    uri = (env.get(RELAY_URI_ENV) or "").strip()
    if not uri:
        return None
    sha256 = (env.get(RELAY_SHA256_ENV) or "").strip().lower()
    ack_token = (env.get(RELAY_ACK_TOKEN_ENV) or "").strip()
    missing = [
        name
        for name, val in ((RELAY_SHA256_ENV, sha256), (RELAY_ACK_TOKEN_ENV, ack_token))
        if not val
    ]
    if missing:
        raise RelayConfigError(
            f"{RELAY_URI_ENV} is set but required companion env is missing: "
            f"{', '.join(missing)}"
        )
    return uri, sha256, ack_token


def validate_bundle_path(raw: str) -> str:
    """Validate a bundle-relative path and return its cleaned form.

    Rejects absolute paths, ``..`` traversal, and any character outside the CP's
    ``^[A-Za-z0-9._/-]+$`` charset. Mirrors the CP's write-side validation so the
    delivered object and the legacy provisioner bundle share ONE unpack path.
    """
    if not raw or "\x00" in raw:
        raise RelayConfigError(f"invalid bundle path {raw!r}")
    cleaned = posixpath.normpath(raw)
    if (
        cleaned in (".", "")
        or cleaned.startswith("../")
        or cleaned.startswith("/")
        or ".." in cleaned.split("/")
        or not _VALID_PATH.match(cleaned)
    ):
        raise RelayConfigError(f"invalid bundle path {raw!r} (traversal or bad charset)")
    return cleaned


def _sleep_backoff(attempt: int, sleep: Callable[[float], None]) -> None:
    sleep(min(_BASE_DELAY * (2 ** attempt), _MAX_DELAY))


def fetch_bundle(
    uri: str,
    expected_sha256: str,
    *,
    client: httpx.Client | None = None,
    sleep: Callable[[float], None] = time.sleep,
    max_attempts: int = _MAX_ATTEMPTS,
) -> bytes:
    """GET the presigned bundle and return its body iff sha256 matches.

    Retries transient failures — a transport error, a non-2xx (this INCLUDES the
    cold-presign 403/404 window right after the PUT), and a sha256 mismatch (a
    truncated download) — with exponential backoff. After ``max_attempts`` the
    last failure is raised as ``RelayConfigError`` (fail-closed). The digest is
    checked on the RAW body bytes, which is exactly what the CP hashed.
    """
    expected = expected_sha256.strip().lower()
    owns_client = client is None
    if owns_client:
        client = httpx.Client(timeout=_HTTP_TIMEOUT, follow_redirects=True)
    last_detail = "no attempts made"
    try:
        for attempt in range(max_attempts):
            try:
                resp = client.get(uri)
                status = resp.status_code
                if 200 <= status < 300:
                    body = resp.content
                    actual = hashlib.sha256(body).hexdigest()
                    if actual == expected:
                        return body
                    # A mismatch is usually a truncated/incomplete read — re-fetch.
                    # Digests are logged (not secret); the body is not.
                    last_detail = f"sha256 mismatch (want {expected[:12]}…, got {actual[:12]}…)"
                elif 400 <= status < 500:
                    # Cold presign (403/404 just after PUT) and genuine 4xx both
                    # land here: retry to the ceiling, then fail closed. Status
                    # code only — the presigned URI is never logged.
                    last_detail = f"HTTP {status}"
                else:
                    last_detail = f"HTTP {status}"
            except httpx.HTTPError as exc:
                last_detail = f"transport error: {type(exc).__name__}: {exc}"

            if attempt < max_attempts - 1:
                _log(f"fetch attempt {attempt + 1}/{max_attempts} failed ({last_detail}); retrying")
                _sleep_backoff(attempt, sleep)
    finally:
        if owns_client:
            client.close()
    raise RelayConfigError(
        f"presigned bundle fetch failed after {max_attempts} attempts ({last_detail})"
    )


def unpack_bundle(body: bytes, config_path: str | os.PathLike[str]) -> list[str]:
    """Decode the ``{path: base64(content)}`` JSON bundle into ``config_path``.

    Every path is re-validated (``validate_bundle_path``) and every destination is
    confirmed to resolve INSIDE ``config_path`` before any write, so a traversal or
    a symlink-escape aborts before touching the filesystem. Files are written with
    ``O_CREAT|O_TRUNC`` at mode 0600 (never a wider transient window); created
    parent dirs are 0700. Returns the list of written absolute paths (sorted).

    Any malformed JSON / non-string value / bad base64 raises ``RelayConfigError``
    (fail-closed).
    """
    try:
        data = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RelayConfigError(f"bundle is not valid UTF-8 JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise RelayConfigError("bundle JSON must be an object of {path: base64}")

    base = Path(config_path)
    base.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(base, _DIR_MODE)
    except OSError:
        # A pre-existing /configs mount may not be chmod-able by the agent uid;
        # that's fine — we only tighten what we create below.
        pass
    base_resolved = base.resolve()

    written: list[str] = []
    for raw_path, b64 in sorted(data.items()):
        rel = validate_bundle_path(raw_path)
        if not isinstance(b64, str):
            raise RelayConfigError(f"bundle value for {raw_path!r} is not a base64 string")
        try:
            # binascii.Error (bad alphabet/padding) is a ValueError subclass.
            content = base64.b64decode(b64, validate=True)
        except ValueError as exc:
            raise RelayConfigError(f"invalid base64 for {raw_path!r}: {exc}") from exc

        dest = base / rel
        # Belt-and-suspenders: the resolved destination must stay under base even
        # after symlink resolution of any pre-existing parent.
        parent = dest.parent
        parent.mkdir(parents=True, exist_ok=True, mode=_DIR_MODE)
        resolved_parent = parent.resolve()
        if resolved_parent != base_resolved and base_resolved not in resolved_parent.parents:
            raise RelayConfigError(f"bundle path {raw_path!r} escapes {base_resolved}")

        fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, _FILE_MODE)
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(content)
        finally:
            # Re-assert perms even if the file pre-existed with wider mode.
            try:
                os.chmod(dest, _FILE_MODE)
            except OSError:
                pass
        written.append(str(dest))
    return written


def ack_url(cp_url: str, workspace_id: str) -> str:
    """Derive the ready-ack endpoint: ``<cp>/cp/workspaces/<id>/relay-ack``."""
    return f"{cp_url.rstrip('/')}/cp/workspaces/{workspace_id}/relay-ack"


def post_ack(
    cp_url: str,
    workspace_id: str,
    ack_token: str,
    *,
    client: httpx.Client | None = None,
    sleep: Callable[[float], None] = time.sleep,
    max_attempts: int = _MAX_ATTEMPTS,
) -> bool:
    """POST the one-time ready-ack so the CP deletes the transient R2 object.

    Best-effort (NEVER fatal): returns True on a 204, False otherwise. A 5xx / 408
    / 429 / transport error is transient and retried with backoff; a 401/403 (our
    token is wrong) or 400 (bad id) is permanent and returns False immediately —
    the CP reaper + bucket lifecycle rule are the delete backstops, so a failed ack
    must not block a box that already has its config.
    """
    if not cp_url or not workspace_id:
        _log(f"ack skipped — {CP_URL_ENV}/{WORKSPACE_ID_ENV} unavailable (backstop reaper will delete the object)")
        return False
    url = ack_url(cp_url, workspace_id)
    headers = {"Authorization": f"Bearer {ack_token}"}
    owns_client = client is None
    if owns_client:
        client = httpx.Client(timeout=_HTTP_TIMEOUT)
    last_detail = "no attempts made"
    try:
        for attempt in range(max_attempts):
            try:
                resp = client.post(url, headers=headers)
                status = resp.status_code
                if status == 204:
                    return True
                if status in (400, 401, 403):
                    # Permanent: a bad token / id won't heal by retrying. Never log
                    # the token; status code only.
                    _log(f"ack rejected HTTP {status} (permanent) — leaving object to the backstop reaper")
                    return False
                last_detail = f"HTTP {status}"
            except httpx.HTTPError as exc:
                last_detail = f"transport error: {type(exc).__name__}: {exc}"

            if attempt < max_attempts - 1:
                _log(f"ack attempt {attempt + 1}/{max_attempts} failed ({last_detail}); retrying")
                _sleep_backoff(attempt, sleep)
    finally:
        if owns_client:
            client.close()
    _log(f"ack failed after {max_attempts} attempts ({last_detail}) — backstop reaper/lifecycle will delete the object")
    return False


def run_config_relay_prelude(
    *,
    workspace_id: str | None = None,
    config_path: str | os.PathLike[str] | None = None,
    env: Mapping[str, str] | None = None,
    client: httpx.Client | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> RelayResult | None:
    """Boot prelude: fetch → verify → unpack → ack the R2 config relay.

    No-op (returns ``None``) when the relay env is absent — the inert default, so a
    relay-DISABLED provision boots unchanged. On a genuine delivery failure raises
    ``SystemExit`` (fail-closed) so boot aborts loudly. The ack is best-effort.
    Returns a ``RelayResult`` when the relay ran.
    """
    env = env if env is not None else os.environ
    try:
        parsed = relay_env(env)
    except RelayConfigError as exc:
        raise SystemExit(f"FATAL: config-relay env contract invalid — refusing to boot: {exc}")
    if parsed is None:
        return None  # inert: MOLECULE_CONFIG_RELAY_URI not injected (relay disabled on CP)

    uri, expected_sha256, ack_token = parsed
    if workspace_id is None:
        workspace_id = (env.get(WORKSPACE_ID_ENV) or "").strip()
    if config_path is None:
        config_path = (env.get(WORKSPACE_CONFIG_PATH_ENV) or DEFAULT_CONFIG_PATH).strip() or DEFAULT_CONFIG_PATH

    _log(f"active — fetching config bundle over presigned HTTPS into {config_path} (ws={workspace_id or '?'})")
    try:
        body = fetch_bundle(uri, expected_sha256, client=client, sleep=sleep)
        written = unpack_bundle(body, config_path)
    except RelayConfigError as exc:
        # Fail-closed: a mis-delivered or tampered config must abort the provision,
        # never boot a mis-configured agent.
        raise SystemExit(f"FATAL: config-relay delivery failed — refusing to boot on incomplete/tampered config: {exc}")

    _log(f"unpacked {len(written)} file(s) into {config_path}")

    cp_url = (env.get(CP_URL_ENV) or "").strip()
    acked = post_ack(cp_url, workspace_id, ack_token, client=client, sleep=sleep)
    _log("object acked — CP will delete the transient bundle" if acked else "object NOT acked — backstop reaper/lifecycle will delete it")
    return RelayResult(written=written, acked=acked)
