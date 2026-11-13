"""The Light run copy step: ``molecule-runtime-light-copy`` (contract C2).

The control plane runs this in a run pod's init container. It copies the
agent's volume (mounted read-only) into the pod's private emptyDir, leaves out
the agent's own token and inbound secret plus the engine's leave-out list
(conversation history, logs), and writes the run's token. A run must never
start on a partial copy, so any read error fails the step loudly, and nothing
is ever followed through a symlink.
"""
from __future__ import annotations

import json
import os
import socket
import stat
import tomllib
from pathlib import Path

import pytest

from molecule_runtime import light_copy
from molecule_runtime.light_copy import BUILTIN_EXCLUDES, EXCLUDE_FILE_ENV

pytestmark = pytest.mark.skipif(
    os.name != "posix", reason="POSIX file semantics: symlinks, sockets, FIFOs, mode bits"
)

TOKEN_ENV = "LIGHT_COPY_TEST_TOKEN"


def mode(path) -> int:
    return stat.S_IMODE(os.lstat(path).st_mode)


def rel_entries(root: Path) -> list[str]:
    """Every non-directory entry (files and symlinks), relative, never following links."""
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        for name in filenames + [d for d in dirnames if os.path.islink(os.path.join(dirpath, d))]:
            out.append(Path(os.path.join(dirpath, name)).relative_to(root).as_posix())
    return sorted(out)


def regular_files(root: Path) -> list[Path]:
    return [
        Path(dirpath) / name
        for dirpath, _dirs, files in os.walk(root)
        for name in files
        if os.path.isfile(os.path.join(dirpath, name)) and not os.path.islink(os.path.join(dirpath, name))
    ]


@pytest.fixture
def dirs(tmp_path):
    src = tmp_path / "agent"
    dst = tmp_path / "run"
    src.mkdir()
    dst.mkdir()
    return src, dst


@pytest.fixture
def run(tmp_path, monkeypatch, capsys):
    def _run(src, dst, *, exclude_text=None, exclude_path=None, token="run-token"):
        if exclude_path is not None:
            monkeypatch.setenv(EXCLUDE_FILE_ENV, str(exclude_path))
        elif exclude_text is not None:
            path = tmp_path / "exclude.txt"
            path.write_text(exclude_text, encoding="utf-8")
            monkeypatch.setenv(EXCLUDE_FILE_ENV, str(path))
        else:
            monkeypatch.delenv(EXCLUDE_FILE_ENV, raising=False)
        if token is None:
            monkeypatch.delenv(TOKEN_ENV, raising=False)
        else:
            monkeypatch.setenv(TOKEN_ENV, token)
        rc = light_copy.main(["--src", str(src), "--dst", str(dst), "--token-env", TOKEN_ENV])
        out, err = capsys.readouterr()
        summary = None
        if rc == 0:
            lines = out.splitlines()
            assert len(lines) == 1, f"stdout must be exactly one JSON line, got {out!r}"
            summary = json.loads(lines[0])
            assert set(summary) == {"files", "bytes", "excluded", "seconds"}
        return rc, summary, out, err

    return _run


def test_copies_tree_and_mode_bits_leaving_dst_root_alone(dirs, run):
    src, dst = dirs
    (src / "configs/skills").mkdir(parents=True)
    (src / "configs/config.yaml").write_bytes(b"x: 1\n")
    run_sh = src / "configs/skills/run.sh"
    run_sh.write_bytes(b"#!/bin/sh")
    os.chmod(run_sh, 0o755)
    notes = src / "rtstate/home/agent/notes.txt"
    notes.parent.mkdir(parents=True)
    notes.write_bytes(b"notes!\n")
    os.chmod(notes, 0o640)
    ro = src / "configs/ro"
    ro.mkdir()
    os.chmod(ro, 0o500)
    os.chmod(dst, 0o751)
    root_mode_before = mode(dst)

    rc, summary, _out, err = run(src, dst)

    assert rc == 0, err
    assert (dst / "configs/config.yaml").read_bytes() == b"x: 1\n"
    assert mode(dst / "configs/skills/run.sh") == 0o755
    assert mode(dst / "rtstate/home/agent/notes.txt") == 0o640
    assert mode(dst / "configs/ro") == 0o500
    assert mode(dst) == root_mode_before  # the emptyDir root is never touched
    assert (summary["files"], summary["bytes"], summary["excluded"]) == (3, 5 + 9 + 7, 0)
    assert isinstance(summary["seconds"], float)


