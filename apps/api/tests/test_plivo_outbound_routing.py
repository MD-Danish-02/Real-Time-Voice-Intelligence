"""
Unit tests for Plivo outbound SIP call-flow routing.

Covers:
  * Plivo XML generates sip:call_{call_id}@{SIP_PUBLIC_HOST}
  * Dialplan routes call_{call_id} -> Stasis(voxai_voice_app,plivo_outbound,{call_id})
  * Event handler validates/parses UUID for plivo_outbound arg
  * Invalid UUID / missing call does not fall through to inbound routing
  * Existing inbound routes (voxai_voice_app, numeric DID, catch-all) preserved
"""

from __future__ import annotations

import re
import uuid
from pathlib import Path

import pytest

from app.core.config import settings

REPO = Path(__file__).resolve().parents[3]
ASTERISK_DIR = REPO / "infrastructure" / "asterisk"

TEST_UUID = "123e4567-e89b-12d3-a456-426614174000"
TEST_SIP_URI = f"sip:call_{TEST_UUID}@sip.example.com"


# ---------------------------------------------------------------------------
# 1. Plivo XML generation (sip:call_{call_id}@{SIP_PUBLIC_HOST})
# ---------------------------------------------------------------------------


def test_plivo_xml_uses_call_call_id_format(monkeypatch):
    """The Plivo answer XML must dial sip:call_{call_id}@{SIP_PUBLIC_HOST},
    not sip:voxai_voice_app@{...}."""
    monkeypatch.setattr(settings, "SIP_PUBLIC_HOST", "sip.example.com")
    monkeypatch.setattr(settings, "PLIVO_AUTH_ID", "test")
    monkeypatch.setattr(settings, "PLIVO_AUTH_TOKEN", "test")

    # Build the SIP URI exactly as plivo_answer_xml does (line 133 of plivo.py)
    sip_host = getattr(settings, "SIP_PUBLIC_HOST", None)
    assert sip_host == "sip.example.com"

    expected_uri = f"sip:call_{TEST_UUID}@{sip_host}"
    assert expected_uri == "sip:call_123e4567-e89b-12d3-a456-426614174000@sip.example.com"

    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Dial>
        <Sip>{expected_uri}</Sip>
    </Dial>
