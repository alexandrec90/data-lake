#!/usr/bin/env python3
"""The local end-to-end tier: every test that needs this workstation, not a runner.

The contract `.claude/rules/local-e2e.md` states, which devkit's nightly `devkit-local-e2e`
job runs in this checkout: exit 0 green, anything else red, each failure printed as a
`FAILED <id>` line. It runs the tests marked `local`, which `addopts` deselects from every
other run, because they reach the real SeaweedFS pool through credentials only a pool PC
holds.

**A skip is a failure under the scheduler** (`DEVKIT_E2E` set): the checkout is the one
the tests were written for, so nothing has a reason left to skip. By hand a skip is listed
and does not fail the run by itself.

**A run that executed nothing is red**, whoever ran it, a PC outside the pool included: a
green from zero tests is a skip reported as a pass.

**A fixture error is printed as `FAILED` too.** pytest's own line for one says `ERROR`,
and the failing id is what the fix pass groups by, so a pool that stopped answering would
otherwise be signed by an exit code alone.

**It goes through `uv run --all-extras --frozen`.** The tier needs both optional extras,
and a checkout's `.venv` need not hold them; a missing one would skip the whole tier.
`--frozen` because the pass requires this checkout free of tracked edits, and `uv run`
would otherwise be free to rewrite `uv.lock`.

Stdlib only, for `.claude/rules/task-scripts.md`'s reason: the caller may be the machine's
Python, not this project's. The junit report stays in `logs/e2e-junit.xml`.

Usage:
    python scripts/e2e.py                                   # the whole tier
    python scripts/e2e.py tests/test_e2e_pool.py::test_x    # those alone, still in the tier
"""

from __future__ import annotations

import os
import subprocess
import sys
import xml.etree.ElementTree as ET
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
REPORT = REPO_ROOT / "logs" / "e2e-junit.xml"

# Spelled as devkit's `e2e-pass.ENV_FLAG` spells it.
ENV_FLAG = "DEVKIT_E2E"
MARKER = "local"

PASSED = "passed"
FAILED = "failed"
SKIPPED = "skipped"

# Windows opens a console for a console program a window-less parent starts, so a nightly
# run would put one on the desktop. 0 everywhere else, and absent from `subprocess` there.
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


@dataclass(frozen=True)
class Case:
    """One test from the junit report, named as pytest's summary names it."""

    ident: str
    outcome: str
    message: str = ""


def pytest_id(classname: str, name: str) -> str:
    """`tests.test_x.TestY` + `test_z` as `tests/test_x.py::TestY::test_z`. Pure.

    A collection error has no classname and holds the module as its name, which is
    already the best id there is.
    """
    parts = classname.split(".") if classname else []
    modules = [i for i, part in enumerate(parts) if part.startswith("test_")]
    if not modules:
        return name
    module = modules[0] + 1
    return "::".join(["/".join(parts[:module]) + ".py", *parts[module:], name])


def cases(report: Path) -> list[Case]:
    """Every case in a pytest junit report; unreadable is none, an empty run to `verdict`."""
    try:
        root = ET.parse(report).getroot()
    except (OSError, ET.ParseError):
        return []
    found = []
    for case in root.iter("testcase"):
        ident = pytest_id(case.get("classname", ""), case.get("name", "?"))
        ended = next((e for e in case if e.tag in ("failure", "error", "skipped")), None)
        if ended is None:
            found.append(Case(ident, PASSED))
        else:
            outcome = SKIPPED if ended.tag == SKIPPED else FAILED
            found.append(Case(ident, outcome, ended.get("message", "")))
    return found


def _first_line(message: str) -> str:
    return message.strip().splitlines()[0] if message.strip() else "no reason given"


def verdict(code: int, found: Sequence[Case], strict: bool) -> tuple[int, list[str]]:
    """`(exit code, lines to print)` for pytest's `code` and the cases it reported. Pure.

    pytest printed its own `FAILED` lines, which `failed` here repeats: its report does
    not say which of them were `ERROR`, and a repeat of an id is the same signature.
    """
    skips = [case for case in found if case.outcome == SKIPPED]
    ran = [case for case in found if case.outcome != SKIPPED]
    lines = [f"FAILED {c.ident} - {_first_line(c.message)}" for c in ran if c.outcome == FAILED]
    word = "FAILED" if strict else "SKIPPED"
    lines += [f"{word} {c.ident} - skipped: {_first_line(c.message)}" for c in skips]
    if not ran:
        lines.append(f"e2e: no {MARKER} test executed, which is not a pass")
        return (code or 1), lines
    if strict and skips:
        lines.append(f"e2e: {len(skips)} test(s) skipped under the scheduled run")
        return (code or 1), lines
    return code, lines


def pytest_argv(report: Path, named: Sequence[str] = ()) -> list[str]:
    """The argv to run. Pure. `-m` comes after `addopts`, so it replaces their `not local`."""
    return [
        "uv",
        "run",
        "--all-extras",
        "--frozen",
        "python",
        "-m",
        "pytest",
        *(named or ["tests"]),
        "-m",
        MARKER,
        "--tb=short",
        "-q",
        "-rfE",
        # This tier must not rewrite the last-failed set `run-tests.py --changed` reads.
        "-p",
        "no:cacheprovider",
        f"--junitxml={report}",
    ]


def _printable(text: str) -> str:
    """ASCII, so a by-hand run on a cp1252 console cannot die printing a traceback."""
    return text.encode("ascii", "backslashreplace").decode("ascii")


def main(
    argv: list[str] | None = None,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    env: Mapping[str, str] = os.environ,
) -> int:
    named = sys.argv[1:] if argv is None else argv
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.unlink(missing_ok=True)
    # Captured and re-printed rather than inherited: a window-less child that captures
    # nothing writes to its hidden console, not to the pipe the pass reads.
    done = runner(
        pytest_argv(REPORT, named),
        cwd=REPO_ROOT,
        env={**env, "PYTHONUTF8": "1"},
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        creationflags=NO_WINDOW,
    )
    print(_printable(done.stdout or ""), end="", flush=True)
    print(_printable(done.stderr or ""), end="", file=sys.stderr, flush=True)
    code, lines = verdict(done.returncode, cases(REPORT), bool(env.get(ENV_FLAG)))
    for line in lines:
        print(_printable(line), flush=True)
    print(f"e2e: report in {REPORT.relative_to(REPO_ROOT).as_posix()}", flush=True)
    return code


if __name__ == "__main__":
    sys.exit(main())
