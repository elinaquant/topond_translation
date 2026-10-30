#!/usr/bin/env python3
"""Russian-language GUI for the TOPOND export pipeline."""

from __future__ import annotations

import importlib.util
import json
import math
import os
import queue
import re
import shlex
import signal
import subprocess
import sys
import threading
import time
import traceback
from pathlib import Path

import tkinter as tk
from tkinter import filedialog, font, messagebox, ttk

from topond_logging import (
    get_logger,
    install_excepthook,
    log_action,
    log_error,
    log_path,
    setup,
)

BASE_DIR = Path(__file__).resolve().parent
LOGGER = get_logger("gui")


def _child_env() -> dict[str, str]:
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    env["TOPOND_GUI_CHILD"] = "1"
    env["TOPOND_LOG_FILE"] = str(log_path())
    env.setdefault("TOPOND_LOG_LEVEL", "INFO")
    return env


def _find_script(*relative_paths: str) -> Path:
    for relative in relative_paths:
        path = BASE_DIR / relative
        if path.is_file():
            return path
    return BASE_DIR / relative_paths[0]


DEDUP_SCRIPT = _find_script(
    "Очистка от дубликатов/dedup_cp_bps.py",
    "dedup_cp_bps.py",
)
EXPORT_SCRIPT = BASE_DIR / "topond_cp_export.py"
SUPERCELL_SCRIPT = BASE_DIR / "make_supercell.py"
VIEWER_SCRIPT = BASE_DIR / "topond_jsmol_viewer.py"

WORKER_PYTHON_ENV = "TOPOND_WORKER_PYTHON"
GUI_PYTHON_ENV = "TOPOND_GUI_PYTHON"
GUI_REEXEC_ENV = "TOPOND_GUI_REEXEC"

UI_FONT_CANDIDATES = (
    "Arial",
    "Segoe UI",
    "Noto Sans",
    "Noto Sans Display",
    "DejaVu Sans",
    "Liberation Sans",
    "Ubuntu",
    "Ubuntu Sans",
    "Cantarell",
    "Helvetica Neue",
    "Helvetica",
    "Lucida Grande",
    "Tahoma",
    "Verdana",
    "Nimbus Sans",
)
MONO_FONT_CANDIDATES = (
    "DejaVu Sans Mono",
    "Noto Sans Mono",
    "Liberation Mono",
    "Ubuntu Mono",
    "Consolas",
    "Menlo",
    "Monaco",
    "Courier New",
)

COLORS = {
    "bg": "#EFF9F4",
    "bg_alt": "#E3F4EC",
    "surface": "#F9FDFB",
    "field": "#FCFEFD",
    "border": "#A7D7C4",
    "border_soft": "#C8E7DA",
    "text": "#1D3B32",
    "muted": "#4E6F63",
    "accent": "#2F7D68",
    "accent_hover": "#286B59",
    "accent_pressed": "#215B4C",
    "accent_soft": "#CBEBDD",
    "accent_dark": "#174C3E",
    "selection": "#A9DFCB",
    "selection_text": "#13372C",
    "disabled_bg": "#DDEBE5",
    "disabled_text": "#7F9A8F",
    "success": "#247A54",
    "error": "#A33F3A",
    "log_bg": "#F3FBF7",
    "trough": "#D8EEE5",
}

_FONT_PROBE = (
    "import tkinter as tk\n"
    "from tkinter import font\n"
    f"known = set({UI_FONT_CANDIDATES!r})\n"
    "root = tk.Tk()\n"
    "root.withdraw()\n"
    "families = set(font.families(root))\n"
    "root.destroy()\n"
    "print('1' if families & known or len(families) > 10 else '0')\n"
)
_SPGLIB_PROBE = "import spglib\nprint('1')\n"


class ValidationError(Exception):
    pass


def worker_python() -> str:
    return os.environ.get(WORKER_PYTHON_ENV) or sys.executable


def _configure_fonts(root: tk.Misc) -> None:
    families = set(font.families(root))
    ui_family = next(
        (name for name in UI_FONT_CANDIDATES if name in families),
        None,
    )
    mono_family = next(
        (name for name in MONO_FONT_CANDIDATES if name in families),
        None,
    )
    if ui_family is not None:
        for name in (
            "TkDefaultFont",
            "TkTextFont",
            "TkMenuFont",
            "TkHeadingFont",
            "TkCaptionFont",
            "TkSmallCaptionFont",
            "TkIconFont",
            "TkTooltipFont",
        ):
            try:
                font.nametofont(name, root).configure(family=ui_family)
            except tk.TclError:
                continue
    if mono_family is not None:
        try:
            font.nametofont("TkFixedFont", root).configure(family=mono_family)
        except tk.TclError:
            pass


def _tk_has_unicode_fonts() -> bool | None:
    try:
        root = tk.Tk()
    except tk.TclError:
        return None
    try:
        root.withdraw()
        families = set(font.families(root))
        return bool(families & set(UI_FONT_CANDIDATES)) or len(families) > 10
    finally:
        root.destroy()


def _python_candidates() -> list[str]:
    if os.name == "nt":
        names = ("python.exe", "python3.exe")
    else:
        names = (
            "python3",
            "python3.13",
            "python3.12",
            "python3.11",
            "python3.10",
            "python",
        )
    candidates: list[str] = []
    seen = {os.path.realpath(sys.executable)}

    def add(candidate: str) -> None:
        try:
            path = Path(candidate)
            if not path.is_file():
                return
            real = os.path.realpath(path)
        except OSError:
            return
        if real in seen:
            return
        seen.add(real)
        candidates.append(str(path))

    if os.name != "nt":
        for path in (
            "/usr/bin/python3",
            "/usr/local/bin/python3",
            "/opt/homebrew/bin/python3",
            "/bin/python3",
        ):
            add(path)
    for directory in os.environ.get("PATH", "").split(os.pathsep):
        if not directory:
            continue
        for name in names:
            add(str(Path(directory) / name))
    return candidates


