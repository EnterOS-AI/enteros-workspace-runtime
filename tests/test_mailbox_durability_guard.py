"""Mailbox durability guard — turn a silent 'writes land on ephemeral root
disk' failure into a LOUD, observable warning at kernel-on boot.

The guard classifies the resolved mailbox base:
  - DURABLE     : writable AND on a distinct persistent mount (st_dev != '/')
  - EPHEMERAL   : writable but on the SAME device as '/' (root fs) — lost on
                  instance recreate / auto-heal (cp#326); the case we guard.
  - UNWRITABLE  : cannot write under the base at all.

Device identity can't be forged in a unit test (you can't mount an EBS volume),
so the ``st_dev`` dimension is monkeypatched; the writability dimension and the
kernel.install() wiring are exercised for real.
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

import pytest

_THIS = Path(__file__).resolve()
sys.path.insert(0, str(_THIS.parent.parent))

import molecule_runtime.mailbox_dir as mailbox_dir  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv(mailbox_dir.KERNEL_FLAG_ENV, raising=False)
    monkeypatch.delenv(mailbox_dir.MAILBOX_DIR_ENV, raising=False)
    mailbox_dir._last_durability_status = mailbox_dir.DURABILITY_NA
    yield


# --- _is_writable ------------------------------------------------------------

def test_is_writable_true_for_real_dir(tmp_path):
    assert mailbox_dir._is_writable(tmp_path / "base") is True
    # sentinel is cleaned up, not left behind
    assert not (tmp_path / "base" / mailbox_dir._PROBE_FILENAME).exists()


def test_is_writable_false_when_parent_is_a_file(tmp_path):
    afile = tmp_path / "afile"
    afile.write_text("x", encoding="utf-8")
    # mkdir(parents=True) under a *file* raises NotADirectoryError (OSError).
    assert mailbox_dir._is_writable(afile / "sub") is False


# --- _is_on_root_device ------------------------------------------------------

def test_root_path_reads_as_on_root_device():
    # '/' is trivially on the same device as '/'.
    assert mailbox_dir._is_on_root_device(Path("/")) is True


def test_distinct_device_reads_as_not_on_root(monkeypatch, tmp_path):
    base = tmp_path / "vol"
    base.mkdir()

    real_stat = mailbox_dir.os.stat

    class _St:
        def __init__(self, dev):
            self.st_dev = dev

    def fake_stat(p, *a, **k):
        if str(p) == "/":
            return _St(1)  # root device
        if str(p) == str(base):
            return _St(2)  # a different mount
        return real_stat(p, *a, **k)

    monkeypatch.setattr(mailbox_dir.os, "stat", fake_stat)
    assert mailbox_dir._is_on_root_device(base) is False


def test_undeterminable_device_reads_as_none(monkeypatch, tmp_path):
    base = tmp_path / "vol"
    base.mkdir()

    def boom(*a, **k):
        raise OSError("nope")

    monkeypatch.setattr(mailbox_dir.os, "stat", boom)
    assert mailbox_dir._is_on_root_device(base) is None


# --- probe_durability mapping ------------------------------------------------

@pytest.mark.parametrize(
    "writable,on_root,expected",
    [
        (False, None, mailbox_dir.DURABILITY_UNWRITABLE),
        (True, True, mailbox_dir.DURABILITY_EPHEMERAL),
        (True, False, mailbox_dir.DURABILITY_DURABLE),
        (True, None, mailbox_dir.DURABILITY_EPHEMERAL),  # unverifiable => warn, not lull
    ],
)
def test_probe_durability_mapping(monkeypatch, tmp_path, writable, on_root, expected):
    monkeypatch.setattr(mailbox_dir, "_is_writable", lambda b: writable)
    monkeypatch.setattr(mailbox_dir, "_is_on_root_device", lambda b: on_root)
    assert mailbox_dir.probe_durability(tmp_path) == expected


# --- verify_durability -------------------------------------------------------

def test_verify_is_noop_when_kernel_off(caplog):
    with caplog.at_level(logging.INFO, logger="molecule_runtime.mailbox_dir"):
        assert mailbox_dir.verify_durability() == mailbox_dir.DURABILITY_NA
    assert mailbox_dir.last_durability_status() == mailbox_dir.DURABILITY_NA
    assert caplog.records == []  # legacy path stays silent


def test_verify_durable_logs_info(monkeypatch, caplog):
    monkeypatch.setenv(mailbox_dir.KERNEL_FLAG_ENV, "1")
    monkeypatch.setattr(mailbox_dir, "probe_durability", lambda b=None: mailbox_dir.DURABILITY_DURABLE)
    with caplog.at_level(logging.INFO, logger="molecule_runtime.mailbox_dir"):
        assert mailbox_dir.verify_durability() == mailbox_dir.DURABILITY_DURABLE
    assert mailbox_dir.last_durability_status() == mailbox_dir.DURABILITY_DURABLE
    assert not any(r.levelno >= logging.WARNING for r in caplog.records)
    assert any("durability: OK" in r.message for r in caplog.records)


def test_verify_ephemeral_logs_loud_error(monkeypatch, caplog):
    monkeypatch.setenv(mailbox_dir.KERNEL_FLAG_ENV, "1")
    monkeypatch.setattr(mailbox_dir, "probe_durability", lambda b=None: mailbox_dir.DURABILITY_EPHEMERAL)
    with caplog.at_level(logging.ERROR, logger="molecule_runtime.mailbox_dir"):
        assert mailbox_dir.verify_durability() == mailbox_dir.DURABILITY_EPHEMERAL
    errs = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert errs, "ephemeral base must emit an ERROR"
    msg = errs[0].getMessage()
    assert "EPHEMERAL" in msg and "cp#326" in msg
    assert mailbox_dir.last_durability_status() == mailbox_dir.DURABILITY_EPHEMERAL


def test_verify_unwritable_logs_loud_error(monkeypatch, caplog):
    monkeypatch.setenv(mailbox_dir.KERNEL_FLAG_ENV, "1")
    monkeypatch.setattr(mailbox_dir, "probe_durability", lambda b=None: mailbox_dir.DURABILITY_UNWRITABLE)
    with caplog.at_level(logging.ERROR, logger="molecule_runtime.mailbox_dir"):
        assert mailbox_dir.verify_durability() == mailbox_dir.DURABILITY_UNWRITABLE
    assert any("UNWRITABLE" in r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR)


def test_verify_never_raises_when_probe_throws(monkeypatch, caplog):
    monkeypatch.setenv(mailbox_dir.KERNEL_FLAG_ENV, "1")

    def boom(b=None):
        raise RuntimeError("probe blew up")

    monkeypatch.setattr(mailbox_dir, "probe_durability", boom)
    # Must not propagate — a guard that crashes boot is worse than the risk it guards.
    assert mailbox_dir.verify_durability() == mailbox_dir.DURABILITY_EPHEMERAL
    assert mailbox_dir.last_durability_status() == mailbox_dir.DURABILITY_EPHEMERAL


# --- real end-to-end (default tmp base, no monkeypatch of device) ------------

def test_probe_real_ephemeral_base_under_tmp(monkeypatch, tmp_path):
    """A plain dir whose st_dev matches '/' classifies EPHEMERAL for real."""
    base = tmp_path / "ws" / ".molecule"
    monkeypatch.setattr(mailbox_dir, "_is_on_root_device", lambda b: True)
    assert mailbox_dir.probe_durability(base) == mailbox_dir.DURABILITY_EPHEMERAL
    assert base.is_dir()  # probe created it


# --- kernel.install() wiring -------------------------------------------------

def test_kernel_install_invokes_guard(monkeypatch, tmp_path):
    import molecule_runtime.kernel as kernel

    monkeypatch.setenv(mailbox_dir.KERNEL_FLAG_ENV, "1")
    monkeypatch.setenv(mailbox_dir.MAILBOX_DIR_ENV, str(tmp_path / "ws" / ".molecule"))
    monkeypatch.setattr(mailbox_dir, "_is_on_root_device", lambda b: True)  # force ephemeral
    try:
        kernel.install()
        assert mailbox_dir.last_durability_status() == mailbox_dir.DURABILITY_EPHEMERAL
    finally:
        kernel.uninstall()
