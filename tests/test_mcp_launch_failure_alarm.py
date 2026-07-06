"""GUARD D (task #229 / #228): loud #1027 alarm + refuse-online on a HARD MCP
launch-failure.

The pre-existing DEGRADE-SAFE contract (test_loaded_mcp_tools_probe.py) proves a
*transient stall* is absorbed into core's grace window. This proves the OTHER
half: a hard `npx` launch-failure (e.g. ETARGET — the plugin pins an
@molecule-ai/mcp-server version ahead of the version baked into this image) must
NOT be silently absorbed. It must:

  * fire a LOUD ``logger.critical`` #1027 alarm the moment it's seen, and
  * record a non-None ``launch_failure_reason()`` — the REFUSE-ONLINE signal —

so the concierge fails closed loudly instead of sitting degraded for the whole
grace window (#228). And a *clean* server (or a still-running stall) must NEVER
false-alarm.
"""

import json
import logging
import sys

import pytest

from molecule_runtime import loaded_mcp_tools_probe as probe


@pytest.fixture(autouse=True)
def _reset_launch_signal():
    probe.record_launch_failure(None)
    yield
    probe.record_launch_failure(None)


def _write(tmp_path, name, body):
    p = tmp_path / name
    p.write_text(body)
    return p


# A fake stdio server that reproduces `npx ... ETARGET`: writes the npm error to
# stderr and exits non-zero WITHOUT ever answering an MCP message.
_ETARGET_SERVER = (
    "import sys\n"
    "sys.stderr.write('npm error code ETARGET\\n"
    "npm error notarget No matching version found for "
    "@molecule-ai/mcp-server@1.8.1\\n')\n"
    "sys.stderr.flush()\n"
    "sys.exit(1)\n"
)

# A healthy server: answers initialize + tools/list (zero tools) then exits 0.
_CLEAN_SERVER = (
    "import sys, json\n"
    "for line in sys.stdin:\n"
    "    line = line.strip()\n"
    "    if not line:\n"
    "        continue\n"
    "    msg = json.loads(line)\n"
    "    mid = msg.get('id')\n"
    "    method = msg.get('method')\n"
    "    if method == 'initialize':\n"
    "        sys.stdout.write(json.dumps({'jsonrpc':'2.0','id':mid,'result':"
    "{'protocolVersion':'2024-11-05','capabilities':{},'serverInfo':"
    "{'name':'fake','version':'1'}}}) + '\\n'); sys.stdout.flush()\n"
    "    elif method == 'tools/list':\n"
    "        sys.stdout.write(json.dumps({'jsonrpc':'2.0','id':mid,'result':"
    "{'tools':[]}}) + '\\n'); sys.stdout.flush()\n"
    "        break\n"
)

# A server that just dies non-zero with NO recognizable npm signature.
_BARE_DEATH_SERVER = "import sys; sys.exit(2)\n"


def _spec(server_path):
    return {"command": sys.executable, "args": [str(server_path)]}


@pytest.mark.asyncio
async def test_etarget_launch_failure_fires_1027_and_refuse_online(tmp_path, caplog):
    srv = _write(tmp_path, "etarget.py", _ETARGET_SERVER)
    with caplog.at_level(logging.CRITICAL, logger="platform-agent.identity"):
        result = await probe._list_tools_from_mcp_server("molecule-platform", _spec(srv))

    # No tools enumerated (server never handshook).
    assert result is None
    # REFUSE-ONLINE signal is set and names the ETARGET launch-failure.
    reason = probe.launch_failure_reason()
    assert reason is not None
    assert "ETARGET" in reason
    assert "molecule-platform" in reason
    # LOUD #1027 CRITICAL alarm was emitted (not a silent degrade/warning).
    crit = [r for r in caplog.records if r.levelno >= logging.CRITICAL]
    assert crit, "expected a CRITICAL #1027 alarm on ETARGET launch-failure"
    assert any("#1027" in r.getMessage() for r in crit)


@pytest.mark.asyncio
async def test_clean_server_does_not_false_alarm(tmp_path, caplog):
    srv = _write(tmp_path, "clean.py", _CLEAN_SERVER)
    with caplog.at_level(logging.CRITICAL, logger="platform-agent.identity"):
        result = await probe._list_tools_from_mcp_server("molecule-platform", _spec(srv))

    # Connected + advertised zero tools => [] (distinct from None), NO alarm.
    assert result == []
    assert probe.launch_failure_reason() is None
    assert not [r for r in caplog.records if r.levelno >= logging.CRITICAL]


@pytest.mark.asyncio
async def test_bare_nonzero_exit_still_refuses_online(tmp_path, caplog):
    srv = _write(tmp_path, "die.py", _BARE_DEATH_SERVER)
    with caplog.at_level(logging.CRITICAL, logger="platform-agent.identity"):
        result = await probe._list_tools_from_mcp_server("molecule-platform", _spec(srv))

    assert result is None
    reason = probe.launch_failure_reason()
    assert reason is not None and "exit=2" in reason


@pytest.mark.asyncio
async def test_unspawnable_binary_alarms(tmp_path, caplog):
    spec = {"command": str(tmp_path / "does-not-exist-xyz123"), "args": []}
    with caplog.at_level(logging.CRITICAL, logger="platform-agent.identity"):
        result = await probe._list_tools_from_mcp_server("molecule-platform", spec)

    assert result is None
    assert probe.launch_failure_reason() is not None
    assert any("#1027" in r.getMessage() for r in caplog.records if r.levelno >= logging.CRITICAL)


@pytest.mark.asyncio
async def test_still_running_child_is_not_a_launch_failure():
    # A child still running (returncode None — the stall case) must NEVER be
    # classified as a launch-failure; that path stays with the grace window.
    class _FakeProc:
        returncode = None
        stderr = None

        async def wait(self):
            return None

    await probe._maybe_alarm_launch_failure(_FakeProc(), "molecule-platform")
    assert probe.launch_failure_reason() is None


def test_classify_launch_failure_pure():
    # still running -> not a launch failure
    assert probe._classify_launch_failure(None, "anything") is None
    # clean exit -> not a launch failure
    assert probe._classify_launch_failure(0, "") is None
    # non-zero with signature -> named
    r = probe._classify_launch_failure(1, "npm error code ETARGET")
    assert r is not None and "ETARGET" in r
    # non-zero without signature -> still reported
    assert probe._classify_launch_failure(2, "") == "exit=2"
