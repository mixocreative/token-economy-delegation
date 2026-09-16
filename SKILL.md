---
name: token-economy-delegation
description: Use at the start of any task that would pull a large amount of text through the main context — reading big files, enumerating conditionals or parameters, listing call sites, tabulating or reformatting a long document, summarising logs, bulk searches — and whenever the user says "use ollama", "local model", "save tokens", "use a subagent", or asks whether a cheaper model can do a job. Applies in every Claude Code and Codex session.
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
| 1 | **Local Ollama** (`run.py`) | enumeration, extraction, reformatting, counting, first-pass summary of one input; output is verifiable mechanically | free |
| 2 | **Cheapest Claude subagent** — `cavecrew-investigator` (read-only locate), `cavecrew-builder` (1–2 file edit), `Explore`, or `Agent` with `model: haiku` / `sonnet` | needs to read across many files, follow references, or make a bounded edit; output compressed before it returns | low |
| 3 | **Main model** | judgment, design, security, grading, anything that lands in a plan or register, code that ships | full |

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
