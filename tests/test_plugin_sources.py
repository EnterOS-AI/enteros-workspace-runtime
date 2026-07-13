"""Tests for plugin_sources — the declared-plugins boot-install (F1).

Locks in the git-native, provider-agnostic boot-install:
  * ``parse_declared_plugins`` — accepts ``gitea://owner/repo[/subpath][#ref]``
    AND a full HTTPS git URL (``https|git+https://host/...[.git/sub][#ref]``);
    rejects insecure, unknown, and malformed schemes/tokens.
  * ``install_declared_plugins`` — materializes ``<plugins_dir>/<name>`` by
    ``git clone`` (subprocess monkeypatched), ANONYMOUS by default (no token in
    URL/argv), a per-host credential helper only for 401 (private) repos, the
    subpath-containment guard, fail-soft per source, atomic build-then-swap, and
    the empty-signal no-op that preserves existing behaviour.
"""
from __future__ import annotations

import logging
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


def test_parse_rejects_insecure_http_sources():
    assert ps.parse_declared_plugins("http://host.example/o/repo.git") == []
    assert ps.parse_declared_plugins("git+http://host.example/o/repo.git") == []


def test_parse_full_url_subpath_via_dotgit_delimiter():
    # ``.git/`` delimits the repo (clone target) from an in-repo subpath.
    (s,) = ps.parse_declared_plugins("https://h.example/o/r.git/sub/skill#dev")
    assert s.clone_url == "https://h.example/o/r.git"
    assert s.subpath == "sub/skill"
    assert (s.ref, s.name) == ("dev", "skill")


def test_parse_rejects_url_credentials_and_query_without_logging_them(caplog):
    secret = "PLUGIN-SOURCE-SECRET-SENTINEL"
    sources = (
        f"https://user:{secret}@host.example/o/repo.git",
        f"https://host.example/o/repo.git?token={secret}",
        f"gitea://user:{secret}@owner/repo",
    )

    with caplog.at_level(logging.INFO, logger="molecule_runtime.plugin_sources"):
        for source in sources:
            assert ps.parse_declared_plugins(source) == []

    assert secret not in caplog.text
    assert "<redacted>" in caplog.text


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


def test_parse_rejects_destination_path_components():
    # The derived install name must be one safe directory entry. A final dot
    # segment otherwise makes ``staging_dir / name`` resolve to the staging
    # directory or its parent even when the fetched subpath stays in-clone.
    assert ps.parse_declared_plugins("gitea://owner/repo/foo/..#main") == []
    assert ps.parse_declared_plugins("gitea://owner/repo/foo/.#main") == []
    assert ps.parse_declared_plugins("https://host/o/r.git/foo/..#main") == []


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


def test_fetch_failure_redacts_source_and_git_stderr(monkeypatch, tmp_path, caplog):
    secret = "PLUGIN-FETCH-SECRET-SENTINEL"
    secret_ref = "PLUGIN-REF-SECRET-SENTINEL"

    def fail_clone(cmd, **kwargs):
        raise ps.subprocess.CalledProcessError(
            128,
            cmd,
            stderr=(
                f"fatal: unable to access https://user:{secret}@host/repo.git"
                f"?token={secret}; remote ref {secret_ref} not found"
            ),
        )

    monkeypatch.setattr(ps.subprocess, "run", fail_clone)
    with caplog.at_level(logging.WARNING, logger="molecule_runtime.plugin_sources"):
        result = ps._git_fetch_tree(
            clone_url="https://host/repo.git",
            host="host",
            scheme="https",
            ref=secret_ref,
            subpath="",
            raw=f"https://user:{secret}@host/repo.git?token={secret}",
            token=secret,
            git_binary="git",
            workdir=tmp_path,
            timeout=1,
        )

    assert result is None
    assert secret not in caplog.text
    assert secret_ref not in caplog.text
    assert "<redacted>" in caplog.text


