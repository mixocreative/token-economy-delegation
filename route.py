#!/usr/bin/env python3
r"""Decide who does a job: no model, local Ollama, Codex, a cheap Claude subagent, or the main model.

    python route.py --job list   --files src/OrderPage.php          one file, mechanical, verifiable
    python route.py --job repo-read --tokens 400000                 "find every caller of X across the repo"
    python route.py --job second-opinion                            independent review of finished work
    python route.py --status                                        what is usable right now, and why not

Prints one decision line (USE ...), the exact command or Agent call, and the fallback chain, then exits 0.
It never runs the job: it costs no model tokens, so ask it whenever the choice is not obvious.

The principle: the cheapest worker whose answer can be CHECKED at the quality the job needs. Cheap and
unverifiable is not cheap - it is a defect the main model pays for later. So:

  job shape                         first choice                 fallback chain (on unavailable or FAIL)
  long non-AI step                  detached process (no model)  -
  mechanical, one input, checkable  Ollama via run.py            Codex task (low)  -> Claude haiku -> main
  repo-wide read, short answer      Codex task (low)             Claude Explore/haiku -> main
  bounded 1-2 file edit             Claude sonnet subagent       main
  draft / first-pass review         Ollama (review) + Codex      Claude sonnet -> main
  second opinion on finished work   Codex audit (medium)         Claude sonnet reviewer (fresh context)
  judgment, design, security, grading, anything that ships       main model - never delegated

Availability is live: Ollama models that fit the input and the free VRAM (run.py), Codex unless it is cooling
down after a usage limit (codex_run.py), and the measured pass rate of each worker on this job type (ledger).
A worker that keeps failing verification on a job type here is skipped for it automatically.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import codex_run  # noqa: E402
import run  # noqa: E402

SKILL = Path(__file__).resolve().parent
PY = "python " + str(SKILL).replace("\\", "/")

JOBS = {
    # job: (ollama task or None, codex effort or None, claude tier, needs judgment)
    "long-run":       (None,        None,     None,     False),
    "list":           ("list",      "low",    "haiku",  False),
    "extract":        ("extract",   "low",    "haiku",  False),
    "count":          ("count",     "low",    "haiku",  False),
    "format":         ("format",    "low",    "haiku",  False),
    "classify":       ("classify",  "low",    "haiku",  False),
    "summarize":      ("summarize", "low",    "haiku",  False),
    "translate":      ("translate", "medium", "sonnet", False),
    "repo-read":      (None,        "low",    "haiku",  False),
    "edit":           (None,        None,     "sonnet", False),
    "review":         ("review",    "medium", "sonnet", False),
    "second-opinion": (None,        "medium", "sonnet", False),
    "judgment":       (None,        None,     None,     True),
}
MIN_PASS = 0.35   # a worker whose smoothed pass rate on this job is below this is skipped for it
CODEX_MIN_INPUT = 20000   # every Codex call carries ~40-125k tokens of its own overhead (measured 2026-10-12):
#                           worth it for a repo-wide read, wasteful for one small file a local model or haiku can do


def tokens_of(files: list[str]) -> int:
    n = 0
    for f in files:
        try:
            n += run.est_tokens(Path(f).read_text(encoding="utf-8", errors="replace"))
        except OSError:
            pass
    return n


def ollama_choice(task: str, need: int) -> tuple[str | None, str]:
    try:
        st = run.discover()
    except Exception as e:  # noqa: BLE001 - any failure means "not usable now"
        return None, f"Ollama unreachable ({e})"
    rows = run.ledger_rows()
    ranked = [c for c in run.rank(st, task, need, {"completion"}) if run.pass_rate(rows, c[1], task) >= MIN_PASS]
    if not ranked:
        return None, f"no healthy local model fits ~{need:,} tokens with an acceptable pass rate on '{task}'"
    g = st["gpu"]
    if g and g["used_mib"] - sum(st["loaded"].values()) > 6000:
        return None, f"GPU busy with another job ({g['used_mib']} MiB used) - local models would evict or crawl"
    return ranked[0][1], f"score {ranked[0][0]} ({ranked[0][2]})"


def codex_choice(job: str) -> tuple[bool, str]:
    ok, why = codex_run.status()
    if not ok:
        return False, why
    rows = run.ledger_rows()
    mode = "audit" if job == "second-opinion" else "task"
    if run.pass_rate(rows, "codex", f"codex-{mode}") < MIN_PASS:
        return False, f"Codex keeps failing '{mode}' here (ledger)"
    return True, why


def decide(job: str, need: int, verifiable: bool) -> list[str]:
    task, effort, tier, judgment = JOBS[job]
    plan: list[str] = []
    if judgment:
        return ["USE main model - judgment, design, security and grading are never delegated"]
    if job == "long-run":
        return [f"USE detached process (no model): {PY}/detached.py start NAME -- <command>",
                "      record NAME in the handoff; on resume read `detached.py status`, never re-run"]
    chain: list[str] = []
    if task:
        model, why = ollama_choice(task, need + 4096)
        if model and (verifiable or job in ("review", "summarize", "translate", "format")):
            v = {"list": "quotes", "extract": "quotes", "count": "count", "classify": "json"}.get(task, "")
            chain.append(f"Ollama {model} [{why}]: {PY}/run.py --task {task}"
                         + (" --number-lines" if v == "quotes" else "") + (" --count-regex '<regex>'" if v == "count" else "")
                         + " --prompt \"...\" < input   (exit 4 = ESCALATE to the next rung)")
        else:
            plan.append(f"  skip Ollama: {why if not model else 'output not mechanically checkable for this job'}")
    if effort and job not in ("repo-read", "second-opinion", "review") and need and need < CODEX_MIN_INPUT:
        plan.append(f"  skip Codex: ~{need:,} tokens of input is below its per-call overhead - a subagent is cheaper overall")
        effort = None
    if effort:
        ok, why = codex_choice(job)
        mode = "audit" if job == "second-opinion" else "task"
        if ok:
            chain.append(f"Codex {mode} --effort {effort} [{why}]: write the prompt to a file, then "
                         f"{PY}/codex_run.py {mode} prompt.md reply.md --effort {effort}   (from the repo root; "
                         "ask for path:line + a quoted line per item; codex_run checks every reference itself)")
        else:
            plan.append(f"  skip Codex: {why}")
    if tier:
        agent = {"haiku": "Agent(subagent_type: Explore, model: haiku)" if job == "repo-read" else "Agent(model: haiku)",
                 "sonnet": "Agent(model: sonnet)"}[tier]
        chain.append(f"Claude {tier} subagent: {agent} - tell it to return only the short answer, compressed, "
                     f"items as path:line + quoted line; check with {PY}/refs.py reply.md")
    chain.append("main model, inline")
    head, *rest = chain
    plan.insert(0, "USE " + head)
    if rest:
        plan.append("  then: " + "  ->  ".join(r.split(":")[0] if r != "main model, inline" else r for r in rest))
    plan.append("  always: verify what comes back (count vs grep, 10% spot-read, quotes vs source) before it is used; "
                "say in the reply what was delegated and how it was checked")
    if need and need < 1500 and job not in ("second-opinion", "long-run"):
        plan.insert(0, f"NOTE: ~{need} tokens of input - small in, small out; doing it inline is cheaper than any handoff")
    return plan


def show_status() -> int:
    try:
        st = run.discover()
        ok = [n for n, m in st["models"].items() if m.get("healthy")]
        g = st["gpu"]
        print(f"Ollama : {len(ok)} usable ({', '.join(sorted(ok))}); {run.server_info()}"
              + (f"; GPU {g['used_mib']}/{g['total_mib']} MiB" if g else ""))
        if st.get("warn"):
            print("         WARNING: " + st["warn"])
    except Exception as e:  # noqa: BLE001
        print(f"Ollama : unavailable ({e})")
    ok, why = codex_run.status()
    print(f"Codex  : {'usable' if ok else 'unavailable'} - {why}")
    print("Claude : subagents always available (haiku: read+compress, sonnet: bounded edit/review); main keeps judgment")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--job", choices=sorted(JOBS))
    ap.add_argument("--files", nargs="*", default=[], help="the input files (sizes them)")
    ap.add_argument("--tokens", type=int, default=0, help="or: estimated input tokens")
    ap.add_argument("--unverifiable", action="store_true", help="the output cannot be checked mechanically")
    a = ap.parse_args()
    if a.status:
        return show_status()
    if not a.job:
        ap.error("--job is required (or --status)")
    need = a.tokens or tokens_of(a.files)
    for line in decide(a.job, need, not a.unverifiable):
        sys.stdout.buffer.write((line + "\n").encode("utf-8", "replace"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
