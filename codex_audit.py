r"""Run one read-only Codex audit and say plainly whether it worked.

    python scripts\codex_audit.py PROMPT.md REPLY.md

Prints the reply's VERDICT line on success. On any failure - Codex missing, out of credit / rate limited, crashed, or a reply without a
VERDICT line - prints `CODEX FAILED: <reason>` with the tail of Codex's log and exits 2, so the caller switches to a fallback reviewer.
"""
from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path.cwd()   # run from the repo to audit
LIMIT_SIGNS = re.compile(r"rate.?limit|quota|usage limit|insufficient|credit|429|too many requests|exceeded", re.I)


def run(prompt: Path, reply: Path, timeout: float = 2400) -> int:
    codex = shutil.which("codex") or shutil.which("codex.cmd")
    if not codex:
        print("CODEX FAILED: codex CLI not found on PATH")
        return 2
    log = reply.with_suffix(".log")
    reply.unlink(missing_ok=True)
    try:
        with open(prompt, "rb") as stdin, open(log, "wb") as out:
            code = subprocess.call([codex, "exec", "-s", "read-only", "-C", str(ROOT), "-o", str(reply), "-"],
                                   stdin=stdin, stdout=out, stderr=subprocess.STDOUT, timeout=timeout)
    except subprocess.TimeoutExpired:
        print(f"CODEX FAILED: no answer within {timeout:.0f} s")
        return 2
    tail = log.read_text(encoding="utf-8", errors="replace")[-1500:] if log.exists() else ""
    text = reply.read_text(encoding="utf-8", errors="replace") if reply.exists() else ""
    verdict = re.search(r"VERDICT:\s*\**\s*(PASS|FIX NEEDED)", text)
    if code != 0 or not verdict:
        reason = "out of credit / rate limited" if LIMIT_SIGNS.search(tail) else (
            f"exit code {code}" if code != 0 else "reply has no VERDICT line")
        print(f"CODEX FAILED: {reason}\n--- codex log tail ---\n{tail.strip()[-800:]}")
        return 2
    print(f"VERDICT: {verdict.group(1)}")
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise SystemExit("usage: codex_audit.py PROMPT.md REPLY.md")
    sys.exit(run(Path(sys.argv[1]), Path(sys.argv[2])))