def test_fetch_failure_redacts_before_truncating_git_stderr(
    monkeypatch, tmp_path, caplog
):
    secret = "SPLIT-SECRET-PREFIX-AND-SUFFIX"
    stderr = "x" * 470 + f" https://user:{secret}@host/repo.git"

    def fail_clone(cmd, **kwargs):
        raise ps.subprocess.CalledProcessError(128, cmd, stderr=stderr)

    monkeypatch.setattr(ps.subprocess, "run", fail_clone)
    with caplog.at_level(logging.WARNING, logger="molecule_runtime.plugin_sources"):
        result = ps._git_fetch_tree(
            clone_url="https://host/repo.git",
            host="host",
            scheme="https",
            ref="main",
            subpath="",
            raw="https://host/repo.git",
            token=secret,
            git_binary="git",
            workdir=tmp_path,
            timeout=1,
        )

    assert result is None
    assert "SPLIT-SECRET" not in caplog.text
    assert "<redacted>" in caplog.text


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


def test_install_partial_failure_promotes_the_good_source_and_keeps_existing(
    monkeypatch, tmp_path
):
    # Atomic-swap contract, REVISED. The F1 fix originally aborted the whole
    # swap when ANY source failed, to stop a transient blip half-replacing the
    # plugins dir. That protected the live tree but ALSO discarded the sources
    # that fetched fine — including, on a de-baked image, the concierge's own
    # management MCP, which fail-closed the whole agent (staging test5,
    # 2026-07-13). A failed source now fails ONLY that source.
    #
    # The property F1 actually cared about is unchanged and asserted below: a
    # failure never deletes an already-installed plugin, and the tree is never
    # left half-written.
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
        env={
            "MOLECULE_DECLARED_PLUGINS": (
                "gitea://bad/bad-repo,gitea://good/good-repo"
            )
        },
    )
    assert report.failed == ["gitea://bad/bad-repo"]
    assert report.installed == ["gitea://good/good-repo"]
    assert report.swapped is True  # the good source IS promoted (was: False)
    # The good source went live rather than being discarded with the bad one.
    assert (plugins_dir / "good-repo" / "SKILL.md").read_text() == "ok"
    # F1's real invariant, intact: the pre-existing tree was NOT deleted by the
    # swap — it is carried forward.
    assert (plugins_dir / "prior" / "keep.txt").read_text() == "prior"


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


def test_duplicate_install_names_fail_closed_and_preserve_existing(
    monkeypatch, tmp_path
):
    plugins_dir = tmp_path / "plugins"
    (plugins_dir / "prior").mkdir(parents=True)
    (plugins_dir / "prior" / "keep.txt").write_text("prior")

    def fail_if_fetched(*args, **kwargs):
        raise AssertionError("destination collisions must fail before any clone")

    monkeypatch.setattr(ps.subprocess, "run", fail_if_fetched)

    report = ps.install_declared_plugins(
        plugins_dir=plugins_dir,
        env={
            "MOLECULE_DECLARED_PLUGINS": (
                "gitea://first-owner/shared-name,"
                "gitea://second-owner/shared-name"
            )
        },
    )

    assert report.failed == [
        "gitea://first-owner/shared-name",
        "gitea://second-owner/shared-name",
    ]
    assert report.installed == []
    assert report.swapped is False
    assert (plugins_dir / "prior" / "keep.txt").read_text() == "prior"
    assert [path.name for path in tmp_path.iterdir()] == ["plugins"]


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


def test_install_rejects_forged_destination_escape_before_fetch(monkeypatch, tmp_path):
    # Defense in depth: even if a future parser/provider accidentally emits an
    # unsafe name, installation must reject it before fetch/copy and preserve
    # the prior live tree.
    raw = "gitea://owner/repo/foo/..#main"
    forged = ps.PluginSource(
        scheme="gitea",
        owner="owner",
        repo="repo",
        subpath="foo/..",
        ref="main",
        name="..",
        raw=raw,
    )
    monkeypatch.setattr(ps, "parse_declared_plugins", lambda value: [forged])

    def fetch_must_not_run(*args, **kwargs):
        raise AssertionError("unsafe destination reached the fetch boundary")

    monkeypatch.setitem(ps._PROVIDERS, "gitea", fetch_must_not_run)
    plugins_dir = tmp_path / "plugins"
    (plugins_dir / "prior").mkdir(parents=True)
    (plugins_dir / "prior" / "keep.txt").write_text("prior")

    report = ps.install_declared_plugins(
        plugins_dir=plugins_dir,
        env={"MOLECULE_DECLARED_PLUGINS": raw},
    )

    assert report.failed == [raw]
    assert report.installed == []
    assert report.swapped is False
    assert (plugins_dir / "prior" / "keep.txt").read_text() == "prior"


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


