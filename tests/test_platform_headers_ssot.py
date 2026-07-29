"""Structural SSOT gate: every platform-bound request builds its headers
through ``platform_auth`` (issue #373).

WHY THIS SHAPE, and not ``assert "X-Molecule-Org-Id" in headers``
-----------------------------------------------------------------
The same defect was fixed once already in ``gmail-channel-molecule`` (its
PR #7). That plugin HAD a header test — it named the headers it cared
about, and a hand-rolled dict happily satisfied the ``User-Agent``
assertion while silently lacking the tenant-routing header. Naming headers
one at a time only ever pins the headers you already thought of.

So this file gates the *construction path*, not the header names:

  ``test_every_platform_call_uses_the_shared_builder``
      walks the AST of the whole package, finds every HTTP call whose URL
      derives from ``PLATFORM_URL``, and fails unless its ``headers=``
      expression traces back to a builder in :data:`SANCTIONED_BUILDERS`.

  ``test_every_sanctioned_builder_emits_the_tenant_header``
      is parametrized over :data:`SANCTIONED_BUILDERS` itself, so the one
      obvious way to silence the gate above — add your new hand-rolled
      builder to the allowlist — immediately subjects that builder to the
      behavioural assertion. There is no edit that widens the allowlist
      without also proving the new entry attaches the header.

  ``test_the_analyzer_actually_detects_a_hand_rolled_dict``
      negative-controls the analyzer against synthetic source, so a
      refactor that quietly breaks the detector cannot leave a
      green-but-inert gate behind (the failure mode that let this ship).
"""
from __future__ import annotations

import ast
import os
import pathlib
import re
import textwrap

import pytest

import molecule_runtime.platform_auth as platform_auth
from molecule_runtime.a2a_tools_rbac import (
    _auth_headers_for_heartbeat,
    auth_headers_for_heartbeat,
)

PKG_ROOT = pathlib.Path(platform_auth.__file__).resolve().parent

# The tenant-routing header the platform's TenantGuard demands BEFORE it
# authenticates (molecule-core workspace-server
# internal/middleware/tenant_guard.go).
ORG_HEADER = "X-Molecule-Org-Id"

# Every function permitted to produce headers for a platform-bound request.
# Adding an entry here is NOT a free pass: the parametrized behavioural test
# below imports each one and asserts it emits ORG_HEADER.
SANCTIONED_BUILDERS: dict[str, object] = {
    "platform_headers": platform_auth.platform_headers,
    "auth_headers": platform_auth.auth_headers,
    "self_source_headers": platform_auth.self_source_headers,
    "tenant_headers": platform_auth.tenant_headers,
    "auth_headers_for_heartbeat": auth_headers_for_heartbeat,
    # legacy underscore re-export (a2a_tools_rbac), imported under that name
    # by a2a_tools and friends — same object, listed so the AST match stays
    # exact rather than fuzzy.
    "_auth_headers_for_heartbeat": _auth_headers_for_heartbeat,
}

_HTTP_VERBS = {"get", "post", "put", "patch", "delete", "request", "stream", "head", "options"}
_WS_VERBS = {"connect", "ws_connect"}
_PLATFORM_RE = re.compile(r"platform_url|molecule_url", re.I)
_HEADER_KWARGS = ("headers", "extra_headers", "additional_headers")


def _src(node: ast.AST) -> str:
    try:
        return ast.unparse(node)
    except Exception:  # pragma: no cover - defensive
        return "<unparseable>"


def _names(node: ast.AST) -> set[str]:
    return {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}


