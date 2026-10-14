---
name: token-economy-delegation
description: Use at the start of any task that would pull a large amount of text through the main context — reading big files, enumerating conditionals or parameters, listing call sites, repo-wide searches, tabulating or reformatting a long document, summarising logs, first-pass reviews, second opinions — and whenever the user says "use ollama", "local model", "use codex", "save tokens", "use a subagent", "cheaper model", or asks which model should do a job. Routes each job to the cheapest worker whose answer can be checked - no model, local Ollama (auto-discovered, verified, escalates on failure), Codex on its own quota, a Claude haiku/sonnet subagent, or the main model - and learns per-machine pass rates. Also for long non-AI steps (test suites, builds, GPU walks) that must survive a usage limit: run detached, read the result on resume. Applies in every Claude Code and Codex session (keep one copy and link it into both ~/.claude/skills and ~/.codex/skills).
---

# Delegation for token economy

## Overview

The main model's context is the scarce resource. Every bulky, mechanical step goes to the
cheapest worker that can do it **without changing the quality of the result**, and the main
session keeps the judgment: it verifies what came back and decides what it means.

Two tests decide everything:

1. **Big in, small out?** A handoff costs a prompt, a reply and a check. A five-line lookup is
   cheaper inline. Delegate when the input is large and the answer is short.
2. **Can the answer be checked?** Cheap and unverifiable is not cheap: it is a defect the main
   model pays for later. *Would I accept this from a grep?* If a grep could produce it, a local
   model may. If it needs the codebase's meaning, the owner's rules, a grade or a decision, it
   stays with the main model.

## Start here: ask the router (costs no model tokens)

```bash
python <skill>/route.py --status                               # what is usable now: Ollama models, Codex, Claude
python <skill>/route.py --job list --files src/OrderPage.php   # who should do this job, the exact command, the fallback chain
```

Jobs: `long-run` `list` `extract` `count` `format` `classify` `summarize` `translate` `repo-read` `edit`
`review` `second-opinion` `judgment`. The router reads live state - which Ollama models are installed and
healthy, whether the input fits their context and the free VRAM, whether Codex is cooling down after a usage
limit, and the **measured pass rate of each worker on this job type on this machine** (`~/.token-economy/ledger.jsonl`).
A worker that keeps failing verification for a job type is skipped for it automatically.

## The ladder

| Rung | Worker | Fits when | Main-context cost |
|---|---|---|---|
| 0 | **No model** — `detached.py` | test suite, build, smoke/walk scripts, installer or GPU runs, big downloads | ~0, survives the session |
| 1 | **Local Ollama** — `run.py` (auto-routed) | one input, mechanical, **output checkable against the input**: enumerate, extract, count, reformat, classify, first-pass summary | prompt + verified answer |
| 2 | **Codex** — `codex_run.py task` (effort low) | repo-wide reading with a short answer: callers, usages, "where is X handled"; Codex reads the files in its own context on its own quota | prompt + reply |
| 2 | **Claude haiku / Explore subagent** | same as above when Codex is unavailable, or when Claude Code tools are needed | prompt + compressed reply |
| 2 | **Claude sonnet subagent** | a bounded 1–2 file edit; a first-pass review | prompt + diff/reply |
| 2b | **Codex audit** — `codex_run.py audit` (effort medium) | a second opinion on finished work: review, plan consolidation, gap-finding; returns a VERDICT | prompt + verdict |
| 3 | **Main model** | judgment, design, security, grading, anything that lands in a plan or register, code that ships | full |

**Escalation is the quality guarantee.** Each rung's answer is checked; a failed check moves the job up,
and **the failed answer is discarded, never patched**: Ollama → Codex → Claude subagent → main. `run.py`
exits 4 with `ESCALATE: <reason>`; `codex_run.py` exits 2 with `CODEX FAILED: <reason>`.

## Rung 1 — local Ollama, routed and verified (`run.py`)

