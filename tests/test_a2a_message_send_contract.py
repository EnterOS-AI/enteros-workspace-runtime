"""Contract test: outbound A2A ``message/send`` envelopes validate against
the REAL a2a-sdk v0.3 ``SendMessageRequest`` Pydantic schema (#2251).

Why this test exists — the coverage gap that let #2251 ship
====================================================================
The receiver runs a2a-sdk 1.x with ``enable_v0_3_compat=True`` (see
``boot_routes.py``). That compat layer validates every inbound A2A
request against a Pydantic ``SendMessageRequest`` whose
``Message.role`` is REQUIRED. An outbound envelope that omits ``role``
is rejected at parse time with:

    1 validation error for SendMessageRequest
    params.message.role
      Field required ...

surfaced to the caller as JSON-RPC ``-32600 Invalid Request`` — which
silently broke ALL task delegation (the agents-team transport-retry
storm).

The whole runtime test suite stubs the ``a2a`` package in
``conftest.py`` (it's heavy + lives only in the workspace image), so
NO existing test ever exercised the real validator. Every builder's
envelope shape was therefore unverified against the schema it has to
satisfy. This test closes that gap by importing the genuine
``a2a.compat.v0_3.types.SendMessageRequest`` and asserting:

  * the canonical normalizer output validates,
  * an envelope with a MISSING role FAILS with the exact ``role``
    error (so a regression that drops role fails CI), and
  * the live ``send_a2a_message`` outbound body validates.

If a2a-sdk is not installed in the test env, the whole module SKIPS
loudly (never silently passes) — CI installs the wheel, the local
stubbed unit env does not.
"""
from __future__ import annotations

import importlib
import sys

import pytest


def _import_real_v03_types():
    """Import the REAL ``a2a.compat.v0_3.types`` module, bypassing the
    ``conftest.py`` stub.

    ``conftest.py`` registers fake ``a2a*`` modules in ``sys.modules``
    before any test imports, so a plain ``import a2a...`` resolves to the
    stub (no Pydantic validation). This helper temporarily evicts the
    ``a2a*`` stubs, imports the genuine module from site-packages, then
    restores the stubs so the rest of the suite keeps using them.

    Returns the real types module, or ``None`` when a2a-sdk is not
    installed (caller turns that into a loud skip).
    """
    saved = {
        k: v for k, v in list(sys.modules.items())
        if k == "a2a" or k.startswith("a2a.")
    }
    for k in saved:
        del sys.modules[k]
    try:
        return importlib.import_module("a2a.compat.v0_3.types")
    except ModuleNotFoundError:
        return None
    finally:
        # Drop the real a2a modules we just imported and restore the
        # conftest stubs so the remaining tests see the same stub set.
        for k in [k for k in list(sys.modules) if k == "a2a" or k.startswith("a2a.")]:
            del sys.modules[k]
        sys.modules.update(saved)


_V3 = _import_real_v03_types()

pytestmark = pytest.mark.skipif(
    _V3 is None,
    reason="a2a-sdk not installed — install a2a-sdk[http-server] to run the "
    "message/send schema contract test (CI installs it; the stubbed unit "
    "env does not).",
)


def _validate(envelope: dict) -> None:
    """Validate a full JSON-RPC envelope against the real schema.

    Raises ``pydantic.ValidationError`` on a schema-invalid envelope —
    the same failure the receiver produces.
    """
    _V3.SendMessageRequest.model_validate(envelope)


def _envelope(params: dict) -> dict:
    return {
        "jsonrpc": "2.0",
        "id": "test-id",
        "method": "message/send",
        "params": params,
    }


# --------------------------------------------------------------------------
# 0. The canonical OUTBOUND builder generates schema-valid params (#2251
#    SSOT upgrade). This is THE single source of truth every outbound
#    send funnels through — its output must validate for every shape the
#    call sites use (plain text, custom messageId, sibling metadata,
#    file attachments).
# --------------------------------------------------------------------------

def test_builder_plain_text_validates():
    from molecule_runtime.a2a_client import build_message_send_params

    params = build_message_send_params("hello")
    assert params["message"]["role"] == "user"
    assert params["message"]["messageId"]  # defaulted
    assert params["message"]["parts"] == [{"kind": "text", "text": "hello"}]
    _validate(_envelope(params))