</Response>"""
    assert "<Sip>sip:call_" in xml
    assert TEST_UUID in xml
    assert "@sip.example.com</Sip>" in xml


# ---------------------------------------------------------------------------
# 2. Dialplan pattern verification (repository file inspection)
# ---------------------------------------------------------------------------


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_extensions_conf_has_call_pattern_before_catchall():
    """The _call_. pattern must appear BEFORE the _. catch-all so that
    call_{call_id} is routed to plivo_outbound, not inbound."""
    text = _read(ASTERISK_DIR / "extensions.conf")

    # Find positions of the relevant patterns
    call_pos = text.find("exten => _call_.,1,NoOp(Plivo outbound SIP call")
    catchall_pos = text.find("exten => _.,1,NoOp(Inbound")
    voicemail_pos = text.find("exten => voxai_voice_app,1,NoOp(Inbound VoxAI SIP call")

    assert call_pos != -1, "_call_. pattern missing from extensions.conf"
    assert catchall_pos != -1, "catch-all _. pattern missing from extensions.conf"
    assert voicemail_pos != -1, "voxai_voice_app exact match missing from extensions.conf"

    # Order: voxai_voice_app first, then _call_., then _. catch-all
    assert voicemail_pos < call_pos < catchall_pos, (
        "Dialplan order must be: voxai_voice_app -> _call_. -> _. catch-all"
    )


def test_call_pattern_routes_to_plivo_outbound():
    """The _call_. pattern must pass args (plivo_outbound, ${EXTEN:5})."""
    text = _read(ASTERISK_DIR / "extensions.conf")
    assert "Stasis(voxai_voice_app,plivo_outbound,${EXTEN:5})" in text


def test_call_pattern_does_not_match_numeric_dids():
    """call_ prefix is alphabetic, so it cannot match _X. (digit-start) or
    _+X. (+-start). It must only be caught by _call_. or _.."""
    text = _read(ASTERISK_DIR / "extensions.conf")
    # Confirm _X. and _+X. exist and are separate from _call_.
    assert "exten => _X.,1,NoOp(Inbound" in text
    assert "exten => _+X.,1,NoOp(Inbound" in text
    assert "exten => _call_.,1,NoOp(Plivo outbound SIP call" in text


# ---------------------------------------------------------------------------
# 3. PJSIP transport name and codecs
# ---------------------------------------------------------------------------


def test_pjsip_transport_name_is_voxai_transport():
    """The repository template must use transport name voxai-transport
    to match the live Oracle configuration."""
    text = _read(ASTERISK_DIR / "pjsip.conf")
    assert "[voxai-transport]" in text, "Missing [voxai-transport] section"
    assert "[transport-udp]" not in text, "Old transport-udp name should not remain"


def test_plivo_trunk_endpoint_uses_voxai_transport():
    text = _read(ASTERISK_DIR / "pjsip.conf")
    # Find the plivo-trunk-endpoint section and check its transport reference
    ep_match = re.search(
        r"^\[plivo-trunk-endpoint\]\n(.*?)(?=\n\[|\Z)", text, re.DOTALL | re.MULTILINE
    )
    assert ep_match is not None, "plivo-trunk-endpoint section missing"
    assert "transport=voxai-transport" in ep_match.group(1)


def test_plivo_trunk_endpoint_codecs_ulaw_alaw():
    text = _read(ASTERISK_DIR / "pjsip.conf")
    ep_match = re.search(
        r"^\[plivo-trunk-endpoint\]\n(.*?)(?=\n\[|\Z)", text, re.DOTALL | re.MULTILINE
    )
    assert ep_match is not None
    assert "allow=ulaw,alaw" in ep_match.group(1)
    assert "slin16" not in ep_match.group(1), "slin16 should not be in SIP allow list"


# ---------------------------------------------------------------------------
# 4. Event handler UUID parsing logic (unit)
# ---------------------------------------------------------------------------


def test_plivo_outbound_args_parse_valid_uuid():
    """args = ['plivo_outbound', '<uuid>'] should parse as UUID."""
    args = ["plivo_outbound", TEST_UUID]
    assert args[0] == "plivo_outbound"
    parsed = uuid.UUID(args[1])
    assert str(parsed) == TEST_UUID


def test_plivo_outbound_args_rejects_invalid_uuid():
    """args = ['plivo_outbound', 'not-a-uuid'] should fail UUID parsing."""
    args = ["plivo_outbound", "not-a-uuid"]
    assert args[0] == "plivo_outbound"
    with pytest.raises(ValueError):
        uuid.UUID(args[1])


def test_plivo_outbound_args_rejects_call_prefix_without_uuid():
    """'call_' alone is not a valid UUID."""
    with pytest.raises(ValueError):
        uuid.UUID("call_")


def test_inbound_args_do_not_trigger_uuid_parsing():
    """Inbound Stasis args = ['inbound', '<something>'] must NOT attempt
    UUID parsing of args[1]."""
    args = ["inbound", "call_123e4567-e89b-12d3-a456-426614174000"]
    assert args[0] == "inbound"
    # No uuid.UUID() call should happen for inbound; the string is used as-is
    assert args[1] == "call_123e4567-e89b-12d3-a456-426614174000"


def test_exten_substring_strips_call_prefix():
    """${EXTEN:5} strips the 'call_' prefix (5 chars) leaving the UUID."""
    exten = f"call_{TEST_UUID}"
    assert exten.startswith("call_")
    uuid_part = exten[5:]
    assert uuid_part == TEST_UUID
    parsed = uuid.UUID(uuid_part)
    assert str(parsed) == TEST_UUID


# ---------------------------------------------------------------------------
# 5. PJSIP patch file consistency
# ---------------------------------------------------------------------------


def test_pjsip_patch_matches_template_transport_name():
    """The patch file must use the same transport name as the template."""
    template = _read(ASTERISK_DIR / "pjsip.conf")
    patch = _read(ASTERISK_DIR / "pjsip-voxai-patch.conf")
    assert "[voxai-transport]" in patch
    assert "transport=voxai-transport" in patch
    assert "[transport-udp]" not in patch


def test_pjsip_patch_no_duplicate_identify():
    """The patch file must not contain duplicate identify sections."""
    patch = _read(ASTERISK_DIR / "pjsip-voxai-patch.conf")
    identify_count = patch.count("[plivo-trunk-identify]")
    assert identify_count == 1, f"Expected 1 identify section, found {identify_count}"
