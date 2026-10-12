#!/usr/bin/env python3
r"""Check every `path:line` a delegated reply cites against the files - the check that makes a Codex or subagent
list usable without re-reading it.

    python refs.py REPLY.md [--root DIR] [--tolerance 0]     prints grounded/total and the first failures; exit 0 all ok, 3 otherwise

A reference is grounded when the file exists under ROOT, the line is inside it, and - if the item quotes text
(`code` or "text") or names an identifier - that text is on the cited line (within --tolerance lines).
Why: 2026-10-12, Codex at its cheapest tier listed five subcommands of a 253-line file with the right names
and five wrong line numbers, one past the end of the file. A name check would have passed it.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

REF = re.compile(r"(?P<path>[\w./\\-]+\.[A-Za-z0-9]{1,6}):(?P<line>\d+)(?:-(?P<end>\d+))?")
QUOTE = re.compile(r"`([^`]{3,})`|\"([^\"]{4,})\"")


def norm(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


def check(text: str, root: Path, tolerance: int = 0) -> tuple[int, int, list[str]]:
    total = good = 0
    bad: list[str] = []
    cache: dict[Path, list[str] | None] = {}
    for raw in text.splitlines():
        for m in REF.finditer(raw):
            total += 1
            p = (root / m.group("path")).resolve()
            if p not in cache:
                try:
                    cache[p] = p.read_text(encoding="utf-8", errors="replace").splitlines()
                except OSError:
                    cache[p] = None
            lines = cache[p]
            n = int(m.group("line"))
            if lines is None:
                bad.append(f"{m.group(0)}: no such file")
                continue
            if not 1 <= n <= len(lines):
                bad.append(f"{m.group(0)}: line {n} outside the file ({len(lines)} lines)")
                continue
            rest = raw[:m.start()] + raw[m.end():]
            frags = [norm(a or b) for a, b in QUOTE.findall(rest)]
            if not frags:   # an item like "start - detached.py:216": the leading word must appear on the line
                w = re.match(r"\s*(?:[-*•]\s*|\d+[.)]\s*)?([A-Za-z_][\w.]{2,})", rest)
                frags = [w.group(1)] if w else []
            lo, hi = max(1, n - tolerance), min(len(lines), int(m.group("end") or n) + tolerance)
            window = norm(" ".join(lines[lo - 1:hi]))
            if all(f in window for f in frags):
                good += 1
            else:
                bad.append(f"{m.group(0)}: {frags[0]!r} is not on that line" if frags else f"{m.group(0)}: unverifiable")
    return good, total, bad


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("reply", type=Path)
    ap.add_argument("--root", type=Path, default=Path.cwd())
    ap.add_argument("--tolerance", type=int, default=0)
    a = ap.parse_args()
    good, total, bad = check(a.reply.read_text(encoding="utf-8", errors="replace"), a.root, a.tolerance)
    print(f"refs {good}/{total} grounded" + ("" if total else " (no path:line references to check)"))
    for b in bad[:8]:
        print("  " + b)
    return 0 if total and good == total else 3


if __name__ == "__main__":
    sys.exit(main())
