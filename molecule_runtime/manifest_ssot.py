"""Advisory SSOT validation of plugin manifests (molecule-core#3383, PR-2).

Validates a plugin's ``plugin.yaml`` against the vendored SSOT plugin-manifest
JSON-Schema (``molecule_runtime/contracts/plugin-manifest.schema.json`` — a
byte-for-byte mirror of molecule-ai-sdk's
``contracts/plugin-manifest/plugin-manifest.schema.json``, see
``contracts/PROVENANCE.md``). The helpers here stay ADVISORY: violations are
LOGGED and returned, never raised. The FAIL-CLOSED promotion (PR-4 of
molecule-core#3383) lives at the CALL SITES — ``plugins.load_plugin_manifest``
and ``plugin_sources.install_declared_plugins`` branch on the returned
violations when ``enforcement_enabled()`` (default ON, kill-switch
``MOLECULE_MANIFEST_SSOT_ENFORCE=off``). Carve-out: a MISSING ``plugin.yaml``
is always advisory-only — manifest-less plugins (bare-SKILL.md dirs) are
common and legal.

Two call sites:
  * ``plugins.load_plugin_manifest`` — the single manifest-parse chokepoint —
    calls ``validate_manifest_ssot`` on the already-parsed YAML value;
  * ``plugin_sources.install_declared_plugins`` — the true at-install boot
    path — calls ``advisory_check`` per staged plugin dir before the swap.

Never-silent-skip: if ``jsonschema`` is not importable the gate is OFF, but
that is announced with ONE loud ``logger.error`` per process rather than a
silent pass (the pyyaml/sha256 lesson — a quietly-disabled check is worse
than no check, because it looks like coverage).
"""
from __future__ import annotations

import json
import logging
import os
from importlib import resources
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)

# jsonschema is a base dependency (promoted from the test extra by this PR),
# but the advisory gate must degrade LOUDLY — not crash the runtime boot — if
# an environment somehow lacks it (e.g. a stale image built before the
# promotion). See the module docstring: OFF is announced, never silent.
try:
    import jsonschema

    _JSONSCHEMA_AVAILABLE = True
except ImportError:  # pragma: no cover — exercised via the module flag in tests
    jsonschema = None  # type: ignore[assignment]
    _JSONSCHEMA_AVAILABLE = False

# One loud unavailability error per process (not per manifest).
_unavailable_logged = False

# Cap the violations reported for a single manifest — a totally-wrong file
# (e.g. a list at top level) can produce a flood; the first ~20 are plenty
# for the advisory log line.
_MAX_VIOLATIONS = 20

_SCHEMA_RESOURCE = "contracts/plugin-manifest.schema.json"

_validator = None  # cached Draft202012Validator (schema is static)

# Fail-closed promotion (molecule-core#3383, PR-4): enforcement is ON by
# default; MOLECULE_MANIFEST_SSOT_ENFORCE=off|false|0 is the escape hatch back
# to pure-advisory mode. One loud "disabled" warning per process (mirrors the
# jsonschema-unavailable pattern above — a disabled gate is never silent).
_ENFORCE_ENV = "MOLECULE_MANIFEST_SSOT_ENFORCE"
_disabled_logged = False


class ManifestSSOTViolation(Exception):
    """An existing ``plugin.yaml`` violates the SSOT schema under enforcement.

    Carries the offending ``plugin_name`` and the ``violations`` list so
    callers/handlers can report precisely what was rejected.
    """

    def __init__(self, plugin_name: str, violations: list[str]):
        self.plugin_name = plugin_name
        self.violations = violations
        super().__init__(
            f"plugin {plugin_name}: {len(violations)} SSOT manifest "
            f"violation(s): {'; '.join(violations)}"
        )


def enforcement_enabled() -> bool:
    """Whether SSOT manifest violations are ENFORCED (fail-closed) — default ON.

    Reads ``MOLECULE_MANIFEST_SSOT_ENFORCE`` from the process env; the values
    ``off``/``false``/``0`` (case-insensitive) disable enforcement, dropping
    back to the PR-2 advisory behaviour. When disabled, ONE loud
    ``logger.warning`` per process announces it.
    """
    global _disabled_logged
    value = (os.environ.get(_ENFORCE_ENV) or "").strip().lower()
    if value in ("off", "false", "0"):
        if not _disabled_logged:
            logger.warning(
                "SSOT manifest enforcement DISABLED via env — advisory mode"
            )
            _disabled_logged = True
        return False
    return True


def _get_validator():
    """Load the vendored schema and build the validator once per process."""
    global _validator
    if _validator is None:
        schema = json.loads(
            resources.files("molecule_runtime")
            .joinpath(_SCHEMA_RESOURCE)
            .read_text(encoding="utf-8")
        )
        _validator = jsonschema.Draft202012Validator(schema)
    return _validator


def validate_manifest_ssot(raw: object) -> list[str]:
    """Validate an already-parsed plugin.yaml value against the SSOT schema.

    Returns a sorted, capped list of human-readable violations
    (``"<json path>: <message>"``), empty when conformant. A non-dict value is
    not special-cased — the schema itself reports it (``type: object``).
    Advisory: never raises.
    """
    global _unavailable_logged
    if not _JSONSCHEMA_AVAILABLE:
        if not _unavailable_logged:
            logger.error(
                "SSOT manifest validation unavailable: jsonschema not "
                "importable — advisory gate is OFF"
            )
            _unavailable_logged = True
        return []
    try:
        validator = _get_validator()
        violations = sorted(
            f"{error.json_path}: {error.message}"
            for error in validator.iter_errors(raw)
        )
    except Exception as exc:  # advisory — never let the validator break a load
        logger.warning("SSOT manifest validation errored (skipping): %s", exc)
        return []
    if len(violations) > _MAX_VIOLATIONS:
        violations = violations[:_MAX_VIOLATIONS] + [
            f"... and {len(violations) - _MAX_VIOLATIONS} more"
        ]
    return violations


def advisory_check(
    plugin_name: str,
    manifest_path: str | Path,
    *,
    log: logging.Logger | None = None,
    prefix: str = "",
) -> list[str]:
    """Read + parse ``manifest_path`` itself and validate it (advisory).

    For callers that only have a plugin DIRECTORY on disk (the staged trees in
    ``plugin_sources.install_declared_plugins``) rather than an already-parsed
    manifest. Missing file / YAML-parse failure are reported as violations,
    not raised. Logs ONE warning line (via ``log`` with ``prefix``, so
    plugin_sources keeps its ``"[plugins] "`` convention) when violations
    exist, and returns them. NEVER raises.
    """
    if log is None:
        log = logger
    try:
        path = Path(manifest_path)
        if not path.is_file():
            violations = ["plugin.yaml missing"]
        else:
            try:
                raw = yaml.safe_load(path.read_text(encoding="utf-8"))
            except yaml.YAMLError as exc:
                violations = [f"plugin.yaml is not valid YAML: {exc}"]
            else:
                violations = validate_manifest_ssot(raw)
        if violations:
            log.warning(
                "%sSSOT manifest validation (advisory): plugin=%s %d violation(s): %s",
                prefix, plugin_name, len(violations), "; ".join(violations),
            )
        return violations
    except Exception as exc:  # advisory — must never raise into install/boot
        log.warning(
            "%sSSOT manifest validation (advisory) errored for plugin=%s (skipping): %s",
            prefix, plugin_name, exc,
        )
        return []
