"""<SYSTEM IDLE PROMPT> framing — the transport-layer title on self-fires."""

from molecule_runtime.idle_digest.contract import SYSTEM_IDLE_HEADER, frame_idle_prompt


def test_frames_with_title():
    framed = frame_idle_prompt("digest body")
    assert framed.splitlines()[0] == SYSTEM_IDLE_HEADER
    assert framed.endswith("digest body")


def test_idempotent_no_stacked_banners():
    once = frame_idle_prompt("body")
    assert frame_idle_prompt(once) == once


def test_empty_body_still_titled():
    assert frame_idle_prompt("").startswith(SYSTEM_IDLE_HEADER)
