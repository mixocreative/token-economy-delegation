r"""Run long non-AI work (test suites, builds, GPU/installer walks) as detached processes that outlive the agent session
(Claude Code or Codex), so a usage/credit limit or a closed terminal does not kill it. Run from the project root.

    python <skill>/detached.py start NAME [--max-hours H] [--stall-minutes M] -- <command...>
    python <skill>/detached.py status [NAME]     running / stalled (alive, log silent) / done (exit code, how it ended) / died
    python <skill>/detached.py watch [SECONDS]   sleeps silently; returns when a run ends, dies or stalls (agent background task)
    python <skill>/detached.py stop NAME         ends the run and its whole process tree
    python <skill>/detached.py tail NAME [LINES]

Each run lives in `<project>/.runs/NAME/` (add `.runs/` to .gitignore; override with RUNS_DIR).
A run whose process is gone without a result.json is reported as `died`. A new session reads the status instead of re-running.

Nothing lingers: a run's whole process tree is killed after `--max-hours` (default 6), `stop NAME` ends it at once, `watch`
gives up after 2 h, nothing is registered to start with Windows, and a reboot ends everything (the run then reads `died`).
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


DEFAULT_MAX_HOURS = 6.0       # a run is killed (whole process tree) after this; nothing outlives the job it was started for


def _kill_tree(pid: int) -> None:
    if os.name == "nt":
        subprocess.call(["taskkill", "/T", "/F", "/PID", str(pid)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                        creationflags=CREATE_NO_WINDOW)
    else:
        try:
            os.killpg(os.getpgid(pid), 9)
        except OSError:
            pass


DEFAULT_STALL_MINUTES = 20.0  # a running run whose log has not grown for this long is reported as stalled


def _stalled(folder: Path, spec: dict) -> bool:
    log = folder / "output.log"
    quiet_since = log.stat().st_mtime if log.exists() else time.time()
    return time.time() - quiet_since > float(spec.get("stall_minutes", DEFAULT_STALL_MINUTES)) * 60


def _wrapper_pid(folder: Path, spec: dict) -> int:
    recorded = folder / "wrapper.pid"
    if recorded.exists():
        try:
            return int(recorded.read_text(encoding="utf-8").strip())
        except ValueError:
            pass
    if time.time() - (folder / "command.json").stat().st_mtime < 15:   # the wrapper has not written its pid yet
        return int(spec.get("pid", 0)) or os.getpid()
    return int(spec.get("pid", 0))


def start(name: str, command: list[str], max_hours: float = DEFAULT_MAX_HOURS,
          stall_minutes: float = DEFAULT_STALL_MINUTES) -> Path:
    folder = RUNS / name
    if (folder / "command.json").exists() and status_of(name)["state"] in ("running", "stalled"):
        raise SystemExit(f"{name} is already running")
    folder.mkdir(parents=True, exist_ok=True)
    for stale in ("result.json", "output.log", "stop", "wrapper.pid"):
        (folder / stale).unlink(missing_ok=True)
    flags = DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW if os.name == "nt" else 0
    wrapper = subprocess.Popen(
        [sys.executable, str(Path(__file__).resolve()), "_wrap", name],
        cwd=ROOT, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=flags, close_fds=True, start_new_session=os.name != "nt",
    )
    (folder / "command.json").write_text(json.dumps(
        {"command": command, "cwd": str(ROOT), "started_at": _now(), "pid": wrapper.pid, "max_hours": max_hours,
         "stall_minutes": stall_minutes},
        indent=2), encoding="utf-8")
    return folder


def stop(name: str) -> dict:
    """Ask the run to stop (its wrapper kills the whole tree within seconds); force-kill the wrapper's tree if it does not."""
    folder = RUNS / name
    if status_of(name)["state"] not in ("running", "stalled"):
        return status_of(name)
    (folder / "stop").write_text(_now(), encoding="utf-8")
    deadline = time.time() + 20
    while time.time() < deadline and status_of(name)["state"] in ("running", "stalled"):
        time.sleep(0.5)
    if status_of(name)["state"] in ("running", "stalled"):
        spec = json.loads((folder / "command.json").read_text(encoding="utf-8"))
        _kill_tree(_wrapper_pid(folder, spec))
    return status_of(name)


