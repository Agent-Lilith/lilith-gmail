import logging
import threading
from collections.abc import Callable
from typing import Any

from rich.console import Console, Group, RenderableType
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.progress import (
    BarColumn,
    Progress,
    TaskProgressColumn,
    TextColumn,
    TimeElapsedColumn,
    TimeRemainingColumn,
)
from rich.table import Table
from rich.text import Text

STYLE_TITLE = "bold cyan"
STYLE_VALUE = "default"
STYLE_SUCCESS = "bold green"
STYLE_ERROR = "bold red"
STYLE_WARNING = "bold yellow"
STYLE_BORDER_MAIN = "blue"
STYLE_BORDER_LOGS = "yellow"
STYLE_COMPLETE = "bold green"


class RecentLogHandler(logging.Handler):
    def __init__(self, max_lines: int = 14) -> None:
        super().__init__(level=logging.DEBUG)
        self.max_lines = max_lines
        self._records: list[tuple[int, str]] = []
        self._lock = threading.Lock()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = self.format(record)
            with self._lock:
                self._records.append((record.levelno, msg))
                if len(self._records) > self.max_lines:
                    self._records.pop(0)
        except Exception:
            pass

    def get_lines(self) -> list[tuple[int, str]]:
        with self._lock:
            return list(self._records)


def _styled_log_line(levelno: int, line: str, max_width: int | None = None) -> Text:
    if levelno >= logging.ERROR:
        style = STYLE_ERROR
    elif levelno >= logging.WARNING:
        style = STYLE_WARNING
    else:
        style = STYLE_VALUE
    text = (
        line
        if max_width is None or len(line) <= max_width
        else line[: max_width - 1] + "…"
    )
    return Text(text, style=style)


