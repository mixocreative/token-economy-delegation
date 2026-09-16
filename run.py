#!/usr/bin/env python3
"""Run one prompt through a local Ollama model and print the answer.

    python run.py --model qwen2.5-coder:7b --prompt "List every `if` in this file" < file.php
    python run.py --check          # exit 0 + model list when Ollama is up, exit 1 otherwise

stdin (optional) is appended to the prompt under a `----- INPUT -----` line, so the prompt
stays short and the material stays verbatim. Temperature 0, no streaming: the output is a
deterministic artefact the caller can diff, grep and re-run.
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request

HOST = "http://localhost:11434"


def check() -> int:
    try:
        with urllib.request.urlopen(HOST + "/api/tags", timeout=3) as r:
            tags = json.load(r)
    except (urllib.error.URLError, OSError) as e:
        print(f"ollama: not reachable at {HOST} ({e})", file=sys.stderr)
        return 1
    for m in tags.get("models", []):
        print(m["name"])
    return 0


def run(model: str, prompt: str, num_ctx: int) -> int:
    material = "" if sys.stdin.isatty() else sys.stdin.read()
    if material:
        prompt = prompt + "\n\n----- INPUT -----\n" + material
    body = json.dumps({
        "model": model,
        "prompt": prompt,
        "stream": False,
        "options": {"temperature": 0, "num_ctx": num_ctx},
    }).encode()
    req = urllib.request.Request(HOST + "/api/generate", data=body, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=600) as r:
            out = json.load(r)
    except urllib.error.HTTPError as e:
        # Ollama answers 404 with a JSON body that says why - "model 'x' not found" is the
        # daemon not seeing OLLAMA_MODELS, not a wrong URL. Print it.
        body = e.read().decode("utf-8", "replace")[:300]
        print(f"ollama: request failed ({e}): {body}", file=sys.stderr)
        return 1
    except (urllib.error.URLError, OSError) as e:
        print(f"ollama: request failed ({e})", file=sys.stderr)
        return 1
    # Bytes, not text: the Windows console may be cp950 and the model may answer with anything.
    sys.stdout.buffer.write((out.get("response", "") + "\n").encode("utf-8", "replace"))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--check", action="store_true", help="probe the server and list models")
    ap.add_argument("--model", default="qwen2.5-coder:7b")
    ap.add_argument("--prompt", default="")
    ap.add_argument("--num-ctx", type=int, default=16384, help="context window; raise for big files")
    a = ap.parse_args()
    if a.check:
        return check()
    if not a.prompt:
        ap.error("--prompt is required (or --check)")
    return run(a.model, a.prompt, a.num_ctx)


if __name__ == "__main__":
    sys.exit(main())
