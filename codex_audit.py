r"""Kept for old handoffs and prompts: `codex_audit.py PROMPT.md REPLY.md` == `codex_run.py audit PROMPT.md REPLY.md`.

Prints `VERDICT: PASS|FIX NEEDED` or `CODEX FAILED: <reason>` (exit 2, then fall back to a Claude reviewer).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import codex_run  # noqa: E402

if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise SystemExit("usage: codex_audit.py PROMPT.md REPLY.md")
    sys.exit(codex_run.run("audit", Path(sys.argv[1]), Path(sys.argv[2]), "medium", None, None, 2400, "mid"))
