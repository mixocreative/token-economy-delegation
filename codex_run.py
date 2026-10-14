#!/usr/bin/env python3
r"""Run one read-only Codex job on Codex's own quota, and remember whether Codex is usable right now.

    python codex_run.py task  PROMPT.md REPLY.md [--effort low|medium|high] [--model M] [--schema S.json]
    python codex_run.py audit PROMPT.md REPLY.md [--effort ...]     reply must carry `VERDICT: PASS|FIX NEEDED`
    python codex_run.py --status                                     usable / cooling down until <time> / not installed

Why Codex is a delegation rung: it reads the repository itself, in its own context, on a separate quota. The
main model spends tokens only on the prompt and the reply - a repo-wide "find every caller and list path:line"
costs the main context a few hundred tokens instead of the files.

Prints `CODEX OK` (task) or `VERDICT: ...` (audit) and exits 0. Every `path:line` the reply cites is checked
against the files (refs.py); a reply citing lines that do not exist prints `CODEX UNGROUNDED` and exits 3 - at the
cheap tier it is retried once on the mid tier first. On any failure - not installed, out of credit,
rate limited, crashed, timed out, no VERDICT - prints `CODEX FAILED: <reason>` and exits 2 so the caller moves
to the next rung. A usage-limit failure is remembered (~/.token-economy/codex.json) so route.py stops sending
work to Codex until the cool-down passes instead of discovering the limit again on every call.
Run from the repository Codex should read.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import refs  # noqa: E402

HOME = Path(os.environ.get("TOKEN_ECONOMY_HOME") or Path.home() / ".token-economy")
STATE = HOME / "codex.json"
LEDGER = HOME / "ledger.jsonl"
LIMIT = re.compile(r"rate.?limit|quota|usage limit|insufficient|credit|429|too many requests|exceeded", re.I)
RETRY = re.compile(r"(?:try again|resets?|retry)\D{0,20}(\d+)\s*(seconds?|secs?|s|minutes?|mins?|m|hours?|hrs?|h|days?|d)\b", re.I)
UNIT = {"s": 1, "m": 60, "h": 3600, "d": 86400}   # by first letter of the unit


CODEX_HOME = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
UNSUPPORTED = re.compile(r"model[^\n]{0,80}(?:is not supported|not found|does not exist|no access)", re.I)
# Tier from the vendor's own one-line description, so a new model generation needs no edit here.
TIER_WORDS = {"cheap": ("fast", "affordable", "easier", "efficient"), "top": ("frontier", "most demanding"),
              "mid": ("workhorse", "balanced", "straightforward")}
DEFAULT_TIER = {"task": "cheap", "audit": "mid"}
# Prepended to every prompt. Without it Codex loads its skills - including this one, linked into ~/.codex/skills -
# and a 48-token question cost 42k Codex tokens (2026-10-12). Line numbers come from a numbered listing because the
# cheap tier otherwise estimates them.
PREAMBLE = ("[Delegated sub-task from another agent. Do not load or read any skills or SKILL.md files, do not plan, "
            "do not modify anything. Answer exactly what is asked, in the format asked, and stop. Whenever you cite "
            "path:line, take the number from a numbered listing (Select-String -Path F -Pattern P, findstr /n, or "
            "grep -n) - never by counting or estimating - and put the quoted line text after it in backticks.]\n\n")


def models() -> list[dict]:
    """Codex's own model list (models_cache.json, refreshed by the CLI): visible, API-usable, not known-bad."""
    try:
        d = json.loads((CODEX_HOME / "models_cache.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    bad = set(state().get("bad_models", []))
    out = []
    for m in d.get("models", []):
        if m.get("visibility") != "list" or m.get("supported_in_api") is False or m.get("slug") in bad:
            continue
        desc = (m.get("description") or "").lower()
        tier = next((t for t, words in TIER_WORDS.items() if any(w in desc for w in words)), "mid")
        old = any(w in desc for w in ("older", "previous", "legacy"))
        efforts = [e.get("effort") if isinstance(e, dict) else e for e in m.get("supported_reasoning_levels") or []]
        out.append({"slug": m["slug"], "tier": tier, "old": old, "priority": m.get("priority", 99), "efforts": efforts,
                    "desc": m.get("description")})
    return sorted(out, key=lambda m: (m["old"], m["priority"]))


def model_rate(slug: str, mode: str) -> tuple[float, int]:
    """Smoothed pass rate of one Codex model on one mode, from the ledger (infrastructure rows excluded)."""
    try:
        rows = [json.loads(l) for l in LEDGER.read_text(encoding="utf-8").splitlines() if l.strip()]
    except (OSError, ValueError):
        rows = []
    r = [x for x in rows if x.get("codex_model") == slug and x.get("task") == f"codex-{mode}" and x.get("verify")]
    return (sum(1 for x in r if x.get("passed")) + 1) / (len(r) + 2), len(r)


def pick(tier: str, mode: str = "task") -> list[str]:
    """Models to try, best first: the newest of the wanted tier, then the same tier older, then the next tier up.
    A model that has failed this mode's checks here at least 3 times with a pass rate under 0.35 is skipped."""
    order = {"cheap": ["cheap", "mid", "top"], "mid": ["mid", "top", "cheap"], "top": ["top", "mid"]}[tier]
    out = []
    for t in order:
        for m in models():
            if m["tier"] != t:
                continue
            rate, n = model_rate(m["slug"], mode)
            if n >= 3 and rate < 0.35:
                continue
            out.append(m["slug"])
    return out


def configured_model() -> str | None:
    try:
        m = re.search(r'^\s*model\s*=\s*"([^"]+)"', (CODEX_HOME / "config.toml").read_text(encoding="utf-8"), re.M)
        return m.group(1) if m else None
    except OSError:
        return None


CHATGPT_BACKEND = "https://chatgpt.com/backend-api/codex"


def app_session_model() -> str | None:
    """The model the Codex app (not codex exec) used in its newest session: proof the account accepts it today.
    A fallback for when models_cache.json is stale and every listed model is rejected (2026-10-14: gpt-5.5 and
    gpt-5 were dropped while the app moved on). Never a local / Ollama model."""
    try:
        files = sorted((CODEX_HOME / "sessions").rglob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)[:40]
    except OSError:
        return None
    for f in files:
        try:
            head = f.read_text(encoding="utf-8", errors="replace")[:200_000]
        except OSError:
            continue
        if '"originator":"codex_exec"' in head or '"originator":"Claude Code"' in head:
            continue
        m = re.search(r'"model":"(gpt-[^"]+)"', head)
        if m and m.group(1) not in state().get("bad_models", []):
            return m.group(1)
    return None


def backend_override() -> list[str]:
    """`-c openai_base_url=...` when config.toml points Codex at a LOCAL server (e.g. Ollama on 127.0.0.1:11434)
    while the login is a ChatGPT account: a delegated Codex job must reach real Codex, not the local model, and the
    owner's config.toml is never edited (2026-10-14: the local base URL silently sent audits to a 32B Ollama model).
    Empty when the config already targets OpenAI or when the login is an API key (then the CLI's default is right)."""
    try:
        cfg = (CODEX_HOME / "config.toml").read_text(encoding="utf-8")
    except OSError:
        return []
    m = re.search(r'^\s*openai_base_url\s*=\s*"([^"]+)"', cfg, re.M)
    if not m or not re.search(r"//(localhost|127\.|0\.0\.0\.0|\[::1\])", m.group(1)):
        return []
    try:
        mode = json.loads((CODEX_HOME / "auth.json").read_text(encoding="utf-8")).get("auth_mode")
    except (OSError, ValueError):
        mode = None
    return ["-c", f'openai_base_url="{CHATGPT_BACKEND}"'] if mode == "chatgpt" else ["-c", 'openai_base_url="https://api.openai.com/v1"']


def exe() -> str | None:
    return shutil.which("codex") or shutil.which("codex.cmd")


def state() -> dict:
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save(**kw) -> None:
    HOME.mkdir(parents=True, exist_ok=True)
    s = state()
    s.update(kw)
    STATE.write_text(json.dumps(s, indent=1), encoding="utf-8")


def status() -> tuple[bool, str]:
    """(usable, reason). Cheap: never calls the model."""
    if not exe():
        return False, "codex CLI not on PATH"
    s = state()
    until = s.get("cooldown_until", 0)
    if until > time.time():
        return False, f"cooling down until {time.strftime('%Y-%m-%d %H:%M', time.localtime(until))} ({s.get('last_reason')})"
    cfg = configured_model()
    if cfg and cfg in s.get("bad_models", []):
        return True, (f"ready via explicit model - NOTE config.toml model `{cfg}` was rejected by the account; "
                      f"jobs pass -m {(pick('cheap') or ['?'])[0]} / {(pick('mid') or ['?'])[0]}")
    return True, "ready" + (f" (last ok {time.strftime('%m-%d %H:%M', time.localtime(s['last_ok']))})" if s.get("last_ok") else "")


def run(mode: str, prompt: Path, reply: Path, effort: str | None, model: str | None, schema: str | None,
        timeout: float, tier: str | None = None) -> int:
    ok, why = status()
    if not ok:
        print(f"CODEX FAILED: {why}")
        return 2
    want = tier or DEFAULT_TIER[mode]
    candidates = [model] if model else (pick(want, mode)[:3] or [None])   # None = config.toml's model
    live = app_session_model()
    if live and live not in candidates:
        candidates.append(live)   # last resort: whatever the Codex app itself used most recently
    last = 2
    for m in candidates:
        last, unsupported = _once(mode, prompt, reply, effort, m, schema, timeout)
        if unsupported:
            print(f"codex: model {m or configured_model()} rejected by this account - trying the next one")
            continue
        if last == 3 and not model and want == "cheap":
            up = [x for x in pick("mid", mode) if x != m][:1]
            if up:
                print(f"codex: {m} cited lines that do not exist - one retry on {up[0]} (mid tier); the cheap answer is discarded")
                last, _ = _once(mode, prompt, reply, effort or "low", up[0], schema, timeout)
        return last
    return last


def _once(mode: str, prompt: Path, reply: Path, effort: str | None, model: str | None, schema: str | None,
          timeout: float) -> tuple[int, bool]:
    cmd = [exe(), "exec", "-s", "read-only", "-C", str(Path.cwd()), "--skip-git-repo-check", "--json", "-o", str(reply)]
    cmd += backend_override()
    if effort:
        cmd += ["-c", f'model_reasoning_effort="{effort}"']
    if model:
        cmd += ["-m", model]
    if schema:
        cmd += ["--output-schema", schema]
    cmd.append("-")
    log = reply.with_suffix(".log")
    reply.unlink(missing_ok=True)
    t0 = time.time()
    body = PREAMBLE.encode() + prompt.read_bytes()
    try:
        with open(log, "wb") as out:
            code = subprocess.run(cmd, input=body, stdout=out, stderr=subprocess.STDOUT, timeout=timeout).returncode
    except subprocess.TimeoutExpired:
        print(f"CODEX FAILED: no answer within {timeout:.0f} s")
        return 2, False
    raw = log.read_text(encoding="utf-8", errors="replace") if log.exists() else ""
    text = reply.read_text(encoding="utf-8", errors="replace") if reply.exists() else ""
    used = sum(int(n) for n in re.findall(r'"(?:input_tokens|output_tokens)"\s*:\s*(\d+)', raw))
    verdict = re.search(r"VERDICT:\s*\**\s*(PASS|FIX NEEDED)", text)
    failed = code != 0 or not text.strip() or (mode == "audit" and not verdict)
    name = model or configured_model() or "default"
    row = {"ts": time.time(), "task": f"codex-{mode}", "model": "codex", "codex_model": name, "effort": effort,
           "verify": "exit+reply", "passed": not failed, "secs": round(time.time() - t0, 1), "codex_tokens": used,
           "in": len(prompt.read_text(encoding="utf-8", errors="replace")) // 4}
    if failed:
        tail = raw[-3000:]
        if UNSUPPORTED.search(tail):
            bad = sorted(set(state().get("bad_models", [])) | {name})
            save(bad_models=bad, last_fail=time.time(), last_reason=f"model {name} not available to this account")
            row.update(verify=None, reason="model rejected (config, not quality)")
            _record(row)
            return 2, True
        if LIMIT.search(tail):
            m = RETRY.search(tail)
            wait = int(m.group(1)) * UNIT[m.group(2)[0].lower()] if m else 3600
            save(cooldown_until=time.time() + wait, last_reason="out of credit / rate limited", last_fail=time.time())
            reason = f"out of credit / rate limited - cooling down {wait // 60} min"
            row["verify"] = None
        else:
            reason = f"exit code {code}" if code else ("reply has no VERDICT line" if mode == "audit" and text else "empty reply")
            save(last_fail=time.time(), last_reason=reason)
        row["reason"] = reason
        _record(row)
        print(f"CODEX FAILED: {reason}\n--- codex log tail ---\n{tail.strip()[-800:]}")
        return 2, False
    save(last_ok=time.time(), cooldown_until=0, last_reason=None)
    good, total, bad = refs.check(text, Path.cwd())
    if total and good < total:
        row.update(passed=False, verify="refs", reason=f"refs {good}/{total} grounded; {bad[0]}")
        _record(row)
        print(f"CODEX UNGROUNDED ({name}): refs {good}/{total} - {bad[0]}. Do not use this reply's line numbers.")
        return 3, False
    if total:
        row.update(verify="refs", reason=f"refs {total}/{total} grounded")
    _record(row)
    note = f", refs {total}/{total} grounded" if total else ", no path:line to check - spot-read it"
    print(f"VERDICT: {verdict.group(1)}{note}" if mode == "audit"
          else f"CODEX OK ({name}, effort {effort or 'default'}{note}) -> {reply}  ({used:,} Codex tokens, 0 main-context)")
    return 0, False


def _record(row: dict) -> None:
    HOME.mkdir(parents=True, exist_ok=True)
    with LEDGER.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row) + "\n")


def main() -> int:
    if "--status" in sys.argv:
        ok, why = status()
        print(("usable: " if ok else "unavailable: ") + why)
        for m in models():
            rate, n = model_rate(m["slug"], "task")
            hist = f"  [task pass {rate:.2f} over {n}]" if n else ""
            print(f"  {m['tier']:5} {m['slug']:16} {'(older) ' if m['old'] else ''}{m['desc']}{hist}")
        return 0 if ok else 2
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("mode", choices=["task", "audit"])
    ap.add_argument("prompt", type=Path)
    ap.add_argument("reply", type=Path)
    ap.add_argument("--effort", choices=["minimal", "low", "medium", "high", "xhigh"],
                    help="reasoning effort; low for lookups, medium for audits, high for hard reviews")
    ap.add_argument("--model", help="pin a model (default: routed by --tier from Codex's own model list)")
    ap.add_argument("--tier", choices=["cheap", "mid", "top"], help="default: cheap for task, mid for audit")
    ap.add_argument("--schema", help="JSON schema file for a structured final answer")
    ap.add_argument("--timeout", type=float, default=2400)
    a = ap.parse_args()
    return run(a.mode, a.prompt, a.reply, a.effort, a.model, a.schema, a.timeout, a.tier)


if __name__ == "__main__":
    sys.exit(main())
