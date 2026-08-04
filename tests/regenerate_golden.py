#!/usr/bin/env python3
"""Regenerate the .out.txt golden files from the CURRENT rule pipeline.

    uv run python tests/regenerate_golden.py

This exists to *record* today's behaviour so a refactor can prove it changed
nothing. Its output is only trustworthy when the pipeline is already correct.

Run it when:
  - adding a new .in.txt fixture, or
  - making a rule change you have deliberately decided is an improvement.

Never run it to make a failing test pass. A golden-file test that is
regenerated on failure asserts nothing. If a test fails unexpectedly, the
diff is the finding — read it before touching anything here.
"""
from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

# html_to_video parses sys.argv at import time in some paths; neutralise it.
sys.argv = ["regenerate_golden"]

import html_to_video as h  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures"


def main() -> int:
    inputs = sorted(FIXTURES.glob("*.in.txt"))
    if not inputs:
        print(f"No fixtures found in {FIXTURES}", file=sys.stderr)
        return 1

    for in_path in inputs:
        out_path = in_path.with_name(in_path.name.replace(".in.txt", ".out.txt"))
        source = in_path.read_text(encoding="utf-8")
        # Rewrite line by line: phrases reach rewrite_for_tts individually, so
        # matching that granularity keeps the fixtures faithful to real use.
        rewritten = "".join(
            h.rewrite_for_tts(line) if line.strip() else line
            for line in source.splitlines(keepends=True)
        )
        status = "unchanged"
        if not out_path.exists():
            status = "CREATED"
        elif out_path.read_text(encoding="utf-8") != rewritten:
            status = "UPDATED"
        out_path.write_text(rewritten, encoding="utf-8")
        print(f"  {status:9} {out_path.name}")

    print(f"\n{len(inputs)} golden file(s) written.")
    print("Review the diff before committing — an unexpected change here is a "
          "real behaviour change, not a formality.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
