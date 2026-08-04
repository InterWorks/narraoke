#!/usr/bin/env python3
"""Raw-bytes denylist scan for company-confidential material.

Run over every tracked file before a commit reaches a public repo.

Why raw bytes and not parsed fields
-----------------------------------
Rule files carry a ``why`` rationale alongside ``from``/``to``, and the
rationale leaks independently of the rule. Two real examples from the
onboarding override file: one rule's ``why`` explained itself by referring to
an internal Slack channel that appears nowhere in its own ``from``/``to``, and
another named an internal product in prose. A scanner that parsed JSON and
checked only ``from``/``to`` would pass both.

Scanning raw bytes also catches JSONC comments (``// -- Slack channels ...``),
which are not part of the parsed document at all, and hardcoded strings in
Python and HTML templates.

This script is deliberately standalone and dependency-free so the project
repos (tier 1) and the company-rules repo (tier 3) can adopt the same hook.

Exit codes: 0 clean, 1 findings, 2 usage error.
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

# Patterns that warrant a look before content reaches a public repository.
#
# Keep this list additive and specific. A pattern that fires on ordinary
# technical prose trains people to ignore the output, which defeats the check.
#
# Two kinds of pattern live here, and the distinction matters when triaging a
# hit:
#
#   * Genuinely internal — an internal channel, contact, product, or
#     credential identifier. A hit is a leak. Move it to the private
#     company-rules repo (tier 3a).
#   * Tripwire — the company name is PUBLIC and is not itself a secret. It is
#     matched because company-specific content tends to cluster around it, so
#     a hit is a prompt to check what else is nearby, not a finding on its own.
#     Expect legitimate hits (this org's own repo URLs and preset references).
DENYLIST: list[tuple[str, str]] = [
    (r"(?i)\binterworks\b", "company name (tripwire — public, check context)"),
    (r"#iw-[a-z0-9-]+", "internal Slack channel"),
    (r"\bIW [A-Z][a-z]+", "internal product name"),
    (r"secops@", "internal contact"),
    (r"@interworks\.com", "company email domain"),
    (r"GITHUB_TOKEN_[A-Z_]+", "named production credential"),
]

# Binary and vendored paths are skipped wholesale.
SKIP_SUFFIXES = {
    ".png", ".jpg", ".jpeg", ".gif", ".ico", ".pdf", ".mp3", ".mp4", ".wav",
    ".woff", ".woff2", ".ttf", ".zip", ".gz", ".lock",
}
SKIP_DIRS = {".git", ".venv", ".venv-win", "output", "__pycache__", "node_modules"}


def tracked_files(root: Path) -> list[Path]:
    """Every git-tracked file, plus staged additions.

    Falls back to a filesystem walk when the directory is not a git repo yet —
    which is exactly the state this repo is in before its first commit.
    """
    try:
        out = subprocess.run(
            ["git", "-C", str(root), "ls-files", "--cached", "--others",
             "--exclude-standard"],
            capture_output=True, text=True, check=True,
        ).stdout
        return [root / line for line in out.splitlines() if line]
    except (subprocess.CalledProcessError, FileNotFoundError):
        return [
            p for p in root.rglob("*")
            if p.is_file() and not any(part in SKIP_DIRS for part in p.parts)
        ]


def scan_file(path: Path) -> list[tuple[int, str, str]]:
    """Return (line_number, label, matched_text) for each hit in *path*."""
    if path.suffix.lower() in SKIP_SUFFIXES or not path.is_file():
        return []
    # This file necessarily contains every pattern it hunts for. Exempting it
    # by identity is safer than weakening the patterns to avoid self-matching.
    if path.resolve() == Path(__file__).resolve():
        return []
    try:
        raw = path.read_bytes()
    except OSError:
        return []
    # Decode permissively: a leak in a file with one bad byte still counts.
    text = raw.decode("utf-8", errors="replace")

    findings: list[tuple[int, str, str]] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        for pattern, label in DENYLIST:
            for match in re.finditer(pattern, line):
                findings.append((lineno, label, match.group(0)))
    return findings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("paths", nargs="*", type=Path,
                        help="Files to scan. Default: all tracked files.")
    parser.add_argument("--root", type=Path, default=Path(__file__).parent.parent,
                        help="Repository root (default: parent of scripts/).")
    args = parser.parse_args(argv)

    root = args.root.resolve()
    targets = args.paths or tracked_files(root)

    total = 0
    for path in sorted(set(targets)):
        for lineno, label, matched in scan_file(path):
            total += 1
            try:
                shown = path.relative_to(root)
            except ValueError:
                shown = path
            print(f"{shown}:{lineno}: {label}: {matched!r}")

    if total:
        print(
            f"\n{total} finding(s) to triage.\n"
            f"Anything genuinely internal — a channel, contact, product, or "
            f"credential identifier — belongs in the private company-rules "
            f"repo (tier 3a), never here. Remember the `why` field is part of "
            f"the rule.\n"
            f"Hits labelled 'tripwire' are public material; check what is "
            f"around them rather than the match itself.",
            file=sys.stderr,
        )
        return 1

    print(f"leak scan clean ({len(targets)} files)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