def test_builder_defaults_role_when_invalid():
    from molecule_runtime.a2a_client import build_message_send_params

    params = build_message_send_params("x", role="bogus")
    assert params["message"]["role"] == "user"
    _validate(_envelope(params))


def test_builder_preserves_message_id_and_metadata():
    from molecule_runtime.a2a_client import build_message_send_params

    # Exactly the shape builtin_tools/delegation.py constructs.
    params = build_message_send_params(
        "do the thing",
        message_id="msg-task1-0",
        metadata={"parent_task_id": "task1", "source_workspace_id": "w"},
    )
    assert params["message"]["messageId"] == "msg-task1-0"
    assert params["metadata"]["parent_task_id"] == "task1"
    _validate(_envelope(params))


def test_builder_agent_role_validates():
    from molecule_runtime.a2a_client import build_message_send_params

    params = build_message_send_params("x", role="agent")
    assert params["message"]["role"] == "agent"
    _validate(_envelope(params))


def test_builder_attachments_become_file_parts_and_validate():
    from molecule_runtime.a2a_client import build_message_send_params

    params = build_message_send_params(
        "see attached",
        attachments=[
            {"uri": "workspace:/x.png", "name": "x.png", "mimeType": "image/png"}
        ],
    )
    kinds = [p["kind"] for p in params["message"]["parts"]]
    assert kinds == ["text", "file"]
    _validate(_envelope(params))


# --------------------------------------------------------------------------
# 1. The normalizer output is schema-valid for every input shape.
# --------------------------------------------------------------------------

def test_normalizer_defaults_role_and_validates():
    from molecule_runtime.a2a_client import normalize_a2a_message_send_params

    # Input WITHOUT role — exactly the #2251 broken shape.
    params = normalize_a2a_message_send_params(
        {"message": {"parts": [{"kind": "text", "text": "hello"}]}}
    )
    assert params["message"]["role"] == "user"
    assert params["message"]["messageId"]  # defaulted
    _validate(_envelope(params))  # must not raise


def test_normalizer_rewrites_type_part_to_kind_and_validates():
    from molecule_runtime.a2a_client import normalize_a2a_message_send_params

    # Legacy {"type": "text"} discriminator (heartbeat builders used this).
    params = normalize_a2a_message_send_params(
        {"message": {"parts": [{"type": "text", "text": "hi"}]}}
    )
    part = params["message"]["parts"][0]
    assert part["kind"] == "text"
    assert "type" not in part
    _validate(_envelope(params))


def test_normalizer_preserves_valid_role_and_sibling_metadata():
    from molecule_runtime.a2a_client import normalize_a2a_message_send_params

    params = normalize_a2a_message_send_params(
        {
            "message": {
                "role": "agent",
                "messageId": "fixed-id",
                "parts": [{"kind": "text", "text": "x"}],
            },
            "metadata": {"parent_task_id": "t1"},
        }
    )
    assert params["message"]["role"] == "agent"  # valid role preserved
    assert params["message"]["messageId"] == "fixed-id"  # caller id preserved
    assert params["metadata"] == {"parent_task_id": "t1"}  # sibling untouched
    _validate(_envelope(params))


def test_normalizer_synthesizes_part_from_default_text():
    from molecule_runtime.a2a_client import normalize_a2a_message_send_params

    params = normalize_a2a_message_send_params(
        {"message": {"role": "user"}}, default_text="synthesized"
    )
    assert params["message"]["parts"] == [{"kind": "text", "text": "synthesized"}]
    _validate(_envelope(params))


# --------------------------------------------------------------------------
# 2. The test PROVES it catches the regression: the unfixed (role-less)
#    envelope must FAIL validation with the exact #2251 error.
# --------------------------------------------------------------------------

def test_role_less_envelope_fails_validation_regression_guard():
    """A raw envelope that omits role (the pre-fix shape) MUST be rejected
    by the real schema with a ``params.message.role`` error. This is the
    assertion that would have caught #2251 in CI."""
    from pydantic import ValidationError

    broken = _envelope(
        {"message": {"messageId": "m1", "parts": [{"kind": "text", "text": "hi"}]}}
    )
    with pytest.raises(ValidationError) as exc:
        _validate(broken)
    # The error must be about the missing role field specifically.
    errors = exc.value.errors()
    assert any(
        e["type"] == "missing" and e["loc"][-1] == "role" for e in errors
    ), f"expected a missing-role error, got: {errors}"