def _probe_python(python: str, code: str) -> bool:
    try:
        result = subprocess.run(
            [python, "-c", code],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=8,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0 and result.stdout.strip() == "1"


def _find_unicode_gui_python() -> str | None:
    override = os.environ.get(GUI_PYTHON_ENV)
    if override and _probe_python(override, _FONT_PROBE):
        return override
    for candidate in _python_candidates():
        if _probe_python(candidate, _FONT_PROBE):
            return candidate
    return None


def _find_worker_python() -> str | None:
    for candidate in _python_candidates():
        if _probe_python(candidate, _SPGLIB_PROBE):
            return candidate
    return None


def _current_has_spglib() -> bool:
    try:
        return importlib.util.find_spec("spglib") is not None
    except (ImportError, ValueError):
        return False


def _ensure_worker_python() -> None:
    if os.environ.get(WORKER_PYTHON_ENV):
        return
    if _current_has_spglib():
        os.environ[WORKER_PYTHON_ENV] = sys.executable
        return
    found = _find_worker_python()
    if found is not None:
        os.environ[WORKER_PYTHON_ENV] = found


def _reexec_with_python(python: str) -> None:
    env = os.environ.copy()
    env[GUI_REEXEC_ENV] = "1"
    if WORKER_PYTHON_ENV not in env:
        if _current_has_spglib():
            env[WORKER_PYTHON_ENV] = sys.executable
        else:
            found = _find_worker_python()
            if found is not None:
                env[WORKER_PYTHON_ENV] = found
    script = str(Path(__file__).resolve())
    os.execve(python, [python, script, *sys.argv[1:]], env)


def _report_font_problem() -> int:
    message = (
        "This Python build has no Unicode-capable Tk fonts, so Russian text "
        "cannot be displayed.\n\n"
        "Use a Python whose Tk is built with fontconfig/Xft, for example:\n"
        "  /usr/bin/python3 topond_gui.py\n\n"
        f"Current Python: {sys.executable}"
    )
    root = None
    try:
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror("GUI font problem", message, parent=root)
    except tk.TclError:
        print(message, file=sys.stderr)
    finally:
        if root is not None:
            root.destroy()
    return 3


def resolve_user_path(text: str) -> Path:
    path = Path(text).expanduser()
    if not path.is_absolute():
        path = BASE_DIR / path
    return path


def format_command(command: list[str]) -> str:
    if os.name == "nt":
        return subprocess.list2cmdline(command)
    return shlex.join(command)


def parse_float_value(
    text: str,
    label: str,
    *,
    minimum: float | None = None,
    maximum: float | None = None,
    minimum_exclusive: bool = False,
) -> float:
    raw = text.strip().replace(",", ".")
    if not raw:
        raise ValidationError(f"Заполните поле «{label}».")
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValidationError(
            f"Поле «{label}» должно быть числом, получено: {text!r}."
        ) from exc
    if not math.isfinite(value):
        raise ValidationError(f"Поле «{label}» должно быть конечным числом.")
    if minimum is not None:
        if minimum_exclusive and value <= minimum:
            raise ValidationError(f"Поле «{label}» должно быть больше {minimum:g}.")
        if not minimum_exclusive and value < minimum:
            raise ValidationError(f"Поле «{label}» должно быть не меньше {minimum:g}.")
    if maximum is not None and value > maximum:
        raise ValidationError(f"Поле «{label}» должно быть не больше {maximum:g}.")
    return value


def parse_float_fields(
    variables: list[tk.StringVar],
    labels: list[str],
    title: str,
) -> list[float]:
    if len(variables) != len(labels):
        raise RuntimeError("internal float-field mismatch")
    values: list[float] = []
    for variable, label in zip(variables, labels):
        try:
            values.append(parse_float_value(variable.get(), label))
        except ValidationError as exc:
            raise ValidationError(f"{title}: {exc}") from exc
    return values


def validate_ranges(values: list[float], title: str) -> None:
    for axis, (low, high) in enumerate(zip(values[0::2], values[1::2])):
        if high <= low:
            axis_name = "XYZ"[axis]
            raise ValidationError(
                f"{title}: для оси {axis_name} верхняя граница должна быть "
                f"больше нижней ({axis_name}1 > {axis_name}0)."
            )


def number_text(value: float) -> str:
    return format(value, ".12g")


def dialog_initial_dir(text: str) -> Path:
    if text.strip():
        path = resolve_user_path(text.strip())
        if path.is_file() or path.suffix:
            return path.parent
        return path
    return BASE_DIR


def set_widget_state(widgets: list[tk.Widget], enabled: bool) -> None:
    state = ["!disabled"] if enabled else ["disabled"]
    for widget in widgets:
        widget.state(state)


class ProcessRunner:
    def __init__(self, events: queue.Queue) -> None:
        self.events = events
        self.process: subprocess.Popen | None = None
        self.thread: threading.Thread | None = None

    def running(self) -> bool:
        return self.process is not None and self.process.poll() is None

    def start(self, command: list[str]) -> None:
        if self.running():
            raise RuntimeError("процесс уже выполняется")
        env = _child_env()
        kwargs: dict = {}
        if os.name == "posix":
            kwargs["start_new_session"] = True
        self.process = subprocess.Popen(
            command,
            cwd=str(BASE_DIR),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            env=env,
            **kwargs,
        )
        log_action(LOGGER, "process_start", pid=self.process.pid,
                   command=" ".join(command))
        self.thread = threading.Thread(
            target=self._read_output,
            args=(self.process,),
            daemon=True,
        )
        self.thread.start()

    def _read_output(self, process: subprocess.Popen) -> None:
        try:
            if process.stdout is not None:
                for line in process.stdout:
                    self.events.put(("line", line.rstrip("\r\n")))
        finally:
            code = process.wait()
            self.events.put(("done", code))

    def stop(self) -> None:
        process = self.process
        if process is None or process.poll() is not None:
            return
        try:
            if os.name == "posix":
                os.killpg(os.getpgid(process.pid), signal.SIGTERM)
            else:
                process.terminate()
        except (OSError, ProcessLookupError, PermissionError):
            try:
                process.terminate()
            except OSError:
                pass

    def kill(self) -> None:
        process = self.process
        if process is None or process.poll() is not None:
            return
        try:
            if os.name == "posix":
                os.killpg(os.getpgid(process.pid), signal.SIGKILL)
            else:
                process.kill()
        except (OSError, ProcessLookupError, PermissionError):
            try:
                process.kill()
            except OSError:
                pass


class SpaceGroupWindow(tk.Toplevel):
    def __init__(
        self,
        app: "App",
        records: list[tuple],
        raw_text: str,
        on_pick,
    ) -> None:
        super().__init__(app)
        self.configure(background=COLORS["bg"])
        self.on_pick = on_pick
        self.title("Пространственные группы")
        self.geometry("780x580")
        self.minsize(620, 420)
        self.transient(app)

        outer = ttk.Frame(self, padding=12)
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(1, weight=1)

        ttk.Label(
            outer,
            text=(
                "Выберите полный символ Hermann–Mauguin без пробелов. "
                "Двойной щелчок вставляет символ в поле."
            ),
        ).grid(row=0, column=0, sticky="w", pady=(0, 8))

        if records:
            columns = ("number", "symbol", "hall", "ops", "choice")
            table_frame = ttk.Frame(outer)
            table_frame.grid(row=1, column=0, sticky="nsew")
            table_frame.columnconfigure(0, weight=1)
            table_frame.rowconfigure(0, weight=1)
            self.tree = ttk.Treeview(
                table_frame,
                columns=columns,
                show="headings",
                selectmode="browse",
            )
            headings = {
                "number": "№",
                "symbol": "Символ",
                "hall": "Hall",
                "ops": "Операций",
                "choice": "Установка",
            }
            widths = {
                "number": 50,
                "symbol": 220,
                "hall": 70,
                "ops": 80,
                "choice": 120,
            }
            anchors = {
                "number": "e",
                "symbol": "w",
                "hall": "e",
                "ops": "e",
                "choice": "w",
            }
            for column in columns:
                self.tree.heading(column, text=headings[column])
                self.tree.column(
                    column,
                    width=widths[column],
                    anchor=anchors[column],
                    stretch=column in ("symbol", "choice"),
                )
            for number, symbol, hall, choice, n_ops in records:
                self.tree.insert(
                    "",
                    "end",
                    values=(number, symbol, hall, n_ops, choice or ""),
                )
            scroll = ttk.Scrollbar(
                table_frame,
                orient="vertical",
                command=self.tree.yview,
            )
            self.tree.configure(yscrollcommand=scroll.set)
            self.tree.grid(row=0, column=0, sticky="nsew")
            scroll.grid(row=0, column=1, sticky="ns")
            self.tree.bind("<Double-1>", self._choose)
            self.tree.bind("<Return>", self._choose)
        else:
            self.tree = None
            text_frame = ttk.Frame(outer)
            text_frame.grid(row=1, column=0, sticky="nsew")
            text_frame.columnconfigure(0, weight=1)
            text_frame.rowconfigure(0, weight=1)
            text = tk.Text(
                text_frame,
                wrap="none",
                font="TkFixedFont",
                background=COLORS["field"],
                foreground=COLORS["text"],
                insertbackground=COLORS["text"],
                selectbackground=COLORS["selection"],
                selectforeground=COLORS["selection_text"],
                relief="flat",
                borderwidth=0,
                highlightthickness=1,
                highlightbackground=COLORS["border"],
                highlightcolor=COLORS["accent"],
                padx=6,
                pady=5,
            )
            scroll_y = ttk.Scrollbar(
                text_frame,
                orient="vertical",
                command=text.yview,
            )
            scroll_x = ttk.Scrollbar(
                text_frame,
                orient="horizontal",
                command=text.xview,
            )
            text.configure(
                yscrollcommand=scroll_y.set,
                xscrollcommand=scroll_x.set,
            )
            text.insert("1.0", raw_text)
            text.configure(state="disabled")
            text.grid(row=0, column=0, sticky="nsew")
            scroll_y.grid(row=0, column=1, sticky="ns")
            scroll_x.grid(row=1, column=0, sticky="ew")

        buttons = ttk.Frame(outer)
        buttons.grid(row=2, column=0, sticky="e", pady=(10, 0))
        if records:
            ttk.Button(
                buttons,
                text="Вставить символ",
                command=self._choose,
                style="Run.TButton",
            ).pack(side="left", padx=(0, 8))
        ttk.Button(buttons, text="Закрыть", command=self.destroy).pack(side="left")

        self.grab_set()
        self.focus_set()
        if self.tree is not None:
            children = self.tree.get_children()
            if children:
                self.tree.selection_set(children[0])
                self.tree.focus(children[0])

    def _choose(self, event=None) -> None:
        if self.tree is None:
            return
        selection = self.tree.selection()
        if not selection:
            return
        values = self.tree.item(selection[0], "values")
        if len(values) >= 2:
            self.on_pick(str(values[1]))
            self.destroy()


class ScrollableTab(ttk.Frame):
    def __init__(self, parent: tk.Misc) -> None:
        super().__init__(parent)
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)
        self.canvas = tk.Canvas(
            self,
            background=COLORS["bg"],
            borderwidth=0,
            highlightthickness=0,
        )
        self.scrollbar = ttk.Scrollbar(
            self,
            orient="vertical",
            command=self.canvas.yview,
        )
        self.canvas.configure(yscrollcommand=self.scrollbar.set)
        self.canvas.grid(row=0, column=0, sticky="nsew")
        self.scrollbar.grid(row=0, column=1, sticky="ns")
        self.content = ttk.Frame(self.canvas)
        self.window = self.canvas.create_window(
            (0, 0),
            window=self.content,
            anchor="nw",
        )
        self.content.bind("<Configure>", self._update_scrollregion)
        self.canvas.bind("<Configure>", self._resize_content)
        self.canvas.bind("<Enter>", self._bind_mousewheel)
        self.canvas.bind("<Leave>", self._unbind_mousewheel)
        self.wheel_bound = False

    def _update_scrollregion(self, event=None) -> None:
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))
        self._toggle_scrollbar()

    def _resize_content(self, event) -> None:
        self.canvas.itemconfigure(self.window, width=event.width)
        self._toggle_scrollbar()

    def _toggle_scrollbar(self) -> None:
        need = self.content.winfo_reqheight() > self.canvas.winfo_height() + 1
        if need:
            self.scrollbar.grid()
        else:
            self.scrollbar.grid_remove()
            self.canvas.yview_moveto(0.0)

    def _bind_mousewheel(self, event=None) -> None:
        if self.wheel_bound:
            return
        self.canvas.bind_all("<MouseWheel>", self._on_mousewheel)
        self.canvas.bind_all("<Button-4>", self._on_mousewheel)
        self.canvas.bind_all("<Button-5>", self._on_mousewheel)
        self.wheel_bound = True

    def _unbind_mousewheel(self, event=None) -> None:
        if not self.wheel_bound:
            return
        self.canvas.unbind_all("<MouseWheel>")
        self.canvas.unbind_all("<Button-4>")
        self.canvas.unbind_all("<Button-5>")
        self.wheel_bound = False

    def _on_mousewheel(self, event) -> None:
        if not self.scrollbar.winfo_ismapped():
            return
        if getattr(event, "num", None) == 4:
            delta = -1
        elif getattr(event, "num", None) == 5:
            delta = 1
        else:
            delta = -1 if event.delta > 0 else 1
        self.canvas.yview_scroll(delta, "units")