def test_gitea_http_base_never_sends_configured_token(monkeypatch, tmp_path):
    calls = _patch_git(
        monkeypatch,
        lambda url, ref, cmd, env: _make_repo({"SKILL.md": b"unexpected"}),
    )
    report = ps.install_declared_plugins(
        plugins_dir=tmp_path / "plugins",
        env={
            "MOLECULE_DECLARED_PLUGINS": "gitea://owner/repo",
            "MOLECULE_PLUGIN_REGISTRY": "http://gitea.example.com",
            "GITEA_TOKEN": "CLEAR-TEXT-TOKEN-SENTINEL",
        },
    )

    assert report.failed == ["gitea://owner/repo"]
    assert report.installed == []
    assert calls == []


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


# ---------------------------------------------------------------------------
# SHA-pinned refs (the test5 incident, 2026-07-13)
#
# The catalog pins declared plugins BY COMMIT. `git clone --branch <sha>` cannot
# resolve a bare object id — real git answers "Remote branch <sha> not found in
# upstream origin" — so every SHA-pinned plugin failed to fetch. And since a
# failed source aborts the tree swap, a concierge on a de-baked image lost its
# management MCP along with it and fail-closed to `failed`.
#
# `_fake_git_server` models REAL git: `--branch <sha>` raises, and the only way
# to land a commit is init + fetch <sha> + checkout FETCH_HEAD.
# ---------------------------------------------------------------------------
LARK_SHA = "973a35b70d17694c6412b40fe963689fae2a353f"


def _fake_git_server(monkeypatch, files: dict[str, bytes], *, known_sha: str):
    """A subprocess.run fake that behaves like git against a real remote."""
    calls: list[list[str]] = []
    state: dict[str, object] = {"fetched": False}

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))

        if "clone" in cmd:
            ref = cmd[cmd.index("--branch") + 1] if "--branch" in cmd else "HEAD"
            if ps._is_commit_sha(ref):
                # THE BUG: real git refuses a bare SHA as --branch.
                raise ps.subprocess.CalledProcessError(
                    128, cmd, output="",
                    stderr=f"fatal: Remote branch {ref} not found in upstream origin",
                )
            dest = Path(cmd[-1])
            _materialize(dest, files)
            return _completed(cmd, 0)

        if "init" in cmd:
            Path(cmd[-1]).mkdir(parents=True, exist_ok=True)
            return _completed(cmd, 0)

        if "fetch" in cmd:
            ref = cmd[-1]
            if ref != known_sha:
                raise ps.subprocess.CalledProcessError(
                    128, cmd, output="", stderr=f"fatal: couldn't find remote ref {ref}",
                )
            state["fetched"] = True
            return _completed(cmd, 0)

        if "checkout" in cmd:
            if not state["fetched"]:
                raise ps.subprocess.CalledProcessError(
                    128, cmd, output="", stderr="fatal: FETCH_HEAD unavailable",
                )
            dest = Path(cmd[cmd.index("-C") + 1])
            _materialize(dest, files)
            return _completed(cmd, 0)

        return _completed(cmd, 0)  # remote add, config, ...

    monkeypatch.setattr(ps.subprocess, "run", fake_run)
    return calls


def _materialize(dest: Path, files: dict[str, bytes]) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    (dest / ".git").mkdir(exist_ok=True)
    (dest / ".git" / "HEAD").write_text("ref: refs/heads/x\n")
    for rel, data in files.items():
        p = dest / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)


