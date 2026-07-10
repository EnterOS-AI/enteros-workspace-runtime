"""Tests for plugin_sources — the declared-plugins boot-install (F1).

Locks in the git-native, provider-agnostic boot-install:
  * ``parse_declared_plugins`` — accepts ``gitea://owner/repo[/subpath][#ref]``
    AND a full git URL (``https|http|git+https|git+http://host/...[.git/sub][#ref]``);
    rejects unknown schemes + malformed tokens.
  * ``install_declared_plugins`` — materializes ``<plugins_dir>/<name>`` by
    ``git clone`` (subprocess monkeypatched), ANONYMOUS by default (no token in
    URL/argv), a per-host credential helper only for 401 (private) repos, the
    subpath-containment guard, fail-soft per source, atomic build-then-swap, and
    the empty-signal no-op that preserves existing behaviour.
"""
from __future__ import annotations

from pathlib import Path

import molecule_runtime.plugin_sources as ps


# ---------------------------------------------------------------------------
# parse_declared_plugins
# ---------------------------------------------------------------------------
def test_parse_basic_owner_repo():
    (s,) = ps.parse_declared_plugins("gitea://owner/repo")
    assert (s.scheme, s.owner, s.repo, s.subpath, s.ref, s.name) == (
        "gitea", "owner", "repo", "", "main", "repo",
    )
    # gitea:// leaves host/clone_url empty — the host is resolved from config at
    # fetch time (see _fetch_gitea), not baked into the parse.
    assert (s.host, s.clone_url) == ("", "")


def test_parse_ref_suffix_and_subpath_and_name():
    (s,) = ps.parse_declared_plugins("gitea://o2/r2/sub/skill#dev")
    assert s.ref == "dev"
    assert s.subpath == "sub/skill"
    # name = LAST path segment of subpath (entrypoint.sh:255)
    assert s.name == "skill"


def test_parse_full_https_url():
    # A full git URL is self-contained (any forge): host + clone_url are parsed,
    # owner/repo stay empty, name = repo segment (trailing .git stripped).
    (s,) = ps.parse_declared_plugins("https://github.com/acme/mgmt-mcp#v1")
    assert s.scheme == "https"
    assert s.host == "github.com"
    assert s.clone_url == "https://github.com/acme/mgmt-mcp"
    assert (s.ref, s.name, s.subpath) == ("v1", "mgmt-mcp", "")


def test_parse_git_plus_https_normalizes_and_defaults_ref():
    (s,) = ps.parse_declared_plugins("git+https://gitlab.com/g/proj.git")
    assert s.scheme == "git+https"
    # git+ is normalized away for the actual clone URL.
    assert s.clone_url == "https://gitlab.com/g/proj.git"
    assert (s.host, s.ref, s.name) == ("gitlab.com", "main", "proj")


def test_parse_full_url_subpath_via_dotgit_delimiter():
    # ``.git/`` delimits the repo (clone target) from an in-repo subpath.
    (s,) = ps.parse_declared_plugins("https://h.example/o/r.git/sub/skill#dev")
    assert s.clone_url == "https://h.example/o/r.git"
    assert s.subpath == "sub/skill"
    assert (s.ref, s.name) == ("dev", "skill")


def test_parse_strips_all_whitespace_and_splits_commas():
    out = ps.parse_declared_plugins(" gitea://a/b ,\tgitea://c/d#x ")
    assert [(s.owner, s.repo, s.ref) for s in out] == [("a", "b", "main"), ("c", "d", "x")]


def test_parse_skips_unsupported_scheme():
    # github:// is a bespoke scheme, NOT a full git URL (those are https://…) —
    # skipped (matches the shell's "skip unsupported source"); gitea survives.
    out = ps.parse_declared_plugins("github://x/y,gitea://a/b")
    assert [s.owner for s in out] == ["a"]


def test_parse_skips_malformed_full_url():
    # A full URL with no repo path is malformed → skipped.
    assert ps.parse_declared_plugins("https://only.host") == []
    assert ps.parse_declared_plugins("https://only.host/") == []


