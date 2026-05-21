"""Test-env stubs for workspace-container deps.

`claude_agent_sdk` and `a2a` are installed inside the claude-code workspace
image, not in this package's test environment. Rather than pulling them as
dev deps (they're heavy + change frequently), we stub them before any test
module imports `molecule_runtime.*` — the stubs just need to be importable
so the package source doesn't crash at collect time.

Every known submodule + symbol used anywhere in `molecule_runtime/` is
stubbed here, so test_imports.py (walking every module) + test_session_
resume_gate.py (ClaudeSDKExecutor methods) + future tests all share one
consistent stub set.

NOTE: a2a-sdk 1.x (KI-009 migration) changed the module layout:
- REMOVED: a2a.server.apps (A2AStarletteApplication)
- REMOVED: a2a.utils
- ADDED: a2a.server.routes (create_agent_card_routes, create_jsonrpc_routes)
- ADDED: AgentInterface in a2a.types
"""
from __future__ import annotations

import os
import sys
import types

# Test sentinel for modules that read WORKSPACE_ID at import time
# (a2a_client.py, coordinator.py — added by monorepo dev surface).
# Setting before any test-side import keeps the smoke surface importable
# without forcing every individual test to monkeypatch.
os.environ.setdefault("WORKSPACE_ID", "test-conftest-sentinel")


def _ensure_package(dotted: str) -> types.ModuleType:
    """Create a package-shaped module at `dotted` and all its parents.

    Key detail: the __path__ attribute makes Python treat the stub as a
    PACKAGE, so `from parent.child import X` resolves correctly. Without
    __path__ you get `'parent' is not a package` which is the exact bug
    that broke our first-attempt stubs.
    """
    parts = dotted.split(".")
    for i in range(1, len(parts) + 1):
        name = ".".join(parts[:i])
        if name not in sys.modules:
            mod = types.ModuleType(name)
            mod.__path__ = []  # type: ignore[attr-defined]
            sys.modules[name] = mod
        if i > 1:
            parent = sys.modules[".".join(parts[:i-1])]
            child_name = parts[i-1]
            if not hasattr(parent, child_name):
                setattr(parent, child_name, sys.modules[name])
    return sys.modules[dotted]


def _set_attrs(mod: types.ModuleType, attrs: list[str]) -> None:
    """Attach placeholder classes/functions. AgentExecutor is a class
    so subclasses can inherit; everything else is a no-op callable."""
    for attr in attrs:
        if hasattr(mod, attr):
            continue
        if attr in ("AgentExecutor",):
            setattr(mod, attr, type(attr, (), {}))
        elif attr == "Part":
            # a2a-sdk 1.x Part stub — flat protobuf with optional fields.
            # Tests inspect part.text / part.url / part.filename.
            class Part:
                def __init__(self, text=None, root=None, **kwargs):
                    self.text = text
                    self.root = root
                    for k, v in kwargs.items():
                        setattr(self, k, v)
            setattr(mod, attr, Part)
        elif attr == "Role":
            # a2a-sdk 1.x: Role is an IntEnum. Tests set Message(role=Role.ROLE_AGENT)
            # so the stub needs the canonical values as class attributes.
            class Role:
                ROLE_UNSPECIFIED = 0
                ROLE_USER = 1
                ROLE_AGENT = 2
            setattr(mod, attr, Role)
        elif attr == "TaskState":
            # a2a-sdk 1.x: TaskState is a protobuf EnumTypeWrapper.
            # Stub exposes the canonical SCREAMING_SNAKE_CASE attrs so
            # callers like ``TaskState.TASK_STATE_CANCELED`` resolve to
            # the matching int. Tests added for task #377 (canvas Stop
            # All → A2A tasks/cancel) read TASK_STATE_CANCELED, so the
            # stub MUST expose it.
            class TaskState:
                TASK_STATE_UNSPECIFIED = 0
                TASK_STATE_SUBMITTED = 1
                TASK_STATE_WORKING = 2
                TASK_STATE_COMPLETED = 3
                TASK_STATE_FAILED = 4
                TASK_STATE_CANCELED = 5
                TASK_STATE_INPUT_REQUIRED = 6
                TASK_STATE_REJECTED = 7
                TASK_STATE_AUTH_REQUIRED = 8
            setattr(mod, attr, TaskState)
        elif attr == "TaskStatus":
            class TaskStatus:
                def __init__(self, state=0, message=None, timestamp=None, **kwargs):
                    self.state = state
                    self.message = message
                    self.timestamp = timestamp
                    for k, v in kwargs.items():
                        setattr(self, k, v)
            setattr(mod, attr, TaskStatus)
        elif attr == "TaskStatusUpdateEvent":
            class TaskStatusUpdateEvent:
                def __init__(self, status=None, final=False, task_id="",
                             context_id="", **kwargs):
                    self.status = status
                    self.final = final
                    self.task_id = task_id
                    self.context_id = context_id
                    for k, v in kwargs.items():
                        setattr(self, k, v)
            setattr(mod, attr, TaskStatusUpdateEvent)
        elif attr == "Message":
            # a2a-sdk 1.x Message stub — kwargs-preserving so tests can assert on
            # message_id / role / parts / task_id / context_id.
            class Message:
                def __init__(self, message_id="", role=0, parts=None,
                             task_id="", context_id="", **kwargs):
                    self.message_id = message_id
                    self.role = role
                    self.parts = list(parts) if parts is not None else []
                    self.task_id = task_id
                    self.context_id = context_id
                    for k, v in kwargs.items():
                        setattr(self, k, v)
            setattr(mod, attr, Message)
        else:
            setattr(mod, attr, lambda *a, **kw: None)


