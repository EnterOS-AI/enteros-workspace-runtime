"""Tests for plugin_sources — the declared-plugins boot-install (F1).

Locks in the Python SSOT port of the proven shell block
(``wt-claude-code/entrypoint.sh:214-284``):
  * ``parse_declared_plugins`` — gitea:// scheme guard, ``#ref`` suffix,
    subpath, name extraction; rejects non-gitea + malformed tokens.
  * ``install_declared_plugins`` — materializes ``<plugins_dir>/<name>`` from a
    fetched gitea archive (httpx monkeypatched), with the path-traversal guard
    the shell ``cp -a`` lacked, fail-soft per source, and the empty-signal
    no-op that preserves existing behaviour.
"""
from __future__ import annotations

import io
import tarfile

import pytest

import molecule_runtime.plugin_sources as ps


# ---------------------------------------------------------------------------
# parse_declared_plugins
# ---------------------------------------------------------------------------
def test_parse_basic_owner_repo():
    (s,) = ps.parse_declared_plugins("gitea://owner/repo")
    assert (s.scheme, s.owner, s.repo, s.subpath, s.ref, s.name) == (
        "gitea", "owner", "repo", "", "main", "repo",
    )
    assert s.archive_path() == "/api/v1/repos/owner/repo/archive/main.tar.gz"


def test_parse_ref_suffix_and_subpath_and_name():
    (s,) = ps.parse_declared_plugins("gitea://o2/r2/sub/skill#dev")
    assert s.ref == "dev"
    assert s.subpath == "sub/skill"
    # name = LAST path segment of subpath (entrypoint.sh:255)
    assert s.name == "skill"
    assert s.archive_path() == "/api/v1/repos/o2/r2/archive/dev.tar.gz"


def test_parse_strips_all_whitespace_and_splits_commas():
    out = ps.parse_declared_plugins(" gitea://a/b ,\tgitea://c/d#x ")
    assert [(s.owner, s.repo, s.ref) for s in out] == [("a", "b", "main"), ("c", "d", "x")]


def test_parse_skips_unsupported_scheme():
    # github:// is not a registered provider in v1 — skipped (matches the shell's
    # "skip unsupported source"); gitea survives.
    out = ps.parse_declared_plugins("github://x/y,gitea://a/b")
    assert [s.owner for s in out] == ["a"]


def test_parse_skips_malformed():
    # No scheme, and a bare owner with no repo → both rejected.
    assert ps.parse_declared_plugins("notaurl") == []
    assert ps.parse_declared_plugins("gitea://onlyowner") == []


def test_parse_empty_is_noop():
    assert ps.parse_declared_plugins("") == []
    assert ps.parse_declared_plugins(None) == []
    assert ps.parse_declared_plugins("  , ,\t") == []