def test_is_commit_sha_classifies_refs():
    assert ps._is_commit_sha(LARK_SHA) is True
    assert ps._is_commit_sha("a" * 64) is True          # sha256 object format
    assert ps._is_commit_sha(LARK_SHA.upper()) is True  # case-insensitive
    for ref in ("main", "v0.5.1", "release/x", "", "abc123", "z" * 40):
        assert ps._is_commit_sha(ref) is False, ref


def test_install_plugin_pinned_to_commit_sha(monkeypatch, tmp_path):
    """A SHA-pinned plugin installs — via init+fetch+checkout, not clone --branch."""
    calls = _fake_git_server(
        monkeypatch, {"plugin.py": b"print('lark')"}, known_sha=LARK_SHA
    )
    plugins_dir = tmp_path / "plugins"
    report = ps.install_declared_plugins(
        plugins_dir=plugins_dir,
        env={"MOLECULE_DECLARED_PLUGINS": f"gitea://molecule-ai/lark-channel-molecule#{LARK_SHA}"},
    )

    assert report.failed == [], "SHA-pinned plugin must not fail to fetch"
    assert report.installed == [f"gitea://molecule-ai/lark-channel-molecule#{LARK_SHA}"]
    assert (plugins_dir / "lark-channel-molecule" / "plugin.py").exists()
    assert not (plugins_dir / "lark-channel-molecule" / ".git").exists()

    flat = [" ".join(c) for c in calls]
    assert any("fetch" in c and LARK_SHA in c for c in flat), "must fetch the object id"
    assert any("checkout" in c and "FETCH_HEAD" in c for c in flat)
    assert not any("--branch" in c for c in flat), "must NOT try clone --branch <sha>"


def test_branch_and_tag_refs_still_use_clone(monkeypatch, tmp_path):
    """The common branch/tag path is unchanged — no init/fetch plumbing."""
    for ref in ("main", "v0.5.1"):
        calls = _fake_git_server(monkeypatch, {"a.txt": b"x"}, known_sha=LARK_SHA)
        report = ps.install_declared_plugins(
            plugins_dir=tmp_path / f"p-{ref.replace('/', '_')}",
            env={"MOLECULE_DECLARED_PLUGINS": f"gitea://owner/repo#{ref}"},
        )
        assert report.failed == []
        flat = [" ".join(c) for c in calls]
        assert any("clone" in c and f"--branch {ref}" in c for c in flat)
        assert not any(" fetch " in f" {c} " for c in flat)


def test_sha_fetch_carries_the_credential_helper(monkeypatch, tmp_path):
    """A private SHA-pinned repo still authenticates: the 401-only cred helper
    must be wired onto the FETCH argv (it is not a clone), and the token must
    never appear in the URL or on argv."""
    calls = _fake_git_server(monkeypatch, {"a.txt": b"x"}, known_sha=LARK_SHA)
    ps.install_declared_plugins(
        plugins_dir=tmp_path / "plugins",
        env={
            "MOLECULE_DECLARED_PLUGINS": f"gitea://owner/repo#{LARK_SHA}",
            "MOLECULE_TEMPLATE_REPO_TOKEN": "tok-SECRET",
        },
    )
    fetch = next(c for c in calls if "fetch" in c)
    assert any(a.startswith("credential.https://") for a in fetch), "cred helper missing on fetch"
    assert not any("tok-SECRET" in a for a in fetch), "token leaked onto argv"


# ---------------------------------------------------------------------------
# Blast radius: a failing source must fail THAT SOURCE ONLY.
#
# The swap used to be all-or-nothing — any failed source aborted the promotion
# of every source staged alongside it. On a de-baked image one of those is the
# concierge's own management MCP, so an unfetchable third-party plugin took the
# mgmt-MCP down with it and the agent fail-closed to `failed`. And the "keep the
# existing tree" justification is VACUOUS on a first boot: no previous tree
# exists, so the workspace came up with an EMPTY plugins dir.
# ---------------------------------------------------------------------------
MGMT_MCP = "gitea://molecule-ai/molecule-ai-plugin-molecule-platform-mcp#main"
BAD_LARK = f"gitea://molecule-ai/lark-channel-molecule#{LARK_SHA}"


