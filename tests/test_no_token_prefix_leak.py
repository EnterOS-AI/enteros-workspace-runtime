"""Regression: main.py registration confirmation logs MUST NOT leak token prefixes.

Task #344 (CWE-532, Insertion of Sensitive Information into Log File).
Both confirmation log lines in ``main.py`` previously printed ``prefix=tok[:8]``
which leaks the first 8 chars of the workspace auth token and the platform
inbound secret to container stdout / journald — a low-effort source for
attackers with log-read access. Fix redacts both.

Pinned by string-search on the source file rather than by importing
``main`` (which has heavy side effects on import).
"""
import pathlib


_MAIN_PY = pathlib.Path(__file__).resolve().parents[1] / "molecule_runtime" / "main.py"


def test_main_does_not_log_token_prefix_8():
    """``main.py`` must not slice the first 8 chars off a secret for any log line."""
    src = _MAIN_PY.read_text(encoding="utf-8")
    # Direct prefix slice that leaked the token (CWE-532).
    assert "tok[:8]" not in src, (
        "main.py must not log tok[:8] — leaks first 8 chars of workspace auth "
        "token (CWE-532, task #344)."
    )
    assert "inbound[:8]" not in src, (
        "main.py must not log inbound[:8] — leaks first 8 chars of "
        "platform_inbound_secret (CWE-532, task #344)."
    )


def test_main_redacts_token_save_confirmations():
    """Both confirmation log lines must use [REDACTED] for the secret value."""
    src = _MAIN_PY.read_text(encoding="utf-8")
    assert "Saved workspace auth token (value=[REDACTED])" in src, (
        "Workspace auth-token save confirmation should log [REDACTED] "
        "instead of any portion of the token."
    )
    assert "Saved platform_inbound_secret (value=[REDACTED])" in src, (
        "Platform inbound secret save confirmation should log [REDACTED] "
        "instead of any portion of the secret."
    )