```bash
python <skill>/run.py --discover                 # server, models, health, context, caps, VRAM fit, speed, routing preview
python <skill>/run.py --bench all                # measure tok/s on this machine once (GPU idle), improves routing
cat file.php | python <skill>/run.py --task list --number-lines \
  --prompt "List every conditional that adds or removes an HTML section."
cat app.log  | python <skill>/run.py --task count --count-regex "ERROR" --prompt "How many ERROR lines?"
python <skill>/run.py --stats                    # pass rate and tokens offloaded per model x task
```

What `run.py` does so delegation does not cost quality:

1. **Uses the native Ollama, and starts it if needed.** On Windows that is the desktop app's server, reached on
   `[::1]:11434` - never `127.0.0.1`, which another listener (a WSL relay, a container, a second install) can
   own with a different model set, as happened 2026-10-12. If nothing answers, `run.py` starts the desktop app
   (stabler: it supervises and updates the server), else `ollama serve` from the Windows CLI; it never stops
   or restarts a running server. `OLLAMA_HOSTS` overrides this on purpose.
2. **Discovers, never assumes.** Model names are read live. Broken models (`/api/show` 404), embedding models
   and models whose context the input would overflow are excluded. Never hard-code a model name in a prompt
   or handoff: the set changes.
3. **Routes** by task fit (code → devstral / coder / qwen; prose → gemma / qwen; zh → qwen), measured pass
   rate here, already-loaded (a cold 15–18 GB load costs 15–60 s), VRAM fit (never evict a GPU job), speed.
   Override the family table in `routing.json`; pin with `--model NAME`.
4. **Runs deterministically**: temperature 0, thinking off for mechanical tasks, think-blocks stripped,
   context sized from the input, JSON schema enforced when `--schema` is given. An answer cut at the token
   limit is `TRUNCATED` and rejected.
5. **Verifies against the input**: `quotes` - every cited line must exist verbatim (with `--number-lines`,
   at that line number); `count` - must equal the regex count; `json` - must parse and carry the schema's
   required keys. `--agree` additionally requires two different models to agree (output = intersection).
6. **Escalates**: tries the next candidate, then exits 4. Every attempt goes to the ledger, so the router
   learns. Infrastructure errors (daemon closed the connection) are logged but do not count against a model.

Choosing `--task`: `list` / `extract` (quotes), `count` (count), `classify` (json), `format`, `summarize`,
`translate`, `review` (a first-pass defect list - every cited line must exist; still never a verdict).

## Rung 2 — Codex on its own quota (`codex_run.py`)

```bash
python <skill>/codex_run.py --status                                   # usable / cooling down until HH:MM / not installed
python <skill>/codex_run.py task  prompt.md reply.md --effort low      # repo-read: from the repo root
python <skill>/codex_run.py audit prompt.md reply.md --effort medium   # second opinion: reply must carry VERDICT
```

- **Never hard-code a Codex model name** (`-m gpt-5.5` broke on 2026-10-14 when the account dropped it). Always go
  through `codex_run.py`: it reads Codex's own live model list (`~/.codex/models_cache.json`, refreshed by the CLI),
  tries the best model of the wanted tier first, marks a model the account rejects as bad and moves to the next. As
  a last resort it uses the model the Codex app itself used in its newest session. `--status` prints the list it
  would use. If `config.toml` points Codex at a local server (Ollama) while the login is ChatGPT, it adds the
  ChatGPT backend override per call and never edits the config.
- Read-only sandbox, always. Run from the repository Codex should read. Launch it through `detached.py` when the
  calling shell may time out - a killed parent kills the Codex child mid-answer.
- **Per-call overhead is large**: 40–125k Codex tokens per call whatever the question (measured 2026-10-12, even
  with the preamble that stops it loading skills). Codex is for big reads with short answers; the router skips it
  for inputs under ~20k tokens.
- **Model by tier, from Codex's own list** (`~/.codex/models_cache.json`, tiered by the vendor's description):
  `task` → cheap (e.g. gpt-6-luna), `audit` → mid (sol), `--tier top` (astra) only for a hard review. A model
  the account rejects ("not supported when using Codex with a ChatGPT account" - 2026-10-12 that was the
  config.toml default, gpt-6.1-sol, so every audit had been failing) is remembered and skipped.