# claude_agent_sdk — used by claude_sdk_executor.py
if "claude_agent_sdk" not in sys.modules:
    sdk_stub = types.ModuleType("claude_agent_sdk")
    sdk_stub.ClaudeAgentOptions = lambda **kwargs: types.SimpleNamespace(**kwargs)
    sdk_stub.query = lambda **kwargs: iter([])
    sys.modules["claude_agent_sdk"] = sdk_stub

# a2a + submodules — every import path in the package gets stubbed.
# NOTE: a2a-sdk 1.x (KI-009 migration) removed a2a.server.apps and
# a2a.utils; added a2a.server.routes. Update stubs accordingly.
_A2A_MODULES: dict[str, list[str]] = {
    "a2a": [],
    "a2a.server": [],
    "a2a.server.agent_execution": ["AgentExecutor", "RequestContext", "RequestContextBuilder"],
    "a2a.server.events": ["EventQueue"],
    "a2a.server.tasks": ["TaskUpdater", "InMemoryTaskStore"],
    "a2a.server.routes": [
        "create_agent_card_routes",
        "create_jsonrpc_routes",
        "create_rest_routes",
        "DefaultServerCallContextBuilder",
    ],
    "a2a.server.request_handlers": ["DefaultRequestHandler"],
    "a2a.types": [
        # TextPart removed in 1.x; Part is now flat (Part(text="...") instead of Part(root=TextPart(...)))
        "Part", "AgentCard", "AgentCapabilities", "AgentSkill",
        "AgentInterface",  # added in a2a-sdk 1.x
        "TaskStatus", "TaskState", "TaskStatusUpdateEvent",
        # Role enum — exposed in a2a-sdk 1.x at module level for callers
        # that need to set Message(role=Role.ROLE_AGENT). Tests in
        # test_executor_helpers::test_new_response_message_* import this
        # symbol directly. Real SDK has a Role IntEnum; stub provides a
        # simple class with the canonical values so equality checks work.
        "Role", "Message",
    ],
    "a2a.helpers": [
        # Added in 1.x; replaces a2a.utils
        "new_text_message", "new_task", "new_artifact",
        "get_message_text", "get_text_parts",
    ],
}
for dotted, attrs in _A2A_MODULES.items():
    mod = _ensure_package(dotted)
    _set_attrs(mod, attrs)
