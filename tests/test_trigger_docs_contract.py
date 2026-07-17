"""The trigger-lane docs must track the shipped lane code.

The pre-fix README claimed the local A2A capability was injected "only into
daemons owned by ``kind: channel`` plugins" — contradicted by
``channel_events._LANE_KINDS`` (channel + trigger). These pins stop the docs
drifting back, and tie the documented allow-list to the code constant so a
future trigger type widening ``TRIGGER_ALLOWED_SOURCE_TYPES`` forces a doc
update.
"""

from pathlib import Path

from molecule_runtime import channel_events

_ROOT = Path(__file__).parents[1]
README = (_ROOT / "README.md").read_text()
TRIGGER_DOC_PATH = _ROOT / "docs" / "trigger-daemons.md"


def test_readme_documents_both_lane_kinds():
    # The refuted pre-fix claim must not come back.
    assert "only into daemons owned by `kind: channel` plugins" not in README
    for kind in channel_events._LANE_KINDS:
        assert f"`kind: {kind}`" in README


def test_readme_links_trigger_daemon_reference():
    assert "docs/trigger-daemons.md" in README
    assert TRIGGER_DOC_PATH.is_file()


def test_docs_state_the_code_allow_list():
    # Both docs quote the allow-list value; pin the prose to the constant.
    trigger_doc = TRIGGER_DOC_PATH.read_text()
    assert channel_events.TRIGGER_ALLOWED_SOURCE_TYPES, "allow-list went empty?"
    for source_type in channel_events.TRIGGER_ALLOWED_SOURCE_TYPES:
        assert f'"{source_type}"' in README
        assert f'"{source_type}"' in trigger_doc