class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.configure(background=COLORS["bg"])
        self.events: queue.Queue = queue.Queue()
        self.runner = ProcessRunner(self.events)
        self.run_buttons: list[ttk.Button] = []
        self.success_callback = None
        self.active_title: str | None = None
        self.stop_requested = False
        self.kill_job: str | None = None
        self.last_output_dir: Path | None = None
        self.closing = False

        self.title("TOPOND: CP, BPs и суперячейка")
        self.geometry("1080x820")
        self.minsize(920, 680)
        _configure_fonts(self)
        self._configure_style()
        self._build_ui()
        self._init_defaults()
        self._bind_ui_logging()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(80, self._poll_events)

    def _configure_style(self) -> None:
        c = COLORS
        style = ttk.Style(self)
        if "clam" in style.theme_names():
            style.theme_use("clam")

        style.configure("TFrame", background=c["bg"])
        style.configure("TLabel", background=c["bg"], foreground=c["text"])
        style.configure(
            "TLabelframe",
            background=c["bg"],
            bordercolor=c["border"],
            lightcolor=c["border_soft"],
            darkcolor=c["border"],
            relief="solid",
            borderwidth=1,
            labelmargins=(6, 0, 6, 0),
        )
        style.configure(
            "TLabelframe.Label",
            background=c["bg"],
            foreground=c["accent_dark"],
            font=("TkDefaultFont", 10, "bold"),
        )

        style.configure(
            "TNotebook",
            background=c["bg"],
            bordercolor=c["border"],
            lightcolor=c["border_soft"],
            darkcolor=c["border"],
            borderwidth=1,
            tabmargins=(0, 4, 0, 0),
        )
        style.configure(
            "TNotebook.Tab",
            background=c["bg_alt"],
            foreground=c["muted"],
            bordercolor=c["border_soft"],
            lightcolor=c["border_soft"],
            darkcolor=c["border_soft"],
            padding=(14, 7),
        )
        style.map(
            "TNotebook.Tab",
            background=[
                ("selected", c["accent_soft"]),
                ("active", c["trough"]),
                ("disabled", c["disabled_bg"]),
            ],
            foreground=[
                ("selected", c["accent_dark"]),
                ("active", c["text"]),
                ("disabled", c["disabled_text"]),
            ],
            lightcolor=[
                ("selected", c["accent_soft"]),
                ("active", c["trough"]),
            ],
            darkcolor=[
                ("selected", c["border"]),
                ("active", c["border_soft"]),
            ],
        )

        style.configure(
            "TButton",
            background=c["bg_alt"],
            foreground=c["text"],
            bordercolor=c["border"],
            lightcolor=c["surface"],
            darkcolor=c["border_soft"],
            focuscolor=c["accent_soft"],
            relief="flat",
            borderwidth=1,
            padding=(10, 5),
        )
        style.map(
            "TButton",
            background=[
                ("disabled", c["disabled_bg"]),
                ("pressed", c["accent_soft"]),
                ("active", c["trough"]),
            ],
            foreground=[
                ("disabled", c["disabled_text"]),
                ("pressed", c["accent_dark"]),
                ("active", c["text"]),
            ],
            bordercolor=[
                ("disabled", c["border_soft"]),
                ("pressed", c["accent"]),
                ("active", c["border"]),
            ],
            lightcolor=[
                ("pressed", c["accent_soft"]),
                ("active", c["surface"]),
            ],
            darkcolor=[
                ("pressed", c["border"]),
                ("active", c["border_soft"]),
            ],
        )

        style.configure(
            "Run.TButton",
            background=c["accent"],
            foreground="#FFFFFF",
            bordercolor=c["accent"],
            lightcolor=c["accent"],
            darkcolor=c["accent_pressed"],
            focuscolor=c["accent_soft"],
            font=("TkDefaultFont", 10, "bold"),
            relief="flat",
            borderwidth=1,
            padding=(14, 6),
        )
        style.map(
            "Run.TButton",
            background=[
                ("disabled", c["disabled_bg"]),
                ("pressed", c["accent_pressed"]),
                ("active", c["accent_hover"]),
            ],
            foreground=[
                ("disabled", c["disabled_text"]),
                ("pressed", "#FFFFFF"),
                ("active", "#FFFFFF"),
            ],
            bordercolor=[
                ("disabled", c["border_soft"]),
                ("pressed", c["accent_pressed"]),
                ("active", c["accent_hover"]),
            ],
            lightcolor=[
                ("disabled", c["disabled_bg"]),
                ("pressed", c["accent_pressed"]),
                ("active", c["accent_hover"]),
            ],
            darkcolor=[
                ("disabled", c["border_soft"]),
                ("pressed", c["accent_pressed"]),
                ("active", c["accent_hover"]),
            ],
        )

        style.configure(
            "TEntry",
            fieldbackground=c["field"],
            foreground=c["text"],
            insertcolor=c["text"],
            bordercolor=c["border"],
            lightcolor=c["surface"],
            darkcolor=c["border"],
            selectbackground=c["selection"],
            selectforeground=c["selection_text"],
            padding=(6, 4),
        )
        style.map(
            "TEntry",
            fieldbackground=[
                ("disabled", c["disabled_bg"]),
                ("readonly", c["bg_alt"]),
            ],
            foreground=[
                ("disabled", c["disabled_text"]),
                ("readonly", c["muted"]),
            ],
            bordercolor=[
                ("focus", c["accent"]),
                ("disabled", c["border_soft"]),
            ],
            lightcolor=[
                ("focus", c["accent_soft"]),
                ("disabled", c["border_soft"]),
            ],
            darkcolor=[
                ("focus", c["accent"]),
                ("disabled", c["border_soft"]),
            ],
        )

        style.configure(
            "TSpinbox",
            fieldbackground=c["field"],
            background=c["bg_alt"],
            foreground=c["text"],
            arrowcolor=c["accent_dark"],
            insertcolor=c["text"],
            bordercolor=c["border"],
            lightcolor=c["surface"],
            darkcolor=c["border"],
            selectbackground=c["selection"],
            selectforeground=c["selection_text"],
            padding=(6, 3),
        )
        style.map(
            "TSpinbox",
            fieldbackground=[
                ("disabled", c["disabled_bg"]),
                ("readonly", c["bg_alt"]),
            ],
            background=[
                ("disabled", c["disabled_bg"]),
                ("active", c["trough"]),
            ],
            foreground=[("disabled", c["disabled_text"])],
            arrowcolor=[
                ("disabled", c["disabled_text"]),
                ("active", c["accent"]),
            ],
            bordercolor=[
                ("focus", c["accent"]),
                ("disabled", c["border_soft"]),
            ],
        )

        for widget_style in ("TCheckbutton", "TRadiobutton"):
            style.configure(
                widget_style,
                background=c["bg"],
                foreground=c["text"],
                indicatorbackground=c["field"],
                indicatorforeground=c["accent"],
                upperbordercolor=c["border"],
                lowerbordercolor=c["border_soft"],
                focuscolor=c["accent_soft"],
                padding=(2, 2),
            )
            style.map(
                widget_style,
                background=[("active", c["trough"])],
                foreground=[("disabled", c["disabled_text"])],
                indicatorbackground=[
                    ("disabled", c["disabled_bg"]),
                    ("selected", c["accent"]),
                    ("active", c["accent_soft"]),
                ],
                indicatorforeground=[
                    ("selected", "#FFFFFF"),
                    ("disabled", c["disabled_text"]),
                ],
                upperbordercolor=[
                    ("selected", c["accent"]),
                    ("active", c["border"]),
                ],
                lowerbordercolor=[
                    ("selected", c["accent"]),
                    ("active", c["border_soft"]),
                ],
            )

        style.configure(
            "TScrollbar",
            background=c["accent_soft"],
            troughcolor=c["trough"],
            bordercolor=c["border_soft"],
            arrowcolor=c["accent_dark"],
            lightcolor=c["accent_soft"],
            darkcolor=c["accent_soft"],
            gripcount=0,
        )
        style.map(
            "TScrollbar",
            background=[
                ("disabled", c["disabled_bg"]),
                ("pressed", c["accent"]),
                ("active", c["border"]),
            ],
            arrowcolor=[("disabled", c["disabled_text"])],
        )

        style.configure(
            "Treeview",
            background=c["surface"],
            fieldbackground=c["surface"],
            foreground=c["text"],
            bordercolor=c["border"],
            lightcolor=c["surface"],
            darkcolor=c["border"],
            borderwidth=1,
            rowheight=25,
        )
        style.map(
            "Treeview",
            background=[("selected", c["selection"])],
            foreground=[("selected", c["selection_text"])],
        )
        style.configure(
            "Treeview.Heading",
            background=c["accent_soft"],
            foreground=c["accent_dark"],
            bordercolor=c["border"],
            lightcolor=c["accent_soft"],
            darkcolor=c["border_soft"],
            relief="flat",
            padding=(8, 5),
            font=("TkDefaultFont", 10, "bold"),
        )
        style.map(
            "Treeview.Heading",
            background=[
                ("active", c["border_soft"]),
                ("pressed", c["border"]),
            ],
        )

        style.configure(
            "TProgressbar",
            background=c["accent"],
            troughcolor=c["trough"],
            bordercolor=c["border_soft"],
            lightcolor=c["accent_soft"],
            darkcolor=c["accent"],
            thickness=14,
        )

        style.configure(
            "Title.TLabel",
            background=c["bg"],
            foreground=c["accent_dark"],
            font=("TkDefaultFont", 15, "bold"),
        )
        style.configure(
            "Subtitle.TLabel",
            background=c["bg"],
            foreground=c["muted"],
        )
        style.configure(
            "Section.TLabelframe.Label",
            background=c["bg"],
            foreground=c["accent_dark"],
            font=("TkDefaultFont", 10, "bold"),
        )
        style.configure(
            "Success.TLabel",
            background=c["bg"],
            foreground=c["success"],
        )
        style.configure(
            "Error.TLabel",
            background=c["bg"],
            foreground=c["error"],
        )
        style.configure(
            "Status.TLabel",
            background=c["bg"],
            foreground=c["text"],
        )

    def _build_ui(self) -> None:
        outer = ttk.Frame(self, padding=(14, 12, 14, 12))
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(2, weight=1)

        ttk.Label(
            outer,
            text="TOPOND: подготовка данных для VESTA / VMD / JSmol",
            style="Title.TLabel",
        ).grid(row=0, column=0, sticky="w")
        ttk.Label(
            outer,
            text=(
                "Три шага доступны сразу. Скрипты запускаются отдельными "
                "процессами; ход работы и команда показаны внизу."
            ),
            style="Subtitle.TLabel",
        ).grid(row=1, column=0, sticky="w", pady=(2, 10))

        self.notebook = ttk.Notebook(outer)
        self.notebook.grid(row=2, column=0, sticky="nsew")

        self.dedup_page = ScrollableTab(self.notebook)
        self.export_page = ScrollableTab(self.notebook)
        self.super_page = ScrollableTab(self.notebook)
        self.viewer_page = ScrollableTab(self.notebook)
        self.dedup_tab = DedupTab(self.dedup_page.content, self)
        self.export_tab = ExportTab(self.export_page.content, self)
        self.super_tab = SupercellTab(self.super_page.content, self)
        self.viewer_tab = ViewerTab(self.viewer_page.content, self)
        self.dedup_tab.pack(fill="both", expand=True)
        self.export_tab.pack(fill="both", expand=True)
        self.super_tab.pack(fill="both", expand=True)
        self.viewer_tab.pack(fill="both", expand=True)
        self.notebook.add(self.dedup_page, text="1. Очистка .out")
        self.notebook.add(self.export_page, text="2. PDB и BPs")
        self.notebook.add(self.super_page, text="3. Суперячейка")
        self.notebook.add(self.viewer_page, text="4. Просмотр JSmol")

        self._build_bottom_panel(outer)

    def _build_bottom_panel(self, parent: ttk.Frame) -> None:
        panel = ttk.LabelFrame(parent, text="Выполнение", padding=10)
        panel.grid(row=3, column=0, sticky="nsew", pady=(10, 0))
        panel.columnconfigure(1, weight=1)

        ttk.Label(panel, text="Команда:").grid(
            row=0, column=0, sticky="nw", pady=(0, 6)
        )
        self.command_var = tk.StringVar(value="—")
        self.command_entry = ttk.Entry(
            panel,
            textvariable=self.command_var,
            state="readonly",
        )
        self.command_entry.grid(
            row=0, column=1, columnspan=4, sticky="ew", pady=(0, 6)
        )

        log_frame = ttk.Frame(panel)
        log_frame.grid(row=1, column=0, columnspan=5, sticky="nsew")
        log_frame.columnconfigure(0, weight=1)
        log_frame.rowconfigure(0, weight=1)
        self.log_text = tk.Text(
            log_frame,
            height=10,
            wrap="word",
            font="TkFixedFont",
            background=COLORS["log_bg"],
            foreground=COLORS["text"],
            insertbackground=COLORS["text"],
            selectbackground=COLORS["selection"],
            selectforeground=COLORS["selection_text"],
            relief="flat",
            borderwidth=0,
            highlightthickness=1,
            highlightbackground=COLORS["border"],
            highlightcolor=COLORS["accent"],
            padx=7,
            pady=5,
        )
        log_scroll = ttk.Scrollbar(
            log_frame,
            orient="vertical",
            command=self.log_text.yview,
        )
        self.log_text.configure(yscrollcommand=log_scroll.set)
        self.log_text.grid(row=0, column=0, sticky="nsew")
        log_scroll.grid(row=0, column=1, sticky="ns")
        self.log_text.configure(state="disabled")

        controls = ttk.Frame(panel)
        controls.grid(row=2, column=0, columnspan=5, sticky="ew", pady=(8, 0))
        controls.columnconfigure(1, weight=1)

        self.progress = ttk.Progressbar(
            controls,
            mode="determinate",
            length=230,
            maximum=100,
        )
        self.progress.grid(row=0, column=0, sticky="w")
        self.status_label = ttk.Label(
            controls,
            text="Готов к работе",
            style="Status.TLabel",
        )
        self.status_label.grid(row=0, column=1, sticky="w", padx=(12, 8))
        self.stop_button = ttk.Button(
            controls,
            text="Остановить",
            command=self._on_stop,
            state="disabled",
        )
        self.stop_button.grid(row=0, column=2, padx=(8, 6))
        self.open_button = ttk.Button(
            controls,
            text="Открыть папку результата",
            command=self.open_last_output,
            state="disabled",
        )
        self.open_button.grid(row=0, column=3)

        self._append_log("Готов к работе.")
        self._append_log(
            "Выберите вкладку и нажмите кнопку запуска. "
            "Пути можно вводить вручную или выбирать через «Обзор…»."
        )

    def _init_defaults(self) -> None:
        raw_input = BASE_DIR / "CP_BPs.out"
        if raw_input.is_file():
            self.dedup_tab.set_input(str(raw_input))
        self.dedup_tab.update_output_default()

        final_input = BASE_DIR / "CP_BPs_dedup_final.out"
        if final_input.is_file():
            self.export_tab.set_input(str(final_input))
        else:
            self.export_tab.set_input(self.dedup_tab.output_var.get())

        results_dir = BASE_DIR / "results"
        if results_dir.is_dir():
            self.super_tab.suggest_indir(str(results_dir))
            self.viewer_tab.set_directory(str(results_dir), announce=False)
            self.last_output_dir = results_dir
            self.open_button.state(["!disabled"])
        else:
            self.super_tab.suggest_indir(self.export_tab.outdir_var.get())
            export_dir = self.export_tab.outdir_var.get().strip()
            if export_dir:
                self.viewer_tab.set_directory(export_dir, announce=False)

    def register_run_button(self, button: ttk.Button) -> None:
        self.run_buttons.append(button)

    def start_command(
        self,
        command: list[str],
        title: str,
        output_dir: Path,
        on_success=None,
    ) -> bool:
        if self.runner.running():
            messagebox.showinfo(
                "Уже выполняется",
                "Дождитесь завершения текущего процесса или остановите его.",
                parent=self,
            )
            return False
        try:
            self.runner.start(command)
        except OSError as exc:
            log_error(LOGGER, "GUI-E002", "failed to launch process", exc=exc,
                      command=" ".join(command))
            messagebox.showerror(
                "Не удалось запустить",
                f"{exc}\n\nКоманда:\n{format_command(command)}",
                parent=self,
            )
            self._append_log(f"ОШИБКА запуска: {exc}")
            return False

        log_action(LOGGER, "run", title=title, command=" ".join(command))
        self.active_title = title
        self.success_callback = on_success
        self.stop_requested = False
        self.last_output_dir = output_dir
        self.command_var.set(format_command(command))
        self.command_entry.xview_moveto(0.0)
        self._append_log("")
        self._append_log(
            f"=== {title} — {time.strftime('%H:%M:%S')} ==="
        )
        self._append_log(f"$ {format_command(command)}")
        for button in self.run_buttons:
            button.state(["disabled"])
        self.stop_button.state(["!disabled"])
        self.open_button.state(["!disabled"])
        self.progress.stop()
        self.progress.configure(mode="indeterminate", value=0)
        self.progress.start(12)
        self.status_label.configure(
            text=f"Выполняется: {title}",
            style="Status.TLabel",
        )
        return True

    def _poll_events(self) -> None:
        if self.closing:
            return
        while True:
            try:
                kind, payload = self.events.get_nowait()
            except queue.Empty:
                break
            if kind == "line":
                self._append_log(str(payload))
            elif kind == "done":
                self._finish_process(int(payload))
        self.after(80, self._poll_events)

    def _finish_process(self, code: int) -> None:
        if self.active_title is None:
            return
        if self.kill_job is not None:
            try:
                self.after_cancel(self.kill_job)
            except tk.TclError:
                pass
            self.kill_job = None

        title = self.active_title
        callback = self.success_callback
        stopped = self.stop_requested
        self.active_title = None
        self.success_callback = None
        self.stop_requested = False

        self.progress.stop()
        self.progress.configure(mode="determinate")
        for button in self.run_buttons:
            button.state(["!disabled"])
        self.stop_button.state(["disabled"])

        if code == 0:
            log_action(LOGGER, "process_done", title=title, code=code)
            self.progress["value"] = 100
            self.status_label.configure(
                text=f"Готово: {title}",
                style="Success.TLabel",
            )
            self._append_log(f"Процесс завершён успешно (код 0).")
            if callback is not None:
                try:
                    callback()
                except Exception:
                    self._append_log(traceback.format_exc().rstrip())
        elif stopped:
            log_action(LOGGER, "process_stopped", title=title, code=code)
            self.progress["value"] = 0
            self.status_label.configure(
                text="Остановлено пользователем",
                style="Error.TLabel",
            )
            self._append_log("Процесс остановлен пользователем.")
        else:
            log_error(LOGGER, "GUI-E005", "process finished with error",
                      title=title, code=code)
            self.progress["value"] = 0
            self.status_label.configure(
                text=f"Ошибка (код {code})",
                style="Error.TLabel",
            )
            self._append_log(
                f"Процесс завершён с ошибкой, код {code}. Смотрите журнал выше."
            )
            self.bell()

        if self.last_output_dir is not None:
            self.open_button.state(["!disabled"])

    def _append_log(self, text: str) -> None:
        self.log_text.configure(state="normal")
        self.log_text.insert("end", text + "\n")
        self.log_text.see("end")
        line_count = int(self.log_text.index("end-1c").split(".")[0])
        if line_count > 6000:
            self.log_text.delete("1.0", f"{line_count - 5000}.0")
        self.log_text.configure(state="disabled")
        if text.strip():
            if text.startswith("[topond."):
                LOGGER.info("%s", text)
            else:
                LOGGER.info("ui: %s", text)

    def _ui_action(self, name: str, **fields) -> None:
        log_action(LOGGER, f"ui.{name}", **fields)

    def _bind_ui_logging(self) -> None:
        self.bind_all("<ButtonRelease-1>", self._log_ui_click, add="+")
        self.bind_all("<FocusOut>", self._log_ui_focus, add="+")
        self.bind_all("<<ComboboxSelected>>", self._log_ui_combo, add="+")
        self.bind_all("<<NotebookTabChanged>>", self._log_ui_tab, add="+")

    @staticmethod
    def _widget_label(widget: tk.Misc) -> str:
        try:
            return str(widget.cget("text"))
        except tk.TclError:
            pass
        try:
            return str(widget.cget("value"))
        except tk.TclError:
            pass
        return ""

    def _log_ui_click(self, event) -> None:
        widget = event.widget
        try:
            if not widget.winfo_exists():
                return
            cls = widget.winfo_class()
        except (tk.TclError, AttributeError):
            return
        if cls not in {
            "TButton", "TCheckbutton", "TRadiobutton", "TCombobox",
            "TNotebook", "Treeview", "TEntry", "TScale",
        }:
            return
        fields = {"widget": cls, "label": self._widget_label(widget)}
        try:
            if cls in {"TCheckbutton", "TRadiobutton"}:
                fields["checked"] = bool(widget.instate(["selected"]))
            elif cls == "TCombobox":
                fields["value"] = widget.get()
            elif cls == "TNotebook":
                fields["tab"] = widget.index("current")
            elif cls == "Treeview":
                fields["item"] = widget.identify_row(event.y)
            elif cls == "TScale":
                fields["value"] = widget.get()
        except (tk.TclError, ValueError):
            pass
        self._ui_action("click", **fields)

    def _log_ui_focus(self, event) -> None:
        widget = event.widget
        try:
            if not widget.winfo_exists():
                return
            if widget.winfo_class() != "TEntry":
                return
        except (tk.TclError, AttributeError):
            return
        try:
            value = widget.get()
        except tk.TclError:
            value = ""
        self._ui_action("entry", value=value)

    def _log_ui_combo(self, event) -> None:
        widget = event.widget
        try:
            value = widget.get()
        except (tk.TclError, AttributeError):
            value = ""
        self._ui_action("combobox", value=value,
                        label=self._widget_label(widget))

    def _log_ui_tab(self, event) -> None:
        widget = event.widget
        try:
            tab = widget.index("current")
        except (tk.TclError, ValueError):
            tab = -1
        self._ui_action("tab", tab=tab)

    def _on_stop(self) -> None:
        if not self.runner.running():
            self.stop_button.state(["disabled"])
            return
        self.stop_requested = True
        log_action(LOGGER, "stop_requested", title=self.active_title)
        self._append_log("Запрошена остановка процесса…")
        self.runner.stop()
        self.stop_button.state(["disabled"])
        if self.kill_job is not None:
            try:
                self.after_cancel(self.kill_job)
            except tk.TclError:
                pass
        self.kill_job = self.after(3000, self._force_kill)

    def _force_kill(self) -> None:
        self.kill_job = None
        if self.runner.running():
            log_action(LOGGER, "force_kill", title=self.active_title)
            self._append_log("Процесс не завершился, принудительное завершение.")
            try:
                self.runner.kill()
            except Exception as exc:
                log_error(LOGGER, "GUI-E004", "failed to kill process", exc=exc,
                          title=self.active_title)

    def open_last_output(self) -> None:
        target = self.last_output_dir
        if target is None or not target.exists():
            messagebox.showwarning(
                "Папка не найдена",
                "Результат ещё не создан или папка была удалена.",
                parent=self,
            )
            return
        if target.is_file():
            target = target.parent
        try:
            if os.name == "nt":
                os.startfile(str(target))
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(target)])
            else:
                subprocess.Popen(["xdg-open", str(target)])
        except OSError as exc:
            messagebox.showerror(
                "Не удалось открыть папку",
                f"{exc}\n\n{target}",
                parent=self,
            )

    def report_callback_exception(self, exc, value, tb) -> None:
        text = "".join(traceback.format_exception(exc, value, tb))
        LOGGER.error("code=GUI-E001 uncaught UI exception: %s: %s\n%s",
                     exc.__name__, value, text.rstrip())
        if hasattr(self, "log_text"):
            self._append_log(text.rstrip())
        messagebox.showerror(
            "Ошибка интерфейса",
            f"{exc.__name__}: {value}",
            parent=self,
        )

    def _on_close(self) -> None:
        log_action(LOGGER, "close", running=self.runner.running())
        if self.runner.running():
            answer = messagebox.askyesno(
                "Процесс выполняется",
                "Остановить процесс и закрыть программу?",
                parent=self,
            )
            if not answer:
                return
            self.runner.stop()
        self.closing = True
        self.viewer_tab.stop_viewer(silent=True)
        if self.kill_job is not None:
            try:
                self.after_cancel(self.kill_job)
            except tk.TclError:
                pass
        try:
            self.destroy()
        except Exception:
            pass


