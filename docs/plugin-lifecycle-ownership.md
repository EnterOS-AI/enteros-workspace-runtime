# Plugin lifecycle ownership

The canonical built-in adaptor implementation is
`molecule_runtime/plugins_registry/builtins.py`. The top-level
`plugins_registry/builtins.py` package is a compatibility alias for published
plugin adaptors that import the historical module path; it must not contain a
second implementation.

`AgentskillsAdaptor` records each successful install under
`/configs/.molecule/plugin-ownership/`. The mode-0600 record contains hashes for
plugin-created skills, hooks, and commands, plus the exact marked memory block
and settings contribution. A mode-0600 lifecycle lock serializes updates to
shared memory and settings.

Reinstall updates and retires only unchanged owned files. Uninstall removes
only content the record still proves the plugin created. Pre-existing paths,
modified files or memory blocks, unrelated settings, and invalid/legacy records
are preserved. A legacy install without a record is intentionally a no-op and
requires manual review before cleanup.

When the mailbox kernel is enabled, memory ownership records the resolved
durable mailbox path rather than assuming `/configs/<memory file>`. The stored
path is accepted only when it matches the runtime's allowed config or mailbox
target.

Custom adaptors and plugin `setup.sh` scripts can create state outside these
managed surfaces. They remain responsible for their own explicit ownership and
cleanup contracts.