def test_unreadable_file_fails_loudly(dirs, run, monkeypatch):
    src, dst = dirs
    (src / "configs/.hermes").mkdir(parents=True)
    (src / "configs/.hermes/state.db").write_bytes(b"sqlite")
    (src / "configs/config.yaml").write_bytes(b"x: 1\n")
    real_open = light_copy._open_source

    def deny(path):
        if path.endswith("configs/.hermes/state.db"):
            raise PermissionError(13, "Permission denied", path)
        return real_open(path)

    # Patched rather than chmod 000: CI may run as root, which reads anything.
    monkeypatch.setattr(light_copy, "_open_source", deny)

    rc, _summary, out, err = run(src, dst)

    assert rc == 1
    assert "configs/.hermes/state.db" in err
    assert out == ""  # no summary line: nothing may treat this copy as done


def test_symlinks_copied_as_links_never_followed(dirs, run, tmp_path):
    src, dst = dirs
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret").write_bytes(b"TOP SECRET")
    (src / "configs/skills").mkdir(parents=True)
    (src / "configs/skills/s.md").write_bytes(b"skill")
    (src / "configs/config.yaml").write_bytes(b"x: 1\n")
    links = {
        "in": "config.yaml",
        "abs_out": str(outside / "secret"),
        "rel_out": "../../outside/secret",
        "dangling": "nope",
        "dirlink": "skills",
    }
    for name, target in links.items():
        os.symlink(target, src / "configs" / name)

    rc, summary, _out, err = run(src, dst)

    assert rc == 0, err
    for name, target in links.items():
        path = dst / "configs" / name
        assert os.path.islink(path), name
        assert os.readlink(path) == target
    assert b"TOP SECRET" not in b"".join(p.read_bytes() for p in regular_files(dst))
    assert summary["files"] == 2 + len(links)  # config.yaml + skills/s.md + five links


def test_sockets_and_fifos_skipped(dirs, run):
    src, dst = dirs
    hermes = src / "configs/.hermes"
    hermes.mkdir(parents=True)
    (hermes / "SOUL.md").write_text("me", encoding="utf-8")
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    cwd = os.getcwd()
    try:
        os.chdir(hermes)  # bind by a relative name: AF_UNIX paths are length-limited
        sock.bind("gateway.sock")
    finally:
        os.chdir(cwd)
    os.mkfifo(src / "configs/fifo")
    try:
        rc, summary, _out, err = run(src, dst)
    finally:
        sock.close()

    assert rc == 0, err
    assert not os.path.lexists(dst / "configs/.hermes/gateway.sock")
    assert not os.path.lexists(dst / "configs/fifo")
    assert (dst / "configs/.hermes/SOUL.md").read_text(encoding="utf-8") == "me"
    assert summary["excluded"] == 2
    assert "configs/.hermes/gateway.sock" in err and "configs/fifo" in err


