"""Validate Asterisk PJSIP / dialplan configuration files -- local, read-only.

This module does NOT connect to a running Asterisk instance.  It performs
static, file-based sanity checks and documents their limitations.  If an
actual `asterisk` CLI is available it can be invoked for a real
`pjsip show configuration` / `dialplan show` validation, but that is
optional and explicitly opt-in via --asterisk-binary.

Limitations:
  * No Asterisk parser is bundled; this is a best-effort structural check.
  * It does NOT confirm that Asterisk would accept the file at runtime.
  * It does NOT connect to any remote host.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path
from typing import Optional

REQUIRED_PJSIP_SECTIONS = {
    "transport-udp": ["type=transport", "protocol=udp", "bind="],
    "plivo-trunk-endpoint": ["type=endpoint", "context=from-trunk"],
    "plivo-trunk-aor": ["type=aor"],
    "plivo-trunk-identify": ["type=identify", "endpoint=plivo-trunk-endpoint"],
}

REQUIRED_EXTENSIONS_CONTEXTS = {
    "from-trunk": ["Stasis(voxai_voice_app,"],
    "outbound-ari": ["Stasis(voxai_voice_app,outbound,"],
}


def _read_file(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""
    except Exception as exc:  # noqa: BLE001
        print(f"WARNING: could not read {path}: {exc}", file=sys.stderr)
        return ""


def validate_pjsip_conf(path: Path) -> list[str]:
    """Return list of validation error strings (empty = OK)."""
    errors: list[str] = []
    text = _read_file(path)
    if not text:
        errors.append(f"{path}: file not found or empty")
        return errors

    # Check that every required section exists with its key lines
    for section, required_lines in REQUIRED_PJSIP_SECTIONS.items():
        section_pattern = re.compile(rf"^\[{re.escape(section)}\]\n(.*?)(?=\n\[|\Z)", re.DOTALL | re.MULTILINE)
        m = section_pattern.search(text)
        if not m:
            errors.append(f"{path}: missing section [{section}]")
            continue
        section_body = m.group(1)
        for line in required_lines:
            if line not in section_body:
                errors.append(f"{path}: [{section}] missing required line: {line}")

    # Check for duplicate section names
    section_names = re.findall(r"^\[([^\]]+)\]$", text, re.MULTILINE)
    seen: set[str] = set()
    for name in section_names:
        if name in seen:
            errors.append(f"{path}: duplicate section [{name}]")
        seen.add(name)

    # Check that plivo-trunk-endpoint references a defined transport
    ep_match = re.search(
        r"^\[plivo-trunk-endpoint\]\n(.*?)(?=\n\[|\Z)", text, re.DOTALL | re.MULTILINE
    )
    if ep_match:
        ep_body = ep_match.group(1)
        transport_match = re.search(r"^\s*transport\s*=\s*(\S+)", ep_body, re.MULTILINE)
        if transport_match:
            transport_name = transport_match.group(1)
            if f"[{transport_name}]" not in text:
                errors.append(
                    f"{path}: plivo-trunk-endpoint references undefined transport "
                    f"[{transport_name}]"
                )

    return errors


def validate_extensions_conf(path: Path) -> list[str]:
    """Return list of validation error strings (empty = OK)."""
    errors: list[str] = []
    text = _read_file(path)
    if not text:
        errors.append(f"{path}: file not found or empty")
        return errors

    for context, required_patterns in REQUIRED_EXTENSIONS_CONTEXTS.items():
        ctx_pattern = re.compile(
            rf"^\[{re.escape(context)}\]\n(.*?)(?=\n\[|\Z)", re.DOTALL | re.MULTILINE
        )
        m = ctx_pattern.search(text)
        if not m:
            errors.append(f"{path}: missing context [{context}]")
            continue
        ctx_body = m.group(1)
        for pat in required_patterns:
            if pat not in ctx_body:
                errors.append(f"{path}: [{context}] missing pattern: {pat}")

    return errors


def validate_with_asterisk_cli(binary: str, path: Path) -> Optional[str]:
    """Optionally invoke `asterisk -rx` for real config validation.

    Returns an error string or None.  Only called when --asterisk-binary is
    explicitly provided; otherwise this is skipped and a limitation notice
    is printed.
    """
    try:
        result = subprocess.run(
            [binary, "-rx", "pjsip show configuration"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0:
            return f"asterisk CLI returned {result.returncode}: {result.stderr.strip()}"
    except FileNotFoundError:
        return f"asterisk binary not found: {binary}"
    except subprocess.TimeoutExpired:
        return f"asterisk CLI timed out: {binary}"
    except Exception as exc:  # noqa: BLE001
        return f"asterisk CLI error: {exc}"
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate Asterisk config files (local/static)")
    parser.add_argument("--pjsip", type=Path, default="infrastructure/asterisk/pjsip.conf")
    parser.add_argument("--extensions", type=Path, default="infrastructure/asterisk/extensions.conf")
    parser.add_argument(
        "--asterisk-binary",
        type=str,
        default=None,
        help="Optional path to asterisk CLI for live validation (opt-in only)",
    )
    args = parser.parse_args()

    errors: list[str] = []

    errors.extend(validate_pjsip_conf(args.pjsip))
    errors.extend(validate_extensions_conf(args.extensions))

    if args.asterisk_binary:
        cli_err = validate_with_asterisk_cli(args.asterisk_binary, args.pjsip)
        if cli_err:
            errors.append(f"asterisk CLI: {cli_err}")
    else:
        print(
            "NOTE: --asterisk-binary not provided; skipping live Asterisk syntax validation. "
            "This tool performs structural checks only and cannot guarantee Asterisk "
            "will accept the file at runtime."
        )

    if errors:
        print("VALIDATION FAILED:")
        for e in errors:
            print(f"  - {e}")
        return 1

    print("VALIDATION PASSED: pjsip.conf and extensions.conf are structurally sound.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
