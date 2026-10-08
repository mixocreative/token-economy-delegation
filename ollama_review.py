r"""Optional free first-pass review by a local Ollama model: a list of suspicious spots per changed file, never a verdict.

    python scripts\ollama_review.py RANGE OUT.md [--model qwen3-coder:30b] [--url http://localhost:11434]

RANGE is a git range (e.g. `a56a896..HEAD`). For each changed source file the model sees only that file's diff and lists possible
defects. Codex (scripts\codex_audit.py) or the main session checks the list; nothing here passes or fails a task.

It refuses to start while the machine is busy - a GPU job using the card, or a test suite running - because a 30B model needs most of
the graphics memory and slows everything else. Run it detached: `python scripts\detached.py start review -- python scripts\ollama_review.py ...`.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import urllib.request
from pathlib import Path

ROOT = Path.cwd()   # run from the repo to review
SOURCE = (".py", ".js", ".ps1", ".css", ".html")
PROMPT = ("You are a first-pass reviewer. List only REAL defects in this diff: wrong behaviour, data loss, security holes, races, "
          "Windows API misuse. Skip style and generic advice (no 'add validation', no 'wrap in try/except' unless a concrete failure "
          "follows). For each: the changed line, what goes wrong, the fix. If there are none, answer exactly NONE.\n\nFILE: {name}\n\n{diff}")


def gpu_busy(limit_mib: int = 4000) -> bool:
    try:
        used = subprocess.run(["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
                              capture_output=True, text=True, timeout=15, creationflags=0x08000000).stdout
        return any(int(v) > limit_mib for v in used.split())
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return False


def suite_running() -> bool:
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-Command",
                              "(Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match 'pytest' }).Count"],
                             capture_output=True, text=True, timeout=30, creationflags=0x08000000).stdout.strip()
        return int(out or 0) > 0
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return False


def changed_files(rng: str) -> list[str]:
    names = subprocess.run(["git", "diff", "--name-only", rng], cwd=ROOT, capture_output=True, text=True, check=True).stdout
    return [n for n in names.split() if n.endswith(SOURCE) and not n.startswith(("tests/", "docs/"))]


def ask(url: str, model: str, text: str) -> str:
    body = {"model": model, "prompt": text, "stream": False, "keep_alive": 0,
            "options": {"temperature": 0, "num_ctx": 16384, "num_predict": 700}}
    req = urllib.request.Request(f"{url}/api/generate", json.dumps(body).encode(), {"Content-Type": "application/json"})
    return json.load(urllib.request.urlopen(req, timeout=900)).get("response", "").strip()


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("range")
    parser.add_argument("out")
    parser.add_argument("--model", default="qwen3-coder:30b")
    parser.add_argument("--url", default="http://localhost:11434")
    parser.add_argument("--force", action="store_true", help="run even if the GPU or a suite is busy")
    args = parser.parse_args(argv)
    if not args.force and (gpu_busy() or suite_running()):
        print("SKIPPED: the GPU or a test suite is busy - run the review when the machine is idle")
        return 3
    lines = [f"# Ollama first-pass review ({args.model}) of {args.range}", "",
             "Advisory only: every item must be checked before anything is changed.", ""]
    for name in changed_files(args.range):
        diff = subprocess.run(["git", "diff", args.range, "--", name], cwd=ROOT, capture_output=True, text=True,
                              encoding="utf-8", errors="replace").stdout[:24000]
        try:
            answer = ask(args.url, args.model, PROMPT.format(name=name, diff=diff))
        except OSError as exc:
            print(f"OLLAMA FAILED: {exc}")
            return 2
        lines += [f"## {name}", "", answer or "NONE", ""]
    Path(args.out).write_text("\n".join(lines), encoding="utf-8")
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
