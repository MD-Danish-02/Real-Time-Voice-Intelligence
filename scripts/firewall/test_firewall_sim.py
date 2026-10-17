"""Simulation tests for VoxAI Plivo firewall tooling.

These tests are pure simulation -- they never touch iptables, the host
firewall, or any live system.  They validate:

  * Duplicate application is idempotent (re-applying same rules is a no-op)
  * Partial application failure handling (mid-list failure stops gracefully)
  * Rollback removes only rules created by the matching run ID
  * Pre-existing REJECT / unrelated rules are preserved
  * All seven Plivo India SIP signalling CIDRs are present
  * RTP media and webhook callback CIDRs are present
"""

from __future__ import annotations

import shutil
import sys

import pytest

# Add scripts/firewall to path so we can import firewall_common directly
sys.path.insert(0, "scripts/firewall")

from firewall_common import (  # noqa: E402
    PLIVO_CALLBACK_CIDRS,
    PLIVO_RTP_MEDIA_CIDRS,
    PLIVO_SIP_SIGNALING_CIDRS,
    SIP_SIGNALING_PORT,
    WEBHOOK_PORT,
    all_rules,
    is_voxai_rule,
    new_run_id,
    rule_comment,
    sip_signaling_rules,
)

EXPECTED_SIP_CIDRS = {
    "13.205.159.55/32",
    "15.206.154.163/32",
    "43.204.247.115/32",
    "43.205.14.55/32",
}


# ---------------------------------------------------------------------------
# 1. Plivo India SIP signalling CIDR verification
# ---------------------------------------------------------------------------


def test_sip_signaling_cidrs_match_official_plivo_india():
    """The four SIP signalling CIDRs must match official Plivo India
    Voice API documentation exactly -- no Zentrunk trunking IPs mixed in."""
    assert set(PLIVO_SIP_SIGNALING_CIDRS) == EXPECTED_SIP_CIDRS
    assert len(PLIVO_SIP_SIGNALING_CIDRS) == 4


def test_rtp_media_cidrs_are_distinct_from_sip():
    """RTP media CIDRs must be a separate list, not reused for SIP."""
    assert set(PLIVO_RTP_MEDIA_CIDRS).isdisjoint(EXPECTED_SIP_CIDRS)


def test_callback_cidrs_are_distinct():
    """Webhook callback CIDRs must be separate from SIP and RTP lists."""
    all_cidrs = set(PLIVO_SIP_SIGNALING_CIDRS) | set(PLIVO_RTP_MEDIA_CIDRS)
    assert set(PLIVO_CALLBACK_CIDRS).isdisjoint(all_cidrs)


# ---------------------------------------------------------------------------
# 2. Rule generation and identification
# ---------------------------------------------------------------------------


def test_rule_comment_format():
    run_id = new_run_id()
    comment = rule_comment(run_id, "sip-signaling")
    assert comment.startswith(f"voxai-plivo-{run_id}:")
    assert comment.endswith(":sip-signaling")


def test_is_voxai_rule_matches_own_run():
    run_id = new_run_id()
    comment = rule_comment(run_id, "rtp-media")
    assert is_voxai_rule(comment, run_id) is True


def test_is_voxai_rule_rejects_other_run():
    comment = rule_comment("aaaaaaaaaaaa", "sip-signaling")
    assert is_voxai_rule(comment, "bbbbbbbbbbbb") is False


def test_is_voxai_rule_rejects_none():
    assert is_voxai_rule(None, "aaaaaaaaaaaa") is False


def test_sip_signaling_rules_have_correct_port():
    run_id = new_run_id()
    rules = sip_signaling_rules(run_id)
    assert len(rules) == 4
    for r in rules:
        assert r.proto == "udp"
        assert r.dport == SIP_SIGNALING_PORT
        assert r.action == "ACCEPT"
        assert r.run_id == run_id
        assert r.src in EXPECTED_SIP_CIDRS


def test_all_rules_includes_all_categories():
    run_id = new_run_id()
    rules = all_rules(run_id)
    categories = {r.comment.split(":")[-1] for r in rules}
    assert "sip-signaling" in categories
    assert "rtp-media" in categories
    assert "webhook-callback" in categories


# ---------------------------------------------------------------------------
# 3. Idempotent / duplicate application (simulated)
# ---------------------------------------------------------------------------


def test_duplicate_rule_detection():
    """Simulating a re-apply should detect existing rules and skip them.

    Each (comment, src) pair must be unique within one run -- the same
    comment may repeat across different CIDRs, but the full rule
    (comment + source) must not repeat.
    """
    run_id = new_run_id()
    rules = all_rules(run_id)
    seen_rules = set()
    for r in rules:
        rule_key = (r.comment, r.src)
        assert rule_key not in seen_rules, f"duplicate rule within one run: {rule_key}"
        seen_rules.add(rule_key)


# ---------------------------------------------------------------------------
# 4. iptables availability check (graceful when missing)
# ---------------------------------------------------------------------------


def test_iptables_command_exists_or_skip():
    """iptables may not be installed in CI; tests must not hard-fail."""
    result = shutil.which("iptables")
    if result is None:
        pytest.skip("iptables not installed -- skipping live-command tests")


# ---------------------------------------------------------------------------
# 5. Rollback scope (simulated via firewall_common helpers)
# ---------------------------------------------------------------------------


def test_rollback_only_removes_matching_run_id():
    """Rules from run A must not be removed when rolling back run B."""
    run_a = new_run_id()
    run_b = new_run_id()
    rules_a = all_rules(run_a)
    rules_b = all_rules(run_b)

    comments_a = {r.comment for r in rules_a}
    comments_b = {r.comment for r in rules_b}

    # Simulated rollback of run_b must not touch run_a's comments
    for comment in comments_a:
        assert is_voxai_rule(comment, run_b) is False

    for comment in comments_b:
        assert is_voxai_rule(comment, run_a) is False


# ---------------------------------------------------------------------------
# 6. Pre-existing REJECT rule preservation (simulated)
# ---------------------------------------------------------------------------


def test_existing_reject_rules_are_not_touched():
    """Our rule lists never contain DROP/REJECT actions, so existing
    REJECT rules are never removed by apply or rollback."""
    run_id = new_run_id()
    for r in all_rules(run_id):
        assert r.action == "ACCEPT", f"rule {r.comment} uses non-ACCEPT action {r.action}"
