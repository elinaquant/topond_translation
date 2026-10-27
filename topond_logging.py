"""Central logging for the TOPOND tools.

All components write to a single append-only file ``logs/topond.log`` in the
project folder.  When a component is started by the GUI, the GUI is the only
process that writes to the file: child processes log to stderr and the GUI
captures that output and mirrors it into the shared log.

Error codes
-----------
GUI  E001 uncaught UI exception, E002 process launch failure,
     E003 no window / no Unicode fonts, E004 cannot kill process,
     E005 process exited with a non-zero code.
VIEW E001 cell PDB missing, E002 local JSmol files missing, E003 HTTP
     server bind failure, E004 export/screenshot failure, E005 PySide6 or
     QtWebEngine unavailable, E006 viewer data/load failure, E007 download
     handler unavailable; W001 HTTP 404.
EXP  E001 space-group table failure, E002 invalid --bp-angle, E003 input
     missing, E004 cannot read input, E005 export data error, E006 cannot
     write summary, E007 no critical points, E008 invalid arguments;
     W001 export warning.
SUP  E001 input missing, E002 parse/build failure, E003 cannot create output
     directory, E004 cannot write summary, E005 invalid arguments;
     W001 supercell warning.
DED  E001 input missing, E002 output equals input, E003 processing failure,
     E004 cannot write output, E005 verification problem, E006 invalid
     arguments.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import threading
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).resolve().parent

LOG_FILE_ENV = "TOPOND_LOG_FILE"
LOG_LEVEL_ENV = "TOPOND_LOG_LEVEL"
LOG_MAX_ENV = "TOPOND_LOG_MAX_BYTES"
LOG_CHILD_ENV = "TOPOND_GUI_CHILD"

DEFAULT_LOG_FILE = BASE_DIR / "logs" / "topond.log"
DEFAULT_MAX_BYTES = 10 * 1024 * 1024

_FILE_FORMAT = "%(asctime)s.%(msecs)03d %(levelname)-8s [%(name)s] %(message)s"
_STREAM_FORMAT = "[%(name)s] %(message)s"
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


def log_path() -> Path:
    raw = os.environ.get(LOG_FILE_ENV)
    return Path(raw).expanduser() if raw else DEFAULT_LOG_FILE


def is_child() -> bool:
    return os.environ.get(LOG_CHILD_ENV, "").strip().lower() in {"1", "true", "yes"}


def _log_level(default: str = "INFO") -> int:
    raw = os.environ.get(LOG_LEVEL_ENV, default).strip().upper()
    return getattr(logging, raw, logging.INFO)


def _max_bytes() -> int:
    raw = os.environ.get(LOG_MAX_ENV)
    if not raw:
        return DEFAULT_MAX_BYTES
    try:
        return max(0, int(raw))
    except ValueError:
        return DEFAULT_MAX_BYTES


def _rotate_if_needed(path: Path) -> None:
    limit = _max_bytes()
    if limit <= 0:
        return
    try:
        if path.is_file() and path.stat().st_size >= limit:
            backup = path.with_name(path.name + ".1")
            try:
                os.replace(path, backup)
            except OSError:
                pass
    except OSError:
        pass


def get_logger(component: str) -> logging.Logger:
    return logging.getLogger(f"topond.{component}")


def setup(
    component: str,
    *,
    level: int | None = None,
    log_file: Path | None = None,
    child: bool | None = None,
) -> logging.Logger:
    logger = get_logger(component)
    if getattr(logger, "_topond_configured", False):
        return logger

    logger.setLevel(_log_level() if level is None else level)
    logger.propagate = False

    stream = logging.StreamHandler(sys.stderr)
    stream.setLevel(logging.DEBUG)
    stream.setFormatter(logging.Formatter(_STREAM_FORMAT))
    logger.addHandler(stream)

    if child is None:
        child = is_child()
    if not child:
        path = Path(log_file).expanduser() if log_file else log_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            _rotate_if_needed(path)
            file_handler = logging.FileHandler(
                path, mode="a", encoding="utf-8", delay=False
            )
            file_handler.setLevel(logging.DEBUG)
            file_handler.setFormatter(
                logging.Formatter(_FILE_FORMAT, datefmt=_DATE_FORMAT)
            )
            logger.addHandler(file_handler)
        except OSError:
            pass

    logger._topond_configured = True  # type: ignore[attr-defined]
    return logger


def _value(value: Any) -> str:
    if isinstance(value, Path):
        value = str(value)
    if isinstance(value, str):
        if value == "" or any(ch.isspace() for ch in value):
            return json.dumps(value, ensure_ascii=False)
        return value
    return str(value)


def _fields(fields: dict[str, Any]) -> str:
    parts = []
    for key, value in fields.items():
        if value is None:
            continue
        parts.append(f"{key}={_value(value)}")
    return " ".join(parts)


def log_action(logger: logging.Logger, action: str, **fields: Any) -> None:
    details = _fields(fields)
    message = f"action={action}"
    if details:
        message += " " + details
    logger.info(message)


def log_error(
    logger: logging.Logger,
    code: str,
    message: str,
    exc: BaseException | None = None,
    **fields: Any,
) -> None:
    details = _fields(fields)
    text = f"code={code} {message}"
    if details:
        text += " " + details
    if exc is not None:
        text += f" exc={type(exc).__name__}: {exc}"
        errno = getattr(exc, "errno", None)
        if errno is not None:
            text += f" errno={errno}"
        logger.error(
            text, exc_info=(type(exc), exc, exc.__traceback__)
        )
    else:
        logger.error(text)


def install_excepthook(
    logger: logging.Logger,
    code: str = "APP-E001",
    *,
    root: Any | None = None,
) -> None:
    def sys_hook(exc_type, exc, tb) -> None:
        if exc is None:
            exc = exc_type("unknown error")
        log_error(logger, code, "uncaught exception", exc=exc)

    sys.excepthook = sys_hook

    def thread_hook(args: Any) -> None:
        exc = getattr(args, "exc_value", None)
        if exc is None:
            return
        log_error(
            logger,
            code,
            "uncaught thread exception",
            exc=exc,
            thread=getattr(args, "thread", None)
            and getattr(args.thread, "name", None),
        )

    try:
        threading.excepthook = thread_hook
    except Exception:
        pass

    if root is not None:
        def tk_hook(exc_type, exc, tb) -> None:
            if exc is None:
                exc = exc_type("unknown error")
            log_error(logger, code, "uncaught UI exception", exc=exc)

        try:
            root.report_callback_exception = tk_hook
        except Exception:
            pass