class DedupTab(ttk.Frame):
    def __init__(self, parent: tk.Misc, app: App) -> None:
        super().__init__(parent, padding=14)
        self.app = app
        self.input_var = tk.StringVar()
        self.output_var = tk.StringVar()
        self.report_var = tk.BooleanVar(value=False)
        self.verify_var = tk.BooleanVar(value=False)
        self.output_manual = False
        self.auto_output = ""

        self.columnconfigure(1, weight=1)

        ttk.Label(
            self,
            text="Шаг 1. Удаление дубликатов из сырого вывода CRYSTAL/TOPOND",
            style="Title.TLabel",
        ).grid(row=0, column=0, columnspan=3, sticky="w")
        ttk.Label(
            self,
            text=(
                "Скрипт собирает один чистый .out из перемешанных копий "
                "параллельного расчёта."
            ),
            style="Subtitle.TLabel",
        ).grid(row=1, column=0, columnspan=3, sticky="w", pady=(2, 12))

        ttk.Label(self, text="Входной .out:").grid(
            row=2, column=0, sticky="w", pady=4
        )
        self.input_entry = ttk.Entry(self, textvariable=self.input_var)
        self.input_entry.grid(
            row=2, column=1, sticky="ew", padx=(8, 6), pady=4
        )
        ttk.Button(
            self,
            text="Обзор…",
            command=self._browse_input,
        ).grid(row=2, column=2, pady=4)

        ttk.Label(self, text="Выходной .out:").grid(
            row=3, column=0, sticky="w", pady=4
        )
        self.output_entry = ttk.Entry(self, textvariable=self.output_var)
        self.output_entry.grid(
            row=3, column=1, sticky="ew", padx=(8, 6), pady=4
        )
        ttk.Button(
            self,
            text="Сохранить как…",
            command=self._browse_output,
        ).grid(row=3, column=2, pady=4)

        options = ttk.LabelFrame(self, text="Параметры очистки", padding=10)
        options.grid(row=4, column=0, columnspan=3, sticky="ew", pady=(12, 8))
        ttk.Checkbutton(
            options,
            text="Подробный отчёт (--report)",
            variable=self.report_var,
        ).pack(side="left", padx=(0, 18))
        ttk.Checkbutton(
            options,
            text="Проверить результат (--verify)",
            variable=self.verify_var,
        ).pack(side="left")

        ttk.Label(
            self,
            text=(
                "По умолчанию выходной файл: <имя входного>_dedup.out. "
                "Он должен отличаться от входного."
            ),
            style="Subtitle.TLabel",
        ).grid(row=5, column=0, columnspan=3, sticky="w", pady=(0, 12))

        actions = ttk.Frame(self)
        actions.grid(row=6, column=0, columnspan=3, sticky="w")
        run_button = ttk.Button(
            actions,
            text="Очистить .out",
            command=self._run,
            style="Run.TButton",
        )
        run_button.pack(side="left")
        self.app.register_run_button(run_button)
        ttk.Label(
            actions,
            text="После успешного запуска путь автоматически попадёт на шаг 2.",
            style="Subtitle.TLabel",
        ).pack(side="left", padx=(12, 0))

        self.input_entry.bind("<FocusOut>", self._input_changed)
        self.input_entry.bind("<Return>", self._input_changed)
        self.output_entry.bind("<KeyRelease>", self._mark_output_manual)
        self.output_entry.bind("<<Paste>>", self._mark_output_manual)

    def set_input(self, path: str) -> None:
        self.input_var.set(path)
        self.update_output_default()

    def update_output_default(self) -> None:
        text = self.input_var.get().strip()
        if not text:
            return
        if self.output_manual and self.output_var.get().strip() != self.auto_output:
            return
        source = resolve_user_path(text)
        default = source.parent / f"{source.stem}_dedup.out"
        self.auto_output = str(default)
        self.output_var.set(self.auto_output)

    def _input_changed(self, event=None) -> None:
        self.update_output_default()

    def _mark_output_manual(self, event=None) -> None:
        self.output_manual = True

    def _browse_input(self) -> None:
        path = filedialog.askopenfilename(
            parent=self,
            title="Выберите сырой .out файл",
            initialdir=str(dialog_initial_dir(self.input_var.get())),
            filetypes=(
                ("Вывод CRYSTAL/TOPOND", "*.out"),
                ("Текстовые файлы", "*.txt"),
                ("Все файлы", "*"),
            ),
        )
        if path:
            self.input_entry.delete(0, "end")
            self.input_entry.insert(0, path)
            self.update_output_default()
            self.input_entry.focus_set()

    def _browse_output(self) -> None:
        initial = dialog_initial_dir(self.output_var.get())
        initial_name = Path(self.output_var.get()).name or "cleaned.out"
        path = filedialog.asksaveasfilename(
            parent=self,
            title="Куда сохранить очищенный .out",
            initialdir=str(initial),
            initialfile=initial_name,
            defaultextension=".out",
            filetypes=(("Вывод TOPOND", "*.out"), ("Все файлы", "*")),
        )
        if path:
            self.output_var.set(path)
            self.output_manual = True
            self.auto_output = ""
            self.output_entry.focus_set()

    def build_command(self) -> tuple[list[str], Path]:
        if not DEDUP_SCRIPT.is_file():
            raise ValidationError(f"Не найден скрипт очистки: {DEDUP_SCRIPT}")
        input_text = self.input_var.get().strip()
        if not input_text:
            raise ValidationError("Укажите входной .out файл.")
        source = resolve_user_path(input_text)
        if not source.is_file():
            raise ValidationError(f"Входной файл не найден:\n{source}")
        output_text = self.output_var.get().strip()
        if not output_text:
            raise ValidationError("Укажите выходной .out файл.")
        destination = resolve_user_path(output_text)
        if source.resolve() == destination.resolve():
            raise ValidationError(
                "Выходной файл должен отличаться от входного."
            )
        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise ValidationError(
                f"Не удалось создать папку результата:\n{destination.parent}\n{exc}"
            ) from exc

        command = [
            worker_python(),
            "-u",
            str(DEDUP_SCRIPT),
            str(source),
            "-o",
            str(destination),
        ]
        if self.report_var.get():
            command.append("--report")
        if self.verify_var.get():
            command.append("--verify")
        self.last_output = destination
        return command, destination.parent

    def _run(self) -> None:
        try:
            command, output_dir = self.build_command()
        except ValidationError as exc:
            messagebox.showerror("Проверка параметров", str(exc), parent=self)
            return
        self.app.start_command(
            command,
            "Очистка .out",
            output_dir,
            on_success=self._after_success,
        )

    def _after_success(self) -> None:
        self.app.export_tab.suggest_input(str(self.last_output))
        self.app.notebook.select(self.app.export_page)