def _is_url_expr(node: ast.AST) -> bool:
    """True for expressions that BUILD a URL string rather than read one out
    of a response body.

    Without this, ``target_url = resp.json()["url"]`` (a PEER-direct URL
    discovered via the registry) would inherit platform-taint from ``resp``
    and be misreported as platform-bound.
    """
    if isinstance(node, (ast.JoinedStr, ast.BinOp, ast.Name, ast.Attribute, ast.Constant)):
        return True
    if isinstance(node, ast.Await):
        return _is_url_expr(node.value)
    if isinstance(node, ast.Call):
        # os.environ.get("PLATFORM_URL", ...) / _resolve_platform_url(ws) /
        # platform_url.rstrip("/") — a call is URL-building when the platform
        # name appears in the callee or in a literal argument, never when it
        # merely appears somewhere in the receiver chain.
        if _PLATFORM_RE.search(_src(node.func)):
            return True
        return any(
            isinstance(a, ast.Constant) and isinstance(a.value, str) and _PLATFORM_RE.search(a.value)
            for a in node.args
        )
    return False


class _ModuleIndex:
    """Per-module lookups the resolver needs: platform-base variables,
    assignments (by name and by ``self.<attr>``), and function defs."""

    def __init__(self, path: pathlib.Path, tree: ast.Module):
        self.path = path
        self.tree = tree
        self.assigns: dict[str, list[ast.AST]] = {}
        # attr name -> [(assigned value, function the assignment lives in)] —
        # the scope matters: ``self._headers = dict(headers)`` only resolves
        # if we look ``headers`` up in __init__, not in the method doing the
        # request.
        self.attr_assigns: dict[str, list[tuple[ast.AST, ast.AST | None]]] = {}
        self.functions: dict[str, list[ast.AST]] = {}
        # function node -> owning class name, so a call to ``Foo(headers=...)``
        # can be matched against ``Foo.__init__``'s parameter.
        self.owner_class: dict[int, str] = {}
        # every node -> innermost enclosing function
        self.enclosing: dict[int, ast.AST] = {}
        # Local names bound to a sanctioned builder by an import, INCLUDING
        # renames (`from ... import auth_headers as _platform_auth`). Resolved
        # exactly, not by string similarity — a heuristic here would be one
        # more thing that can silently stop matching.
        self.local_builders: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    if alias.name in SANCTIONED_BUILDERS:
                        self.local_builders.add(alias.asname or alias.name)
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                for item in node.body:
                    if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        self.owner_class[id(item)] = node.name
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                self.functions.setdefault(node.name, []).append(node)
                # ast.walk is breadth-first, so an inner function is visited
                # after its parent and correctly overwrites the mapping.
                for child in ast.walk(node):
                    self.enclosing[id(child)] = node
        for node in ast.walk(tree):
            targets: list[ast.AST] = []
            value: ast.AST | None = None
            if isinstance(node, ast.Assign):
                targets, value = list(node.targets), node.value
            elif isinstance(node, (ast.AnnAssign, ast.AugAssign)) and node.value is not None:
                targets, value = [node.target], node.value
            if value is None:
                continue
            for tgt in targets:
                if isinstance(tgt, ast.Name):
                    self.assigns.setdefault(tgt.id, []).append(value)
                elif isinstance(tgt, ast.Attribute) and isinstance(tgt.value, ast.Name):
                    if tgt.value.id == "self":
                        self.attr_assigns.setdefault(tgt.attr, []).append(
                            (value, self.enclosing.get(id(node)))
                        )
        self.base_vars = self._platform_base_vars()

    def enclosing_function(self, node: ast.AST) -> ast.AST | None:
        return self.enclosing.get(id(node))

    def _platform_base_vars(self) -> set[str]:
        base: set[str] = set()
        changed = True
        while changed:
            changed = False
            for name, values in self.assigns.items():
                if name in base:
                    continue
                for value in values:
                    if not _is_url_expr(value):
                        continue
                    if _PLATFORM_RE.search(_src(value)) or (_names(value) & base):
                        base.add(name)
                        changed = True
                        break
        return base

    def is_platform_url(self, node: ast.AST) -> bool:
        if not _is_url_expr(node):
            return False
        return bool(_PLATFORM_RE.search(_src(node)) or (_names(node) & self.base_vars))