def test_parse_skips_malformed():
    # No scheme, and a bare owner with no repo → both rejected.
    assert ps.parse_declared_plugins("notaurl") == []
    assert ps.parse_declared_plugins("gitea://onlyowner") == []


def test_parse_empty_is_noop():
    assert ps.parse_declared_plugins("") == []
    assert ps.parse_declared_plugins(None) == []
    assert ps.parse_declared_plugins("  , ,\t") == []


# ---------------------------------------------------------------------------
# install_declared_plugins — git clone monkeypatched
# ---------------------------------------------------------------------------
def _make_repo(members: dict[str, bytes]) -> dict[str, bytes]:
    """A fake checked-out repo tree: rel-path -> bytes (git clone hands us the
    tree directly — no ``<repo>-<ref>/`` archive wrapper)."""
    return dict(members)


def _patch_git(monkeypatch, resolver):
    """Patch ``plugin_sources.subprocess.run`` so a ``git clone ... <dir>`` writes
    a fake checked-out tree instead of hitting the network.

    ``resolver(clone_url, ref, cmd, env) -> dict[str,bytes] | None``
      returns the repo file-map to materialize at ``<dir>`` (success), or None to
      simulate a clone FAILURE (non-zero exit, like an unreachable/private repo).

    Returns a ``calls`` list of ``{"cmd", "env"}`` for argv/env assertions.
    """
    calls: list[dict] = []

    def fake_run(cmd, **kwargs):
        env = kwargs.get("env") or {}
        calls.append({"cmd": list(cmd), "env": dict(env)})
        if "clone" not in cmd:  # e.g. a hypothetical `git config` — succeed no-op
            return _completed(cmd, 0)
        clone_url = cmd[-2]
        dest = Path(cmd[-1])
        ref = cmd[cmd.index("--branch") + 1] if "--branch" in cmd else "main"
        files = resolver(clone_url, ref, list(cmd), dict(env))
        if files is None:
            raise ps.subprocess.CalledProcessError(
                128, cmd, output="", stderr=f"fatal: could not clone {clone_url}"
            )
        dest.mkdir(parents=True, exist_ok=True)
        # git leaves a .git metadata dir in the checkout — the code must strip it.
        (dest / ".git").mkdir(exist_ok=True)
        (dest / ".git" / "HEAD").write_text("ref: refs/heads/x\n")
        for rel, data in files.items():
            p = dest / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(data)
        return _completed(cmd, 0)

    monkeypatch.setattr(ps.subprocess, "run", fake_run)
    return calls


def _completed(cmd, code):
    return ps.subprocess.CompletedProcess(cmd, code, "", "")


def test_install_materializes_plugin(monkeypatch, tmp_path):
    repo = _make_repo({"SKILL.md": b"# hello", "tool.py": b"print(1)"})

    def resolver(clone_url, ref, cmd, env):
        # gitea:// resolves to a token-free https clone URL of the whole repo.
        assert clone_url == "https://git.moleculesai.app/owner/repo.git"
        assert ref == "main"
        return repo

    _patch_git(monkeypatch, resolver)
    plugins_dir = tmp_path / "plugins"
    report = ps.install_declared_plugins(
        plugins_dir=plugins_dir,
        env={"MOLECULE_DECLARED_PLUGINS": "gitea://owner/repo", "MOLECULE_TEMPLATE_REPO_TOKEN": "tok-XYZ"},
    )
    assert report.declared is True
    assert report.installed == ["gitea://owner/repo"]
    assert (plugins_dir / "repo" / "SKILL.md").read_text() == "# hello"
    assert (plugins_dir / "repo" / "tool.py").exists()
    # The .git metadata dir must NOT be copied into the plugins tree.
    assert not (plugins_dir / "repo" / ".git").exists()