# --------------------------------------------------------------------------
# 3. The LIVE send_a2a_message outbound body validates against the schema.
#    Captures the real JSON sent on the wire by monkeypatching the client.
# --------------------------------------------------------------------------

def test_send_a2a_message_outbound_body_validates(monkeypatch):
    import asyncio

    import molecule_runtime.a2a_client as client

    captured: dict = {}

    class _FakeResp:
        status_code = 200

        def json(self):
            # Minimal JSON-RPC success so send_a2a_message returns cleanly.
            return {"jsonrpc": "2.0", "id": "x", "result": {"parts": []}}

    class _FakeAsyncClient:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, headers=None, json=None):
            captured["json"] = json
            return _FakeResp()

    monkeypatch.setattr(client.httpx, "AsyncClient", _FakeAsyncClient)

    peer = "11111111-1111-1111-1111-111111111111"
    asyncio.run(client.send_a2a_message(peer, "do the thing", source_workspace_id=peer))

    assert captured["json"]["method"] == "message/send"
    # The exact body that went on the wire must satisfy the receiver schema.
    _validate(captured["json"])
    assert captured["json"]["params"]["message"]["role"] == "user"


# --------------------------------------------------------------------------
# 4. EVERY outbound send helper funnels through the single builder, so the
#    body each one actually puts on the wire is schema-valid (#2251 SSOT).
#    Each case captures the real httpx POST json and validates it against
#    the genuine SendMessageRequest schema.
# --------------------------------------------------------------------------

class _CapturingPostClient:
    """httpx.AsyncClient stand-in that captures the FIRST message/send POST
    body into the shared ``captured`` dict. Discovery GETs return a fake
    peer URL; the message/send POST returns a minimal JSON-RPC success.
    """

    def __init__(self, captured: dict, *a, **kw):
        self._captured = captured

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url, headers=None, **kw):
        return _FakeGet()

    async def post(self, url, headers=None, json=None, **kw):
        if isinstance(json, dict) and json.get("method") == "message/send":
            self._captured.setdefault("json", json)
        return _FakePost()


class _FakeGet:
    status_code = 200

    def json(self):
        # Discovery response — a URL so the helper proceeds to the POST.
        return {"url": "http://peer.local/a2a", "id": "peer", "name": "peer"}


class _FakePost:
    status_code = 200

    def json(self):
        return {"jsonrpc": "2.0", "id": "x", "result": {"parts": []}}


_PEER = "22222222-2222-2222-2222-222222222222"


def _drive_builtin_delegate_task(captured, monkeypatch):
    import asyncio

    import molecule_runtime.builtin_tools.a2a_tools as bt

    monkeypatch.setattr(
        bt.httpx, "AsyncClient",
        lambda *a, **kw: _CapturingPostClient(captured, *a, **kw),
    )
    asyncio.run(bt.delegate_task(_PEER, "do the thing"))


def _drive_builtin_execute_delegation(captured, monkeypatch):
    import asyncio

    import molecule_runtime.builtin_tools.delegation as dele

    monkeypatch.setattr(
        dele.httpx, "AsyncClient",
        lambda *a, **kw: _CapturingPostClient(captured, *a, **kw),
    )
    # _execute_delegation reads the in-memory record keyed by task_id —
    # register it first (the same thing delegate_task_async does).
    dele._delegations["task-xyz"] = dele.DelegationTask(
        task_id="task-xyz", workspace_id=_PEER, task_description="do the thing"
    )
    try:
        # _record_delegation_on_platform / _update fire their own POSTs that
        # are NOT message/send; the capturing client ignores those.
        asyncio.run(dele._execute_delegation("task-xyz", _PEER, "do the thing"))
    finally:
        dele._delegations.pop("task-xyz", None)


@pytest.mark.parametrize(
    "driver",
    [_drive_builtin_delegate_task, _drive_builtin_execute_delegation],
    ids=["builtin_tools.a2a_tools.delegate_task",
         "builtin_tools.delegation._execute_delegation"],
)
def test_every_outbound_send_helper_body_validates(driver, monkeypatch):
    captured: dict = {}
    driver(captured, monkeypatch)
    assert "json" in captured, "helper did not emit a message/send POST"
    body = captured["json"]
    assert body["method"] == "message/send"
    # The real body must satisfy the receiver's SendMessageRequest schema.
    _validate(body)
    assert body["params"]["message"]["role"] in ("user", "agent")
