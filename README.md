<div align="center">

# token-economy-delegation

### 省 Token 委派 · a skill for Claude Code and OpenAI Codex

**Stop paying your best model to read 2,000 lines and return twelve.**<br/>
**Big in, small out → send it to the cheapest worker that can do it, then verify what comes back.**

**別再讓最貴的模型讀兩千行只為了找十二行。大量輸入、少量輸出的工作，交給最便宜的工人做，主模型只負責驗證與判斷。**

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](#license) [![Claude Code skill](https://img.shields.io/badge/Claude_Code-skill-D97757)](#install) [![Codex skill](https://img.shields.io/badge/OpenAI_Codex-skill-000000)](#install) [![Ollama](https://img.shields.io/badge/Ollama-local_models-black)](#the-ladder) [![Python](https://img.shields.io/badge/Python-stdlib_only-3776AB)](#install)

[Quick start](#quick-start) · [The ladder](#the-ladder) · [Detached runs](#detached-runs) · [FAQ](#faq) · [Install](#install)

</div>

---

Your agent's context window is the scarce resource. Most of what fills it is mechanical: reading a huge file to list its parameters, counting call sites, summarising a log, waiting on a seven-hour test suite. This skill makes the agent ask one question before each of those steps — *would I accept this from a grep?* — and if yes, hand it to the cheapest worker that gives the **same quality of result**: no model at all, a local Ollama model, a small subagent, or a second agent as an independent reviewer. The main model keeps every judgment, grade and decision, and checks what came back.

> Agent 的 context 是最稀缺的資源，而塞滿它的多半是機械性工作：讀大檔案列參數、數呼叫點、摘要 log、等七小時的測試。這個 Skill 讓 Agent 在每一步之前先問：「如果這是 grep 給我的結果，我能接受嗎？」能接受，就交給品質不變的最便宜工人 —— 不用模型、本機 Ollama、小型 subagent，或是另一個 Agent 當獨立審查。所有判斷、評級與決策都留在主模型，並驗證回傳的結果。

<a id="quick-start"></a>

## Quick start｜快速開始

It triggers on its own at the start of bulky tasks. Or ask directly:

```text
Use a local model to list every conditional in OrderPage.php, then verify the count with grep.
```

```text
Run the full test suite detached so it survives if my session hits the usage limit.
```

```text
Get Codex to audit this branch as a second opinion; only bring me the verdict.
```

```text
幫我省 token：用本機模型先把這份 log 裡所有錯誤列出來，再抽查確認。
```

<a id="the-ladder"></a>

## The ladder｜委派階梯

Tried in order; the agent stops at the first rung that fits.

| Rung | Worker | Use it for | Cost |
|---|---|---|---|
| 0 | **No model** — a detached process (`detached.py`) | test suites, builds, installers, GPU walks, long downloads | free, and survives the session |
| 1 | **Local Ollama** (`run.py`) | enumeration, extraction, reformatting, counting, a first-pass summary | free |
| 2 | **Cheapest subagent** (Haiku / Sonnet / Explore) | reading across many files, a bounded 1–2 file edit | low |
| 2b | **Codex CLI as independent auditor** (`codex_audit.py`) | a second opinion on finished work; only the verdict returns | separate quota |
| 3 | **Main model** | judgment, design, security, grading, anything that ships | full |

Every rung below 3 is **verified before use**: a count is re-checked with `grep -c`, a list is spot-read, a summary is checked on the two facts that matter. A result that fails the check is discarded, not patched. And the agent says in its reply what it delegated and how it checked it.

> 第 3 階以下的每個結果都先驗證再使用：數量用 `grep -c` 對、清單抽查、摘要核對兩個關鍵事實。驗證不過就丟掉，不修補。

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

Only judgment-free work is delegated, and every result is verified mechanically before use. The rule: if a grep could have produced it, a local model may. If it needs the codebase's meaning, a grade or a decision, it stays with the main model.
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
| `detached.py` | start / status / watch / stop / tail long runs outside the session |
| `run.py` | one prompt through a local Ollama model, temperature 0, deterministic |
| `ollama_review.py` | emergency-only local first-pass review per changed file |
| `codex_audit.py` | wraps `codex exec` read-only as an independent auditor |

<a id="license"></a>

## License｜授權

MIT — add a `LICENSE` file before publishing. Part of [MixoCreative](https://github.com/mixocreative)'s open skills, alongside [cia](https://github.com/mixocreative/cia) and [ecommerce-cia](https://github.com/mixocreative/ecommerce-cia).
