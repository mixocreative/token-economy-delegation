r"""Run long non-AI work (test suites, builds, GPU/installer walks) as detached processes that outlive the agent session
(Claude Code or Codex), so a usage/credit limit or a closed terminal does not kill it.

    python <skill>/detached.py start NAME -- <command...>   start it in the current folder; returns at once
    python <skill>/detached.py status [NAME]                running / done (exit code) / died, for one or all runs
    python <skill>/detached.py tail NAME [LINES]            the last lines of its log

Run it from the project root. Each run lives in `<project>/.runs/NAME/` (add `.runs/` to .gitignore; override with RUNS_DIR):
`command.json`, `output.log`, `result.json` (exit code, start and end time). The command runs in the folder it was started from.
A run whose process is gone without a result.json is reported as `died`. A new session reads the status instead of re-running.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path.cwd()
RUNS = Path(os.environ.get("RUNS_DIR") or os.environ.get("THREEDGEN_RUNS_DIR") or ROOT / ".runs")

DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_NO_WINDOW = 0x08000000


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _alive(pid: int) -> bool:
    if os.name == "nt":
        import ctypes

        handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        code = ctypes.c_ulong()
        ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
        ctypes.windll.kernel32.CloseHandle(handle)
        return code.value == 259  # STILL_ACTIVE
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def start(name: str, command: list[str]) -> Path:
    folder = RUNS / name
    if (folder / "command.json").exists() and status_of(name)["state"] == "running":
        raise SystemExit(f"{name} is already running")
    folder.mkdir(parents=True, exist_ok=True)
    for stale in ("result.json", "output.log"):
        (folder / stale).unlink(missing_ok=True)
    flags = DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW if os.name == "nt" else 0
    wrapper = subprocess.Popen(
        [sys.executable, str(Path(__file__).resolve()), "_wrap", name],
        cwd=ROOT, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=flags, close_fds=True, start_new_session=os.name != "nt",
    )
    (folder / "command.json").write_text(json.dumps(
        {"command": command, "cwd": str(ROOT), "started_at": _now(), "pid": wrapper.pid}, indent=2), encoding="utf-8")
    return folder


def _wrap(name: str) -> int:
    folder = RUNS / name
    for _ in range(50):                      # the starter writes command.json right after spawning us
        if (folder / "command.json").exists():
            break
        time.sleep(0.1)
    spec = json.loads((folder / "command.json").read_text(encoding="utf-8"))
    with open(folder / "output.log", "wb") as log:
        code = subprocess.call(spec["command"], cwd=spec["cwd"], stdout=log, stderr=subprocess.STDOUT,
                               stdin=subprocess.DEVNULL)
    (folder / "result.json").write_text(json.dumps(
        {"exit_code": code, "started_at": spec["started_at"], "finished_at": _now()}, indent=2), encoding="utf-8")
    return code


def status_of(name: str) -> dict:
    folder = RUNS / name
    if not (folder / "command.json").exists():
        return {"name": name, "state": "unknown"}
    spec = json.loads((folder / "command.json").read_text(encoding="utf-8"))
    if (folder / "result.json").exists():
        result = json.loads((folder / "result.json").read_text(encoding="utf-8"))
        return {"name": name, "state": "done", **result}
    state = "running" if _alive(int(spec.get("pid", 0))) else "died"
    return {"name": name, "state": state, "started_at": spec.get("started_at")}


def main(argv: list[str]) -> int:
    if not argv or argv[0] in {"-h", "--help"}:
        print(__doc__)
        return 0
    verb, rest = argv[0], argv[1:]
    if verb == "_wrap":
        return _wrap(rest[0])
    if verb == "start":
        if "--" not in rest or rest.index("--") != 1 or len(rest) < 3:
            raise SystemExit("usage: detached.py start NAME -- <command...>")
        folder = start(rest[0], rest[2:])
        print(f"started {rest[0]} -> {folder}")
        return 0
    if verb == "status":
        names = rest or sorted(p.name for p in RUNS.glob("*") if p.is_dir()) if RUNS.exists() else rest
        for name in names:
            print(json.dumps(status_of(name)))
        return 0
    if verb == "watch":
        # Blocks (no output, no tokens) until a run that was running ends, or one appears and ends; then prints the changes.
        # Start it as a background task of the agent: it wakes the agent only when something finishes or dies.
        interval = float(rest[0]) if rest else 60.0
        seen: dict[str, str] = {}
        while True:
            names = sorted(p.name for p in RUNS.glob("*") if p.is_dir()) if RUNS.exists() else []
            now = {name: status_of(name)["state"] for name in names}
            changed = [n for n, s in now.items() if seen.get(n) == "running" and s != "running"]
            if changed:
                for name in changed:
                    print(json.dumps(status_of(name)))
                return 0
            seen = now
            time.sleep(interval)
    if verb == "tail":
        lines = int(rest[1]) if len(rest) > 1 else 30
        log = RUNS / rest[0] / "output.log"
        text = log.read_text(encoding="utf-8", errors="replace") if log.exists() else ""
        print("\n".join(text.splitlines()[-lines:]))
        return 0
    raise SystemExit(f"unknown verb {verb}")


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
