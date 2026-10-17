"""Rollback VoxAI Plivo firewall rules by run ID.

Usage:
    python rollback_firewall_rules.py               # dry-run (no changes)
    python rollback_firewall_rules.py --apply       # remove rules from live host
    python rollback_firewall_rules.py --apply --run-id <id>

Safety properties:
  * Dry-run by default -- no iptables invocation unless --apply is passed.
  * Only removes rules whose comment matches the given run ID.
  * Never flushes chains or removes unrelated rules.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from typing import Optional

from firewall_common import RULE_COMMENT_PREFIX, is_voxai_rule  # noqa: F401  (re-exported for tests)


def _run_iptables(cmd: list[str], dry_run: bool = True) -> tuple[int, str, str]:
    display = " ".join(cmd)
    if dry_run:
        print(f"[dry-run] iptables {display}")
        return 0, "", ""
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=15)
        return result.returncode, result.stdout, result.stderr
    except FileNotFoundError:
        print(f"ERROR: iptables binary not found. Cannot run {display}", file=sys.stderr)
        return 127, "", "iptables not found"
    except subprocess.TimeoutExpired:
        print(f"ERROR: iptables timed out: {display}", file=sys.stderr)
        return 1, "", "timeout"


def list_voxai_rules(run_id: str, dry_run: bool = True) -> list[tuple[str, str, str]]:
    """Return list of (chain, rule_spec, comment) for rules matching run_id."""
    rc, stdout, _ = _run_iptables(["iptables", "-S"], dry_run=dry_run)
    if rc != 0:
        return []
    found: list[tuple[str, str, str]] = []
    for line in stdout.splitlines():
        # iptables -S output format: -A chain spec ...
        parts = line.split(None, 2)
        if len(parts) < 3:
            continue
        chain = parts[1]
        rule_spec = parts[2]
        # Extract comment
        import re
        m = re.search(r'--comment "([^"]+)"', rule_spec)
        if not m:
            continue
        comment = m.group(1)
        if is_voip_rule(comment, run_id):
            found.append((chain, rule_spec, comment))
    return found


def rollback_rules(run_id: str, dry_run: bool = True) -> int:
    """Remove all firewall rules created by the given run ID.

    Returns the number of rules removed (or that would be removed).
    """
    rules = list_voxai_rules(run_id, dry_run=dry_run)
    removed = 0

    for chain, rule_spec, comment in rules:
        # Build delete command: iptables -D chain <rule_spec-without--comment>
        # We need to remove the -m comment --comment "..." part
        import re
        clean_spec = re.sub(r'\s*-m\s+comment\s+--comment\s+"[^"]*"\s*', " ", rule_spec).strip()
        cmd = ["iptables", "-D", chain] + clean_spec.split()
        rc, _, stderr = _run_iptables(cmd, dry_run=dry_run)
        if rc != 0:
            print(f"ERROR removing rule {comment}: {stderr.strip()}", file=sys.stderr)
            continue
        removed += 1

    print(f"[{'dry-run' if dry_run else 'ROLLBACK'}] {removed} rule(s) removed (run_id={run_id})")
    return removed


def main() -> None:
    parser = argparse.ArgumentParser(description="Rollback VoxAI Plivo firewall rules by run ID")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Remove rules from the live host (default: dry-run only)",
    )
    parser.add_argument(
        "--run-id",
        type=str,
        required=True,
        help="Run ID whose rules should be removed",
    )
    args = parser.parse_args()

    rollback_rules(run_id=args.run_id, dry_run=not args.apply)


if __name__ == "__main__":
    main()
