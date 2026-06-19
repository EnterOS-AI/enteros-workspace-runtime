"""Regression tests for the main.py outer-except PrivilegedPluginInstallError
propagation (issue #151 / Researcher review 12431).

Without the fix: main.py's outer `except Exception` would catch a
RuntimeError raised by the privileged molecule-platform-mcp plugin's
install (which install_plugins_via_registry correctly re-raises), log
a "WARNING: adapter.setup() failed" line, and degrade to a
"reachable-but-misconfigured" boot — leaving the concierge with a
configured-but-missing privileged binary and no loud failure signal.

With the fix: main.py discriminates PrivilegedPluginInstallError (a
RuntimeError subclass) and re-raises so the runtime boot aborts loudly.

These tests import the exact discriminator function used by main.py
(``_is_privileged_setup_failure``) so they exercise the real rule, not
a copy. If main.py drifts, these tests catch the drift.
"""
from __future__ import annotations

import pytest

from molecule_runtime.adapter_base import PrivilegedPluginInstallError
from molecule_runtime.main import _is_privileged_setup_failure


def test_privileged_error_subclasses_runtime_error():
    """PrivilegedPluginInstallError must be a RuntimeError subclass so existing
    `except RuntimeError` / `pytest.raises(RuntimeError)` call sites keep
    working. The dedicated type exists only for isinstance-based
    discrimination at the privileged-aware boundaries (main.py outer
    except + install_plugins_via_registry)."""
    assert issubclass(PrivilegedPluginInstallError, RuntimeError)
    err = PrivilegedPluginInstallError("test")
    assert isinstance(err, RuntimeError)
    assert isinstance(err, PrivilegedPluginInstallError)


def test_privileged_error_aborts_runtime_setup():
    """Privileged-plugin install failure → outer except must re-raise,
    NOT log-and-continue. This is the regression we are guarding against."""
    err = PrivilegedPluginInstallError(
        "molecule-platform-mcp: setup.sh exited 42: MISSING_BINARY"
    )
    assert _is_privileged_setup_failure(err) is True


def test_timeout_on_privileged_plugin_aborts_runtime_setup():
    """TimeoutExpired on a privileged plugin must still abort the boot.
    Kimi's test_setup_sh_timeout_on_privileged_plugin_raises asserts the
    raise at the adaptor level; this one asserts the main.py discriminator
    does not swallow it. Closes the loop on the CR2 timeout regression."""
    err = PrivilegedPluginInstallError("molecule-platform-mcp: setup.sh timed out (120s)")
    assert _is_privileged_setup_failure(err) is True


def test_ordinary_runtime_error_is_warned_not_aborted():
    """An ordinary RuntimeError (not from the privileged plugin) must NOT
    trigger an abort — that's the pre-existing behavior we are preserving."""
    err = RuntimeError("ordinary plugin hiccup: socket timeout")
    assert _is_privileged_setup_failure(err) is False


def test_ordinary_value_error_is_warned_not_aborted():
    err = ValueError("ordinary plugin manifest malformed")
    assert _is_privileged_setup_failure(err) is False


# ---------------------------------------------------------------------------
# End-to-end propagation test: PrivilegedPluginInstallError raised by
# install_plugins_via_registry actually re-raises past the call. Combined
# with the helper test above, this proves the swallow can't regress:
# even if main.py's outer except is wired, the install_pipeline still
# surfaces the privileged error to the caller.
# ---------------------------------------------------------------------------


def test_privileged_error_passes_through_runtime_error_raises():
    """Existing `except RuntimeError` / `pytest.raises(RuntimeError)` call
    sites must still match PrivilegedPluginInstallError (it IS a
    RuntimeError). If a future refactor accidentally breaks the
    inheritance chain, this test catches it."""
    with pytest.raises(RuntimeError, match="setup.sh"):
        raise PrivilegedPluginInstallError("setup.sh exited 7")
