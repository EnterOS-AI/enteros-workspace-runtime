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


def test_install_partial_failure_does_not_swap_and_keeps_existing(monkeypatch, tmp_path):
    # Atomic-swap contract (F1 fix): if ANY declared source fails to fetch, the
    # staging build is NOT promoted — the existing live tree is left intact. A
    # transient blip on one source must never half-replace the plugins dir. The
    # per-source outcomes are still reported so observability is unchanged.
    good = _make_targz({"SKILL.md": b"ok"})

    def router(url, kwargs):
        if "/bad/" in url:
            return _FakeStream(None, status=404)  # raise_for_status -> fail
        return _FakeStream(good)

    _patch_stream(monkeypatch, router)
    plugins_dir = tmp_path / "plugins"
    # A prior boot's tree exists and must survive the partial failure.
    (plugins_dir / "prior").mkdir(parents=True)
    (plugins_dir / "prior" / "keep.txt").write_text("prior")

    report = ps.install_declared_plugins(
        plugins_dir=plugins_dir,
        env={"MOLECULE_DECLARED_PLUGINS": "gitea://bad/repo,gitea://good/repo"},
    )
    assert report.failed == ["gitea://bad/repo"]
    assert report.installed == ["gitea://good/repo"]  # staged OK...
    assert report.swapped is False  # ...but the build was NOT promoted
    # Existing tree intact; the good source was NOT half-installed into the live
    # dir (it only ever landed in the discarded staging tree).
    assert (plugins_dir / "prior" / "keep.txt").read_text() == "prior"
    assert not (plugins_dir / "repo").exists()


def test_install_fetch_failure_preserves_existing_tree(monkeypatch, tmp_path):
    # THE F1 regression guard: a transient fetch failure must leave the prior
    # plugins tree intact (the old code rm -rf'd up front and wiped it).
    plugins_dir = tmp_path / "plugins"
    (plugins_dir / "old-skill").mkdir(parents=True)
    (plugins_dir / "old-skill" / "SKILL.md").write_text("prior-good")
    (plugins_dir / "old-skill" / "tool.py").write_text("print('keep me')")

    # Every fetch 503s (gitea blip) for this boot.
    _patch_stream(monkeypatch, lambda url, kw: _FakeStream(None, status=503))

    report = ps.install_declared_plugins(
        plugins_dir=plugins_dir,
        env={"MOLECULE_DECLARED_PLUGINS": "gitea://owner/repo"},
    )
    assert report.declared is True
    assert report.failed == ["gitea://owner/repo"]
    assert report.installed == []
    assert report.swapped is False
    # The prior tree MUST be byte-for-byte intact — the transient failure did
    # not wipe the already-materialized skill.
    assert (plugins_dir / "old-skill" / "SKILL.md").read_text() == "prior-good"
    assert (plugins_dir / "old-skill" / "tool.py").read_text() == "print('keep me')"
    # No staging leftovers next to the live dir.
    assert [p.name for p in tmp_path.iterdir()] == ["plugins"]


def test_install_full_success_swaps_and_drops_removed(monkeypatch, tmp_path):
    # On a fully-successful build the swap still fully replaces the live tree —
    # a plugin no longer in the declared set must not linger after the swap.
    plugins_dir = tmp_path / "plugins"
    (plugins_dir / "stale").mkdir(parents=True)
    (plugins_dir / "stale" / "x.txt").write_text("old")

    archive = _make_targz({"SKILL.md": b"new"})
    _patch_stream(monkeypatch, lambda url, kw: _FakeStream(archive))

    report = ps.install_declared_plugins(
        plugins_dir=plugins_dir,
        env={"MOLECULE_DECLARED_PLUGINS": "gitea://owner/repo"},
    )
    assert report.swapped is True
    assert report.installed == ["gitea://owner/repo"]
    assert (plugins_dir / "repo" / "SKILL.md").read_text() == "new"
    # The de-declared plugin is gone after the atomic full-replace.
    assert not (plugins_dir / "stale").exists()
    # No staging/backup leftovers next to the live dir.
    assert [p.name for p in tmp_path.iterdir()] == ["plugins"]


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


# ---------------------------------------------------------------------------
# presign:// scheme — RETIRED. The box-side presign plugin provider resolved
# plugin trees the CP staged in the config-relay drop; that CP channel was
# retired (CP#1195, no customer), so the provider was removed. A presign://
# token is now just an unknown scheme — skipped + logged like any other
# unregistered provider, never special-cased.
# ---------------------------------------------------------------------------
def test_parse_presign_is_now_unknown_scheme():
    # Every presign:// spelling is dropped by parse (unknown scheme), including
    # the shape that used to parse into a valid source — no special-casing.
    assert ps.parse_declared_plugins("presign://my-plugin") == []
    assert ps.parse_declared_plugins("presign://a/b") == []
    assert ps.parse_declared_plugins("presign://") == []


def test_install_presign_scheme_no_longer_resolves(monkeypatch, tmp_path):
    # Retired: even if a leftover .relay-plugins drop is present on disk, a
    # presign:// source is an unknown scheme now — never resolved, never
    # installed, and no network fetch is attempted for it.
    def _boom(*a, **k):  # pragma: no cover - asserts non-invocation
        raise AssertionError("presign is retired — must not perform a network fetch")

    monkeypatch.setattr(ps.httpx, "stream", _boom)

    config_path = tmp_path / "configs"
    # A stale relay drop must NOT be picked up now that the provider is gone.
    stale_drop = config_path / ".relay-plugins" / "seo-mcp"
    stale_drop.mkdir(parents=True)
    (stale_drop / "SKILL.md").write_bytes(b"# seo")

    report = ps.install_declared_plugins(
        plugins_dir=config_path / "plugins",
        env={
            "MOLECULE_DECLARED_PLUGINS": "presign://seo-mcp",
            "WORKSPACE_CONFIG_PATH": str(config_path),
        },
    )
    # Unknown scheme -> parse drops it -> nothing installed from the drop.
    assert report.installed == []
    assert not (config_path / "plugins" / "seo-mcp").exists()


def test_install_mixed_gitea_and_retired_presign(monkeypatch, tmp_path):
    # A gitea:// plugin still installs; a co-declared presign:// token is now an
    # unknown scheme (retired) and is silently dropped — the gitea install is
    # unaffected.
    archive = _make_targz({"SKILL.md": b"# git"})
    _patch_stream(monkeypatch, lambda url, kw: _FakeStream(archive))
    config_path = tmp_path / "configs"
    report = ps.install_declared_plugins(
        plugins_dir=config_path / "plugins",
        env={
            "MOLECULE_DECLARED_PLUGINS": "gitea://owner/repo,presign://relayed",
            "WORKSPACE_CONFIG_PATH": str(config_path),
        },
    )
    assert report.installed == ["gitea://owner/repo"]
    assert "presign://relayed" not in report.installed
    assert report.swapped is True
    assert (config_path / "plugins" / "repo" / "SKILL.md").read_text() == "# git"
    assert not (config_path / "plugins" / "relayed").exists()
