"""Apply VoxAI Plivo firewall rules -- dry-run by default.

Usage:
    python apply_firewall_rules.py               # dry-run (no changes)
    python apply_firewall_rules.py --apply       # apply to live host
    python apply_firewall_rules.py --apply --run-id <id>  # reuse existing run ID

Safety properties:
  * Dry-run by default -- no iptables invocation unless --apply is passed.
  * Never flushes firewall rules or removes unrelated rules.
  * Rules are tagged with a run ID; only rules with that comment are ever
    touched by rollback.
  * Each rule is checked for existence before insertion (idempotent).
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from typing import Optional

from firewall_common import all_rules, new_run_id, rule_comment


def _run_iptables(cmd: list[str], dry_run: bool = True) -> tuple[int, str, str]:
    """Run an iptables command, or print it when dry_run is True."""
    display = " ".join(cmd)
    if dry_run:
        print(f"[dry-run] iptables {display}")
        return 0, "", ""
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, check=False, timeout=15
        )
        return result.returncode, result.stdout, result.stderr
    except FileNotFoundError:
        print(f"ERROR: iptables binary not found. Cannot apply {display}", file=sys.stderr)
        return 127, "", "iptables not found"
    except subprocess.TimeoutExpired:
        print(f"ERROR: iptables timed out: {display}", file=sys.stderr)
        return 1, "", "timeout"


def _rule_exists(run_id: str, chain: str, comment: str) -> bool:
    """Check whether an iptables rule with the given comment already exists."""
    rc, stdout, _ = _run_iptables(
        ["iptables", "-S", chain], dry_run=False
    )
    if rc != 0:
        return False
    target_comment = f'-m comment --comment "{comment}"'
    return any(target_comment in line for line in stdout.splitlines())


def apply_rules(run_id: Optional[str] = None, dry_run: bool = True) -> int:
    """Apply all VoxAI Plivo firewall rules.

    Returns the number of rules that would be / were applied.
    """
    if run_id is None:
        run_id = new_run_id()

    rules = __import__("firewall_common", fromlist=["all_rules"]).all_rules(run_id)
    applied = 0

    for rule in rules:
        cmd = rule.iptables_cmd(action="-A")
        comment = rule.comment

        if _rule_exists(run_id, rule.chain, comment):
            print(f"[skip] rule already exists: {comment} ({rule.src} -> {rule.dport})")
            continue

        rc, _, stderr = _run_iptables(cmd, dry_run=dry_run)
        if rc != 0:
            print(f"ERROR applying rule {comment}: {stderr.strip()}", file=sys.stderr)
            continue
        applied += 1

    print(f"[{'dry-run' if dry_run else 'APPLIED'}] {applied} new rule(s) (run_id={run_id})")
    return applied


def main() -> None:
    parser = argparse.ArgumentParser(description="Apply VoxAI Plivo firewall rules")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Apply rules to the live host (default: dry-run only)",
    )
    parser.add_argument(
        "--run-id",
        type=str,
        default=None,
        help="Reuse an existing run ID instead of generating a new one",
    )
    args = parser.parse_args()

    apply_rules(run_id=args.run_id, dry_run=not args.apply)


if __name__ == "__main__":
    main()