def _load_modules() -> dict[pathlib.Path, _ModuleIndex]:
    mods: dict[pathlib.Path, _ModuleIndex] = {}
    for path in sorted(PKG_ROOT.rglob("*.py")):
        mods[path] = _ModuleIndex(path, ast.parse(path.read_text()))
    return mods


class CallSite:
    def __init__(self, mod: _ModuleIndex, call: ast.Call, headers: ast.AST | None, func: ast.AST | None):
        self.mod = mod
        self.call = call
        self.headers = headers
        self.func = func

    @property
    def where(self) -> str:
        try:
            rel: object = self.mod.path.relative_to(PKG_ROOT.parent)
            return f"molecule-ai-workspace-runtime/{rel}:{self.call.lineno}"
        except ValueError:  # synthetic module in the analyzer's own test
            return f"{self.mod.path}:{self.call.lineno}"

    def __repr__(self) -> str:  # pragma: no cover - test output only
        got = _src(self.headers) if self.headers is not None else "<no headers= kwarg>"
        return f"{self.where}  {_src(self.call.func)}(...)  headers={got}"


def platform_call_sites(mods: dict[pathlib.Path, _ModuleIndex]) -> list[CallSite]:
    sites: list[CallSite] = []
    for mod in mods.values():
        for node in ast.walk(mod.tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            verb = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
            if verb not in _HTTP_VERBS | _WS_VERBS:
                continue
            kwargs = {k.arg: k.value for k in node.keywords if k.arg}
            url = node.args[0] if node.args else kwargs.get("url") or kwargs.get("uri")
            if url is None or isinstance(url, ast.Constant):
                continue
            if not mod.is_platform_url(url):
                continue
            headers = next((kwargs[k] for k in _HEADER_KWARGS if k in kwargs), None)
            sites.append(CallSite(mod, node, headers, mod.enclosing_function(node)))
    return sites


def _resolves_to_builder(
    node: ast.AST | None,
    mod: _ModuleIndex,
    mods: dict[pathlib.Path, _ModuleIndex],
    func: ast.AST | None,
    seen: set[tuple[str, int]],
) -> bool:
    """True when *node* provably derives from a sanctioned builder.

    Handles the indirections this codebase actually uses: a direct call, a
    dict literal that splats a builder, a local variable, a ``self._headers``
    copy, and a function PARAMETER — which is resolved interprocedurally
    against every in-repo caller of that function.
    """
    if node is None:
        return False
    key = (str(mod.path), getattr(node, "lineno", -1) * 1000 + getattr(node, "col_offset", 0))
    if key in seen:
        return False
    seen = seen | {key}

    if isinstance(node, ast.Call):
        if isinstance(node.func, ast.Attribute):
            # platform_auth.auth_headers(...) — attribute access on the module
            name = node.func.attr
            if name in SANCTIONED_BUILDERS:
                return True
        else:
            # bare name, possibly an import rename local to this module
            name = getattr(node.func, "id", "")
            if name in SANCTIONED_BUILDERS or name in mod.local_builders:
                return True
        # dict(headers) / headers.copy() wrappers
        if name in {"dict", "copy"}:
            inner = node.args[0] if node.args else (node.func.value if isinstance(node.func, ast.Attribute) else None)
            return _resolves_to_builder(inner, mod, mods, func, seen)
        return False

    if isinstance(node, ast.Dict):
        parts = [v for k, v in zip(node.keys, node.values) if k is None]  # ** entries
        return any(_resolves_to_builder(p, mod, mods, func, seen) for p in parts)

    if isinstance(node, ast.Await):
        return _resolves_to_builder(node.value, mod, mods, func, seen)

    if isinstance(node, ast.Attribute):
        # self._headers = dict(headers) — resolve the value in the scope the
        # assignment lives in (usually __init__), not the caller's scope.
        for value, scope in mod.attr_assigns.get(node.attr, []):
            if _resolves_to_builder(value, mod, mods, scope, seen):
                return True
        return False

    if isinstance(node, ast.Name):
        # (a) assignment inside the enclosing function, then module scope
        scopes: list[ast.AST] = [s for s in (func, mod.tree) if s is not None]
        for scope in scopes:
            for sub in ast.walk(scope):
                tgts: list[ast.AST] = []
                val: ast.AST | None = None
                if isinstance(sub, ast.Assign):
                    tgts, val = list(sub.targets), sub.value
                elif isinstance(sub, (ast.AnnAssign, ast.AugAssign)) and sub.value is not None:
                    tgts, val = [sub.target], sub.value
                if val is None:
                    continue
                if any(isinstance(t, ast.Name) and t.id == node.id for t in tgts):
                    if _resolves_to_builder(val, mod, mods, func, seen):
                        return True
        # (b) the name is a parameter — prove it at every in-repo call site
        if func is not None and isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
            args = func.args
            params = [a.arg for a in args.args + args.posonlyargs + args.kwonlyargs]
            if node.id in params:
                return _param_proven_by_callers(func, node.id, mod, mods, seen)
    return False


def _param_proven_by_callers(
    func: ast.AST,
    param: str,
    owner: _ModuleIndex,
    mods: dict[pathlib.Path, _ModuleIndex],
    seen: set[tuple[str, int]],
) -> bool:
    """Every in-repo call of ``func`` must pass a sanctioned-builder value for
    ``param``. No callers at all ⇒ unproven ⇒ False (a header path nothing in
    the package feeds is exactly the kind of dead-but-live seam that hid #373).

    Three call shapes are recognised, all of which the runtime actually uses:
    a plain call, a constructor call matched against ``__init__``, and an
    executor submission (``pool.submit(fetch_and_stage, ..., headers=...)``)
    where the function travels as a value rather than a callee.
    """
    args = func.args
    positional = [a.arg for a in args.posonlyargs + args.args]
    idx = positional.index(param) if param in positional else None
    # A constructor is reached by the CLASS name, not by "__init__".
    call_names = {func.name}
    if func.name == "__init__":
        cls = owner.owner_class.get(id(func))
        if cls:
            call_names = {cls}
            idx = idx - 1 if idx is not None and idx > 0 else None  # drop `self`
    callers = 0
    for mod in mods.values():
        for node in ast.walk(mod.tree):
            if not isinstance(node, ast.Call):
                continue
            called = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
            direct = called in call_names
            # executor-style: the function is an ARGUMENT, and the kwargs
            # travelling with it are the callee's kwargs.
            submitted = any(
                isinstance(a, ast.Name) and a.id in call_names for a in node.args
            )
            if not (direct or submitted):
                continue
            supplied = next((k.value for k in node.keywords if k.arg == param), None)
            if supplied is None and direct and idx is not None and len(node.args) > idx:
                supplied = node.args[idx]
            if supplied is None:
                continue
            callers += 1
            if not _resolves_to_builder(supplied, mod, mods, mod.enclosing_function(node), seen):
                return False
    return callers > 0


def _offenders() -> list[CallSite]:
    mods = _load_modules()
    bad: list[CallSite] = []
    for site in platform_call_sites(mods):
        if not _resolves_to_builder(site.headers, site.mod, mods, site.func, set()):
            bad.append(site)
    return bad


def test_every_platform_call_uses_the_shared_builder():
    """No platform-bound request may hand-roll its headers.

    A failure here is not a style nit. The platform's TenantGuard rejects a
    request with no ``X-Molecule-Org-Id`` at 400 BEFORE authentication, and
    the caller-side error handling in this repo turns that 400 into a
    per-message string that nothing aggregates — the exact combination that
    kept a paying customer's channel dark for two days with five health
    signals green (#360 → #373).
    """
    bad = _offenders()
    assert not bad, (
        "These platform-bound requests do not build their headers through "
        "molecule_runtime.platform_auth. Use platform_headers(workspace_id) "
        "(or merge **platform_headers(...) into your dict) so the tenant "
        "routing header cannot go missing:\n  "
        + "\n  ".join(repr(b) for b in bad)
    )


def test_the_analyzer_actually_detects_a_hand_rolled_dict(tmp_path):
    """Negative control for the gate above.

    If a refactor breaks the detector, ``_offenders()`` returns [] forever
    and the gate reads green while testing nothing. This pins that a
    hand-rolled dict IS caught, and that the sanctioned form is NOT.
    """
    def offenders_for(source: str) -> list[CallSite]:
        path = tmp_path / "synthetic.py"
        path.write_text(textwrap.dedent(source))
        mod = _ModuleIndex(path, ast.parse(path.read_text()))
        mods = {path: mod}
        return [
            s
            for s in platform_call_sites(mods)
            if not _resolves_to_builder(s.headers, s.mod, mods, s.func, set())
        ]

    hand_rolled = """
        import httpx, os
        PLATFORM_URL = os.environ.get("PLATFORM_URL", "")
        def send(ws, token):
            return httpx.post(
                f"{PLATFORM_URL}/workspaces/{ws}/a2a",
                headers={"Authorization": f"Bearer {token}", "X-Workspace-ID": ws},
            )
    """
    assert len(offenders_for(hand_rolled)) == 1, "detector went blind to a hand-rolled dict"

    # ...and the same call is accepted once it goes through the builder.
    sanctioned = """
        import httpx, os
        from molecule_runtime.platform_auth import platform_headers
        PLATFORM_URL = os.environ.get("PLATFORM_URL", "")
        def send(ws):
            return httpx.post(
                f"{PLATFORM_URL}/workspaces/{ws}/a2a",
                headers=platform_headers(ws, source=True),
            )
    """
    assert offenders_for(sanctioned) == []

    # A peer-DIRECT url (discovered out of a response body) is correctly not
    # claimed by this gate — it never passes through TenantGuard.
    peer_direct = """
        import httpx, os
        from molecule_runtime.platform_auth import platform_headers
        PLATFORM_URL = os.environ.get("PLATFORM_URL", "")
        async def send(client, ws, task):
            resp = await client.get(
                f"{PLATFORM_URL}/registry/discover/{ws}",
                headers=platform_headers(ws, source=True),
            )
            target_url = resp.json().get("url", "")
            return await client.post(target_url, headers={"X-Workspace-ID": ws})
    """
    assert offenders_for(peer_direct) == []


@pytest.mark.parametrize("builder_name", sorted(SANCTIONED_BUILDERS))
def test_every_sanctioned_builder_emits_the_tenant_header(builder_name, monkeypatch):
    """Widening SANCTIONED_BUILDERS cannot be a way to dodge the gate.

    Whatever is on that allowlist is imported and called here, and must
    attach the tenant-routing header when MOLECULE_ORG_ID is set.
    """
    builder = SANCTIONED_BUILDERS[builder_name]
    monkeypatch.setenv("MOLECULE_ORG_ID", "org-11111111-2222-3333-4444-555555555555")
    platform_auth.clear_cache()

    try:
        headers = builder("ws-tenant-guard")  # type: ignore[operator]
    except TypeError:
        headers = builder()  # type: ignore[operator]

    assert headers.get(ORG_HEADER) == "org-11111111-2222-3333-4444-555555555555", (
        f"{builder_name}() is on the sanctioned-builder allowlist but does not "
        f"attach {ORG_HEADER}. Every platform-bound request in the package is "
        "allowed to build its headers with it, so the omission would be silent "
        "on the wire and rejected 400 by the platform's TenantGuard."
    )


@pytest.mark.parametrize("builder_name", sorted(SANCTIONED_BUILDERS))
def test_sanctioned_builders_omit_rather_than_forge_the_tenant_header(builder_name, monkeypatch):
    """Self-host (no org id anywhere) must send NO tenant header at all.

    The platform's guard is a passthrough when its own MOLECULE_ORG_ID is
    empty; a defaulted or invented value would be an authorization-adjacent
    claim the runtime has no standing to make.
    """
    builder = SANCTIONED_BUILDERS[builder_name]
    monkeypatch.delenv("MOLECULE_ORG_ID", raising=False)
    monkeypatch.delenv("MOLECULE_ORGANIZATION_ID", raising=False)
    platform_auth.clear_cache()

    try:
        headers = builder("ws-tenant-guard")  # type: ignore[operator]
    except TypeError:
        headers = builder()  # type: ignore[operator]

    assert ORG_HEADER not in headers


def test_header_unsafe_org_id_is_dropped_not_forwarded(monkeypatch):
    """A CR/LF-bearing org id must never reach a header value (CWE-20)."""
    monkeypatch.setenv("MOLECULE_ORG_ID", "org-abc\r\nX-Admin-Token: pwned")
    platform_auth.clear_cache()
    assert platform_auth.get_org_id() is None
    assert ORG_HEADER not in platform_auth.platform_headers()


def test_org_id_alias_is_accepted(monkeypatch):
    monkeypatch.delenv("MOLECULE_ORG_ID", raising=False)
    monkeypatch.setenv("MOLECULE_ORGANIZATION_ID", "org-alias-1")
    platform_auth.clear_cache()
    assert platform_auth.get_org_id() == "org-alias-1"
    assert platform_auth.tenant_headers() == {ORG_HEADER: "org-alias-1"}
    # canonical key wins when both are present
    monkeypatch.setenv("MOLECULE_ORG_ID", "org-canonical")
    assert platform_auth.get_org_id() == "org-canonical"


def test_env_is_read_live_not_frozen_at_import(monkeypatch):
    """MOLECULE_ORG_ID lands in the container env at process start, but a
    workspace can be re-provisioned into an org-id-configured tenant while
    the value is injected later. Reading it at call time (not import time)
    is what lets a long-lived runtime pick it up without a code change.
    """
    monkeypatch.delenv("MOLECULE_ORG_ID", raising=False)
    monkeypatch.delenv("MOLECULE_ORGANIZATION_ID", raising=False)
    assert ORG_HEADER not in platform_auth.platform_headers()
    monkeypatch.setenv("MOLECULE_ORG_ID", "org-appeared-later")
    assert platform_auth.platform_headers()[ORG_HEADER] == "org-appeared-later"


def test_multi_tenant_bridge_does_not_stamp_this_process_org_id(monkeypatch):
    """A workspace with its OWN platform_url belongs to a tenant this
    process's MOLECULE_ORG_ID does not describe.

    ``molecule-mcp`` can serve workspaces in several orgs from one laptop
    (MOLECULE_WORKSPACES); README "Multiple External Workspaces" states that
    the tenant is selected by ``platform_url`` and that org_id is
    deliberately not part of that config. Stamping this process's org id on
    a request bound for a different tenant is the forging this fix refuses.
    """
    monkeypatch.setenv("MOLECULE_ORG_ID", "org-of-this-process")
    platform_auth.clear_cache()
    platform_auth.register_workspace_platform_url("ws-other-tenant", "https://other.moleculesai.app")
    try:
        headers = platform_auth.platform_headers("ws-other-tenant")
        assert headers["Origin"] == "https://other.moleculesai.app"
        assert ORG_HEADER not in headers
        # ...while a workspace with no override is this process's own tenant
        assert platform_auth.platform_headers("ws-local")[ORG_HEADER] == "org-of-this-process"
    finally:
        platform_auth.clear_cache()


def test_org_id_is_not_read_from_the_workspace_id(monkeypatch):
    """Guard against the tempting 'derive it from something we have' fix."""
    monkeypatch.delenv("MOLECULE_ORG_ID", raising=False)
    monkeypatch.delenv("MOLECULE_ORGANIZATION_ID", raising=False)
    monkeypatch.setenv("WORKSPACE_ID", "ws-not-an-org")
    platform_auth.clear_cache()
    assert platform_auth.get_org_id() is None
    assert os.environ.get("MOLECULE_ORG_ID") is None