def _patch_git_failing(monkeypatch, *, fail_repo: str, files: dict[str, bytes]):
    """git that fails to clone `fail_repo` and succeeds for everything else."""
    def resolver(clone_url, ref, cmd, env):
        return None if fail_repo in clone_url else dict(files)
    return _patch_git(monkeypatch, resolver)


def test_failed_plugin_does_not_block_the_others(monkeypatch, tmp_path):
    """THE test5 incident. Lark is unfetchable; the mgmt-MCP must STILL install."""
    _patch_git_failing(
        monkeypatch, fail_repo="lark-channel-molecule", files={"mcp.py": b"x"}
    )
    plugins_dir = tmp_path / "plugins"
    report = ps.install_declared_plugins(
        plugins_dir=plugins_dir,
        env={"MOLECULE_DECLARED_PLUGINS": f"{BAD_LARK},{MGMT_MCP}"},
    )

    assert report.failed == [BAD_LARK]
    assert report.installed == [MGMT_MCP]
    # The whole point: the swap HAPPENED, so the mgmt-MCP is actually live.
    assert report.swapped is True
    assert (plugins_dir / "molecule-ai-plugin-molecule-platform-mcp" / "mcp.py").exists()
    # The failed plugin is simply absent — not a half-installed tree.
    assert not (plugins_dir / "lark-channel-molecule").exists()


def test_first_boot_with_a_bad_plugin_still_yields_a_usable_tree(monkeypatch, tmp_path):
    """No previous tree to 'keep intact' — the old veto left this dir EMPTY."""
    _patch_git_failing(
        monkeypatch, fail_repo="lark-channel-molecule", files={"mcp.py": b"x"}
    )
    plugins_dir = tmp_path / "fresh"          # does not exist yet
    report = ps.install_declared_plugins(
        plugins_dir=plugins_dir,
        env={"MOLECULE_DECLARED_PLUGINS": f"{BAD_LARK},{MGMT_MCP}"},
    )
    assert report.swapped is True
    assert list(plugins_dir.iterdir()), "first boot must not yield an empty plugins tree"


def test_transient_failure_preserves_the_previously_installed_copy(monkeypatch, tmp_path):
    """The property the old veto DID protect, kept: a blip must not delete an
    already-installed plugin."""
    plugins_dir = tmp_path / "plugins"
    live = plugins_dir / "lark-channel-molecule"
    live.mkdir(parents=True)
    (live / "SKILL.md").write_text("# installed earlier")

    _patch_git_failing(
        monkeypatch, fail_repo="lark-channel-molecule", files={"mcp.py": b"x"}
    )
    report = ps.install_declared_plugins(
        plugins_dir=plugins_dir,
        env={"MOLECULE_DECLARED_PLUGINS": f"{BAD_LARK},{MGMT_MCP}"},
    )

    assert report.swapped is True
    assert report.failed == [BAD_LARK]
    # Carried forward, NOT deleted by the swap.
    assert (live / "SKILL.md").read_text() == "# installed earlier"
    # ...and the good source still went live alongside it.
    assert (plugins_dir / "molecule-ai-plugin-molecule-platform-mcp" / "mcp.py").exists()


# ---------------------------------------------------------------------------
# REAL GIT. No mock.
#
# Every other test in this file monkeypatches subprocess.run, and that is
# precisely how the SHA-pin bug reached production: the fake accepted
# `clone --branch <sha>` and returned a tree, while real git rejects it
# outright. CI validated the code against a git that does not exist.
#
# This test shells out to the ACTUAL git binary against a REAL repo on disk and
# fetches a REAL commit id. It cannot pass unless the fetch genuinely works, and
# it fails against the old `clone --branch <sha>` implementation for the same
# reason the live workspace did.
# ---------------------------------------------------------------------------
import os
import shutil as _shutil
import subprocess as _subprocess