def test_builtin_and_file_excludes_with_dir_pattern(dirs, run):
    src, dst = dirs
    hermes = src / "configs/.hermes"
    (hermes / "sessions/b").mkdir(parents=True)
    (src / "configs/.auth_token").write_text("agent-old", encoding="utf-8")
    (src / "configs/.platform_inbound_secret").write_text("inbound", encoding="utf-8")
    for name in ("state.db", "state.db-wal", "SOUL.md", "cache"):  # cache is a regular FILE
        (hermes / name).write_bytes(b"x")
    (hermes / "sessions/a.json").write_bytes(b"{}")
    (hermes / "sessions/b/c.json").write_bytes(b"{}")
    exclude_text = (
        "# hermes conversation state\n"
        "\n"
        "configs/.hermes/state.db*\n"
        "configs/.hermes/sessions/\n"
        "configs/.hermes/cache/\n"
    )

    rc, summary, _out, err = run(src, dst, exclude_text=exclude_text)

    assert rc == 0, err
    assert rel_entries(dst) == ["configs/.auth_token", "configs/.hermes/SOUL.md", "configs/.hermes/cache"]
    assert (dst / "configs/.auth_token").read_text(encoding="utf-8") == "run-token"
    # two built-ins + state.db + state.db-wal + sessions/ (one skipped directory)
    assert summary["excluded"] == 5


@pytest.mark.parametrize("case", ["missing_file", "absolute_pattern"])
def test_bad_exclude_file_fails_before_copying(dirs, run, tmp_path, case):
    src, dst = dirs
    (src / "configs").mkdir()
    (src / "configs/config.yaml").write_bytes(b"x: 1\n")
    if case == "missing_file":
        rc, *_rest = run(src, dst, exclude_path=tmp_path / "nope.txt")
    else:
        rc, *_rest = run(src, dst, exclude_text="/configs/.hermes/logs/\n")
    assert rc == 1
    assert list(dst.iterdir()) == []


def test_token_written_0600_replacing_agent_token(dirs, run):
    src, dst = dirs
    (src / "configs").mkdir()
    (src / "configs/.auth_token").write_text("agent-old", encoding="utf-8")

    rc, _summary, out, err = run(src, dst, token=" run-new \n")

    assert rc == 0, err
    assert (dst / "configs/.auth_token").read_text(encoding="utf-8") == "run-new"
    assert mode(dst / "configs/.auth_token") == 0o600
    assert "run-new" not in out + err


def test_token_written_into_a_read_only_configs_dir(dirs, run):
    src, dst = dirs
    (src / "configs").mkdir()
    (src / "configs/config.yaml").write_bytes(b"x: 1\n")
    os.chmod(src / "configs", 0o555)

    rc, _summary, _out, err = run(src, dst)

    assert rc == 0, err
    assert (dst / "configs/.auth_token").read_text(encoding="utf-8") == "run-token"
    assert mode(dst / "configs") == 0o555


@pytest.mark.parametrize("token", [None, "   "])
def test_missing_token_fails_before_copying(dirs, run, token):
    src, dst = dirs
    (src / "configs").mkdir()
    (src / "configs/config.yaml").write_bytes(b"x: 1\n")
    rc, *_rest = run(src, dst, token=token)
    assert rc == 1
    assert list(dst.iterdir()) == []


def test_missing_dst_fails(dirs, run, tmp_path):
    src, _dst = dirs
    rc, *_rest = run(src, tmp_path / "no-such-dst")
    assert rc == 1


def test_lost_and_found_is_a_builtin_exclude(dirs, run, monkeypatch):
    src, dst = dirs
    assert "lost+found/" in BUILTIN_EXCLUDES
    (src / "lost+found").mkdir()
    (src / "lost+found/#1234").write_bytes(b"orphan")
    (src / "configs").mkdir()
    (src / "configs/config.yaml").write_bytes(b"x: 1\n")
    real_open = light_copy._open_source

    def deny(path):  # a root-owned lost+found is unreadable to the run's uid
        if "lost+found" in path:
            raise PermissionError(13, "Permission denied", path)
        return real_open(path)

    monkeypatch.setattr(light_copy, "_open_source", deny)

    rc, summary, _out, err = run(src, dst)

    assert rc == 0, err
    assert not os.path.lexists(dst / "lost+found")
    assert summary["excluded"] == 1


def test_console_script_declared():
    pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
    scripts = tomllib.loads(pyproject.read_text(encoding="utf-8"))["project"]["scripts"]
    assert scripts["molecule-runtime-light-copy"] == "molecule_runtime.light_copy:main"