# ---------------------------------------------------------------------------
# install_declared_plugins — archive fetch monkeypatched
# ---------------------------------------------------------------------------
def _make_targz(members: dict[str, bytes], top: str = "repo-main") -> bytes:
    """Build a gitea-style .tar.gz: a single top dir containing ``members``."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        # top dir entry — DIRTYPE so it extracts as a directory (a gitea archive
        # is rooted at a single ``<repo>-<ref>/`` directory).
        d = tarfile.TarInfo(name=top + "/")
        d.type = tarfile.DIRTYPE
        d.mode = 0o755
        tar.addfile(d)
        for rel, data in members.items():
            info = tarfile.TarInfo(name=f"{top}/{rel}")
            info.size = len(data)
            info.mode = 0o644
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


class _FakeStream:
    """Context-manager stand-in for ``httpx.stream(...)``."""

    def __init__(self, data: bytes | None, status: int = 200):
        self._data = data
        self.status_code = status

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def iter_bytes(self):
        yield self._data or b""


def _patch_stream(monkeypatch, router):
    """Patch plugin_sources.httpx.stream with a URL->_FakeStream router."""

    def _fake_stream(method, url, **kwargs):
        return router(url, kwargs)

    monkeypatch.setattr(ps.httpx, "stream", _fake_stream)


def test_install_materializes_plugin(monkeypatch, tmp_path):
    archive = _make_targz({"SKILL.md": b"# hello", "tool.py": b"print(1)"})

    def router(url, kwargs):
        assert "/api/v1/repos/owner/repo/archive/main.tar.gz" in url
        # token header sent when MOLECULE_TEMPLATE_REPO_TOKEN set
        assert kwargs["headers"].get("Authorization") == "token tok-XYZ"
        return _FakeStream(archive)

    _patch_stream(monkeypatch, router)
    plugins_dir = tmp_path / "plugins"
    report = ps.install_declared_plugins(
        plugins_dir=plugins_dir,
        env={"MOLECULE_DECLARED_PLUGINS": "gitea://owner/repo", "MOLECULE_TEMPLATE_REPO_TOKEN": "tok-XYZ"},
    )
    assert report.declared is True
    assert report.installed == ["gitea://owner/repo"]
    assert (plugins_dir / "repo" / "SKILL.md").read_text() == "# hello"
    assert (plugins_dir / "repo" / "tool.py").exists()


def test_install_honors_subpath_and_name(monkeypatch, tmp_path):
    archive = _make_targz({"sub/skill/SKILL.md": b"# sub", "README.md": b"top"})
    _patch_stream(monkeypatch, lambda url, kw: _FakeStream(archive))
    plugins_dir = tmp_path / "plugins"
    report = ps.install_declared_plugins(
        plugins_dir=plugins_dir,
        env={"MOLECULE_DECLARED_PLUGINS": "gitea://o/r/sub/skill"},
    )
    assert report.installed == ["gitea://o/r/sub/skill"]
    # named after the last subpath segment, contents come from <top>/sub/skill
    assert (plugins_dir / "skill" / "SKILL.md").read_text() == "# sub"
    assert not (plugins_dir / "skill" / "README.md").exists()


def test_install_empty_signal_is_noop_and_preserves_existing(monkeypatch, tmp_path):
    # An EXISTING plugins dir must NOT be wiped when the signal is absent — this
    # is the "existing behavior unchanged" guarantee.
    plugins_dir = tmp_path / "plugins"
    (plugins_dir / "preexisting").mkdir(parents=True)
    (plugins_dir / "preexisting" / "keep.txt").write_text("keep")

    called = {"stream": False}
    monkeypatch.setattr(ps.httpx, "stream", lambda *a, **k: called.__setitem__("stream", True))

    report = ps.install_declared_plugins(plugins_dir=plugins_dir, env={})
    assert report.declared is False
    assert "no MOLECULE_DECLARED_PLUGINS" in report.summary()
    assert (plugins_dir / "preexisting" / "keep.txt").read_text() == "keep"
    assert called["stream"] is False  # never fetched


def test_install_fail_soft_one_bad_source_does_not_abort_others(monkeypatch, tmp_path):
    good = _make_targz({"SKILL.md": b"ok"})

    def router(url, kwargs):
        if "/bad/" in url:
            return _FakeStream(None, status=404)  # raise_for_status -> fail
        return _FakeStream(good)

    _patch_stream(monkeypatch, router)
    plugins_dir = tmp_path / "plugins"
    report = ps.install_declared_plugins(
        plugins_dir=plugins_dir,
        env={"MOLECULE_DECLARED_PLUGINS": "gitea://bad/repo,gitea://good/repo"},
    )
    assert report.failed == ["gitea://bad/repo"]
    assert report.installed == ["gitea://good/repo"]
    assert (plugins_dir / "repo" / "SKILL.md").read_text() == "ok"


def test_install_rejects_path_traversal_member(monkeypatch, tmp_path):
    # A malicious archive member that resolves OUTSIDE the extraction dir must be
    # rejected (the shell cp -a had no such guard). The whole source fails-soft;
    # nothing is written outside, and the escape file never lands.
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        tar.addfile(tarfile.TarInfo(name="repo-main/"))
        evil = b"pwned"
        info = tarfile.TarInfo(name="repo-main/../../escape.txt")
        info.size = len(evil)
        tar.addfile(info, io.BytesIO(evil))
    archive = buf.getvalue()

    _patch_stream(monkeypatch, lambda url, kw: _FakeStream(archive))
    plugins_dir = tmp_path / "plugins"
    report = ps.install_declared_plugins(
        plugins_dir=plugins_dir,
        env={"MOLECULE_DECLARED_PLUGINS": "gitea://owner/repo"},
    )
    assert report.failed == ["gitea://owner/repo"]
    assert report.installed == []
    # No file escaped above the plugins dir.
    assert not (tmp_path / "escape.txt").exists()
    assert not (plugins_dir.parent / "escape.txt").exists()


def test_install_default_plugins_dir_from_env(monkeypatch, tmp_path):
    # When plugins_dir is not passed, it derives from WORKSPACE_CONFIG_PATH.
    archive = _make_targz({"SKILL.md": b"x"})
    _patch_stream(monkeypatch, lambda url, kw: _FakeStream(archive))
    config_path = tmp_path / "configs"
    report = ps.install_declared_plugins(
        env={
            "MOLECULE_DECLARED_PLUGINS": "gitea://owner/repo",
            "WORKSPACE_CONFIG_PATH": str(config_path),
        },
    )
    assert report.plugins_dir == str(config_path / "plugins")
    assert (config_path / "plugins" / "repo" / "SKILL.md").exists()


def test_install_unauth_when_no_token(monkeypatch, tmp_path):
    archive = _make_targz({"SKILL.md": b"x"})
    seen = {}

    def router(url, kwargs):
        seen["headers"] = kwargs["headers"]
        return _FakeStream(archive)

    _patch_stream(monkeypatch, router)
    ps.install_declared_plugins(
        plugins_dir=tmp_path / "plugins",
        env={"MOLECULE_DECLARED_PLUGINS": "gitea://owner/repo"},
    )
    # No token -> no Authorization header (unauth fallback, like the shell).
    assert "Authorization" not in seen["headers"]
