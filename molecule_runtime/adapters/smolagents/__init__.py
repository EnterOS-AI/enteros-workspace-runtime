"""Smolagents adapter for Molecule AI workspace runtime.

Provides env sanitization and safe executor/messaging primitives for use
with HuggingFace's smolagents library.

Two env-sanitization strategies are available:

* **Allowlist** (recommended) — :mod:`adapters.smolagents.env_sanitize`:
  only explicitly-safe variables pass through. Stricter but requires keeping
  the allowlist up-to-date as new safe vars are needed.

* **Denylist** (simple) — :mod:`adapters.smolagents.safe_env`:
  well-known secret names plus ``*_API_KEY`` / ``*_TOKEN`` suffix patterns
  are stripped. Easier to start with; less exhaustive.

Quick start::

    # Allowlist approach (stricter)
    from molecule_runtime.adapters.smolagents.env_sanitize import make_safe_env, SafeLocalPythonExecutor

    # Denylist approach (simpler)
    from molecule_runtime.adapters.smolagents.safe_env import make_safe_env

    # Safe messaging
    from molecule_runtime.adapters.smolagents.send_message_wrapper import safe_send_message
"""

# Re-export the allowlist-based make_safe_env as the default (most secure).
from molecule_runtime.adapters.smolagents.env_sanitize import SafeLocalPythonExecutor, make_safe_env
from molecule_runtime.adapters.smolagents.send_message_wrapper import safe_send_message

__all__ = ["make_safe_env", "SafeLocalPythonExecutor", "safe_send_message"]