class ExportTab(ttk.Frame):
    def __init__(self, parent: tk.Misc, app: App) -> None:
        super().__init__(parent, padding=14)
        self.app = app
        self.input_var = tk.StringVar()
        self.outdir_var = tk.StringVar()
        self.mode_var = tk.StringVar(value="align")
        self.dummy_var = tk.StringVar(value="X")
        self.keep_duplicates_var = tk.BooleanVar(value=False)
        self.space_group_var = tk.StringVar()
        self.symmetry_tol_var = tk.StringVar(value="0.001")
        self.image_var = tk.StringVar(value="shift")
        self.all_settings_var = tk.BooleanVar(value=False)
        self.bp_angle_var = tk.StringVar(value="140")
        self.input_manual = False
        self.outdir_manual = False
        self.auto_outdir = ""
        self.last_outdir: Path | None = None

        self.columnconfigure(1, weight=1)

        ttk.Label(
            self,
            text="Шаг 2. Экспорт CP и bond paths в PDB/XYZ",
            style="Title.TLabel",
        ).grid(row=0, column=0, columnspan=3, sticky="w")
        ttk.Label(
            self,
            text=(
                "Создаются cell.pdb, critical_points.*, crystal_BPs.pdb, "
                "calc_BPs.pdb и summary.txt."
            ),
            style="Subtitle.TLabel",
        ).grid(row=1, column=0, columnspan=3, sticky="w", pady=(2, 12))

        ttk.Label(self, text="Входной .out:").grid(
            row=2, column=0, sticky="w", pady=4
        )
        self.input_entry = ttk.Entry(self, textvariable=self.input_var)
        self.input_entry.grid(
            row=2, column=1, sticky="ew", padx=(8, 6), pady=4
        )
        ttk.Button(
            self,
            text="Обзор…",
            command=self._browse_input,
        ).grid(row=2, column=2, pady=4)

        ttk.Label(self, text="Папка результатов:").grid(
            row=3, column=0, sticky="w", pady=4
        )
        self.outdir_entry = ttk.Entry(self, textvariable=self.outdir_var)
        self.outdir_entry.grid(
            row=3, column=1, sticky="ew", padx=(8, 6), pady=4
        )
        ttk.Button(
            self,
            text="Обзор…",
            command=self._browse_outdir,
        ).grid(row=3, column=2, pady=4)

        coordinates = ttk.LabelFrame(
            self,
            text="Координаты критических точек",
            padding=10,
        )
        coordinates.grid(row=4, column=0, columnspan=3, sticky="ew", pady=(10, 8))
        ttk.Radiobutton(
            coordinates,
            text="Без изменений (raw)",
            variable=self.mode_var,
            value="raw",
        ).pack(side="left", padx=(0, 18))
        ttk.Radiobutton(
            coordinates,
            text="Обернуть в ячейку [0,1) (--wrap)",
            variable=self.mode_var,
            value="wrap",
        ).pack(side="left", padx=(0, 18))
        ttk.Radiobutton(
            coordinates,
            text="Совместить с атомами (--align) — рекомендуется",
            variable=self.mode_var,
            value="align",
        ).pack(side="left")

        options = ttk.Frame(self)
        options.grid(row=5, column=0, columnspan=3, sticky="ew", pady=(0, 8))
        ttk.Label(options, text="Элемент для CP:").pack(side="left")
        ttk.Entry(
            options,
            textvariable=self.dummy_var,
            width=5,
        ).pack(side="left", padx=(6, 4))
        ttk.Label(
            options,
            text="(--dummy-element)",
            style="Subtitle.TLabel",
        ).pack(side="left", padx=(0, 18))
        ttk.Checkbutton(
            options,
            text="Не удалять дубликаты (--keep-duplicates)",
            variable=self.keep_duplicates_var,
        ).pack(side="left")

        symmetry = ttk.LabelFrame(
            self,
            text="Симметрия (необязательно)",
            padding=10,
        )
        symmetry.grid(row=6, column=0, columnspan=3, sticky="ew", pady=(0, 8))
        symmetry.columnconfigure(1, weight=1)

        ttk.Label(symmetry, text="Пространственная группа:").grid(
            row=0, column=0, sticky="w", pady=(0, 6)
        )
        ttk.Entry(
            symmetry,
            textvariable=self.space_group_var,
        ).grid(row=0, column=1, sticky="ew", padx=(8, 6), pady=(0, 6))
        ttk.Button(
            symmetry,
            text="Справочник групп…",
            command=self._show_space_groups,
        ).grid(row=0, column=2, pady=(0, 6))

        ttk.Label(symmetry, text="Допуск симметрии:").grid(
            row=1, column=0, sticky="w", pady=(0, 6)
        )
        self.symmetry_tol_spin = ttk.Spinbox(
            symmetry,
            textvariable=self.symmetry_tol_var,
            from_=0.000001,
            to=1.0,
            increment=0.0001,
            width=12,
            format="%.6f",
        )
        self.symmetry_tol_spin.grid(
            row=1, column=1, sticky="w", padx=(8, 6), pady=(0, 6)
        )
        ttk.Label(
            symmetry,
            text="(--symmetry-tol)",
            style="Subtitle.TLabel",
        ).grid(row=1, column=2, sticky="w", pady=(0, 6))

        image_frame = ttk.Frame(symmetry)
        image_frame.grid(row=2, column=0, columnspan=3, sticky="w", pady=(0, 6))
        ttk.Label(image_frame, text="Образ bond paths:").pack(side="left")
        self.image_shift_radio = ttk.Radiobutton(
            image_frame,
            text="shift (непрерывно)",
            variable=self.image_var,
            value="shift",
        )
        self.image_shift_radio.pack(side="left", padx=(10, 12))
        self.image_cut_radio = ttk.Radiobutton(
            image_frame,
            text="cut (по границе ячейки)",
            variable=self.image_var,
            value="cut",
        )
        self.image_cut_radio.pack(side="left", padx=(0, 10))
        ttk.Label(
            image_frame,
            text="(--symmetry-image)",
            style="Subtitle.TLabel",
        ).pack(side="left")

        ttk.Checkbutton(
            symmetry,
            text="Показывать все 530 установок в справочнике (--all-settings)",
            variable=self.all_settings_var,
        ).grid(row=3, column=0, columnspan=3, sticky="w", pady=(0, 4))

        ttk.Label(
            symmetry,
            text=(
                "Полный символ без пробелов, например P12_1/n1. "
                "Без группы создаются только базовые файлы; "
                "*_symm.* появятся после её указания."
            ),
            style="Subtitle.TLabel",
        ).grid(row=4, column=0, columnspan=3, sticky="w")

        bp_row = ttk.Frame(self)
        bp_row.grid(row=7, column=0, columnspan=3, sticky="w", pady=(0, 10))
        ttk.Label(bp_row, text="Порог угла для calc BPs:").pack(side="left")
        ttk.Spinbox(
            bp_row,
            textvariable=self.bp_angle_var,
            from_=0,
            to=180,
            increment=1,
            width=8,
        ).pack(side="left", padx=(8, 4))
        ttk.Label(
            bp_row,
            text="градусов (--bp-angle)",
            style="Subtitle.TLabel",
        ).pack(side="left")

        actions = ttk.Frame(self)
        actions.grid(row=8, column=0, columnspan=3, sticky="w")
        run_button = ttk.Button(
            actions,
            text="Создать PDB и BPs",
            command=self._run,
            style="Run.TButton",
        )
        run_button.pack(side="left")
        self.app.register_run_button(run_button)
        ttk.Label(
            actions,
            text="После успешного запуска папка попадёт на шаг 3.",
            style="Subtitle.TLabel",
        ).pack(side="left", padx=(12, 0))

        self.input_entry.bind("<KeyRelease>", self._mark_input_manual)
        self.input_entry.bind("<FocusOut>", self._input_changed)
        self.input_entry.bind("<Return>", self._input_changed)
        self.outdir_entry.bind("<KeyRelease>", self._mark_outdir_manual)
        self.outdir_entry.bind("<<Paste>>", self._mark_outdir_manual)
        self.space_group_var.trace_add("write", self._sync_symmetry_state)
        self._sync_symmetry_state()

    def set_input(self, path: str) -> None:
        self.input_var.set(path)
        self.update_outdir_default()

    def suggest_input(self, path: str) -> None:
        if self.input_manual:
            return
        self.input_var.set(path)
        self.update_outdir_default()

    def update_outdir_default(self) -> None:
        if self.outdir_manual:
            return
        text = self.input_var.get().strip()
        if not text:
            return
        source = resolve_user_path(text)
        default = source.parent / "results"
        self.auto_outdir = str(default)
        self.outdir_var.set(self.auto_outdir)

    def _mark_input_manual(self, event=None) -> None:
        self.input_manual = True

    def _mark_outdir_manual(self, event=None) -> None:
        self.outdir_manual = True

    def _input_changed(self, event=None) -> None:
        self.input_manual = True
        self.update_outdir_default()

    def _sync_symmetry_state(self, *args) -> None:
        enabled = bool(self.space_group_var.get().strip())
        set_widget_state(
            [
                self.symmetry_tol_spin,
                self.image_shift_radio,
                self.image_cut_radio,
            ],
            enabled,
        )

    def _browse_input(self) -> None:
        path = filedialog.askopenfilename(
            parent=self,
            title="Выберите очищенный .out файл",
            initialdir=str(dialog_initial_dir(self.input_var.get())),
            filetypes=(
                ("Вывод TOPOND", "*.out"),
                ("Текстовые файлы", "*.txt"),
                ("Все файлы", "*"),
            ),
        )
        if path:
            self.input_manual = True
            self.set_input(path)
            self.input_entry.focus_set()

    def _browse_outdir(self) -> None:
        path = filedialog.askdirectory(
            parent=self,
            title="Выберите папку результатов",
            initialdir=str(dialog_initial_dir(self.outdir_var.get())),
            mustexist=False,
        )
        if path:
            self.outdir_var.set(path)
            self.outdir_manual = True
            self.auto_outdir = ""
            self.outdir_entry.focus_set()

    def _show_space_groups(self) -> None:
        if not EXPORT_SCRIPT.is_file():
            messagebox.showerror(
                "Скрипт не найден",
                f"Не найден экспортёр:\n{EXPORT_SCRIPT}",
                parent=self,
            )
            return
        command = [
            worker_python(),
            str(EXPORT_SCRIPT),
            "--list-space-groups",
        ]
        if self.all_settings_var.get():
            command.append("--all-settings")
        try:
            result = subprocess.run(
                command,
                cwd=str(BASE_DIR),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=60,
                env=_child_env(),
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            messagebox.showerror(
                "Не удалось получить список",
                str(exc),
                parent=self,
            )
            return
        if result.returncode != 0:
            messagebox.showerror(
                "Ошибка справочника",
                result.stderr.strip() or "spglib недоступен или вернул ошибку.",
                parent=self,
            )
            return
        records: list[tuple] = []
        pattern = re.compile(
            r"^\s*(\d+)\s+(\S+)\s+(\d+)\s+(\d+)(?:\s+\[([^\]]*)\])?\s*$"
        )
        for line in result.stdout.splitlines():
            match = pattern.match(line)
            if not match:
                continue
            number, symbol, hall, n_ops, choice = match.groups()
            records.append(
                (int(number), symbol, int(hall), choice or "", int(n_ops))
            )
        SpaceGroupWindow(
            self.app,
            records,
            result.stdout,
            self._set_space_group,
        )

    def _set_space_group(self, symbol: str) -> None:
        self.space_group_var.set(symbol)
        self._sync_symmetry_state()

    def build_command(self) -> tuple[list[str], Path]:
        if not EXPORT_SCRIPT.is_file():
            raise ValidationError(f"Не найден экспортёр: {EXPORT_SCRIPT}")
        input_text = self.input_var.get().strip()
        if not input_text:
            raise ValidationError("Укажите входной .out файл.")
        source = resolve_user_path(input_text)
        if not source.is_file():
            raise ValidationError(f"Входной файл не найден:\n{source}")
        out_text = self.outdir_var.get().strip()
        if not out_text:
            raise ValidationError("Укажите папку результатов.")
        outdir = resolve_user_path(out_text)
        try:
            outdir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise ValidationError(
                f"Не удалось создать папку результатов:\n{outdir}\n{exc}"
            ) from exc

        bp_angle = parse_float_value(
            self.bp_angle_var.get(),
            "Порог угла для calc BPs",
            minimum=0.0,
            maximum=180.0,
        )
        group = self.space_group_var.get().strip()
        if any(character.isspace() for character in group):
            raise ValidationError(
                "Символ пространственной группы должен быть без пробелов. "
                "Возьмите полный символ из справочника."
            )
        symmetry_tol = None
        if group:
            symmetry_tol = parse_float_value(
                self.symmetry_tol_var.get(),
                "Допуск симметрии",
                minimum=0.0,
                minimum_exclusive=True,
            )

        command = [worker_python(), "-u", str(EXPORT_SCRIPT), str(source)]
        command += ["--outdir", str(outdir)]
        mode = self.mode_var.get()
        if mode == "wrap":
            command.append("--wrap")
        elif mode == "align":
            command.append("--align")
        dummy = self.dummy_var.get().strip()
        if dummy:
            command += ["--dummy-element", dummy]
        if self.keep_duplicates_var.get():
            command.append("--keep-duplicates")
        if group:
            command += [
                "--space-group",
                group,
                "--symmetry-tol",
                number_text(symmetry_tol),
                "--symmetry-image",
                self.image_var.get(),
            ]
        command += ["--bp-angle", number_text(bp_angle)]
        self.last_outdir = outdir
        return command, outdir

    def _run(self) -> None:
        try:
            command, outdir = self.build_command()
        except ValidationError as exc:
            messagebox.showerror("Проверка параметров", str(exc), parent=self)
            return
        self.app.start_command(
            command,
            "Экспорт CP и BPs",
            outdir,
            on_success=self._after_success,
        )

    def _after_success(self) -> None:
        if self.last_outdir is not None:
            self.app.super_tab.suggest_indir(str(self.last_outdir))
            self.app.viewer_tab.set_directory(
                str(self.last_outdir),
                announce=False,
            )
            self.app.notebook.select(self.app.super_page)


class SupercellTab(ttk.Frame):
    def __init__(self, parent: tk.Misc, app: App) -> None:
        super().__init__(parent, padding=14)
        self.app = app
        self.indir_var = tk.StringVar()
        self.outdir_var = tk.StringVar()
        self.source_var = tk.StringVar(value="symm")
        self.mode_var = tk.StringVar(value="frac")
        self.center_frac_vars = [
            tk.StringVar(value="0.5"),
            tk.StringVar(value="0.5"),
            tk.StringVar(value="0.5"),
        ]
        self.range_frac_vars = [
            tk.StringVar(value="-0.5"),
            tk.StringVar(value="0.5"),
            tk.StringVar(value="-0.5"),
            tk.StringVar(value="0.5"),
            tk.StringVar(value="-0.5"),
            tk.StringVar(value="0.5"),
        ]
        self.center_ang_vars = [
            tk.StringVar(value="0"),
            tk.StringVar(value="0"),
            tk.StringVar(value="0"),
        ]
        self.range_ang_vars = [
            tk.StringVar(value="-10"),
            tk.StringVar(value="10"),
            tk.StringVar(value="-10"),
            tk.StringVar(value="10"),
            tk.StringVar(value="-10"),
            tk.StringVar(value="10"),
        ]
        self.ang_axes_var = tk.StringVar(value="cell")
        self.indir_manual = False
        self.outdir_manual = False
        self.source_manual = False
        self.auto_outdir = ""
        self.frac_entries: list[ttk.Entry] = []
        self.ang_entries: list[ttk.Entry] = []
        self.ang_axis_radios: list[ttk.Radiobutton] = []
        self.last_outdir: Path | None = None

        self.columnconfigure(1, weight=1)

        ttk.Label(
            self,
            text="Шаг 3. Построение суперячейки из готовых PDB",
            style="Title.TLabel",
        ).grid(row=0, column=0, columnspan=3, sticky="w")
        ttk.Label(
            self,
            text=(
                "Скрипт читает cell.pdb и файлы CP/BPs, размножает их по "
                "выбранному боксу и пишет новую CRYST1."
            ),
            style="Subtitle.TLabel",
        ).grid(row=1, column=0, columnspan=3, sticky="w", pady=(2, 12))

        ttk.Label(self, text="Папка с PDB:").grid(
            row=2, column=0, sticky="w", pady=4
        )
        self.indir_entry = ttk.Entry(self, textvariable=self.indir_var)
        self.indir_entry.grid(
            row=2, column=1, sticky="ew", padx=(8, 6), pady=4
        )
        ttk.Button(
            self,
            text="Обзор…",
            command=self._browse_indir,
        ).grid(row=2, column=2, pady=4)

        ttk.Label(self, text="Папка результата:").grid(
            row=3, column=0, sticky="w", pady=4
        )
        self.outdir_entry = ttk.Entry(self, textvariable=self.outdir_var)
        self.outdir_entry.grid(
            row=3, column=1, sticky="ew", padx=(8, 6), pady=4
        )
        ttk.Button(
            self,
            text="Обзор…",
            command=self._browse_outdir,
        ).grid(row=3, column=2, pady=4)

        source_frame = ttk.LabelFrame(
            self,
            text="Источник файлов",
            padding=10,
        )
        source_frame.grid(row=4, column=0, columnspan=3, sticky="ew", pady=(10, 8))
        ttk.Radiobutton(
            source_frame,
            text="Симметрийные *_symm.pdb (--source symm)",
            variable=self.source_var,
            value="symm",
            command=self._mark_source_manual,
        ).pack(side="left", padx=(0, 18))
        ttk.Radiobutton(
            source_frame,
            text="Базовые файлы (--source base)",
            variable=self.source_var,
            value="base",
            command=self._mark_source_manual,
        ).pack(side="left")
        ttk.Label(
            source_frame,
            text=(
                "  Автовыбор: symm, если есть все *_symm.pdb, иначе base. "
                "Ручной выбор фиксируется."
            ),
            style="Subtitle.TLabel",
        ).pack(side="left")

        presets = ttk.Frame(self)
        presets.grid(row=5, column=0, columnspan=3, sticky="w", pady=(0, 8))
        ttk.Label(presets, text="Пресеты:").pack(side="left", padx=(0, 8))
        ttk.Button(
            presets,
            text="1×1×1",
            command=lambda: self._apply_preset(0.5),
        ).pack(side="left", padx=(0, 6))
        ttk.Button(
            presets,
            text="3×3×3",
            command=lambda: self._apply_preset(1.5),
        ).pack(side="left", padx=(0, 6))
        ttk.Button(
            presets,
            text="3×3×1",
            command=lambda: self._apply_preset_3x3x1(),
        ).pack(side="left")

        geometry = ttk.LabelFrame(
            self,
            text="Размер и положение бокса",
            padding=10,
        )
        geometry.grid(row=6, column=0, columnspan=3, sticky="ew", pady=(0, 8))
        geometry.columnconfigure(0, weight=1)

        mode_frame = ttk.Frame(geometry)
        mode_frame.grid(row=0, column=0, sticky="w", pady=(0, 8))
        ttk.Radiobutton(
            mode_frame,
            text="Дробные координаты (--center-frac / --range)",
            variable=self.mode_var,
            value="frac",
            command=self._sync_mode_state,
        ).pack(side="left", padx=(0, 18))
        ttk.Radiobutton(
            mode_frame,
            text="Ангстремы (--center-ang / --range-ang)",
            variable=self.mode_var,
            value="ang",
            command=self._sync_mode_state,
        ).pack(side="left")

        self.frac_frame = ttk.Frame(geometry)
        self.frac_frame.grid(row=1, column=0, sticky="ew", pady=(0, 6))
        self._build_range_fields(
            self.frac_frame,
            "Центр (доли ячейки):",
            self.center_frac_vars,
            labels=("cx", "cy", "cz"),
            entries=self.frac_entries,
        )
        self._build_range_fields(
            self.frac_frame,
            "Смещения от центра:",
            self.range_frac_vars,
            labels=("x0", "x1", "y0", "y1", "z0", "z1"),
            entries=self.frac_entries,
            row=1,
        )
        ttk.Label(
            self.frac_frame,
            text=(
                "Порядок: X0 X1 Y0 Y1 Z0 Z1. Ширина по оси = X1 − X0. "
                "1.0 = одна ячейка."
            ),
            style="Subtitle.TLabel",
        ).grid(row=2, column=0, columnspan=2, sticky="w", pady=(2, 0))

        self.ang_frame = ttk.Frame(geometry)
        self.ang_frame.grid(row=2, column=0, sticky="ew", pady=(0, 6))
        self._build_range_fields(
            self.ang_frame,
            "Центр (Å):",
            self.center_ang_vars,
            labels=("cx", "cy", "cz"),
            entries=self.ang_entries,
        )
        self._build_range_fields(
            self.ang_frame,
            "Смещения (Å):",
            self.range_ang_vars,
            labels=("x0", "x1", "y0", "y1", "z0", "z1"),
            entries=self.ang_entries,
            row=1,
        )
        axes_frame = ttk.Frame(self.ang_frame)
        axes_frame.grid(row=2, column=0, columnspan=2, sticky="w", pady=(8, 0))
        ttk.Label(axes_frame, text="Оси смещений:").pack(side="left")
        radio_cell = ttk.Radiobutton(
            axes_frame,
            text="вдоль a,b,c (--ang-axes cell)",
            variable=self.ang_axes_var,
            value="cell",
        )
        radio_cell.pack(side="left", padx=(8, 12))
        radio_xyz = ttk.Radiobutton(
            axes_frame,
            text="вдоль x,y,z (--ang-axes xyz)",
            variable=self.ang_axes_var,
            value="xyz",
        )
        radio_xyz.pack(side="left")
        self.ang_axis_radios.extend([radio_cell, radio_xyz])
        ttk.Label(
            self.ang_frame,
            text=(
                "Режим xyz — прямоугольный вырез в декартовых координатах. "
                "Оба поля центра и диапазона обязательны."
            ),
            style="Subtitle.TLabel",
        ).grid(row=3, column=0, columnspan=2, sticky="w", pady=(4, 0))

        actions = ttk.Frame(self)
        actions.grid(row=7, column=0, columnspan=3, sticky="w", pady=(2, 0))
        run_button = ttk.Button(
            actions,
            text="Построить суперячейку",
            command=self._run,
            style="Run.TButton",
        )
        run_button.pack(side="left")
        self.app.register_run_button(run_button)
        ttk.Label(
            actions,
            text="В summary_supercell.txt будут счётчики и предупреждения.",
            style="Subtitle.TLabel",
        ).pack(side="left", padx=(12, 0))

        self.indir_entry.bind("<KeyRelease>", self._mark_indir_manual)
        self.indir_entry.bind("<FocusOut>", self._indir_changed)
        self.indir_entry.bind("<Return>", self._indir_changed)
        self.outdir_entry.bind("<KeyRelease>", self._mark_outdir_manual)
        self.outdir_entry.bind("<<Paste>>", self._mark_outdir_manual)
        self._sync_mode_state()

    def _build_range_fields(
        self,
        parent: ttk.Frame,
        title: str,
        variables: list[tk.StringVar],
        *,
        labels: tuple[str, ...],
        entries: list[ttk.Entry],
        row: int = 0,
    ) -> None:
        ttk.Label(parent, text=title).grid(
            row=row, column=0, sticky="w", pady=3
        )
        values = ttk.Frame(parent)
        values.grid(row=row, column=1, sticky="w", pady=3)
        for index, (variable, label) in enumerate(zip(variables, labels)):
            cell = ttk.Frame(values)
            cell.grid(row=0, column=index, padx=(0, 8))
            ttk.Label(
                cell,
                text=label,
                style="Subtitle.TLabel",
            ).pack(anchor="w")
            entry = ttk.Entry(cell, textvariable=variable, width=9)
            entry.pack(anchor="w")
            entries.append(entry)

    def set_indir(self, path: str) -> None:
        self.indir_var.set(path)
        self.update_outdir_default()
        self.update_source_default()

    def suggest_indir(self, path: str) -> None:
        if self.indir_manual:
            return
        self.indir_var.set(path)
        self.update_outdir_default()
        self.update_source_default()

    def update_outdir_default(self) -> None:
        if self.outdir_manual:
            return
        text = self.indir_var.get().strip()
        if not text:
            return
        source = resolve_user_path(text)
        default = source.with_name(source.name + "_supercell")
        self.auto_outdir = str(default)
        self.outdir_var.set(self.auto_outdir)

    def update_source_default(self) -> None:
        if self.source_manual:
            return
        text = self.indir_var.get().strip()
        if not text:
            return
        directory = resolve_user_path(text)
        symm_files = (
            directory / "critical_points_symm.pdb",
            directory / "crystal_BPs_symm.pdb",
            directory / "calc_BPs_symm.pdb",
        )
        self.source_var.set(
            "symm" if all(path.is_file() for path in symm_files) else "base"
        )

    def _mark_indir_manual(self, event=None) -> None:
        self.indir_manual = True

    def _mark_outdir_manual(self, event=None) -> None:
        self.outdir_manual = True

    def _mark_source_manual(self) -> None:
        self.source_manual = True

    def _indir_changed(self, event=None) -> None:
        self.indir_manual = True
        self.update_outdir_default()
        self.update_source_default()

    def _browse_indir(self) -> None:
        path = filedialog.askdirectory(
            parent=self,
            title="Выберите папку с cell.pdb и CP/BPs",
            initialdir=str(dialog_initial_dir(self.indir_var.get())),
            mustexist=False,
        )
        if path:
            self.indir_manual = True
            self.set_indir(path)
            self.indir_entry.focus_set()

    def _browse_outdir(self) -> None:
        path = filedialog.askdirectory(
            parent=self,
            title="Выберите папку для суперячейки",
            initialdir=str(dialog_initial_dir(self.outdir_var.get())),
            mustexist=False,
        )
        if path:
            self.outdir_var.set(path)
            self.outdir_manual = True
            self.auto_outdir = ""
            self.outdir_entry.focus_set()

    def _sync_mode_state(self) -> None:
        fractional = self.mode_var.get() == "frac"
        set_widget_state(self.frac_entries, fractional)
        set_widget_state(self.ang_entries, not fractional)
        set_widget_state(self.ang_axis_radios, not fractional)

    def _apply_preset(self, half_width: float) -> None:
        self.mode_var.set("frac")
        for variable in self.center_frac_vars:
            variable.set("0.5")
        values = [
            -half_width,
            half_width,
            -half_width,
            half_width,
            -half_width,
            half_width,
        ]
        for variable, value in zip(self.range_frac_vars, values):
            variable.set(number_text(value))
        self._sync_mode_state()

    def _apply_preset_3x3x1(self) -> None:
        self.mode_var.set("frac")
        for variable in self.center_frac_vars:
            variable.set("0.5")
        values = ["-1.5", "1.5", "-1.5", "1.5", "-0.5", "0.5"]
        for variable, value in zip(self.range_frac_vars, values):
            variable.set(value)
        self._sync_mode_state()

    def _expected_inputs(self, directory: Path, source: str) -> tuple[Path, ...]:
        if source == "symm":
            return (
                directory / "cell.pdb",
                directory / "critical_points_symm.pdb",
                directory / "crystal_BPs_symm.pdb",
                directory / "calc_BPs_symm.pdb",
            )
        return (
            directory / "cell.pdb",
            directory / "critical_points.pdb",
            directory / "crystal_BPs.pdb",
            directory / "calc_BPs.pdb",
        )

    def build_command(self) -> tuple[list[str], Path]:
        if not SUPERCELL_SCRIPT.is_file():
            raise ValidationError(
                f"Не найден скрипт суперячейки: {SUPERCELL_SCRIPT}"
            )
        indir_text = self.indir_var.get().strip()
        if not indir_text:
            raise ValidationError("Укажите папку с готовыми PDB.")
        indir = resolve_user_path(indir_text)
        if not indir.is_dir():
            raise ValidationError(f"Папка с PDB не найдена:\n{indir}")
        out_text = self.outdir_var.get().strip()
        if out_text:
            outdir = resolve_user_path(out_text)
        else:
            outdir = indir.with_name(indir.name + "_supercell")

        source = self.source_var.get()
        expected = self._expected_inputs(indir, source)
        missing = [path for path in expected if not path.is_file()]
        if missing:
            listing = "\n".join(str(path) for path in missing)
            raise ValidationError(
                "Для выбранного источника не хватает файлов:\n" + listing
            )

        command = [
            worker_python(),
            "-u",
            str(SUPERCELL_SCRIPT),
            "--indir",
            str(indir),
            "--outdir",
            str(outdir),
            "--source",
            source,
        ]

        if self.mode_var.get() == "frac":
            center = parse_float_fields(
                self.center_frac_vars,
                ["cx", "cy", "cz"],
                "Дробный центр",
            )
            ranges = parse_float_fields(
                self.range_frac_vars,
                ["x0", "x1", "y0", "y1", "z0", "z1"],
                "Дробные смещения",
            )
            validate_ranges(ranges, "Дробные смещения")
            command += ["--center-frac", *(number_text(value) for value in center)]
            command += ["--range", *(number_text(value) for value in ranges)]
        else:
            center = parse_float_fields(
                self.center_ang_vars,
                ["cx", "cy", "cz"],
                "Центр в ангстремах",
            )
            ranges = parse_float_fields(
                self.range_ang_vars,
                ["x0", "x1", "y0", "y1", "z0", "z1"],
                "Смещения в ангстремах",
            )
            validate_ranges(ranges, "Смещения в ангстремах")
            command += ["--center-ang", *(number_text(value) for value in center)]
            command += ["--range-ang", *(number_text(value) for value in ranges)]
            command += ["--ang-axes", self.ang_axes_var.get()]

        self.last_outdir = outdir
        return command, outdir

    def _run(self) -> None:
        try:
            command, outdir = self.build_command()
        except ValidationError as exc:
            messagebox.showerror("Проверка параметров", str(exc), parent=self)
            return
        self.app.start_command(
            command,
            "Построение суперячейки",
            outdir,
            on_success=self._after_success,
        )

    def _after_success(self) -> None:
        if self.last_outdir is not None:
            self.app.last_output_dir = self.last_outdir
            self.app.open_button.state(["!disabled"])
            self.app.viewer_tab.set_directory(
                str(self.last_outdir),
                announce=False,
            )


class ViewerTab(ttk.Frame):
    DATASETS = [
        ("auto", "Авто"),
        ("base", "Базовая ячейка"),
        ("symm", "Симметрия"),
        ("supercell", "Суперячейка"),
        ("symm_supercell", "Симметрия + суперячейка"),
    ]

    FILE_SPECS = {
        "base": (
            "cell.pdb",
            "critical_points.pdb",
            "crystal_BPs.pdb",
            "calc_BPs.pdb",
        ),
        "symm": (
            "cell.pdb",
            "critical_points_symm.pdb",
            "crystal_BPs_symm.pdb",
            "calc_BPs_symm.pdb",
        ),
        "supercell": (
            "cell_supercell.pdb",
            "critical_points_supercell.pdb",
            "crystal_BPs_supercell.pdb",
            "calc_BPs_supercell.pdb",
        ),
        "symm_supercell": (
            "cell_supercell.pdb",
            "critical_points_symm_supercell.pdb",
            "crystal_BPs_symm_supercell.pdb",
            "calc_BPs_symm_supercell.pdb",
        ),
    }

    def __init__(self, parent: tk.Misc, app: App) -> None:
        super().__init__(parent, padding=14)
        self.app = app
        self.process: subprocess.Popen[str] | None = None
        self.reader_thread: threading.Thread | None = None
        self.poll_job: str | None = None
        self.viewer_running = False
        self.log_queue: queue.Queue[tuple[str, object]] = queue.Queue()
        self.dir_var = tk.StringVar()
        self.dataset_var = tk.StringVar(value="Авто")
        self.cell_var = tk.StringVar()
        self.cp_var = tk.StringVar()
        self.crystal_bp_var = tk.StringVar()
        self.calc_bp_var = tk.StringVar()
        self.topond_out_var = tk.StringVar()
        self.title_var = tk.StringVar()
        self.align_cp_var = tk.BooleanVar(value=True)
        self.status_var = tk.StringVar(
            value=(
                "Выберите папку с PDB. Файлы можно указать вручную, "
                "если автоматический поиск не подходит."
            )
        )

        self.columnconfigure(1, weight=1)

        ttk.Label(
            self,
            text="Шаг 4. Просмотр структуры в JSmol",
            style="Title.TLabel",
        ).grid(row=0, column=0, columnspan=4, sticky="w")
        ttk.Label(
            self,
            text=(
                "Атомы показываются в CPK-стиле. Критические точки и "
                "BondPaths рисуются плоскими точками; CP по умолчанию "
                "совмещаются с атомами. Цвета, размеры и видимость слоёв "
                "настраиваются в окне JSmol."
            ),
            style="Subtitle.TLabel",
            wraplength=980,
            justify="left",
        ).grid(row=1, column=0, columnspan=4, sticky="w", pady=(2, 12))

        ttk.Label(self, text="Папка с PDB:").grid(
            row=2, column=0, sticky="w", pady=4
        )
        self.dir_entry = ttk.Entry(self, textvariable=self.dir_var)
        self.dir_entry.grid(
            row=2, column=1, columnspan=2, sticky="ew", padx=(8, 6), pady=4
        )
        self.dir_entry.bind("<Return>", lambda _event: self._detect())
        ttk.Button(
            self,
            text="Обзор…",
            command=self._browse_dir,
        ).grid(row=2, column=3, pady=4)

        ttk.Label(self, text="Набор файлов:").grid(
            row=3, column=0, sticky="w", pady=4
        )
        self.dataset_combo = ttk.Combobox(
            self,
            textvariable=self.dataset_var,
            values=[label for _key, label in self.DATASETS],
            state="readonly",
            width=28,
        )
        self.dataset_combo.grid(
            row=3, column=1, sticky="w", padx=(8, 6), pady=4
        )
        self.dataset_combo.bind(
            "<<ComboboxSelected>>",
            lambda _event: self._detect(),
        )
        ttk.Button(
            self,
            text="Определить файлы",
            command=self._detect,
        ).grid(row=3, column=2, sticky="w", padx=(0, 6), pady=4)

        files_frame = ttk.LabelFrame(self, text="Файлы PDB", padding=10)
        files_frame.grid(
            row=4, column=0, columnspan=4, sticky="ew", pady=(12, 6)
        )
        files_frame.columnconfigure(1, weight=1)

        self._add_file_row(
            files_frame,
            0,
            "Атомы / ячейка:",
            self.cell_var,
            "cell.pdb",
        )
        self._add_file_row(
            files_frame,
            1,
            "Критические точки:",
            self.cp_var,
            "critical_points.pdb",
        )
        self._add_file_row(
            files_frame,
            2,
            "BondPaths (crystal):",
            self.crystal_bp_var,
            "crystal_BPs.pdb",
        )
        self._add_file_row(
            files_frame,
            3,
            "BondPaths (calc):",
            self.calc_bp_var,
            "calc_BPs.pdb",
        )

        controls = ttk.Frame(self)
        controls.grid(row=5, column=0, columnspan=4, sticky="w", pady=(8, 4))
        self.launch_button = ttk.Button(
            controls,
            text="Открыть JSmol",
            style="Run.TButton",
            command=self._launch,
        )
        self.launch_button.pack(side="left", padx=(0, 8))
        self.stop_button = ttk.Button(
            controls,
            text="Остановить",
            command=self._stop,
            state="disabled",
        )
        self.stop_button.pack(side="left")
        ttk.Checkbutton(
            controls,
            text="Совместить CP с атомами",
            variable=self.align_cp_var,
            command=lambda: self._describe_files(announce=False),
        ).pack(side="left", padx=(16, 0))

        ttk.Label(
            self,
            textvariable=self.status_var,
            style="Subtitle.TLabel",
            wraplength=980,
            justify="left",
        ).grid(row=6, column=0, columnspan=4, sticky="w", pady=(8, 2))

        self.after(250, self._poll)

    def _add_file_row(
        self,
        parent: ttk.LabelFrame,
        row: int,
        label: str,
        variable: tk.StringVar,
        default_name: str,
    ) -> None:
        ttk.Label(parent, text=label).grid(
            row=row, column=0, sticky="w", pady=3
        )
        ttk.Entry(parent, textvariable=variable).grid(
            row=row, column=1, sticky="ew", padx=(8, 6), pady=3
        )
        ttk.Button(
            parent,
            text="Обзор…",
            command=lambda v=variable, n=default_name: self._browse_file(v, n),
        ).grid(row=row, column=2, pady=3)

    def _dataset_key(self) -> str:
        for key, label in self.DATASETS:
            if label == self.dataset_var.get():
                return key
        return "auto"

    def _browse_dir(self) -> None:
        initial = dialog_initial_dir(self.dir_var.get())
        path = filedialog.askdirectory(
            title="Папка с PDB-файлами",
            initialdir=str(initial),
            parent=self,
        )
        if path:
            self.dir_var.set(path)
            self._detect()

    def _browse_topond_out(self) -> None:
        initial = dialog_initial_dir(self.dir_var.get())
        path = filedialog.askopenfilename(
            title="Выберите файл CP_BPs .out",
            initialdir=str(initial),
            filetypes=(("TOPOND .out", "*.out"), ("Все файлы", "*.*")),
            parent=self,
        )
        if path:
            self.topond_out_var.set(path)

    def _browse_file(self, variable: tk.StringVar, default_name: str) -> None:
        initial = dialog_initial_dir(self.dir_var.get())
        initial_file = initial / default_name
        path = filedialog.askopenfilename(
            title="Выберите PDB-файл",
            initialdir=str(initial),
            initialfile=initial_file.name,
            filetypes=[
                ("PDB files", "*.pdb"),
                ("All files", "*"),
            ],
            parent=self,
        )
        if path:
            variable.set(path)

    def set_directory(self, path: str, announce: bool = True) -> None:
        text = str(path).strip()
        if not text:
            return
        self.dir_var.set(text)
        self._detect(announce=announce)

    def _detect(self, announce: bool = True) -> None:
        directory = resolve_user_path(self.dir_var.get())
        if not directory.is_dir():
            if announce:
                messagebox.showerror(
                    "Проверка папки",
                    "Укажите существующую папку с PDB-файлами.",
                    parent=self,
                )
            return

        key = self._dataset_key()
        if key == "auto":
            key = self._autodetect_dataset(directory)
        names = self.FILE_SPECS[key]
        paths = [directory / name for name in names]
        self.cell_var.set(str(paths[0]) if paths[0].is_file() else "")
        self.cp_var.set(str(paths[1]) if paths[1].is_file() else "")
        self.crystal_bp_var.set(str(paths[2]) if paths[2].is_file() else "")
        self.calc_bp_var.set(str(paths[3]) if paths[3].is_file() else "")
        for name in ("CP_BPs_dedup_final.out", "CP_BPs_dedup.out", "CP_BPs.out"):
            p = directory / name
            if p.is_file() and not self.topond_out_var.get().strip():
                self.topond_out_var.set(str(p))
                break
        self._describe_files(announce)

    def _autodetect_dataset(self, directory: Path) -> str:
        checks = (
            (
                "symm_supercell",
                (
                    "critical_points_symm_supercell.pdb",
                    "crystal_BPs_symm_supercell.pdb",
                ),
            ),
            (
                "supercell",
                (
                    "critical_points_supercell.pdb",
                    "crystal_BPs_supercell.pdb",
                ),
            ),
            (
                "symm",
                (
                    "critical_points_symm.pdb",
                    "crystal_BPs_symm.pdb",
                ),
            ),
            (
                "base",
                (
                    "critical_points.pdb",
                    "crystal_BPs.pdb",
                ),
            ),
        )
        for key, names in checks:
            if any((directory / name).is_file() for name in names):
                return key
        return "base"

    def _describe_files(self, announce: bool = True) -> None:
        cell = resolve_user_path(self.cell_var.get())
        if not cell.is_file():
            self.status_var.set(
                "Не найден файл атомов/ячейки. Проверьте папку или "
                "выберите cell.pdb (cell_supercell.pdb) вручную."
            )
            if announce:
                self.app._append_log(
                    "JSmol: файл атомов/ячейки не найден."
                )
            return

        command = self._build_command()
        if command is None:
            self.status_var.set(
                f"Найден файл атомов: {cell.name}. "
                "Остальные файлы можно указать вручную."
            )
            return
        command.append("--summary")

        self.status_var.set("Проверка PDB-файлов…")
        self.update_idletasks()
        try:
            result = subprocess.run(
                command,
                cwd=str(BASE_DIR),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=30,
                env=_child_env(),
            )
        except (OSError, subprocess.SubprocessError) as exc:
            self.status_var.set(
                f"Файлы выбраны, но проверить их не удалось: {exc}"
            )
            return
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip().splitlines()
            message = detail[-1] if detail else "неизвестная ошибка"
            self.status_var.set(f"Ошибка проверки файлов: {message}")
            return
        payload = None
        for line in reversed(result.stdout.splitlines()):
            line = line.strip()
            if line.startswith("{"):
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError:
                    payload = None
                if payload is not None:
                    break
        if not isinstance(payload, dict):
            self.status_var.set("Файлы выбраны. JSmol готов к запуску.")
            return
        counts = payload.get("counts", {})
        parts = [
            f"атомов: {counts.get('atoms', 0)}",
            f"BCP: {counts.get('bcp', 0)}",
            f"RCP: {counts.get('rcp', 0)}",
            f"CCP: {counts.get('ccp', 0)}",
            f"NCP: {counts.get('ncp', 0)}",
            f"crystal BP: {counts.get('bp_crystal', 0)}",
            f"calc BP: {counts.get('bp_calc', 0)}",
        ]
        align = payload.get("cpAlign") or {}
        if align.get("enabled"):
            parts.append(f"CP выровнено: {int(align.get('moved', 0) or 0)}")
        self.status_var.set(" · ".join(parts))
        if announce:
            self.app._append_log(f"JSmol: {' · '.join(parts)}")

    def _build_command(self) -> list[str] | None:
        cell = self.cell_var.get().strip()
        if not cell:
            return None
        command = [
            worker_python(),
            "-u",
            str(VIEWER_SCRIPT),
            "--cell",
            cell,
        ]
        for option, text in (
            ("--cp", self.cp_var.get()),
            ("--crystal-bp", self.crystal_bp_var.get()),
            ("--calc-bp", self.calc_bp_var.get()),
            ("--topond-out", self.topond_out_var.get()),
        ):
            text = text.strip()
            if text:
                command.extend([option, text])
        command.append(
            "--align-cp" if self.align_cp_var.get() else "--no-align-cp"
        )
        return command

    def _launch(self) -> None:
        if not VIEWER_SCRIPT.is_file():
            messagebox.showerror(
                "JSmol",
                f"Не найден модуль просмотра:\n{VIEWER_SCRIPT}",
                parent=self,
            )
            return
        if not self.cell_var.get().strip():
            self._detect()
            if not self.cell_var.get().strip():
                messagebox.showerror(
                    "Проверка параметров",
                    "Не найден PDB-файл атомов/ячейки.",
                    parent=self,
                )
                return
        command = self._build_command()
        if command is None:
            messagebox.showerror(
                "Проверка параметров",
                "Не найден PDB-файл атомов/ячейки.",
                parent=self,
            )
            return
        for path in command[4:]:
            if path.startswith("--"):
                continue
            if not resolve_user_path(path).is_file():
                messagebox.showerror(
                    "Проверка параметров",
                    f"Файл не найден:\n{path}",
                    parent=self,
                )
                return

        if self.viewer_running:
            answer = messagebox.askyesno(
                "JSmol уже открыт",
                "Закрыть текущее окно JSmol и открыть новое?",
                parent=self,
            )
            if not answer:
                return
            self._stop()

        kwargs: dict[str, object] = {}
        if os.name != "nt":
            kwargs["start_new_session"] = True
        env = _child_env()
        try:
            process = subprocess.Popen(
                command,
                cwd=str(BASE_DIR),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                env=env,
                **kwargs,
            )
        except OSError as exc:
            log_error(LOGGER, "GUI-E002", "failed to launch viewer", exc=exc,
                      command=" ".join(command))
            messagebox.showerror(
                "JSmol",
                f"Не удалось запустить просмотрщик:\n{exc}",
                parent=self,
            )
            return

        log_action(LOGGER, "viewer_launch", pid=process.pid,
                   command=" ".join(command))
        self.process = process
        self.viewer_running = True
        self.launch_button.state(["disabled"])
        self.stop_button.state(["!disabled"])
        self.app._append_log("JSmol: запуск просмотрщика…")
        self.status_var.set("JSmol загружается…")
        self.reader_thread = threading.Thread(
            target=self._read_output,
            args=(process,),
            daemon=True,
        )
        self.reader_thread.start()

    def _read_output(self, process: subprocess.Popen[str]) -> None:
        stream = process.stdout
        if stream is not None:
            try:
                for line in stream:
                    self.log_queue.put(("line", line.rstrip()))
            except (OSError, ValueError):
                pass
        code = process.wait()
        self.log_queue.put(("done", code))

    def _poll(self) -> None:
        try:
            while True:
                kind, value = self.log_queue.get_nowait()
                if kind == "line":
                    self._handle_line(str(value))
                elif kind == "done":
                    self._finish(int(value))
        except queue.Empty:
            pass
        try:
            self.poll_job = self.after(150, self._poll)
        except tk.TclError:
            self.poll_job = None

    def _handle_line(self, text: str) -> None:
        if not text:
            return
        if text.startswith("STATE "):
            return
        if text.startswith("VIEWER_READY "):
            self.status_var.set("Окно JSmol открыто.")
            self.app._append_log("JSmol: окно открыто.")
            return
        if text.startswith("SCREENSHOT ") or text.startswith("EVAL "):
            return
        if text.startswith("Загружено:"):
            self.status_var.set(text)
        elif text.startswith("Крупная ячейка:"):
            self.status_var.set(text)
        elif text.startswith("CP выровнены:"):
            self.status_var.set(text)
        elif text.startswith("ERROR"):
            self.status_var.set(text)
        self.app._append_log(f"JSmol: {text}")

    def _finish(self, code: int) -> None:
        if not self.viewer_running and self.process is None:
            return
        self.process = None
        self.viewer_running = False
        self.launch_button.state(["!disabled"])
        self.stop_button.state(["disabled"])
        if code == 0:
            self.status_var.set("Окно JSmol закрыто.")
            self.app._append_log("JSmol: окно закрыто.")
        else:
            self.status_var.set(f"JSmol завершился с кодом {code}.")
            self.app._append_log(
                f"JSmol: процесс завершился с кодом {code}."
            )

    def _stop(self) -> None:
        self.stop_viewer(silent=False)

    def stop_viewer(self, silent: bool = False) -> None:
        process = self.process
        if process is None:
            return
        if process.poll() is not None:
            self.process = None
            self.viewer_running = False
            try:
                self.launch_button.state(["!disabled"])
                self.stop_button.state(["disabled"])
            except tk.TclError:
                pass
            return
        try:
            process.terminate()
            process.wait(timeout=2)
        except (OSError, subprocess.TimeoutExpired):
            try:
                process.kill()
                process.wait(timeout=1)
            except (OSError, subprocess.TimeoutExpired):
                pass
        self.process = None
        self.viewer_running = False
        if not silent:
            self.status_var.set("Окно JSmol остановлено.")
            self.app._append_log("JSmol: просмотрщик остановлен.")
        try:
            self.launch_button.state(["!disabled"])
            self.stop_button.state(["disabled"])
        except tk.TclError:
            pass



def main() -> int:
    setup("gui")
    install_excepthook(LOGGER, "GUI-E001")
    has_fonts = _tk_has_unicode_fonts()
    if has_fonts is False and not os.environ.get(GUI_REEXEC_ENV):
        alternative = _find_unicode_gui_python()
        if alternative is not None:
            log_action(LOGGER, "reexec", python=alternative)
            try:
                _reexec_with_python(alternative)
            except OSError:
                pass
    if has_fonts is False:
        log_error(LOGGER, "GUI-E003", "no Unicode fonts available")
        return _report_font_problem()
    log_action(LOGGER, "start", log_file=str(log_path()), pid=os.getpid())
    try:
        app = App()
    except tk.TclError as exc:
        log_error(LOGGER, "GUI-E003", "cannot open GUI window", exc=exc)
        print(
            "ERROR: не удалось открыть графическое окно "
            f"(проверьте DISPLAY): {exc}",
            file=sys.stderr,
        )
        return 2
    _ensure_worker_python()
    app.mainloop()
    log_action(LOGGER, "exit")
    return 0


if __name__ == "__main__":
    sys.exit(main())