def test_public_fetch_sends_no_token_in_url_or_argv(monkeypatch, tmp_path):
    # A token IS configured, but for a PUBLIC repo git never 401s so the helper is
    # never invoked and NO token is transmitted. Mechanism guarantees: the token
    # value never appears in the clone URL or anywhere on git's argv; it is
    # handed to git ONLY via the child env, read solely by the 401-time helper.
    calls = _patch_git(monkeypatch, lambda url, ref, cmd, env: _make_repo({"SKILL.md": b"x"}))
    ps.install_declared_plugins(
        plugins_dir=tmp_path / "plugins",
        env={"MOLECULE_DECLARED_PLUGINS": "gitea://owner/repo", "MOLECULE_TEMPLATE_REPO_TOKEN": "sekret-tok"},
    )
    (call,) = [c for c in calls if "clone" in c["cmd"]]
    argv = call["cmd"]
    # Token NEVER on argv (no ps leak) and NEVER in the URL.
    assert not any("sekret-tok" in a for a in argv)
    clone_url = argv[-2]
    assert "sekret-tok" not in clone_url and "@" not in clone_url
    # A per-host credential helper IS wired (for the private/401 case) and reads
    # the token from the env var, not argv.
    assert any(a.startswith("credential.https://git.moleculesai.app.helper=!") for a in argv)
    assert any(ps._CRED_TOKEN_ENVVAR in a for a in argv)  # helper references the env var name
    # The token is supplied to git ONLY via the child env.
    assert call["env"].get(ps._CRED_TOKEN_ENVVAR) == "sekret-tok"
    # Never block boot on an interactive credential prompt.
    assert call["env"].get("GIT_TERMINAL_PROMPT") == "0"


def test_private_fetch_wires_per_host_cred_helper(monkeypatch, tmp_path):
    # Simulate a PRIVATE repo: the resolver refuses UNLESS git could supply the
    # token — i.e. the per-host helper is wired and the token is in the child env.
    # This proves the credential-as-abstraction path is available on a 401.
    def resolver(clone_url, ref, cmd, env):
        helper_wired = any(
            a.startswith("credential.https://git.moleculesai.app.helper=!") for a in cmd
        )
        token_in_env = env.get(ps._CRED_TOKEN_ENVVAR) == "priv-tok"
        if helper_wired and token_in_env:
            return _make_repo({"SKILL.md": b"# private"})
        return None  # 401 with no usable credential -> clone fails

    _patch_git(monkeypatch, resolver)
    plugins_dir = tmp_path / "plugins"
    report = ps.install_declared_plugins(
        plugins_dir=plugins_dir,
        env={"MOLECULE_DECLARED_PLUGINS": "gitea://owner/repo", "GITEA_TOKEN": "priv-tok"},
    )
    assert report.installed == ["gitea://owner/repo"]
    assert report.swapped is True
    assert (plugins_dir / "repo" / "SKILL.md").read_text() == "# private"


def test_anonymous_when_no_token_no_cred_helper(monkeypatch, tmp_path):
    # No token configured: NO credential helper is wired and NO token env is set —
    # the clone is fully anonymous. GIT_TERMINAL_PROMPT=0 still guards against a
    # boot-blocking prompt if the repo turns out to be private.
    calls = _patch_git(monkeypatch, lambda url, ref, cmd, env: _make_repo({"SKILL.md": b"x"}))
    ps.install_declared_plugins(
        plugins_dir=tmp_path / "plugins",
        env={"MOLECULE_DECLARED_PLUGINS": "gitea://owner/repo"},
    )
    (call,) = [c for c in calls if "clone" in c["cmd"]]
    assert not any("credential." in a for a in call["cmd"])
    assert ps._CRED_TOKEN_ENVVAR not in call["env"]
    assert call["env"].get("GIT_TERMINAL_PROMPT") == "0"


def test_install_honors_subpath_and_name(monkeypatch, tmp_path):
    repo = _make_repo({"sub/skill/SKILL.md": b"# sub", "README.md": b"top"})
    _patch_git(monkeypatch, lambda url, ref, cmd, env: repo)
    plugins_dir = tmp_path / "plugins"
    report = ps.install_declared_plugins(
        plugins_dir=plugins_dir,
        env={"MOLECULE_DECLARED_PLUGINS": "gitea://o/r/sub/skill"},
    )
    assert report.installed == ["gitea://o/r/sub/skill"]
    # named after the last subpath segment, contents come from <clone>/sub/skill
    assert (plugins_dir / "skill" / "SKILL.md").read_text() == "# sub"
    assert not (plugins_dir / "skill" / "README.md").exists()