import pytest


def _git_available() -> bool:
    return _shutil.which("git") is not None


@pytest.mark.skipif(not _git_available(), reason="git binary not on PATH")
def test_real_git_fetches_a_real_commit_sha(tmp_path):
    """End-to-end against the REAL git binary — the gate CI was missing.

    Drives `_git_fetch_tree` (the fetch core that was broken) directly, so the
    assertion is about what git actually does, not what a mock pretends it does.
    """
    origin = tmp_path / "origin"
    origin.mkdir()

    def git(*args, cwd=origin):
        return _subprocess.run(
            ["git", *args], cwd=cwd, check=True, capture_output=True, text=True,
        )

    git("init", "--quiet", "--initial-branch=main")
    git("config", "user.email", "t@t.t")
    git("config", "user.name", "t")
    # Serving a bare object id to `git fetch <sha>` requires this on the REMOTE.
    # Gitea enables it; the fixture must too, or we would be asserting a
    # capability the real remote does not grant.
    git("config", "uploadpack.allowReachableSHA1InWant", "true")
    git("config", "uploadpack.allowAnySHA1InWant", "true")
    (origin / "SKILL.md").write_text("# real")
    git("add", "-A")
    git("commit", "--quiet", "-m", "real commit")
    sha = git("rev-parse", "HEAD").stdout.strip()
    assert len(sha) == 40

    # Move the branch tip PAST the pin, so a plain clone of the default branch
    # would yield the WRONG tree — proving we really resolved the commit.
    (origin / "SKILL.md").write_text("# moved on")
    git("commit", "--quiet", "-am", "later commit")

    workdir = tmp_path / "wd"
    workdir.mkdir()
    content = ps._git_fetch_tree(
        clone_url=origin.as_uri(),            # file:// — a real git remote
        host="", scheme="https", ref=sha, subpath="",
        raw=f"gitea://o/r#{sha}", token="", git_binary="git",
        workdir=workdir, timeout=60.0,
    )

    assert content is not None, "real git could not fetch the SHA-pinned commit"
    skill = content / "SKILL.md"
    assert skill.is_file()
    assert skill.read_text() == "# real"          # the PIN, not the branch tip
    # VCS metadata stripped — on EVERY platform. git marks .git objects
    # read-only, which a plain rmtree(ignore_errors=True) silently fails to
    # remove, shipping .git inside the plugins tree. Asserted unconditionally
    # so that stays fixed.
    assert not (content / ".git").exists()


@pytest.mark.skipif(not _git_available(), reason="git binary not on PATH")
def test_real_git_still_fetches_a_branch_ref(tmp_path):
    """The unchanged hot path, also against real git."""
    origin = tmp_path / "origin2"
    origin.mkdir()

    def git(*args, cwd=origin):
        return _subprocess.run(
            ["git", *args], cwd=cwd, check=True, capture_output=True, text=True,
        )

    git("init", "--quiet", "--initial-branch=main")
    git("config", "user.email", "t@t.t")
    git("config", "user.name", "t")
    (origin / "SKILL.md").write_text("# tip")
    git("add", "-A")
    git("commit", "--quiet", "-m", "c1")

    workdir = tmp_path / "wd2"
    workdir.mkdir()
    content = ps._git_fetch_tree(
        clone_url=origin.as_uri(), host="", scheme="https", ref="main", subpath="",
        raw="gitea://o/r#main", token="", git_binary="git",
        workdir=workdir, timeout=60.0,
    )
    assert content is not None
    assert (content / "SKILL.md").read_text() == "# tip"


