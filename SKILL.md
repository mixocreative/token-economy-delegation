---
name: token-economy-delegation
description: Use at the start of any task that would pull a large amount of text through the main context — reading big files, enumerating conditionals or parameters, listing call sites, tabulating or reformatting a long document, summarising logs, bulk searches — and whenever the user says "use ollama", "local model", "save tokens", "use a subagent", or asks whether a cheaper model can do a job. Also at the start of any long non-AI step (full test suite, build, installer or GPU walk, long download) and when a session may hit a usage/credit limit or be cut off: run it detached, record it in the handoff, and on resume read its result instead of re-running. Applies in every Claude Code and Codex session (one shared copy: S:\ai\skills\token-economy-delegation, linked from both ~/.claude/skills and ~/.codex/skills).
---

# Delegation for token economy

## Overview

The main model's context is the scarce resource. Every bulky, mechanical step goes to the
cheapest worker that can do it **without changing the quality of the result**, and the main
session keeps the judgment: it verifies what came back and decides what it means. The test for
"cheaper is fine" is one question — *would I accept this from a grep?* If a grep could produce
it, a local model may. If it needs the codebase's meaning, the owner's rules, a grade or a
decision, it stays with the main model.

## The ladder — try in this order, stop at the first that fits

| Rung | Worker | Fits when | Cost |
|---|---|---|---|
| 0 | **No model at all: a detached process** (`detached.py`) | the step needs no judgment while it runs - test suite, build, smoke/walk scripts, installer or GPU runs, big downloads | free, and survives the session |
| 1 | **Local Ollama** (`run.py`) | enumeration, extraction, reformatting, counting, first-pass summary of one input; output is verifiable mechanically | free |
| 2 | **Cheapest Claude subagent** — `cavecrew-investigator` (read-only locate), `cavecrew-builder` (1–2 file edit), `Explore`, or `Agent` with `model: haiku` / `sonnet` | needs to read across many files, follow references, or make a bounded edit; output compressed before it returns | low |
| 2b | **Codex CLI as an independent auditor** (`codex exec -s read-only -C <repo> -o reply.md - < prompt.md`) | a second opinion on a finished task: review/audit, plan consolidation, gap-finding; it reads the repo itself, so the main context only receives the verdict | separate quota |
| 3 | **Main model** | judgment, design, security, grading, anything that lands in a plan or register, code that ships | full |

**The test for "delegate at all": big in, small out.** A subagent re-reads its instructions and
the files, so a five-line lookup through one costs more than doing it inline. Delegate when the
input is large and the answer is short (a list, a count, twelve lines out of two thousand); keep
small precise lookups and every judgment in the main session.

Rules that hold on every rung:

- **Probe Ollama once per session**: `python ~/.claude/skills/token-economy-delegation/run.py --check`.
  Empty model list or `/api/generate` 404 = a stale daemon (seen 2026-09-16: tray app 0.21.0,
  CLI 0.34.0, `OLLAMA_MODELS=S:\ai\ollama_models` unseen by the daemon). Tell the owner to quit
  and restart the Ollama tray app; do not kill it. Skip to rung 2.
- **Verify before use.** A count from a local model is checked against `grep -c`; a list is
  spot-read (ten per cent); a summary is checked against the source on the two facts that
  matter. A result that fails the check is discarded, not corrected.
- **One artefact per call.** Enumerate A; enumerate B; diff locally. Never "read these three
  files and tell me what is wrong".
- **Say what was delegated and what was checked** in the reply. Delegation without a stated
  check is a rumour with a model name on it.
- **Never delegate the decision.** Rung 1 and 2 return material; rung 3 decides.

## Detached runs (rung 0) - long work that must survive a limit or a closed session

Background commands of Claude Code / Codex die with the session and are never restored on resume. Anything long that needs no
judgment while it runs is started as its own process instead:

```bash
python ~/.claude/skills/token-economy-delegation/detached.py start suite -- python -m pytest -q   # from the project root
python ~/.claude/skills/token-economy-delegation/detached.py status        # running / done (exit code) / died
python ~/.claude/skills/token-economy-delegation/detached.py tail suite 40
```

Rules: write the run NAME and what to do with its result into the project's handoff file and commit it before waiting; chain the
whole close sequence (suite -> build -> smoke) in one script so it runs unattended; on resume read `status` and the log tail - never
re-run a finished run; arm `detached.py watch` as a background task so you wake only when a run ends, dies or
stalls (alive but its log silent); every run has a hard time limit (`--max-hours`, default 6) that kills its whole process tree, `stop
NAME` ends one at once, the watcher gives up after 2 h, nothing starts with Windows and a reboot ends all; never shut down WSL/Docker/a test VM while a run depends on it; pace work so one heavy agent or run owns the
machine at a time, and poll status rarely (each check costs tokens; waiting does not).

## Runner (rung 1)

```bash
python ~/.claude/skills/token-economy-delegation/run.py --check
cat src/Storefront/OrderPage.php | python ~/.claude/skills/token-economy-delegation/run.py \
  --model qwen2.5-coder:7b --num-ctx 32768 \
  --prompt "List every parameter of render() as 'name = default', then every conditional that adds or removes an HTML section as 'line: condition'. Output only the two lists."
```

Models on this machine: `qwen2.5-coder:7b` for code, `gemma4:e4b` for prose. Temperature 0,
no streaming; stdin lands under `----- INPUT -----` verbatim. `--help` for flags.

## Common mistakes

| Mistake | Fix |
|---|---|
| Reading a 2000-line file in the main context to find twelve lines | rung 1 or 2, return the twelve lines |
| Asking a cheap worker "is this a defect?" | ask for the list; judge it on rung 3 |
| Trusting a count | grep the same thing; compare |
| Assuming the daemon serves the CLI's models | `--check` first, every session |
| Delegating the write-up of a finding | findings, grades and plans are rung 3 |
