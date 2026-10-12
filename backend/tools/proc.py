"""
Safe subprocess execution for scientific tools.

Rules this module enforces for every external executable:
  * argv list only, never a shell, so user-influenced strings cannot inject
    commands;
  * the child runs in its own process group and is killed as a group on
    timeout or cancellation (tools like Vina spawn worker threads, and some
    wrappers spawn children);
  * stdout/stderr are streamed to a log file (artifact) instead of held in
    memory, and a bounded tail is returned for error messages;
  * the environment is built explicitly: only what the tool needs is passed.
"""
from __future__ import annotations

import dataclasses
import os
import signal
import subprocess
import threading
import time
from pathlib import Path

from ..logging_config import get_logger

log = get_logger("proc")


class ToolError(RuntimeError):
    """An external tool failed (non-zero exit, timeout, missing output)."""

    def __init__(self, message: str, *, returncode: int | None = None, tail: str = ""):
        super().__init__(message)
        self.returncode = returncode
        self.tail = tail


class ToolTimeout(ToolError):
    pass


class ToolCancelled(ToolError):
    pass


@dataclasses.dataclass
class ProcResult:
    returncode: int
    seconds: float
    tail: str


def _tail(path: Path, max_bytes: int = 4000) -> str:
    try:
        size = path.stat().st_size
        with path.open("rb") as fh:
            fh.seek(max(0, size - max_bytes))
            return fh.read().decode("utf-8", errors="replace")
    except OSError:
        return ""


def base_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    """Minimal environment for a child process (no inherited secrets)."""
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": os.environ.get("HOME", "/tmp"),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
    }
    env.update(extra or {})
    return env


def run(
    argv: list[str],
    *,
    log_path: Path,
    timeout: float,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    cancel_event: threading.Event | None = None,
    check: bool = True,
) -> ProcResult:
    """Run `argv`, logging output to `log_path`. Raises ToolError on failure."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    with log_path.open("ab") as out:
        out.write(f"$ {' '.join(argv)}\n".encode())
        out.flush()
        proc = subprocess.Popen(  # noqa: S603 — argv list, no shell
            argv,
            stdout=out,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            cwd=str(cwd) if cwd else None,
            env=env if env is not None else base_env(),
            start_new_session=True,  # own process group => killpg works
        )
        deadline = started + timeout
        try:
            while True:
                try:
                    proc.wait(timeout=0.5)
                    break
                except subprocess.TimeoutExpired:
                    pass
                if cancel_event is not None and cancel_event.is_set():
                    _kill_group(proc)
                    raise ToolCancelled("Cancelled by user", tail=_tail(log_path))
                if time.monotonic() > deadline:
                    _kill_group(proc)
                    raise ToolTimeout(
                        f"Timed out after {int(timeout)}s", tail=_tail(log_path)
                    )
        except BaseException:
            if proc.poll() is None:
                _kill_group(proc)
            raise
    result = ProcResult(proc.returncode, time.monotonic() - started, _tail(log_path))
    if check and proc.returncode != 0:
        raise ToolError(
            f"{Path(argv[0]).name} exited with code {proc.returncode}",
            returncode=proc.returncode,
            tail=result.tail,
        )
    return result


def _kill_group(proc: subprocess.Popen) -> None:
    try:
        os.killpg(proc.pid, signal.SIGTERM)
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait(timeout=5)
    except ProcessLookupError:
        pass


def kill_orphans(marker: str) -> int:
    """Kill tool processes left behind by a previous (crashed) server.

    Tools run in their own session so they survive a hard kill of the API
    process; on the next start they could still be writing into a batch
    directory the resumed batch is about to reuse. Any process whose command
    line mentions `marker` (the runs directory of THIS installation) is
    terminated. Returns the number killed. Linux (/proc) only; elsewhere a no-op.
    """
    proc_dir = Path("/proc")
    if not proc_dir.is_dir():
        return 0
    me = os.getpid()
    killed = 0
    for entry in proc_dir.iterdir():
        if not entry.name.isdigit() or int(entry.name) == me:
            continue
        try:
            cmd = (entry / "cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace")
        except OSError:
            continue
        if marker in cmd:
            try:
                os.killpg(int(entry.name), signal.SIGKILL)
                killed += 1
            except (ProcessLookupError, PermissionError):
                pass
    if killed:
        log.warning("killed %d orphaned tool process(es) from a previous run", killed)
    return killed