def run_transform_with_tui(
    run_pipeline_fn: Callable[[Callable[[dict], None]], int],
    total: int,
    *,
    refresh_per_sec: float = 4.0,
) -> int:
    state: dict[str, Any] = {
        "processed": 0,
        "failed": 0,
        "by_tier": {},
        "body_full": 0,
        "body_chunked": 0,
        "batch_num": 0,
        "total_batches": max(1, (total or 1)),
        "total": total or 0,
        "done": False,
        "result": 0,
        "error": None,
    }
    state_lock = threading.Lock()

    def progress_callback(data: dict) -> None:
        with state_lock:
            state["processed"] = data.get("processed", 0)
            state["failed"] = data.get("failed", 0)
            state["by_tier"] = data.get("by_tier") or {}
            state["body_full"] = data.get("body_full", 0)
            state["body_chunked"] = data.get("body_chunked", 0)
            state["batch_num"] = data.get("batch_num", 0)
            tb = data.get("total_batches")
            if tb is not None:
                state["total_batches"] = max(1, tb)
            if data.get("total") is not None:
                state["total"] = data["total"]

    def run() -> None:
        try:
            n = run_pipeline_fn(progress_callback)
            state["result"] = n
        except Exception as e:
            state["error"] = str(e)
        finally:
            state["done"] = True

    log_handler = RecentLogHandler(max_lines=14)
    log_handler.setLevel(logging.WARNING)
    log_handler.setFormatter(
        logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
    )
    transform_logger = logging.getLogger("transform")
    transform_logger.addHandler(log_handler)
    old_propagate = transform_logger.propagate
    transform_logger.propagate = False

    httpx_logger = logging.getLogger("httpx")
    httpcore_logger = logging.getLogger("httpcore")
    old_httpx_level = httpx_logger.level
    old_httpcore_level = httpcore_logger.level
    old_httpx_propagate = httpx_logger.propagate
    old_httpcore_propagate = httpcore_logger.propagate
    httpx_logger.setLevel(logging.WARNING)
    httpcore_logger.setLevel(logging.WARNING)
    httpx_logger.propagate = False
    httpcore_logger.propagate = False

    try:
        thread = threading.Thread(target=run, daemon=False)
        thread.start()

        progress = Progress(
            TextColumn("[bold blue]Transform[/]  ", table_column=None),
            BarColumn(
                bar_width=36,
                complete_style=STYLE_SUCCESS,
                finished_style=STYLE_SUCCESS,
                pulse_style="blue",
            ),
            TaskProgressColumn(),
            TextColumn("  "),
            TimeElapsedColumn(),
            TextColumn("  "),
            TimeRemainingColumn(compact=True, elapsed_when_finished=True),
            TextColumn("  "),
            TextColumn(f"[{STYLE_VALUE}]{{task.fields[status]}}[/]", table_column=None),
            expand=False,
            refresh_per_second=refresh_per_sec,
        )
        task_id = progress.add_task("run", total=total or 1, status="starting…")
        _last_total: list[int] = [total]
        _terminal_height: list[int | None] = [None]

        def make_layout(terminal_height: int | None = None) -> RenderableType:
            if terminal_height is not None:
                _terminal_height[0] = terminal_height
            height = _terminal_height[0]
            with state_lock:
                p = int(state.get("processed") or 0)
                f = int(state.get("failed") or 0)
                t = int(state.get("total") or total or 0)
                batch_num = int(state.get("batch_num") or 0)
                total_batches = max(1, int(state.get("total_batches") or 1))
                by_tier = state.get("by_tier") or {}
                if not isinstance(by_tier, dict):
                    by_tier = {}
                b_full = int(state.get("body_full") or 0)
                b_chunked = int(state.get("body_chunked") or 0)

            if t and t != _last_total[0]:
                _last_total[0] = t
                progress.update(task_id, total=t)
            completed = min(p + f, t) if t else (p + f)
            progress.update(
                task_id,
                completed=completed,
                status=f"batch {batch_num}/{total_batches}  ·  ok [green]{p:,}[/]  failed [red]{f:,}[/]",
            )

            stats = Table(box=None, show_header=False, expand=False, padding=(0, 1))
            stats.add_column(width=6)
            stats.add_column(justify="right", width=8)
            sens = int(by_tier.get(1) or 0)
            pers = int(by_tier.get(2) or 0)
            pub = int(by_tier.get(3) or 0)
            stats.add_row(f"[{STYLE_SUCCESS}]Ok[/]", f"[{STYLE_SUCCESS}]{p:,}[/]")
            stats.add_row(f"[{STYLE_ERROR}]Fail[/]", f"[{STYLE_ERROR}]{f:,}[/]")
            stats.add_row(f"[{STYLE_VALUE}]Total[/]", f"[{STYLE_VALUE}]{t:,}[/]")
            stats.add_row("", "")
            stats.add_row(f"[{STYLE_VALUE}]Sens[/]", f"[{STYLE_VALUE}]{sens:,}[/]")
            stats.add_row(f"[{STYLE_VALUE}]Pers[/]", f"[{STYLE_VALUE}]{pers:,}[/]")
            stats.add_row(f"[{STYLE_VALUE}]Pub[/]", f"[{STYLE_VALUE}]{pub:,}[/]")
            stats.add_row("", "")
            stats.add_row(f"[{STYLE_VALUE}]Full[/]", f"[{STYLE_VALUE}]{b_full:,}[/]")
            stats.add_row(
                f"[{STYLE_VALUE}]Chunk[/]", f"[{STYLE_VALUE}]{b_chunked:,}[/]"
            )

            stats_panel = Panel(
                stats,
                title=f"[{STYLE_TITLE}]Stats[/]",
                border_style=STYLE_BORDER_MAIN,
                padding=(0, 1),
            )

            log_records = log_handler.get_lines()
            if log_records:
                log_content = Group(
                    *[_styled_log_line(levelno, line) for levelno, line in log_records]
                )
                log_title = f"[{STYLE_TITLE}]Warnings / errors[/]  [{STYLE_VALUE}]({len(log_records)})[/]"
            else:
                log_content = Text("No warnings or errors yet.", style=STYLE_VALUE)
                log_title = f"[{STYLE_TITLE}]Warnings / errors[/]"
            log_panel = Panel(
                log_content,
                title=log_title,
                border_style=STYLE_BORDER_LOGS,
                padding=(0, 1),
            )

            TOP_ROWS = 3
            bottom_rows = (max(8, height - TOP_ROWS - 1)) if height is not None else 18
            layout = Layout()
            layout.split_column(
                Layout(Group(progress, ""), name="top", size=TOP_ROWS),
                Layout(name="bottom", size=bottom_rows),
            )
            layout["bottom"].split_row(
                Layout(stats_panel, name="left", size=28),
                Layout(log_panel, name="right"),
            )
            return layout

        with Live(
            make_layout(),
            refresh_per_second=refresh_per_sec,
            console=None,
            screen=True,
        ) as live:
            while thread.is_alive():
                h = live.console.size.height
                live.update(make_layout(terminal_height=h))
                thread.join(timeout=0.25)
            progress.update(task_id, completed=state["processed"] + state["failed"])
            h = live.console.size.height
            live.update(make_layout(terminal_height=h))

        if state["error"]:
            raise RuntimeError(state["error"])

        Console().print(
            f"  [{STYLE_COMPLETE}]Transform complete.[/] {state['result']:,} emails transformed."
        )
        return state["result"]
    finally:
        transform_logger.removeHandler(log_handler)
        transform_logger.propagate = old_propagate
        httpx_logger.setLevel(old_httpx_level)
        httpcore_logger.setLevel(old_httpcore_level)
        httpx_logger.propagate = old_httpx_propagate
        httpcore_logger.propagate = old_httpcore_propagate
