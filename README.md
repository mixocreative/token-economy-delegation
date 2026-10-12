<div align="center">

# token-economy-delegation

### 省 Token 委派 · a skill for Claude Code and OpenAI Codex

**Stop paying your best model to read 2,000 lines and return twelve.**<br/>
**One router across local Ollama, Codex and Claude: the cheapest worker whose answer can be *checked* gets the job — and a failed check escalates, never ships.**

**別再讓最貴的模型讀兩千行只為了找十二行。大量輸入、少量輸出的工作，交給最便宜的工人做，主模型只負責驗證與判斷。**

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](#license) [![Claude Code skill](https://img.shields.io/badge/Claude_Code-skill-D97757)](#install) [![Codex skill](https://img.shields.io/badge/OpenAI_Codex-skill-000000)](#install) [![Ollama](https://img.shields.io/badge/Ollama-local_models-black)](#the-ladder) [![Python](https://img.shields.io/badge/Python-stdlib_only-3776AB)](#install)

[Quick start](#quick-start) · [The ladder](#the-ladder) · [Why it doesn't cost quality](#quality) · [What it caught](#what-it-caught) · [Detached runs](#detached-runs) · [FAQ](#faq) · [Install](#install)

</div>

---

Your agent's context window is the scarce resource. Most of what fills it is mechanical: reading a huge file to list its parameters, counting call sites, summarising a log, waiting on a seven-hour test suite. This skill makes the agent ask one question before each of those steps — *would I accept this from a grep?* — and if yes, hand it to the cheapest worker that gives the **same quality of result**: no model at all, a local Ollama model, a small subagent, or a second agent as an independent reviewer. The main model keeps every judgment, grade and decision, and checks what came back.

> Agent 的 context 是最稀缺的資源，而塞滿它的多半是機械性工作：讀大檔案列參數、數呼叫點、摘要 log、等七小時的測試。這個 Skill 讓 Agent 在每一步之前先問：「如果這是 grep 給我的結果，我能接受嗎？」能接受，就交給品質不變的最便宜工人 —— 不用模型、本機 Ollama、小型 subagent，或是另一個 Agent 當獨立審查。所有判斷、評級與決策都留在主模型，並驗證回傳的結果。

<a id="quick-start"></a>

## Quick start｜快速開始

Ask the router who should do a job. It costs no model tokens, it never runs the job, and it reads live state: which Ollama models are installed and healthy, whether the input fits them and the free VRAM, whether Codex is cooling down after a usage limit, and how often each worker has actually passed verification on this job type **on your machine**.

```bash
python route.py --status
#  Ollama : 5 usable (devstral-small-2:24b, gemma4:26b, qwen3.6:27b, ...); GPU 2158/24576 MiB
#  Codex  : usable - ready
#  Claude : subagents always available (haiku: read+compress, sonnet: bounded edit/review)

python route.py --job list --files src/OrderPage.php
#  USE Ollama qwen3.6:27b [score 2.4 (fit2 q0.50 loaded)]: python run.py --task list --number-lines --prompt "..." < input
#    then: Codex task --effort low  ->  Claude haiku subagent  ->  main model, inline
```

Or just tell your agent:

```text
Save tokens: list every conditional in OrderPage.php with a local model and verify it.
Use Codex to find every caller of reserve_stock() across the repo; only bring me path:line.
Get a second opinion on this branch from Codex before I merge.
Run the full suite detached so it survives if I hit the usage limit.
幫我省 token：用本機模型把這份 log 的錯誤全部列出來，驗證過再給我。
```

<a id="the-ladder"></a>

## The ladder｜委派階梯

| Job shape | First choice | Fallback chain (on unavailable or failed check) |
|---|---|---|
| long non-AI step (suite, build, GPU walk) | **no model** — `detached.py` | — |
| mechanical, one input, checkable (list, extract, count, classify, reformat) | **local Ollama** — `run.py`, auto-routed | Codex (cheap tier) → Claude haiku → main |
| repo-wide read, short answer ("every caller of X") | **Codex** — `codex_run.py task`, its own quota | Claude Explore/haiku → main |
| bounded 1–2 file edit | **Claude sonnet** subagent | main |
| second opinion on finished work | **Codex** — `codex_run.py audit` (mid tier) | Claude sonnet reviewer → main |
| judgment, design, security, grading, anything that ships | **main model** — never delegated | — |

<a id="quality"></a>

## Why it doesn't cost quality｜為什麼不犧牲品質

Cheap-and-unverifiable is not cheap — it's a defect the main model pays for later. So every rung below the main model is **checked mechanically against the source**, and a failed check moves the job up the ladder. The failed answer is discarded, never patched.

- **Live discovery, never hard-coded models.** `run.py` reads every Ollama daemon (Windows *and* WSL), excludes broken and embedding models and any model the input would overflow, and routes by task fit, measured pass rate, already-loaded (a cold 17 GB load is 15–60 s) and VRAM fit (it never evicts your GPU job). Codex models come from Codex's own model list, tiered by the vendor's description: cheap for lookups, mid for audits, top only on request.
- **Deterministic runs.** Temperature 0, thinking off for mechanical work, think-blocks stripped, context sized from the input, JSON schema enforced when given. An answer cut at the token limit is `TRUNCATED` and rejected.
- **Verification against the input.** Cited lines must exist verbatim at their line numbers; counts must equal a regex count; JSON must parse with its required keys; every `path:line` in a Codex or subagent reply must hold what it claims (`refs.py`). Optional `--agree`: two different local models must agree.
- **A ledger that learns.** Every attempt is logged (`~/.token-economy/ledger.jsonl`); a worker that keeps failing a job type on your machine stops being chosen for it. `run.py --stats` shows pass rates and tokens offloaded.

> 便宜但無法驗證，其實不便宜。主模型以下的每一階都會拿原始資料做機械式驗證，驗證失敗就往上一階交辦，失敗的答案直接丟棄、不修補。

<a id="what-it-caught"></a>

## What it caught on the first day｜第一天就抓到的問題

Built and tested on a real workstation (RTX 3090, Windows + WSL, Codex CLI). Four things the old, hard-coded version never noticed:

| Found | What was true |
|---|---|
| **Every default local call failed** | the skill named `qwen2.5-coder:7b`; the Windows daemon had devstral, gemma4 and qwen3.6 instead |
| **Two daemons on one port** | a WSL relay and the Windows app both answered `11434` with different models, so requests landed on either at random — now discovered, routed per host, and reported |
| **Every Codex audit was failing** | `config.toml` named a model the ChatGPT-account Codex rejects; the router now picks from Codex's own model list and remembers rejections |
| **Right names, wrong lines** | Codex's cheapest tier listed the subcommands correctly and every line number wrong (one past the end of the file), three runs in a row. `refs.py` caught it each time; the automatic retry one tier up came back **6/6 grounded** — the main model never read the file |
| **A 48-token question cost 42k Codex tokens** | Codex loaded its skills (including this one) on every call. Every prompt now carries a short "delegated sub-task" preamble, and the router keeps Codex for reads big enough to be worth its overhead |

And the cascade working as designed: `qwen3.6:27b` listed all 10 function definitions in a 253-line file — 10/10 grounded, matching `grep`, 90 tok/s, ~4,400 tokens that never entered the main context.

<a id="detached-runs"></a>

## Detached runs｜脫離 Session 的長任務

Background commands in Claude Code and Codex die with the session. A usage limit mid-suite means re-running seven hours. `detached.py` starts long work as its own process, records it, and lets the next session read the result instead of re-running it:

```bash
python <skill>/detached.py start suite -- python -m pytest -q   # from the project root
python <skill>/detached.py status                             # running / stalled / done (exit code) / died
python <skill>/detached.py watch                              # wake only when a run ends, dies or stalls
python <skill>/detached.py tail suite 40
```

Nothing lingers: every run has a hard time limit (`--max-hours`, default 6) that kills its whole process tree, `stop NAME` ends it at once, nothing registers to start with the OS.

<a id="faq"></a>

## FAQ｜常見問題

<details>
<summary><strong>Won't a cheaper model make the result worse?｜便宜模型不會讓品質變差嗎？</strong></summary>

Only judgment-free work is delegated, and every result is verified mechanically before use; a failed check escalates to a stronger worker and the cheap answer is thrown away. If a grep could have produced it, a local model may. If it needs the codebase's meaning, a grade or a decision, it stays with the main model.
</details>

<details>
<summary><strong>Do I need Codex?｜一定要有 Codex 嗎？</strong></summary>

No. Without it (or while it is out of credit — the cool-down is read from Codex's own "try again in …"), the router goes straight from Ollama to a Claude haiku/sonnet subagent.
</details>

<details>
<summary><strong>Do I need Ollama?｜一定要裝 Ollama 嗎？</strong></summary>

No. The agent probes Ollama once per session (`run.py --check`); if it is absent or stale, it skips to rung 2. Detached runs need only Python.
</details>

<details>
<summary><strong>When is delegation a waste?｜什麼時候不該委派？</strong></summary>

Small in, small out. A subagent re-reads its instructions and files, so a five-line lookup costs more delegated than done inline. Delegate when the input is large and the answer is short.
</details>

<a id="install"></a>

## Install｜安裝

Clone once and link it into each agent's skills folder:

```bash
git clone <this-repo> ~/skills/token-economy-delegation
ln -s ~/skills/token-economy-delegation ~/.claude/skills/token-economy-delegation   # Claude Code
ln -s ~/skills/token-economy-delegation ~/.codex/skills/token-economy-delegation    # Codex
```

On Windows, use `mklink /J` instead of `ln -s`. Requirements: Python 3.10+ (stdlib only); optional Ollama; optional Codex CLI for rung 2b.

> 安裝一次，再連結到各 Agent 的 skills 資料夾。需要 Python 3.10+；Ollama 與 Codex CLI 為選配。

## Files｜檔案

| File | Purpose |
|---|---|
| `SKILL.md` | the ladder, the verification rules, common mistakes |
| `route.py` | who should do this job — Ollama, Codex, Claude, or no model — with the exact command and fallback chain |
| `codex_run.py` | read-only Codex task / audit, model tier from Codex's own list, cool-down on usage limits, reference check |
| `refs.py` | checks every `path:line` a delegated reply cites against the files |
| `detached.py` | start / status / watch / stop / tail long runs outside the session |
| `run.py` | local Ollama: discovery across daemons, routing, deterministic run, verification, escalation, ledger, bench |
| `routing.json` | optional: override which model families suit code / prose / zh |
| `ollama_review.py` | emergency-only local first-pass review per changed file |
| `codex_audit.py` | kept for old prompts: same as `codex_run.py audit` |

<a id="license"></a>

## Inspired by｜參考

- [Houtini LM](https://mcpservers.org/servers/houtini-ai/lm) — model discovery and per-hardware performance memory, truncation flags, "Claude is the architect, the other model drafts, Claude checks".
- [claude-ollama-agents](https://github.com/PratikHotchandani22/claude-ollama-agents) — role-specific local agents and token stats.
- [SerpentStack model-routing](https://openskillindex.com/skills/benja-pauls-serpentstack-model-routing) — keep planning and review on the cloud model; verify generated work with the project's own checks.
- [RouteLLM](https://github.com/lm-sys/RouteLLM) — routing between a cheap and a strong model by expected quality. This skill uses a cascade instead: try cheap, verify, escalate.

## License｜授權

MIT — add a `LICENSE` file before publishing. Part of [MixoCreative](https://github.com/mixocreative)'s open skills, alongside [cia](https://github.com/mixocreative/cia) and [ecommerce-cia](https://github.com/mixocreative/ecommerce-cia).
