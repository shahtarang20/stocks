"""Soak driver (plan section 8, W15.3): keep a Windows host running for hours while yield, sleep and reboot events are injected, and
record every health probe. WRITTEN, NOT RUN. The logic is pure (injected clock, probe and injectors) so it is unit-tested without
a GPU; the command line wires it to real commands taken from the environment.

    python tests/windows/soak.py --hours 24 --state e2e-artifacts/soak-state.json --report e2e-artifacts/soak-report.json

Environment (all commands run without a shell, split like a command line; none contains a secret):
  CC_SOAK_PROBE_CMD       prints one JSON object, e.g. {"service": "running", "distro_ok": true, "gpu_ok": true}
  CC_SOAK_YIELD_ON_CMD    make the owner "use the GPU" (the host must stop claiming)      CC_SOAK_YIELD_OFF_CMD  undo it
  CC_SOAK_SLEEP_CMD       put the PC to sleep and wake it (blocks until it is back)
  CC_SOAK_REBOOT_CMD      reboot the PC; the driver resumes from --state when the runner is back
An event whose command is not set is reported as NOT INJECTED and the soak is INCOMPLETE (not passed) unless --allow-skipped.
A reboot kills this process: the state file records the next event index, and running the same command again resumes.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

EVENTS = ("yield", "sleep", "reboot")


@dataclass
class SoakResult:
    polls: int = 0
    unhealthy: list[dict] = field(default_factory=list)
    injected: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    elapsed_hours: float = 0.0

    def passed(self, *, allow_skipped: bool = False) -> bool:
        return self.polls > 0 and not self.unhealthy and (allow_skipped or not self.skipped)


def schedule(hours: float, events: tuple[str, ...] = EVENTS, per_event: int = 3) -> list[tuple[float, str]]:
    """(seconds from start, event) evenly spread over the middle 80% of the run, round-robin over the event kinds."""
    total = hours * 3600.0
    n = per_event * len(events)
    return [(total * (0.1 + 0.8 * (i + 0.5) / n), events[i % len(events)]) for i in range(n)]


def is_healthy(sample: dict) -> bool:
    """Healthy means every field the probe reported says so; a missing field is NOT healthy (L1)."""
    return sample.get("service") == "running" and sample.get("distro_ok") is True and sample.get("gpu_ok") is True


def run_soak(
    hours: float,
    probe: Callable[[], dict],
    inject: dict[str, Optional[Callable[[], None]]],
    *,
    poll_seconds: float = 60.0,
    state_path: Optional[Path] = None,
    clock: Callable[[], float] = time.time,
    sleep: Callable[[float], None] = time.sleep,
) -> SoakResult:
    plan = schedule(hours)
    state = {"started": clock(), "next_event": 0, "result": {}}
    if state_path and state_path.exists():
        state = json.loads(state_path.read_text(encoding="utf-8"))
    res = SoakResult(**state.get("result", {})) if state.get("result") else SoakResult()
    started = float(state["started"])
    end = started + hours * 3600.0

    def save() -> None:
        if state_path:
            state["result"] = res.__dict__
            state_path.parent.mkdir(parents=True, exist_ok=True)
            state_path.write_bytes((json.dumps(state, indent=2) + "\n").encode("utf-8"))

    while clock() < end:
        elapsed = clock() - started
        idx = int(state["next_event"])
        if idx < len(plan) and elapsed >= plan[idx][0]:
            kind = plan[idx][1]
            state["next_event"] = idx + 1
            action = inject.get(kind)
            if action is None:
                res.skipped.append(kind)
            else:
                res.injected.append(kind)
                save()  # before the action: a reboot ends this process
                action()
            save()
        try:
            sample = probe()
        except Exception as exc:  # noqa: BLE001 - a failing probe is an unhealthy sample, not a crash
            sample = {"error": type(exc).__name__}
        res.polls += 1
        if not is_healthy(sample):
            res.unhealthy.append({"at_s": round(clock() - started), "sample": sample})
        save()
        sleep(poll_seconds)
    res.elapsed_hours = round((clock() - started) / 3600.0, 2)
    save()
    return res


def _cmd_runner(name: str) -> Optional[Callable[[], None]]:
    value = os.environ.get(name)
    if not value:
        return None
    from e2e_support import run

    argv = shlex.split(value, posix=(os.name != "nt"))
    return lambda: _must(run(argv, timeout=1800), name)


def _must(result, name: str) -> None:
    if not result.ok:
        raise RuntimeError(f"{name} failed with exit code {result.returncode}")


def _probe_from_env() -> Callable[[], dict]:
    from e2e_support import run

    value = os.environ.get("CC_SOAK_PROBE_CMD")
    if not value:
        raise SystemExit("CC_SOAK_PROBE_CMD is not set")
    argv = shlex.split(value, posix=(os.name != "nt"))

    def probe() -> dict:
        res = run(argv, timeout=120)
        return json.loads(res.stdout.strip().splitlines()[-1]) if res.ok and res.stdout.strip() else {"error": "probe failed"}

    return probe


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--hours", type=float, default=float(os.environ.get("COMPUTECOIN_SOAK_HOURS", "24")))
    ap.add_argument("--poll-seconds", type=float, default=60.0)
    ap.add_argument("--state", type=Path, default=Path("e2e-artifacts/soak-state.json"))
    ap.add_argument("--report", type=Path, default=Path("e2e-artifacts/soak-report.json"))
    ap.add_argument("--allow-skipped", action="store_true")
    a = ap.parse_args(argv)
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    inject = {
        "yield": _compose(_cmd_runner("CC_SOAK_YIELD_ON_CMD"), _cmd_runner("CC_SOAK_YIELD_OFF_CMD"), hold=120.0),
        "sleep": _cmd_runner("CC_SOAK_SLEEP_CMD"),
        "reboot": _cmd_runner("CC_SOAK_REBOOT_CMD"),
    }
    res = run_soak(a.hours, _probe_from_env(), inject, poll_seconds=a.poll_seconds, state_path=a.state)
    a.report.parent.mkdir(parents=True, exist_ok=True)
    a.report.write_bytes((json.dumps({**res.__dict__, "passed": res.passed(allow_skipped=a.allow_skipped)}, indent=2) + "\n").encode("utf-8"))
    print(f"soak: polls={res.polls} unhealthy={len(res.unhealthy)} injected={res.injected} skipped={res.skipped}")
    return 0 if res.passed(allow_skipped=a.allow_skipped) else 1


def _compose(on: Optional[Callable[[], None]], off: Optional[Callable[[], None]], *, hold: float) -> Optional[Callable[[], None]]:
    if on is None or off is None:
        return None

    def both() -> None:
        on()
        time.sleep(hold)
        off()

    return both


if __name__ == "__main__":
    sys.exit(main())