def _wrap(name: str) -> int:
    folder = RUNS / name
    for _ in range(50):                      # the starter writes command.json right after spawning us
        if (folder / "command.json").exists():
            break
        time.sleep(0.1)
    spec = json.loads((folder / "command.json").read_text(encoding="utf-8"))
    # The pid the starter saw may be a launcher stub (venv / py.exe / Store alias) that exits at once; record our own.
    (folder / "wrapper.pid").write_text(str(os.getpid()), encoding="utf-8")
    deadline = time.time() + float(spec.get("max_hours", DEFAULT_MAX_HOURS)) * 3600
    ended = "finished"
    with open(folder / "output.log", "wb") as log:
        child = subprocess.Popen(spec["command"], cwd=spec["cwd"], stdout=log, stderr=subprocess.STDOUT,
                                 stdin=subprocess.DEVNULL, start_new_session=os.name != "nt",
                                 creationflags=CREATE_NO_WINDOW if os.name == "nt" else 0)   # never pop a console window
        while child.poll() is None:
            if (folder / "stop").exists():
                ended = "stopped"
            elif time.time() > deadline:
                ended = "timed out"
            if ended != "finished":
                _kill_tree(child.pid)
                child.wait(timeout=30)
                break
            time.sleep(2)
        code = child.returncode
    (folder / "result.json").write_text(json.dumps(
        {"exit_code": code, "ended": ended, "started_at": spec["started_at"], "finished_at": _now()}, indent=2),
        encoding="utf-8")
    return code


def status_of(name: str) -> dict:
    folder = RUNS / name
    if not (folder / "command.json").exists():
        return {"name": name, "state": "unknown"}
    spec = json.loads((folder / "command.json").read_text(encoding="utf-8"))
    if (folder / "result.json").exists():
        result = json.loads((folder / "result.json").read_text(encoding="utf-8"))
        return {"name": name, "state": "done", **result}
    state = "running" if _alive(_wrapper_pid(folder, spec)) else "died"
    if state == "running" and _stalled(folder, spec):
        state = "stalled"                                   # alive but silent too long: look at it, maybe `stop` it
    return {"name": name, "state": state, "started_at": spec.get("started_at")}


def main(argv: list[str]) -> int:
    if not argv or argv[0] in {"-h", "--help"}:
        print(__doc__)
        return 0
    verb, rest = argv[0], argv[1:]
    if verb == "_wrap":
        return _wrap(rest[0])
    if verb == "start":
        if "--" not in rest or len(rest) < 3:
            raise SystemExit("usage: detached.py start NAME [--max-hours H] -- <command...>")
        cut = rest.index("--")
        head, command = rest[:cut], rest[cut + 1:]
        opts = dict(zip(head[1::2], head[2::2]))
        if not command or len(head) % 2 != 1 or set(opts) - {"--max-hours", "--stall-minutes"}:
            raise SystemExit("usage: detached.py start NAME [--max-hours H] [--stall-minutes M] -- <command...>")
        max_hours = float(opts.get("--max-hours", DEFAULT_MAX_HOURS))
        folder = start(head[0], command, max_hours, float(opts.get("--stall-minutes", DEFAULT_STALL_MINUTES)))
        print(f"started {head[0]} (killed after {max_hours:g} h) -> {folder}")
        return 0
    if verb == "stop":
        print(json.dumps(stop(rest[0])))
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
        give_up = time.time() + (float(rest[1]) if len(rest) > 1 else 2.0) * 3600   # a watcher never lingers past 2 h
        seen: dict[str, str] = {}
        while time.time() < give_up:
            names = sorted(p.name for p in RUNS.glob("*") if p.is_dir()) if RUNS.exists() else []
            now = {name: status_of(name)["state"] for name in names}
            changed = [n for n, s in now.items() if seen.get(n) in ("running", "stalled") and s != seen.get(n)]
            if changed:
                for name in changed:
                    print(json.dumps(status_of(name)))
                return 0
            seen = now
            time.sleep(interval)
        print(json.dumps({"watch": "expired", "note": "re-arm the watcher if runs are still going"}))
        return 0
    if verb == "tail":
        lines = int(rest[1]) if len(rest) > 1 else 30
        log = RUNS / rest[0] / "output.log"
        text = log.read_text(encoding="utf-8", errors="replace") if log.exists() else ""
        print("\n".join(text.splitlines()[-lines:]))
        return 0
    raise SystemExit(f"unknown verb {verb}")


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
