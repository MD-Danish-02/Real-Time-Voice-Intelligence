"""Shared firewall rule definitions and run-ID tracking for VoxAI Plivo SIP trunking.

All rule definitions live here so that apply, rollback, and test modules share
a single source of truth.  Nothing in this module touches the host firewall --
it only describes desired state and provides helpers for generating deterministic
comment tokens used to identify rules created by a specific run.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Final

# ---------------------------------------------------------------------------
# Plivo India (Mumbai) region -- SIP signalling source IPs
# These are the *external SIP endpoint* addresses from Plivo's published
# Voice API firewall requirements.  Zentrunk SIP trunking uses a different
# IP set and is intentionally NOT mixed into this list.
# ---------------------------------------------------------------------------
PLIVO_SIP_SIGNALING_CIDRS: Final[list[str]] = [
    "13.205.159.55/32",
    "15.206.154.163/32",
    "43.204.247.115/32",
    "43.205.14.55/32",
]

# ---------------------------------------------------------------------------
# Plivo India (Mumbai) region -- RTP media source IPs
# Used in the host firewall (NOT in PJSIP identify match).
# ---------------------------------------------------------------------------
PLIVO_RTP_MEDIA_CIDRS: Final[list[str]] = [
    "204.89.151.64/26",
    "204.89.151.128/25",
]

# ---------------------------------------------------------------------------
# Plivo India (Mumbai) region -- Webhook callback source IPs
# Used in the webhook host firewall (NOT in Asterisk).
# ---------------------------------------------------------------------------
PLIVO_CALLBACK_CIDRS: Final[list[str]] = [
    "3.7.4.233/32",
    "3.7.220.174/32",
]

# ---------------------------------------------------------------------------
# Ports
# ---------------------------------------------------------------------------
SIP_SIGNALING_PORT: Final[int] = 5060
RTP_MEDIA_PORT_START: Final[int] = 10000
RTP_MEDIA_PORT_END: Final[int] = 20000
WEBHOOK_PORT: Final[int] = 8001
ARI_PORT: Final[int] = 8088

# ---------------------------------------------------------------------------
# Rule comment prefix.  Every rule we insert carries this prefix followed by a
# run ID so that rollback can delete exactly the rules we created and nothing
# else.
# ---------------------------------------------------------------------------
RULE_COMMENT_PREFIX: Final[str] = "voxai-plivo-"


def new_run_id() -> str:
    """Generate a short, filesystem-safe run ID for tagging firewall rules."""
    return uuid.uuid4().hex[:12]


def rule_comment(run_id: str, purpose: str) -> str:
    """Build an iptables comment string for a rule created by *run_id*."""
    return f"{RULE_COMMENT_PREFIX}{run_id}:{purpose}"


def is_voxai_rule(comment: str | None, run_id: str) -> bool:
    """Return True if *comment* was created by our run *run_id*."""
    if not comment:
        return False
    return comment.startswith(f"{RULE_COMMENT_PREFIX}{run_id}:")


@dataclass(frozen=True)
class FirewallRule:
    """A single iptables rule definition."""

    chain: str
    proto: str
    src: str
    dport: str | int
    action: str
    comment: str
    run_id: str

    def iptables_cmd(self, action: str = "-A") -> list[str]:
        """Return the iptables invocation for this rule."""
        return [
            "iptables",
            action,
            self.chain,
            "-p",
            self.proto,
            "-s",
            self.src,
            "--dport",
            str(self.dport),
            "-j",
            self.action,
            "-m",
            "comment",
            "--comment",
            self.comment,
        ]


# ---------------------------------------------------------------------------
# Pre-built rule lists
# ---------------------------------------------------------------------------

def sip_signaling_rules(run_id: str, chain: str = "INPUT") -> list[FirewallRule]:
    """SIP signalling rules: allow Plivo signalling IPs to UDP 5060."""
    return [
        FirewallRule(
            chain=chain,
            proto="udp",
            src=cidr,
            dport=SIP_SIGNALING_PORT,
            action="ACCEPT",
            comment=rule_comment(run_id, "sip-signaling"),
            run_id=run_id,
        )
        for cidr in PLIVO_SIP_SIGNALING_CIDRS
    ]


def rtp_media_rules(run_id: str, chain: str = "INPUT") -> list[FirewallRule]:
    """RTP media rules: allow Plivo media IPs to UDP 10000-20000."""
    return [
        FirewallRule(
            chain=chain,
            proto="udp",
            src=cidr,
            dport=f"{RTP_MEDIA_PORT_START}:{RTP_MEDIA_PORT_END}",
            action="ACCEPT",
            comment=rule_comment(run_id, "rtp-media"),
            run_id=run_id,
        )
        for cidr in PLIVO_RTP_MEDIA_CIDRS
    ]


def webhook_callback_rules(run_id: str, chain: str = "INPUT") -> list[FirewallRule]:
    """Webhook callback rules: allow Plivo callback IPs to TCP 8001."""
    return [
        FirewallRule(
            chain=chain,
            proto="tcp",
            src=cidr,
            dport=WEBHOOK_PORT,
            action="ACCEPT",
            comment=rule_comment(run_id, "webhook-callback"),
            run_id=run_id,
        )
        for cidr in PLIVO_CALLBACK_CIDRS
    ]


def all_rules(run_id: str) -> list[FirewallRule]:
    """All VoxAI Plivo firewall rules."""
    return sip_signaling_rules(run_id) + rtp_media_rules(run_id) + webhook_callback_rules(run_id)