# ---------------------------------------------------------------------------
# Multi-provider reality: forges disagree about fetch-by-object-id.
#
# A plugin source can name ANY forge (gitea://, or a full git URL to github /
# gitlab / a self-hosted box). Serving a bare SHA requires
# `uploadpack.allowReachableSHA1InWant` on the REMOTE. Gitea/GitHub/GitLab
# enable it; plain git ships with it OFF, so a self-hosted remote refuses with
# "Server does not allow request for unadvertised object". The fetcher must
# still resolve the pin there — via clone + checkout — or a SHA-pinned plugin on
# a customer's own forge fails exactly like Lark did on ours.
# ---------------------------------------------------------------------------
def test_sha_pin_falls_back_to_clone_checkout_on_a_restrictive_forge(monkeypatch, tmp_path):
    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        if "fetch" in cmd:
            # A forge with SHA-in-want disabled.
            raise ps.subprocess.CalledProcessError(
                128, cmd, output="",
                stderr="error: Server does not allow request for unadvertised object",
            )
        if "clone" in cmd:                       # the fallback's full clone
            _materialize(Path(cmd[-1]), {"SKILL.md": b"# pinned"})
            return _completed(cmd, 0)
        return _completed(cmd, 0)                # init / remote add / checkout

    monkeypatch.setattr(ps.subprocess, "run", fake_run)
    plugins_dir = tmp_path / "plugins"
    report = ps.install_declared_plugins(
        plugins_dir=plugins_dir,
        env={"MOLECULE_DECLARED_PLUGINS": f"https://self-hosted.example/o/r#{LARK_SHA}"},
    )

    assert report.failed == [], "a restrictive forge must not fail a SHA-pinned plugin"
    assert (plugins_dir / "r" / "SKILL.md").read_text() == "# pinned"

    flat = [" ".join(c) for c in calls]
    assert any("fetch" in c for c in flat), "must TRY the cheap object-id fetch first"
    assert any("checkout" in c and LARK_SHA in c for c in flat), \
        "fallback must check the pin out of local history"


@pytest.mark.skipif(not _git_available(), reason="git binary not on PATH")
def test_real_git_fallback_resolves_the_pin_without_sha_in_want(tmp_path, monkeypatch):
    """The fallback, against REAL git: force the object-id fetch to fail and
    assert clone+checkout still lands the pinned commit."""
    origin = tmp_path / "origin3"
    origin.mkdir()

    def git(*args, cwd=origin):
        return _subprocess.run(
            ["git", *args], cwd=cwd, check=True, capture_output=True, text=True,
        )

    git("init", "--quiet", "--initial-branch=main")
    git("config", "user.email", "t@t.t")
    git("config", "user.name", "t")
    (origin / "SKILL.md").write_text("# pinned")
    git("add", "-A")
    git("commit", "--quiet", "-m", "c1")
    sha = git("rev-parse", "HEAD").stdout.strip()
    (origin / "SKILL.md").write_text("# tip")     # move the tip past the pin
    git("commit", "--quiet", "-am", "c2")

    # Simulate a forge that refuses fetch-by-object-id, leaving every OTHER git
    # command real. This is the only honest way to exercise the fallback: the
    # file:// transport bypasses upload-pack's want-checks, so a fixture cannot
    # actually refuse the way an http/ssh remote does.
    real_run = _subprocess.run

    def run_but_refuse_fetch(cmd, **kwargs):
        if isinstance(cmd, list) and "fetch" in cmd:
            raise ps.subprocess.CalledProcessError(
                128, cmd, output="",
                stderr="error: Server does not allow request for unadvertised object",
            )
        return real_run(cmd, **kwargs)

    monkeypatch.setattr(ps.subprocess, "run", run_but_refuse_fetch)

    workdir = tmp_path / "wd3"
    workdir.mkdir()
    content = ps._git_fetch_tree(
        clone_url=origin.as_uri(), host="", scheme="https", ref=sha, subpath="",
        raw=f"https://self-hosted.example/o/r#{sha}", token="", git_binary="git",
        workdir=workdir, timeout=60.0,
    )

    assert content is not None, "fallback failed — a restrictive forge would brick the plugin"
    assert (content / "SKILL.md").read_text() == "# pinned"   # the PIN, not the tip
    assert not (content / ".git").exists()


