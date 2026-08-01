#!/usr/bin/env python3
"""Mirror the management-MCP npm tree into our own Gitea npm registry, and
regenerate the vendored lockfile the prebake installs from.

WHY (issue #393). ``molecule_runtime/scripts/prebake-mgmt-mcp.sh`` used to pull
``@molecule-ai/mcp-server``'s TRANSITIVE tree live from registry.npmjs.org on
every workspace-template image build: only the ``@molecule-ai`` SCOPE pointed at
our registry, so ``@modelcontextprotocol/sdk``, ``pino``, ``zod``, ``express``,
... all came from upstream. An npmjs.org bad minute therefore blocked producing
a workspace image AT ALL -- including a security fix we might need to ship
urgently. It took CI down on 2026-08-01 (ETIMEDOUT on
``registry.npmjs.org/@modelcontextprotocol%2fsdk``).

WHAT THIS DOES (operator tool, run on the prod box, NOT part of any build):

  1. Resolves the FULL transitive tree of
     ``<MANAGEMENT_MCP_NPM_PACKAGE>@<MANAGEMENT_MCP_PINNED_VERSION>`` against
     upstream, via ``npm install --package-lock-only`` (npm is the SSOT
     resolver -- we do not reimplement semver resolution).
  2. Downloads each resolved tarball and re-publishes it BYTE-IDENTICALLY into
     our Gitea npm registry. The original ``dist.integrity``/``dist.shasum`` are
     published verbatim and Gitea preserves them, so the mirrored artifact is
     provably the same bytes as upstream -- ``npm ci`` re-verifies the SAME
     sha512 against a tarball served from our host.
  3. Rewrites every ``resolved`` URL in the lockfile to our registry and writes
     the vendored pin to ``molecule_runtime/scripts/mgmt-mcp-lock/``.
     ``resolved`` matters: a lockfile entry's ``resolved`` URL OVERRIDES the
     configured registry, so a lock that still names registry.npmjs.org would
     keep the live upstream dependency no matter what ``.npmrc`` says.

The emitted lock is the REPRODUCIBILITY PIN: the prebake ``npm ci``s it, so two
builds of the same runtime release install the same 120-package tree, and a
package missing from the mirror is a HARD 404 (fail-loud), never a silent
fallback to npmjs.org.

CREDENTIALS -- reuses the ESTABLISHED publish path, the ``gitea-npm-publisher``
Gitea user (basic auth), same identity that publishes ``@molecule-ai/mcp-server``
itself. No new mechanism, no new account:

    GITEA_NPM_PUBLISHER_USER / GITEA_NPM_PUBLISHER_PASSWORD

PUBLISH ENDPOINT -- Cloudflare in front of git.moleculesai.app rejects the npm
``PUT`` publish (403 ``error code: 1010``), the same class of block that forces
container-registry pushes host-direct. Point ``--publish-base`` at the origin
when running on the box:

    export $(grep -v '^#' /d/MoleculesAI/.secrets/gitea-npm-publisher.env | xargs)
    python scripts/mirror_mgmt_mcp_npm.py --publish-base http://127.0.0.1:3200

READS stay on the public URL, which is what lands in the lockfile and what the
image build resolves against.

Re-run this whenever ``MANAGEMENT_MCP_PINNED_VERSION`` moves. It is idempotent:
a version already mirrored with a matching integrity is skipped.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from molecule_runtime.platform_agent_identity import (  # noqa: E402
    MANAGEMENT_MCP_LOCK_DIR,
    MANAGEMENT_MCP_NPM_PACKAGE,
    MANAGEMENT_MCP_PINNED_VERSION,
    MANAGEMENT_MCP_REGISTRY,
    MANAGEMENT_MCP_UPSTREAM_REGISTRY_HOST,
)

UPSTREAM = f"https://{MANAGEMENT_MCP_UPSTREAM_REGISTRY_HOST}/"
LOCK_DIR = REPO_ROOT / "molecule_runtime" / MANAGEMENT_MCP_LOCK_DIR


# Cloudflare fronts git.moleculesai.app and 403s (``error code: 1010``) the
# default ``Python-urllib/x`` User-Agent. npm's own UA is fine -- this only
# affects this operator tool.
_UA = {"User-Agent": "molecule-mgmt-mcp-mirror/1.0 (+https://git.moleculesai.app)"}


def _fetch(url: str, *, headers: dict[str, str] | None = None) -> bytes:
    req = urllib.request.Request(url, headers={**_UA, **(headers or {})})
    with urllib.request.urlopen(req, timeout=120) as resp:
        return resp.read()


def _write_lf(path: Path, text: str) -> None:
    """Write LF + utf-8 explicitly.

    The emitted files are read by ``grep``/``npm ci`` inside the image and are
    .gitattributes-pinned to LF, so a Windows-default CRLF write would churn all
    ~120 entries on line endings alone every time this regenerates.
    """
    path.write_text(text, encoding="utf-8", newline="\n")


def _quoted(name: str) -> str:
    """npm registry path form: the scope separator is percent-encoded."""
    return name.replace("/", "%2f")


def _entry_name(path: str, entry: dict[str, Any]) -> str:
    """Package name for a lockfile-v3 ``packages`` key.

    v3 entries usually omit ``name``; the key is the install PATH, and a nested
    dedupe looks like ``node_modules/type-is/node_modules/content-type`` -- so
    the name is what follows the LAST ``node_modules/``, not the first (that
    off-by-one would try to mirror a package literally named
    ``type-is/node_modules/content-type``).
    """
    return entry.get("name") or path.rsplit("node_modules/", 1)[-1]


def resolve_tree(spec: str, workdir: Path) -> dict[str, Any]:
    """Let npm resolve the transitive tree against upstream. npm is the SSOT."""
    workdir.mkdir(parents=True, exist_ok=True)
    (workdir / ".npmrc").write_text(
        f"registry={UPSTREAM}\n"
        f"@molecule-ai:registry={MANAGEMENT_MCP_REGISTRY}\n"
        "audit=false\nfund=false\n"
    )
    (workdir / "package.json").write_text(
        json.dumps(
            {
                "name": "molecule-mgmt-mcp-bake",
                "version": "0.0.0",
                "private": True,
                # EXACT, not a range: this file is the pin.
                "dependencies": {
                    MANAGEMENT_MCP_NPM_PACKAGE: MANAGEMENT_MCP_PINNED_VERSION
                },
            },
            indent=2,
        )
        + "\n"
    )
    subprocess.run(
        [
            "npm",
            "install",
            "--package-lock-only",
            "--no-audit",
            "--no-fund",
            "--loglevel=error",
        ],
        cwd=workdir,
        check=True,
        shell=(os.name == "nt"),
    )
    return json.loads((workdir / "package-lock.json").read_text())


def mirrored_versions(read_base: str, name: str) -> dict[str, Any]:
    """Versions already present in our registry (empty dict if unpublished)."""
    try:
        return json.loads(_fetch(read_base + _quoted(name))).get("versions", {})
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return {}
        raise


def publish(
    publish_base: str,
    name: str,
    version: str,
    manifest: dict[str, Any],
    tgz: bytes,
    auth: str,
) -> None:
    filename = f"{name.split('/')[-1]}-{version}.tgz"
    payload = {
        "_id": name,
        "name": name,
        "description": manifest.get("description", ""),
        "dist-tags": {"latest": version},
        "versions": {version: manifest},
        "_attachments": {
            filename: {
                "content_type": "application/octet-stream",
                "data": base64.b64encode(tgz).decode(),
                "length": len(tgz),
            }
        },
    }
    req = urllib.request.Request(
        publish_base + _quoted(name),
        data=json.dumps(payload).encode(),
        method="PUT",
        headers={**_UA, "Content-Type": "application/json", "Authorization": auth},
    )
    with urllib.request.urlopen(req, timeout=300) as resp:
        if resp.status not in (200, 201):  # pragma: no cover - server contract
            raise RuntimeError(f"publish {name}@{version} -> HTTP {resp.status}")


def tarball_url(read_base: str, name: str, version: str) -> str:
    """The URL Gitea serves the mirrored tarball at -- what goes in the lock."""
    base = name.split("/")[-1]
    return (
        read_base
        + urllib.parse.quote(name, safe="")
        + f"/-/{version}/{base}-{version}.tgz"
    )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--publish-base",
        default=MANAGEMENT_MCP_REGISTRY,
        help="registry base to PUT to (use http://127.0.0.1:3200/... on the box; CF blocks PUT)",
    )
    ap.add_argument(
        "--read-base",
        default=MANAGEMENT_MCP_REGISTRY,
        help="registry base for the lock URLs",
    )
    ap.add_argument(
        "--dry-run", action="store_true", help="resolve + report, publish nothing"
    )
    args = ap.parse_args()

    publish_base = (
        args.publish_base.rstrip("/") + "/api/packages/molecule-ai/npm/"
        if "/api/" not in args.publish_base
        else args.publish_base
    )
    if not publish_base.endswith("/"):
        publish_base += "/"
    read_base = args.read_base if args.read_base.endswith("/") else args.read_base + "/"

    user = os.environ.get("GITEA_NPM_PUBLISHER_USER")
    password = os.environ.get("GITEA_NPM_PUBLISHER_PASSWORD")
    if not args.dry_run and not (user and password):
        print(
            "mirror: GITEA_NPM_PUBLISHER_USER / GITEA_NPM_PUBLISHER_PASSWORD are required "
            "(source /d/MoleculesAI/.secrets/gitea-npm-publisher.env)",
            file=sys.stderr,
        )
        return 2
    auth = (
        "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()
        if user
        else ""
    )

    spec = f"{MANAGEMENT_MCP_NPM_PACKAGE}@{MANAGEMENT_MCP_PINNED_VERSION}"
    tmp = Path(tempfile.mkdtemp(prefix="mgmt-mcp-mirror-"))
    try:
        print(f"mirror: resolving {spec} against {UPSTREAM}")
        lock = resolve_tree(spec, tmp / "resolve")
        packages: dict[str, Any] = lock["packages"]

        # DEDUPE on (name, version): a nested dedupe puts the SAME name+version
        # at two lockfile paths (e.g. content-type@2.0.0 under both body-parser
        # and type-is), and publishing it twice conflicts. Sorted ascending by
        # version so that when a name IS mirrored at several versions, the
        # highest is published last and owns the ``latest`` dist-tag.
        wanted = sorted(
            {
                (_entry_name(path, entry), entry["version"])
                for path, entry in packages.items()
                if path != "" and "resolved" in entry
            },
            key=lambda nv: (
                nv[0],
                tuple(int(p) if p.isdigit() else 0 for p in nv[1].split(".")),
            ),
        )
        print(f"mirror: {len(wanted)} distinct packages in the tree")

        published, skipped = 0, 0
        for name, version in wanted:
            have = mirrored_versions(read_base, name)
            if version in have:
                skipped += 1
                continue
            if args.dry_run:
                print(f"mirror: WOULD publish {name}@{version}")
                published += 1
                continue
            packument = json.loads(_fetch(UPSTREAM + _quoted(name)))
            manifest = dict(packument["versions"][version])
            dist = dict(manifest.get("dist", {}))
            tgz = _fetch(dist["tarball"])
            # Byte-identity gate: never mirror something that is not the artifact
            # upstream's integrity names.
            actual = "sha512-" + base64.b64encode(hashlib.sha512(tgz).digest()).decode()
            if dist.get("integrity") and dist["integrity"] != actual:
                raise RuntimeError(f"{name}@{version}: upstream integrity mismatch")
            manifest["dist"] = {
                "integrity": dist.get("integrity", actual),
                "shasum": dist.get("shasum", hashlib.sha1(tgz).hexdigest()),  # noqa: S324
            }
            publish(publish_base, name, version, manifest, tgz, auth)
            published += 1
            print(f"mirror: published {name}@{version} ({len(tgz)} bytes)")

        print(f"mirror: {published} published, {skipped} already mirrored")

        # --- rewrite the lock so NOTHING points at npmjs.org -----------------
        for path, entry in packages.items():
            if path == "" or "resolved" not in entry:
                continue
            name = _entry_name(path, entry)
            entry["resolved"] = tarball_url(read_base, name, entry["version"])
        lock["name"] = "molecule-mgmt-mcp-bake"
        lock["version"] = "0.0.0"
        packages[""]["name"] = "molecule-mgmt-mcp-bake"
        packages[""]["version"] = "0.0.0"

        leftover = [
            e["resolved"]
            for e in packages.values()
            if e.get("resolved", "").startswith(UPSTREAM)
        ]
        if leftover:  # pragma: no cover - defensive
            raise RuntimeError(f"lock still references upstream: {leftover[:3]}")

        LOCK_DIR.mkdir(parents=True, exist_ok=True)
        _write_lf(
            LOCK_DIR / "package.json",
            json.dumps(
                {
                    "name": "molecule-mgmt-mcp-bake",
                    "version": "0.0.0",
                    "private": True,
                    "description": (
                        "Generated by scripts/mirror_mgmt_mcp_npm.py -- do not hand-edit. "
                        "The pinned management-MCP tree the image prebake npm-ci's from our mirror."
                    ),
                    "dependencies": {
                        MANAGEMENT_MCP_NPM_PACKAGE: MANAGEMENT_MCP_PINNED_VERSION
                    },
                },
                indent=2,
            )
            + "\n",
        )
        _write_lf(LOCK_DIR / "package-lock.json", json.dumps(lock, indent=2) + "\n")
        print(f"mirror: wrote pin to {LOCK_DIR}")
        return 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