- **Every `path:line` in the reply is checked against the files** (`refs.py`). 2026-10-12, the cheap tier got
  five subcommand names right and all five line numbers wrong, one past the end of the file. An ungrounded reply
  prints `CODEX UNGROUNDED`, exits 3, and at the cheap tier is retried once on the mid tier; the cheap answer is
  discarded. A reply with no references to check says so - spot-read it. Measured the same day: luna 0/6
  three times running, then sol at effort low 6/6 - the cascade turned a wrong cheap answer into a right one
  without the main model reading the file. A Codex model that fails a job type 3+ times here is skipped for it.
- **Effort by job**: `low` for lookups and enumeration, `medium` for audits, `high` only for a hard review the
  main model would otherwise spend a lot on. Higher effort burns Codex quota, not quality you can check.
- **Ask for checkable output**: every item as `path:line - quoted line`, so the main model spot-checks 10% with
  one read each instead of trusting the list.
- A usage-limit failure starts a **cool-down** (parsed from Codex's own "try again in …", else 1 h) recorded in
  `~/.token-economy/codex.json`; the router stops proposing Codex until it passes.
- `codex_audit.py PROMPT REPLY` still works (= `codex_run.py audit … --effort medium`).

## Rung 2 — Claude subagents

`Agent(subagent_type: Explore, model: haiku)` for read-only locate across many files; `Agent(model: haiku)` for
read-and-compress; `Agent(model: sonnet)` for a bounded edit or a first-pass review. Tell it the exact shape of
the answer and to return only that, compressed.

## Rules that hold on every rung

- **Run `route.py --status` once per session** (it includes the Ollama probe, and starts the desktop app if it
  is not running). A server that lists no models or answers `/api/show` with 404 for everything is stale: tell
  the owner to quit and restart the Ollama desktop app; do not kill it. Use the `ollama` command line only for
  pulling and removing models, and check `run.py --discover` afterwards - on a machine with a second listener on
  `127.0.0.1:11434` the CLI can talk to the wrong server (2026-10-12: `ollama rm` answered "not found" for a
  model the desktop app held).
- **Verify before use.** `run.py` and `codex_run.py` check mechanically; a subagent's list of references goes
  through `python <skill>/refs.py reply.md` (exit 3 = a cited line does not hold what it claims). Otherwise the
  main model checks: a count against `grep -c`, a list spot-read (ten per cent), a summary against the source on the two
  facts that matter. A result that fails the check is discarded, not corrected.
- **One artefact per call.** Enumerate A; enumerate B; diff locally. Never "read these three files and tell me
  what is wrong".
- **Say what was delegated and how it was checked** in the reply - which worker, which model, which check.
  Delegation without a stated check is a rumour with a model name on it.
- **Never delegate the decision.** Rungs 0–2 return material; rung 3 decides.
- **Don't fight the GPU.** If another GPU job holds the card, the router skips Ollama; never unload or kill
  someone else's job to make room. `--keep-alive 0` unloads a model after a one-off call.

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

## Emergency: local first-pass review (`ollama_review.py`)

`ollama_review.py RANGE out.md` - per changed file, only when the GPU and suites are idle. Use only when Codex is
cooling down: noisy and slow (2026-10-08: 1 real of 8), never a verdict. Its model is routed like `run.py`'s.

## Common mistakes

| Mistake | Fix |
|---|---|
| Reading a 2000-line file in the main context to find twelve lines | `route.py --job list --files …`, then the command it prints |
| Hard-coding a model name from memory | `run.py --discover`; let `--model auto` route |
| Asking a cheap worker "is this a defect?" | ask for the list; judge it on rung 3 |
| Trusting a count | `--task count --count-regex` checks it; else grep the same thing |
| Trusting a delegated `path:line` | `refs.py` - right names with wrong lines is the cheap tier's typical miss |
| Patching a local answer that failed verification | discard it; take the next rung |
| Sending Codex work while it is out of credit | `codex_run.py --status`; the router already skips it |
| Delegating the write-up of a finding | findings, grades and plans are rung 3 |
| Delegating a 5-line lookup | inline - a handoff costs more than the read |