# ---------------------------------------------------------------------------
# Multi-provider CREDENTIALS.
#
# The fetch layer is provider-agnostic; the credential layer used to be keyed to
# the single configured gitea host, so a PRIVATE plugin repo on github / gitlab /
# a customer's self-hosted forge got no token, took a 401 and (with
# GIT_TERMINAL_PROMPT=0) failed the boot. The abstraction was a lie one layer
# down. These pin the N-provider contract — including that a token for one forge
# is NEVER offered to another.
# ---------------------------------------------------------------------------
def test_host_token_map_reads_the_json_map(monkeypatch):
    tokens = ps._host_token_map(
        {"MOLECULE_GIT_TOKENS": '{"github.com":"gh","gitlab.com":"gl","git.acme.io":"ac"}'},
        "https://git.moleculesai.app",
    )
    assert tokens == {"github.com": "gh", "gitlab.com": "gl", "git.acme.io": "ac"}


def test_host_token_map_reads_per_host_env_vars():
    tokens = ps._host_token_map(
        {"MOLECULE_GIT_TOKEN__GITHUB_COM": "gh", "MOLECULE_GIT_TOKEN__GITLAB_COM": "gl"},
        "https://git.moleculesai.app",
    )
    assert tokens == {"github.com": "gh", "gitlab.com": "gl"}


def test_host_token_map_keeps_the_gitea_seed_and_lets_explicit_win():
    env = {
        "MOLECULE_TEMPLATE_REPO_TOKEN": "gitea-tok",
        "MOLECULE_GIT_TOKENS": '{"github.com":"gh"}',
    }
    tokens = ps._host_token_map(env, "https://git.moleculesai.app")
    # back-compat: the single gitea credential still lands, keyed to its host...
    assert tokens["git.moleculesai.app"] == "gitea-tok"
    # ...alongside the other providers.
    assert tokens["github.com"] == "gh"


def test_host_token_map_survives_a_malformed_json_blob():
    # A bad credential blob must not kill the boot — a plugin that needs the
    # token will fail loudly on its own 401 instead.
    tokens = ps._host_token_map(
        {"MOLECULE_GIT_TOKENS": "{not json", "MOLECULE_TEMPLATE_REPO_TOKEN": "t"},
        "https://git.moleculesai.app",
    )
    assert tokens == {"git.moleculesai.app": "t"}


def test_private_github_plugin_gets_its_own_token(monkeypatch, tmp_path):
    """A private plugin on a NON-gitea forge authenticates."""
    calls = _patch_git(monkeypatch, lambda url, ref, cmd, env: _make_repo({"S.md": b"x"}))
    ps.install_declared_plugins(
        plugins_dir=tmp_path / "p",
        env={
            "MOLECULE_DECLARED_PLUGINS": "https://github.com/acme/private-plugin",
            "MOLECULE_GIT_TOKENS": '{"github.com":"gh-SECRET"}',
        },
    )
    call = calls[0]
    # The 401-only helper is wired for github.com specifically...
    assert any(a.startswith("credential.https://github.com") for a in call["cmd"])
    # ...the token reaches git ONLY via the child env, never argv or the URL.
    assert call["env"].get(ps._CRED_TOKEN_ENVVAR) == "gh-SECRET"
    assert not any("gh-SECRET" in a for a in call["cmd"])


def test_a_forges_token_is_never_offered_to_another_forge(monkeypatch, tmp_path):
    """The isolation property. A gitlab plugin must not receive the github token
    — cross-forge credential leakage would be a real incident."""
    calls = _patch_git(monkeypatch, lambda url, ref, cmd, env: _make_repo({"S.md": b"x"}))
    ps.install_declared_plugins(
        plugins_dir=tmp_path / "p",
        env={
            "MOLECULE_DECLARED_PLUGINS": "https://gitlab.com/acme/plugin",
            "MOLECULE_GIT_TOKENS": '{"github.com":"gh-SECRET"}',
        },
    )
    call = calls[0]
    assert call["env"].get(ps._CRED_TOKEN_ENVVAR) is None, "github token leaked to gitlab"
    assert not any("gh-SECRET" in a for a in call["cmd"])
    assert not any("credential." in a for a in call["cmd"])
