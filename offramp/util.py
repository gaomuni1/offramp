"""Decimal helpers, console output and the single-instance lock."""
from __future__ import annotations

import fcntl
import sys
from datetime import datetime
from decimal import ROUND_DOWN, ROUND_HALF_UP, ROUND_UP, Decimal, InvalidOperation, getcontext
from pathlib import Path

getcontext().prec = 28

ZERO = Decimal(0)


def dec(x) -> Decimal:
    """Exchange strings / config values -> Decimal. Empty means zero."""
    if x is None or x == "":
        return ZERO
    if isinstance(x, Decimal):
        return x
    try:
        return Decimal(str(x))
    except InvalidOperation as e:
        raise ValueError(f"不是有效数字:{x!r}") from e


def floor_step(x: Decimal, step: Decimal) -> Decimal:
    return (x / step).to_integral_value(rounding=ROUND_DOWN) * step


def ceil_step(x: Decimal, step: Decimal) -> Decimal:
    return (x / step).to_integral_value(rounding=ROUND_UP) * step


def round_step(x: Decimal, step: Decimal) -> Decimal:
    return (x / step).to_integral_value(rounding=ROUND_HALF_UP) * step


def fmt(x: Decimal | None, places: int | None = None) -> str:
    """Plain decimal string without exponent; '-' for None."""
    if x is None:
        return "-"
    if places is not None:
        return format(x.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP), "f")
    if x == 0:
        return "0"
    return format(x.normalize(), "f")


class Console:
    """Timestamped, coloured terminal output that is also appended to a log file."""

    COLORS = {"ok": "\033[32m", "warn": "\033[33m", "error": "\033[31m", "head": "\033[1m"}
    RESET = "\033[0m"

    def __init__(self, log_path: Path | None = None, color: bool | None = None, echo: bool = True):
        self.log_path = log_path
        self.color = sys.stdout.isatty() if color is None else color
        self.echo = echo
        self.lines: list[str] = []

    def _write(self, level: str, msg: str, stamp: bool = True) -> None:
        line = f"[{datetime.now():%H:%M:%S}] {msg}" if stamp else msg
        self.lines.append(line)
        if self.echo:
            c = self.COLORS.get(level, "")
            print(f"{c}{line}{self.RESET}" if self.color and c else line, flush=True)
        if self.log_path is not None:
            with open(self.log_path, "a", encoding="utf-8") as f:
                f.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} {level.upper():5} {msg}\n")

    def info(self, msg: str) -> None:
        self._write("info", msg)

    def ok(self, msg: str) -> None:
        self._write("ok", msg)

    def warn(self, msg: str) -> None:
        self._write("warn", msg)

    def error(self, msg: str) -> None:
        self._write("error", msg)

    def head(self, msg: str) -> None:
        self._write("head", msg, stamp=False)

    def plain(self, msg: str = "") -> None:
        self._write("info", msg, stamp=False)


class InstanceLock:
    """Only one trading command may run at a time (two processes would hedge twice)."""

    def __init__(self, path: Path):
        self.path = path
        self._f = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._f = open(self.path, "w")
        try:
            fcntl.flock(self._f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self._f.close()
            raise SystemExit("另一个 offramp 命令正在运行。会下单的命令同一时间只能跑一个,先关掉另一个。")
        return self

    def __exit__(self, *exc):
        fcntl.flock(self._f, fcntl.LOCK_UN)
        self._f.close()