def test_install_full_git_url_source(monkeypatch, tmp_path):
    # A full git URL (any forge) clones its self-contained URL; name = repo seg.
    def resolver(clone_url, ref, cmd, env):
        assert clone_url == "https://github.com/acme/mgmt-mcp"
        assert ref == "v2"
        return _make_repo({"SKILL.md": b"# gh"})

    _patch_git(monkeypatch, resolver)
    plugins_dir = tmp_path / "plugins"
    report = ps.install_declared_plugins(
        plugins_dir=plugins_dir,
        env={"MOLECULE_DECLARED_PLUGINS": "https://github.com/acme/mgmt-mcp#v2"},
    )
    assert report.installed == ["https://github.com/acme/mgmt-mcp#v2"]
    assert (plugins_dir / "mgmt-mcp" / "SKILL.md").read_text() == "# gh"


def test_install_empty_signal_is_noop_and_preserves_existing(monkeypatch, tmp_path):
    # An EXISTING plugins dir must NOT be wiped when the signal is absent — this
    # is the "existing behavior unchanged" guarantee.
    plugins_dir = tmp_path / "plugins"
    (plugins_dir / "preexisting").mkdir(parents=True)
    (plugins_dir / "preexisting" / "keep.txt").write_text("keep")

    called = {"run": False}
    monkeypatch.setattr(ps.subprocess, "run", lambda *a, **k: called.__setitem__("run", True))

    report = ps.install_declared_plugins(plugins_dir=plugins_dir, env={})
    assert report.declared is False
    assert "no MOLECULE_DECLARED_PLUGINS" in report.summary()
    assert (plugins_dir / "preexisting" / "keep.txt").read_text() == "keep"
    assert called["run"] is False  # never cloned


def test_install_partial_failure_does_not_swap_and_keeps_existing(monkeypatch, tmp_path):
    # Atomic-swap contract (F1 fix): if ANY declared source fails to fetch, the
    # staging build is NOT promoted — the existing live tree is left intact. A
    # transient blip on one source must never half-replace the plugins dir. The
    # per-source outcomes are still reported so observability is unchanged.
    def resolver(clone_url, ref, cmd, env):
        if "/bad/" in clone_url:
            return None  # clone fails
        return _make_repo({"SKILL.md": b"ok"})

    _patch_git(monkeypatch, resolver)
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

    # Every clone fails (gitea blip) for this boot.
    _patch_git(monkeypatch, lambda url, ref, cmd, env: None)

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

    _patch_git(monkeypatch, lambda url, ref, cmd, env: _make_repo({"SKILL.md": b"new"}))

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


def test_install_rejects_subpath_escape(monkeypatch, tmp_path):
    # The containment guard: a crafted subpath that resolves OUTSIDE the clone
    # dir must be rejected (fail-soft), so nothing outside the plugins tree is
    # read/copied. Replaces the old tar-member traversal guard (git clone writes
    # the tree within the clone dir — there is no archive write-escape vector).
    # `..` segments in the subpath try to climb out of the checkout.
    _patch_git(monkeypatch, lambda url, ref, cmd, env: _make_repo({"SKILL.md": b"ok"}))
    plugins_dir = tmp_path / "plugins"
    report = ps.install_declared_plugins(
        plugins_dir=plugins_dir,
        # subpath = "../../escape" -> name "escape", content_dir escapes clone dir
        env={"MOLECULE_DECLARED_PLUGINS": "gitea://owner/repo/../../escape"},
    )
    assert report.failed == ["gitea://owner/repo/../../escape"]
    assert report.installed == []
    assert not (plugins_dir / "escape").exists()


def test_install_default_plugins_dir_from_env(monkeypatch, tmp_path):
    # When plugins_dir is not passed, it derives from WORKSPACE_CONFIG_PATH.
    _patch_git(monkeypatch, lambda url, ref, cmd, env: _make_repo({"SKILL.md": b"x"}))
    config_path = tmp_path / "configs"
    report = ps.install_declared_plugins(
        env={
            "MOLECULE_DECLARED_PLUGINS": "gitea://owner/repo",
            "WORKSPACE_CONFIG_PATH": str(config_path),
        },
    )
    assert report.plugins_dir == str(config_path / "plugins")
    assert (config_path / "plugins" / "repo" / "SKILL.md").exists()


