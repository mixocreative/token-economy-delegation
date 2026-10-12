#!/usr/bin/env python3
r"""Delegate one mechanical job to the best local Ollama model, verify the answer, escalate if it fails.

    python run.py --check                         exit 0 + healthy models when Ollama is up, exit 1 otherwise
    python run.py --discover [--refresh]          what is installed: context, capabilities, size, loaded, VRAM fit, speed
    python run.py --bench [MODEL|all]             measure tokens/s and load time on THIS machine (cached)
    python run.py --stats                         pass rate and tokens offloaded, per model x task
    python run.py --task list --verify quotes --number-lines --prompt "List every if-condition as 'N: line'" < file.php
    python run.py --model devstral-small-2:24b --prompt "..." < file      pin a model (skips routing, still verifies)

The cascade - why delegating does not cost quality:
  1. ROUTE   pick candidates that are healthy, have the capability, fit the input in their context window and
             fit the free VRAM; rank them by task fit, measured pass rate on this machine, already-loaded, speed.
  2. RUN     temperature 0, thinking off for mechanical tasks, think-blocks stripped, structured output when a
             JSON schema is given. An answer cut by the token limit is TRUNCATED, never accepted.
  3. VERIFY  mechanically, against the input - quoted lines must exist verbatim, counts must equal a regex count,
             JSON must parse and carry its keys. A local answer that cannot be checked is not accepted for
             anything but a draft (--verify none says so on the footer).
  4. ESCALATE on failure: the next candidate (a different model), then exit 4 with `ESCALATE: <reason>`, which
             means: the main model does this job itself. A failed answer is discarded, never patched.
Every attempt is appended to the ledger, so routing learns which model actually passes which task here.

Exit codes: 0 verified (or --verify none) | 1 Ollama unreachable / bad usage | 4 ESCALATE.
stdin is the material; it lands after the prompt under `----- INPUT -----`, verbatim (or line-numbered).
Stdlib only. State lives in ~/.token-economy/ (override TOKEN_ECONOMY_HOME).
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

# The native Ollama install only: on Windows the desktop app (it runs and updates the server) or, without it, the
# Windows CLI's `ollama serve`. Reached on [::1], never 127.0.0.1: on Windows another listener (a WSL relay, a
# container, a second install) can own the IPv4 loopback port and answer with a different model set - seen
# 2026-10-12. Set OLLAMA_HOSTS (comma-separated) to use other or several daemons on purpose.
WINDOWS = os.name == "nt"
HOSTS = [h.strip().rstrip("/") for h in os.environ.get(
    "OLLAMA_HOSTS", "http://[::1]:11434" if WINDOWS else "http://127.0.0.1:11434").split(",") if h.strip()]
HOST = HOSTS[0]
OLLAMA_DIR = Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Ollama" if WINDOWS else None
HOME = Path(os.environ.get("TOKEN_ECONOMY_HOME") or Path.home() / ".token-economy")
CACHE = HOME / "models.json"
LEDGER = HOME / "ledger.jsonl"
ROUTING = Path(__file__).resolve().parent / "routing.json"
ESCALATE = 4

# ---------------------------------------------------------------- tasks
# kind: what the input is, for family affinity. weight: quality vs speed. think: allow reasoning tokens.
TASKS = {
    "list":      {"kind": "code",  "weight": "speed",   "think": False, "verify": "quotes", "desc": "enumerate lines/items from the input"},
    "extract":   {"kind": "code",  "weight": "speed",   "think": False, "verify": "quotes", "desc": "pull specific values or lines out"},
    "count":     {"kind": "code",  "weight": "speed",   "think": False, "verify": "count",  "desc": "count occurrences (always cross-checked)"},
    "format":    {"kind": "prose", "weight": "speed",   "think": False, "verify": "nonempty", "desc": "reformat / tabulate without changing content"},
    "classify":  {"kind": "prose", "weight": "speed",   "think": False, "verify": "json",   "desc": "label items from a fixed set (give --schema)"},
    "summarize": {"kind": "prose", "weight": "quality", "think": False, "verify": "quotes", "desc": "first-pass summary; quoted facts are checked"},
    "translate": {"kind": "zh",    "weight": "quality", "think": False, "verify": "nonempty", "desc": "draft translation (main model reviews)"},
    "review":    {"kind": "code",  "weight": "quality", "think": True,  "verify": "quotes", "desc": "first-pass defect list; every cited line must exist"},
    "general":   {"kind": "prose", "weight": "quality", "think": False, "verify": "nonempty", "desc": "anything else"},
}

# Family affinity by model-name pattern, per input kind. Edit routing.json beside this file to override.
DEFAULT_AFFINITY = {
    "code":  {"devstral": 3, "coder": 3, "qwen": 2, "deepseek": 2, "gemma": 1, "llama": 1, "mistral": 1},
    "prose": {"gemma": 3, "qwen": 3, "llama": 2, "mistral": 2, "devstral": 1},
    "zh":    {"qwen": 3, "gemma": 2, "glm": 3, "deepseek": 2, "llama": 1},
}


def affinity() -> dict:
    if ROUTING.exists():
        try:
            return {**DEFAULT_AFFINITY, **json.loads(ROUTING.read_text(encoding="utf-8")).get("affinity", {})}
        except (OSError, ValueError) as e:
            print(f"run.py: ignoring unreadable {ROUTING.name} ({e})", file=sys.stderr)
    return DEFAULT_AFFINITY


# ---------------------------------------------------------------- http
def api(path: str, body: dict | None = None, timeout: float = 10, host: str | None = None):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request((host or HOST) + path, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def say(msg: str) -> None:
    sys.stderr.buffer.write((msg + "\n").encode("utf-8", "replace"))
    sys.stderr.flush()


# ---------------------------------------------------------------- discovery
def gpu() -> dict:
    try:
        out = subprocess.run(["nvidia-smi", "--query-gpu=memory.total,memory.used", "--format=csv,noheader,nounits"],
                             capture_output=True, text=True, timeout=10,
                             creationflags=0x08000000 if os.name == "nt" else 0).stdout
        tot, used = (int(x) for x in out.split("\n")[0].split(","))
        return {"total_mib": tot, "used_mib": used}
    except (OSError, ValueError, subprocess.TimeoutExpired, IndexError):
        return {}


def load_cache() -> dict:
    try:
        return json.loads(CACHE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"models": {}}


def save_cache(c: dict) -> None:
    HOME.mkdir(parents=True, exist_ok=True)
    CACHE.write_text(json.dumps(c, indent=1), encoding="utf-8")


def server_up() -> bool:
    for h in HOSTS:
        try:
            api("/api/version", host=h, timeout=3)
            return True
        except (urllib.error.URLError, OSError, ValueError):
            pass
    return False


def ensure_server(wait: float = 25) -> str:
    """Start the native Ollama if nothing answers: the desktop app if installed (stabler - it supervises and updates
    the server), else `ollama serve` from the CLI. Never stops or restarts a running one. Returns what it did."""
    if server_up():
        return "running"
    how = None
    flags = (0x00000008 | 0x00000200 | 0x08000000) if WINDOWS else 0   # detached, own group, no window
    app = OLLAMA_DIR / "ollama app.exe" if OLLAMA_DIR else None
    try:
        if app and app.exists():
            subprocess.Popen([str(app)], creationflags=flags, close_fds=True)
            how = "started the Ollama desktop app"
        else:
            cli = shutil.which("ollama") or (str(OLLAMA_DIR / "ollama.exe") if OLLAMA_DIR and (OLLAMA_DIR / "ollama.exe").exists() else None)
            if not cli:
                return "not installed"
            subprocess.Popen([cli, "serve"], creationflags=flags, close_fds=True,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=not WINDOWS)
            how = "started `ollama serve`"
    except OSError as e:
        return f"could not start ({e})"
    t0 = time.time()
    while time.time() - t0 < wait:
        time.sleep(1)
        if server_up():
            return how
    return how + f", but it did not answer within {wait:.0f} s"


def server_info() -> str:
    try:
        v = api("/api/version", timeout=3).get("version", "?")
    except (urllib.error.URLError, OSError, ValueError):
        return "not answering"
    kind = "desktop app" if WINDOWS and _running("ollama app.exe") else ("CLI `ollama serve`" if WINDOWS else "ollama serve")
    return f"{kind} {v} at {HOST.split('//')[1]}"


def _running(image: str) -> bool:
    try:
        out = subprocess.run(["tasklist", "/FI", f"IMAGENAME eq {image}", "/NH"], capture_output=True, text=True,
                             timeout=10, creationflags=0x08000000).stdout
        return image.lower() in out.lower()
    except (OSError, subprocess.TimeoutExpired):
        return False


def discover(refresh: bool = False) -> dict:
    """Installed models across every reachable daemon, with what routing needs.

    /api/show is cached per digest; /api/ps and VRAM are always live. A model on two daemons keeps both hosts.
    Raises URLError only when no daemon answers at all.
    """
    cache = load_cache()
    known = cache.setdefault("models", {})
    seen: dict[str, dict] = {}
    hosts_up, loaded, sets = [], {}, {}
    for h in HOSTS:
        try:
            tags = api("/api/tags", host=h, timeout=4).get("models", [])
        except (urllib.error.URLError, OSError, ValueError):
            continue
        hosts_up.append(h)
        sets[h] = sorted(t["name"] for t in tags)
        for t in tags:
            e = seen.setdefault(t["name"], {"tag": t, "hosts": []})
            e["hosts"].append(h)
        try:
            for p in api("/api/ps", host=h, timeout=4).get("models", []):
                loaded[p["name"]] = int(p.get("size_vram", p.get("size", 0)) / 2**20)
        except (urllib.error.URLError, OSError, ValueError):
            pass
    if not hosts_up:
        started = ensure_server()
        if started in ("running",) or (started.startswith("started") and "did not answer" not in started):
            return discover(refresh)
        raise urllib.error.URLError(f"no Ollama answered at {', '.join(HOSTS)} ({started})")
    for name, e in seen.items():
        t = e["tag"]
        m = known.get(name, {})
        if refresh or m.get("digest") != t.get("digest") or not m.get("healthy"):
            m = {"digest": t.get("digest"), "bench": m.get("bench") if m.get("digest") == t.get("digest") else None}
            m.update(healthy=False, caps=[], ctx=None, problem=None)
            for h in e["hosts"]:
                try:
                    s = api("/api/show", {"model": name}, timeout=30, host=h)
                except (urllib.error.URLError, OSError, ValueError) as ex:
                    m["problem"] = f"/api/show failed on {h}: {ex} - broken pull or a daemon too old for it; re-pull"
                    continue
                info = s.get("model_info") or {}
                ctx = next((v for k, v in info.items() if k.endswith(".context_length")), None)
                m.update(healthy=bool(s.get("capabilities") or info), caps=s.get("capabilities") or [],
                         ctx=int(ctx) if ctx else None,
                         family=(s.get("details") or {}).get("family") or info.get("general.architecture"),
                         params=(s.get("details") or {}).get("parameter_size"),
                         quant=(s.get("details") or {}).get("quantization_level"), problem=None)
                if "embedding" in m["caps"] and "completion" not in m["caps"]:
                    m["healthy"] = False
                    m["problem"] = "embedding model - not a chat worker"
                break
        m["size_mib"] = int(t.get("size", 0) / 2**20)
        m["hosts"] = e["hosts"]
        known[name] = m
    for gone in set(known) - set(seen):
        del known[gone]
    save_cache(cache)
    warn = None
    if len({tuple(v) for v in sets.values()}) > 1:
        warn = ("OLLAMA_HOSTS lists daemons with different models (" + "; ".join(f"{h}: {len(v)}" for h, v in sets.items())
                + "); they share one GPU - routing sends each model to a daemon that has it.")
    return {"models": known, "loaded": loaded, "gpu": gpu(), "hosts": hosts_up, "warn": warn}


# ---------------------------------------------------------------- ledger
def ledger_rows() -> list[dict]:
    try:
        return [json.loads(l) for l in LEDGER.read_text(encoding="utf-8").splitlines() if l.strip()]
    except (OSError, ValueError):
        return []


def record(row: dict) -> None:
    HOME.mkdir(parents=True, exist_ok=True)
    with LEDGER.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def pass_rate(rows: list[dict], model: str, task: str) -> float:
    """Laplace-smoothed pass rate of verified attempts; 0.5 when there is no history (unknown, not bad)."""
    r = [x for x in rows if x.get("model") == model and x.get("task") == task and x.get("verify") not in (None, "none")]
    return (sum(1 for x in r if x.get("passed")) + 1) / (len(r) + 2)


# ---------------------------------------------------------------- routing
def est_tokens(text: str) -> int:
    cjk = len(re.findall(r"[　-鿿가-힯＀-￯]", text))
    return int(cjk / 1.3 + (len(text) - cjk) / 3.6) + 1


def rank(state: dict, task: str, need_tokens: int, need_caps: set[str]) -> list[tuple[float, str, str]]:
    t = TASKS[task]
    aff = affinity()[t["kind"]]
    rows = ledger_rows()
    g = state["gpu"]
    free = (g["total_mib"] - g["used_mib"]) if g else None
    out = []
    for name, m in state["models"].items():
        why = []
        if not m.get("healthy"):
            continue
        if need_caps - set(m.get("caps") or []):
            continue
        ctx = m.get("ctx") or 8192
        if need_tokens > ctx * 0.85:
            continue                                     # would not fit; chunking is the caller's choice (--chunk)
        a = max((v for k, v in aff.items() if k in name.lower() or k in str(m.get("family", "")).lower()), default=1)
        score = a * 1.0
        why.append(f"fit{a}")
        q = pass_rate(rows, name, task)
        score += 4 * (q - 0.5)
        why.append(f"q{q:.2f}")
        loaded = name in state["loaded"]
        if loaded:
            score += 1.5
            why.append("loaded")
        elif free is not None and m["size_mib"] > free + sum(state["loaded"].values()) - 512:
            score -= 3                                   # would spill to CPU or evict a GPU job: slow, and rude
            why.append("vram!")
        b = m.get("bench") or {}
        tps = b.get("tok_s")
        size_b = m["size_mib"] / 1024
        if t["weight"] == "speed":
            score += (tps / 40) if tps else (-size_b / 15)
        else:
            score += min(size_b / 10, 2)                 # bigger model for judgment-shaped drafts
        out.append((round(score, 2), name, " ".join(why)))
    return sorted(out, reverse=True)


# ---------------------------------------------------------------- run one model
THINK_RE = re.compile(r"<think>.*?</think>", re.S)


def call(model: str, system: str, user: str, num_ctx: int, think: bool, schema: dict | None,
         num_predict: int, keep_alive: str, hosts: list[str] | None = None) -> dict:
    last: Exception | None = None
    for h in hosts or [HOST]:
        try:
            return _call(h, model, system, user, num_ctx, think, schema, num_predict, keep_alive)
        except urllib.error.HTTPError as e:
            if e.code != 404:
                raise
            last = e                                  # this daemon does not have it after all - try the next
        except (urllib.error.URLError, OSError) as e:
            last = e
    raise last or urllib.error.URLError("no host")


def _call(host: str, model: str, system: str, user: str, num_ctx: int, think: bool, schema: dict | None,
          num_predict: int, keep_alive: str) -> dict:
    body = {"model": model, "stream": False, "keep_alive": keep_alive,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "options": {"temperature": 0, "num_ctx": num_ctx, "num_predict": num_predict}}
    body["think"] = bool(think)
    if schema:
        body["format"] = schema
    t0 = time.time()
    try:
        r = api("/api/chat", body, timeout=1800, host=host)
    except urllib.error.HTTPError as e:
        if e.code == 400 and "think" in e.read().decode("utf-8", "replace"):
            body.pop("think")                             # model without a thinking switch
            r = api("/api/chat", body, timeout=1800, host=host)
        else:
            raise
    text = THINK_RE.sub("", (r.get("message") or {}).get("content", "")).strip()
    ev = r.get("eval_count") or 0
    dur = (r.get("eval_duration") or 0) / 1e9
    return {"host": host, "text": text, "done_reason": r.get("done_reason"), "in": r.get("prompt_eval_count") or 0, "out": ev,
            "secs": round(time.time() - t0, 1), "tok_s": round(ev / dur, 1) if dur else None,
            "load_s": round((r.get("load_duration") or 0) / 1e9, 1)}


# ---------------------------------------------------------------- verification
def norm(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip().strip("`").strip()


ITEM = re.compile(r"^\s*(?:[-*•]\s*|\d+[.)]\s+)?(?:L?(\d+)\s*[:|]\s*)?(.*)$")


def verify(kind: str, answer: str, material: str, numbered: bool, args) -> tuple[bool, str]:
    if not answer.strip():
        return False, "empty answer"
    if kind == "none":
        return True, "UNVERIFIED (draft only)"
    if kind == "nonempty":
        return True, "nonempty"
    if kind == "json":
        try:
            obj = json.loads(answer)
        except ValueError as e:
            return False, f"not JSON ({e})"
        req = (args.schema_obj or {}).get("required", [])
        missing = [k for k in req if not (isinstance(obj, dict) and k in obj)]
        return (not missing), ("json ok" if not missing else f"json missing {missing}")
    if kind == "count":
        if not args.count_regex:
            return False, "count needs --count-regex to cross-check against"
        truth = len(re.findall(args.count_regex, material, re.M))
        m = re.search(r"-?\d+", answer)
        got = int(m.group()) if m else None
        return got == truth, f"count model={got} regex={truth}"
    if kind == "quotes":
        src_lines = material.splitlines()
        hay = norm(material)
        items = [l for l in answer.splitlines() if l.strip() and not l.strip().startswith("#")]
        if len(items) == 1 and items[0].strip().upper() == "NONE":
            return True, "NONE (nothing to ground)"
        grounded, bad = 0, []
        for line in items:
            m = ITEM.match(line)
            n, body = (int(m.group(1)) if m.group(1) else None), norm(m.group(2) if m else line)
            quoted = re.findall(r"`([^`]{4,})`|\"([^\"]{6,})\"", line)
            frag = [norm(a or b) for a, b in quoted] or ([body] if n is not None or args.task in ("list", "extract") else [])
            ok = True
            if n is not None and numbered:
                ok = 1 <= n <= len(src_lines) and all(f in norm(src_lines[n - 1]) or norm(src_lines[n - 1]) in f for f in frag if f)
            elif frag:
                ok = all(f in hay for f in frag if len(f) >= 4)
            grounded += ok
            if not ok:
                bad.append(line.strip()[:90])
        rate = grounded / len(items)
        verdict = rate >= args.min_grounded
        msg = f"quotes {grounded}/{len(items)} grounded"
        if bad:
            msg += f"; first ungrounded: {bad[0]!r}"
        return verdict, msg
    return False, f"unknown verifier {kind}"


# ---------------------------------------------------------------- the cascade
SYSTEM = ("You are a precise extraction tool. Answer only from the INPUT. Do not explain, do not add advice, "
          "do not invent lines. When you cite a line, copy the WHOLE line verbatim, character for character. "
          "If nothing matches, answer exactly NONE.")


def agree(a: str, b: str) -> tuple[str, float]:
    sa = {norm(x) for x in a.splitlines() if x.strip()}
    sb = {norm(x) for x in b.splitlines() if x.strip()}
    both = [x for x in a.splitlines() if norm(x) in sb]
    return "\n".join(both), (len(sa & sb) / max(len(sa | sb), 1))


def delegate(args, material: str) -> int:
    task = args.task
    t = TASKS[task]
    vkind = args.verify or t["verify"]
    shown = material
    if args.number_lines:
        shown = "\n".join(f"{i}: {l}" for i, l in enumerate(material.splitlines(), 1))
    user = args.prompt + ("\n\n----- INPUT -----\n" + shown if shown else "")
    need = est_tokens(args.prompt + shown) + args.max_out + 512
    num_ctx = args.num_ctx or min(max(8192, 2 ** math.ceil(math.log2(need * 1.25))), 131072)
    try:
        state = discover()
    except (urllib.error.URLError, OSError) as e:
        say(f"ollama: not reachable ({e})")
        say("ESCALATE: no local model - use rung 2 (cheap subagent) or do it inline")
        return ESCALATE
    caps = {"completion"} | ({"vision"} if args.vision else set())
    if args.model and args.model != "auto":
        cands = [(0, args.model, "pinned")]
    else:
        cands = rank(state, task, need, caps)
        if not cands:
            say(f"ESCALATE: no healthy local model fits ~{need} tokens with {sorted(caps)} - chunk the input or use rung 2")
            return ESCALATE
    if args.explain:
        for s, n, why in cands:
            say(f"  {s:6} {n:28} {why}")
    tries = cands[: max(1, args.attempts)]
    answers = []
    for _, model, why in tries:
        system = args.system or SYSTEM
        if vkind == "quotes" and args.number_lines:
            system += (" The INPUT lines start with their line number and a colon, e.g. `35: def run():`. Output each "
                       "matching line exactly as it appears there - number, colon and the full text - one per line.")
        try:
            r = call(model, system, user, num_ctx, t["think"] and not args.no_think, args.schema_obj,
                     args.max_out, args.keep_alive, (state["models"].get(model) or {}).get("hosts"))
        except (urllib.error.URLError, OSError, ValueError) as e:
            detail = ""
            if isinstance(e, urllib.error.HTTPError):
                try:
                    detail = " " + e.read().decode("utf-8", "replace")[:200]
                except OSError:
                    pass
            say(f"[run.py] {model}: request failed ({e}){detail} - next candidate")
            # infrastructure, not quality: verify=None keeps it out of the model's pass rate
            record({"ts": time.time(), "task": task, "model": model, "verify": None, "passed": False, "reason": f"error {e}"})
            continue
        if r["done_reason"] == "length":
            ok, why_v = False, f"TRUNCATED at {r['out']} tokens (raise --max-out or narrow the prompt)"
        else:
            ok, why_v = verify(vkind, r["text"], material, args.number_lines, args)
        record({"ts": time.time(), "task": task, "model": model, "verify": vkind, "passed": ok, "reason": why_v,
                "in": r["in"], "out": r["out"], "secs": r["secs"], "tok_s": r["tok_s"]})
        say(f"[run.py] {model} ({why}) task={task} in={r['in']} out={r['out']} {r['secs']}s"
            f"{' load ' + str(r['load_s']) + 's' if r['load_s'] > 1 else ''}"
            f"{' ' + str(r['tok_s']) + ' tok/s' if r['tok_s'] else ''} verify={vkind}: {'PASS' if ok else 'FAIL'} - {why_v}")
        if ok:
            answers.append((model, r["text"]))
            if not args.agree or len(answers) == 2:
                break
    if args.agree:
        if len(answers) < 2:
            say("ESCALATE: --agree needs two verified answers from different models; got " + str(len(answers)))
            return ESCALATE
        text, ratio = agree(answers[0][1], answers[1][1])
        say(f"[run.py] agreement {answers[0][0]} vs {answers[1][0]}: {ratio:.0%} (threshold {args.min_agree:.0%})")
        if ratio < args.min_agree:
            say("ESCALATE: the two local models disagree - do this one on the main model")
            return ESCALATE
        answers = [("agree", text)]
    if not answers:
        say("ESCALATE: every local attempt failed verification - discard them and do it on the main model")
        return ESCALATE
    sys.stdout.buffer.write((answers[0][1] + "\n").encode("utf-8", "replace"))
    say(f"[run.py] offloaded ~{est_tokens(shown)} input tokens from the main context")
    return 0


# ---------------------------------------------------------------- bench / stats / check
BENCH_PROMPT = "Write the numbers from 1 to 120 separated by spaces. Nothing else."


def bench(which: str) -> int:
    state = discover()
    names = [n for n, m in state["models"].items() if m.get("healthy")] if which == "all" else [which]
    g = state["gpu"]
    if g and g["used_mib"] - sum(state["loaded"].values()) > 4000:
        say(f"refusing to bench: another GPU job holds ~{g['used_mib'] - sum(state['loaded'].values())} MiB - run when idle")
        return 3
    cache = load_cache()
    for n in names:
        try:
            r = call(n, "Be terse.", BENCH_PROMPT, 4096, False, None, 600, "0", state["models"][n].get("hosts"))
        except (urllib.error.URLError, OSError, ValueError) as e:
            say(f"  {n:28} FAILED {e}")
            cache["models"].setdefault(n, {})["healthy"] = False
            continue
        cache["models"].setdefault(n, {})["bench"] = {"tok_s": r["tok_s"], "load_s": r["load_s"], "at": int(time.time())}
        say(f"  {n:28} {r['tok_s']} tok/s  load {r['load_s']}s")
    save_cache(cache)
    return 0


def show_discover(refresh: bool) -> int:
    s = discover(refresh)
    g = s["gpu"]
    print(f"GPU: {g.get('used_mib', '?')}/{g.get('total_mib', '?')} MiB used" if g else "GPU: no nvidia-smi")
    print("ollama: " + server_info() + (f" (+ {', '.join(s['hosts'][1:])})" if len(s["hosts"]) > 1 else ""))
    if s.get("warn"):
        print("WARNING: " + s["warn"])
    print(f"{'model':28} {'ok':3} {'size':>6} {'ctx':>7} {'caps':32} {'tok/s':>6} loaded")
    for n, m in sorted(s["models"].items()):
        b = (m.get("bench") or {}).get("tok_s")
        print(f"{n:28} {'yes' if m.get('healthy') else 'NO':3} {m['size_mib'] / 1024:5.1f}G {m.get('ctx') or '?':>7} "
              f"{','.join(m.get('caps') or []):32} {b or '-':>6} {'yes' if n in s['loaded'] else ''}  {' '.join(h.split('//')[1] for h in m.get('hosts', []))}")
        if m.get("problem"):
            print(f"    ! {m['problem']}")
    print("\nrouting preview (task: best first):")
    for task in ("list", "review", "summarize", "translate"):
        r = rank(s, task, 4000, {"completion"})
        print(f"  {task:10} " + " > ".join(n for _, n, _ in r[:3]))
    return 0


def stats() -> int:
    rows = ledger_rows()
    if not rows:
        print("no delegations recorded yet")
        return 0
    agg: dict = {}
    for r in rows:
        k = (r.get("codex_model") and "codex:" + r["codex_model"] or r.get("model"), r.get("task"))
        a = agg.setdefault(k, {"n": 0, "ok": 0, "err": 0, "tok": 0})
        if not r.get("verify"):
            a["err"] += 1                                  # infrastructure or config, not the model's quality
            continue
        a["n"] += 1
        a["ok"] += bool(r.get("passed"))
        a["tok"] += (r.get("in") or 0) if r.get("passed") else 0
    print(f"{'worker':30} {'task':11} {'checked':>7} {'pass':>5} {'infra err':>9} {'tokens offloaded':>17}")
    for (m, t), a in sorted(agg.items(), key=lambda x: -x[1]["tok"]):
        rate = f"{a['ok'] / a['n']:.0%}" if a["n"] else "-"
        print(f"{m:30} {t:11} {a['n']:7} {rate:>5} {a['err']:9} {a['tok']:17,}")
    print(f"\ntotal: {sum(a['tok'] for a in agg.values()):,} input tokens kept out of the main context by verified answers"
          " (Codex rows count only the prompt; the files it read never entered the main context either)")
    return 0


def check() -> int:
    try:
        s = discover()
    except (urllib.error.URLError, OSError) as e:
        say(f"ollama: not reachable ({e})")
        return 1
    if s.get("warn"):
        say("warning: " + s["warn"])
    ok = [n for n, m in s["models"].items() if m.get("healthy")]
    for n in ok:
        print(n)
    for n, m in s["models"].items():
        if not m.get("healthy"):
            say(f"broken: {n} - {m.get('problem')}")
    if not ok:
        say("ollama is up but no model is usable: a daemon that cannot see OLLAMA_MODELS lists none - restart the tray app")
        return 1
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--check", action="store_true", help="probe the server and list usable models")
    ap.add_argument("--discover", action="store_true", help="model table + routing preview")
    ap.add_argument("--refresh", action="store_true", help="with --discover: re-read every model's details")
    ap.add_argument("--bench", nargs="?", const="all", help="measure speed of one model or all (GPU must be idle)")
    ap.add_argument("--stats", action="store_true", help="ledger summary")
    ap.add_argument("--task", choices=sorted(TASKS), default="general")
    ap.add_argument("--model", default="auto", help="'auto' routes; a name pins")
    ap.add_argument("--prompt", default="")
    ap.add_argument("--system", default="", help="replace the default extraction system prompt")
    ap.add_argument("--verify", choices=["quotes", "count", "json", "nonempty", "none"], help="default: per task")
    ap.add_argument("--count-regex", help="for --verify count: the regex whose matches in the input are the truth")
    ap.add_argument("--schema", help="JSON schema file: forces structured output and is checked")
    ap.add_argument("--number-lines", action="store_true", help="prefix input lines with 'N: ' so cited line numbers are checkable")
    ap.add_argument("--min-grounded", type=float, default=1.0, help="share of cited lines that must exist verbatim (default all)")
    ap.add_argument("--attempts", type=int, default=2, help="local models to try before ESCALATE")
    ap.add_argument("--agree", action="store_true", help="two different models must agree; output is their intersection")
    ap.add_argument("--min-agree", type=float, default=0.8)
    ap.add_argument("--max-out", type=int, default=4096, help="output token budget; hitting it = TRUNCATED = fail")
    ap.add_argument("--num-ctx", type=int, default=0, help="context window; default sized from the input")
    ap.add_argument("--keep-alive", default="10m", help="how long the model stays in VRAM after the call ('0' unloads)")
    ap.add_argument("--no-think", action="store_true")
    ap.add_argument("--vision", action="store_true", help="require a vision-capable model")
    ap.add_argument("--explain", action="store_true", help="print the candidate ranking")
    a = ap.parse_args()
    if a.check:
        return check()
    if a.discover:
        return show_discover(a.refresh)
    if a.bench:
        return bench(a.bench)
    if a.stats:
        return stats()
    if not a.prompt:
        ap.error("--prompt is required (or --check / --discover / --bench / --stats)")
    a.schema_obj = json.loads(Path(a.schema).read_text(encoding="utf-8")) if a.schema else None
    if a.agree:
        a.attempts = max(a.attempts, 3)
    material = "" if sys.stdin.isatty() else sys.stdin.buffer.read().decode("utf-8", "replace")
    return delegate(a, material)


if __name__ == "__main__":
    sys.exit(main())