def test_gitea_clone_url_from_configured_base(monkeypatch, tmp_path):
    # MOLECULE_GITEA_BASE_URL sets the host + the credential-helper host key.
    def resolver(clone_url, ref, cmd, env):
        assert clone_url == "https://gitea.example.com/owner/repo.git"
        # helper is keyed on the CONFIGURED host, not the hardcoded default.
        assert any(
            a.startswith("credential.https://gitea.example.com.helper=!") for a in cmd
        )
        return _make_repo({"SKILL.md": b"x"})

    _patch_git(monkeypatch, resolver)
    report = ps.install_declared_plugins(
        plugins_dir=tmp_path / "plugins",
        env={
            "MOLECULE_DECLARED_PLUGINS": "gitea://owner/repo",
            "MOLECULE_GITEA_BASE_URL": "https://gitea.example.com",
            "GITEA_TOKEN": "t",
        },
    )
    assert report.installed == ["gitea://owner/repo"]


def test_gitea_clone_url_from_plugin_registry(monkeypatch, tmp_path):
    # MOLECULE_PLUGIN_REGISTRY is the provider-agnostic name core SETS on the box
    # (conciergePlatformMCPEnv). The resolver MUST read it — this is the SET/READ
    # bridge that makes the registry knob real (a self-host/mirror/airgap points
    # plugin sourcing elsewhere via this var). Regression for the exact SET-one-
    # name/READ-another drift that broke the concierge before. It also takes
    # PRECEDENCE over the MOLECULE_GITEA_BASE_URL back-compat alias.
    def resolver(clone_url, ref, cmd, env):
        assert clone_url == "https://gitea.internal.corp/owner/repo.git"
        assert any(
            a.startswith("credential.https://gitea.internal.corp.helper=!") for a in cmd
        )
        return _make_repo({"SKILL.md": b"x"})

    _patch_git(monkeypatch, resolver)
    report = ps.install_declared_plugins(
        plugins_dir=tmp_path / "plugins",
        env={
            "MOLECULE_DECLARED_PLUGINS": "gitea://owner/repo",
            "MOLECULE_PLUGIN_REGISTRY": "https://gitea.internal.corp",
            "MOLECULE_GITEA_BASE_URL": "https://git.moleculesai.app",  # loses to registry
            "GITEA_TOKEN": "t",  # so the per-host cred-helper wires (keyed on registry host)
        },
    )
    assert report.installed == ["gitea://owner/repo"]


def test_gitea_backcompat_default_host_is_logged(monkeypatch, tmp_path, caplog):
    # Removing the default host would break provisioning where the box relies on
    # it (the shell mirror defaults the same way), so it is KEPT — but emitted
    # NON-SILENTLY: a log line records the reliance.
    import logging
    _patch_git(monkeypatch, lambda url, ref, cmd, env: _make_repo({"SKILL.md": b"x"}))
    # The back-compat-default log now emits from npm_auth's logger: base-host
    # resolution is the SSOT owned by npm_auth (shared git+npm) — capture there.
    with caplog.at_level(logging.INFO, logger="molecule_runtime.npm_auth"):
        ps.install_declared_plugins(
            plugins_dir=tmp_path / "plugins",
            env={"MOLECULE_DECLARED_PLUGINS": "gitea://owner/repo"},
        )
    assert any(
        "MOLECULE_PLUGIN_REGISTRY" in r.getMessage()
        and "back-compat" in r.getMessage()
        for r in caplog.records
    )


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
    # installed, and no clone is attempted for it.
    def _boom(*a, **k):  # pragma: no cover - asserts non-invocation
        raise AssertionError("presign is retired — must not perform a git clone")

    monkeypatch.setattr(ps.subprocess, "run", _boom)

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
    _patch_git(monkeypatch, lambda url, ref, cmd, env: _make_repo({"SKILL.md": b"# git"}))
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
