"""
Split terminal UI for Mudder.
Left pane: main MUD stream (with ANSI color).
Right pane: communication capture stream.
Bottom: tick countdown + input line.
"""

import asyncio
import re
import subprocess
from typing import Callable, List, Optional, Tuple

from prompt_toolkit.application import Application
from prompt_toolkit.document import Document
from prompt_toolkit.filters import has_focus
from prompt_toolkit.formatted_text import ANSI, to_formatted_text
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import HSplit, Layout, VSplit
from prompt_toolkit.layout.containers import Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.lexers import Lexer
from prompt_toolkit.widgets import Frame, TextArea

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[mKHJABCDsuhl]")

# Maximum lines to keep in each buffer to prevent progressive slowdown.
# Increased so users can scroll farther back in-session.
_MAX_LINES = 12000


def _trim_lines(text: str, max_lines: int = _MAX_LINES) -> str:
    """Keep only the last max_lines lines of text."""
    lines = text.split("\n")
    if len(lines) <= max_lines:
        return text
    return "\n".join(lines[-max_lines:])


class _AnsiLexer(Lexer):
    """Lexer that renders ANSI escape codes as prompt_toolkit styles.

    The buffer stores stripped (plain) text for correct cursor/scroll math.
    This lexer holds a parallel list of raw ANSI lines and converts them
    to styled fragments for display.  Fragment text lengths match the
    stripped buffer lines, so no source↔display mapping is needed.
    """

    def __init__(self) -> None:
        self._lines: List[str] = []

    def set_lines(self, ansi_text: str) -> None:
        self._lines = ansi_text.split("\n")

    def lex_document(self, document):
        ansi_lines = self._lines
        doc_lines = document.lines

        def get_line(lineno: int) -> List[Tuple[str, str]]:
            if lineno < len(ansi_lines) and "\x1b[" in ansi_lines[lineno]:
                try:
                    return to_formatted_text(ANSI(ansi_lines[lineno]))
                except Exception:
                    pass
            # Fallback: plain text from the buffer
            if lineno < len(doc_lines):
                return [("", doc_lines[lineno])]
            return [("", "")]

        return get_line


class SplitUI:
    def __init__(self) -> None:
        self._queue: asyncio.Queue[Optional[str]] = asyncio.Queue()
        self._app: Optional[Application] = None
        self._task: Optional[asyncio.Task] = None
        self._tick_task: Optional[asyncio.Task] = None
        self._tick_countdown_fn: Optional[Callable[[], float]] = None

        # ANSI color support: lexer holds raw lines, buffer holds stripped text
        self._ansi_lexer = _AnsiLexer()
        self._main_ansi_text: str = ""

        self.main_area = TextArea(
            text="",
            read_only=True,
            scrollbar=True,
            wrap_lines=False,
            focusable=True,
            focus_on_click=True,
            lexer=self._ansi_lexer,
        )
        self.capture_area = TextArea(
            text="",
            read_only=True,
            scrollbar=True,
            wrap_lines=True,
            focusable=True,
            focus_on_click=True,
        )
        self.input_area = TextArea(
            height=1,
            prompt="> ",
            multiline=False,
            wrap_lines=False,
        )
        self.input_area.accept_handler = self._on_accept
        self._tick_control = FormattedTextControl(text=self._get_tick_text)

    def set_tick_source(self, fn: Callable[[], float]) -> None:
        """Register a callable that returns seconds until next tick."""
        self._tick_countdown_fn = fn

    def _get_tick_text(self) -> list:
        """Return formatted text for the tick countdown label."""
        if self._tick_countdown_fn is None:
            return [("", "  --  ")]
        secs = self._tick_countdown_fn()
        val = int(secs)
        display = f" {val:>3}s "
        if secs <= 5.0:
            return [("fg:red bold", display)]
        return [("fg:cyan", display)]

    async def start(self) -> None:
        kb = KeyBindings()
        non_input_focus = has_focus(self.main_area) | has_focus(self.capture_area)

        @kb.add("c-q")
        def _exit(event) -> None:
            event.app.exit()

        @kb.add("c-c")
        def _copy_selection(event) -> None:
            self._copy_selected_text_to_clipboard()

        @kb.add("backspace", filter=non_input_focus)
        def _backspace_to_input(event) -> None:
            event.app.layout.focus(self.input_area)
            self.input_area.buffer.delete_before_cursor(count=1)

        @kb.add("delete", filter=non_input_focus)
        def _delete_to_input(event) -> None:
            event.app.layout.focus(self.input_area)
            self.input_area.buffer.delete(count=1)

        @kb.add("enter", filter=non_input_focus)
        def _enter_to_input(event) -> None:
            event.app.layout.focus(self.input_area)
            self._queue.put_nowait(self.input_area.buffer.text)
            self.input_area.buffer.reset()

        @kb.add("<any>", filter=non_input_focus)
        def _route_keys_to_input(event) -> None:
            # Keep typing functional even if focus lands in non-input panes.
            data = event.data or ""
            if not data:
                return
            if data in ("\r", "\n", "\x7f", "\x08"):
                return
            if data.isprintable():
                event.app.layout.focus(self.input_area)
                self.input_area.buffer.insert_text(data)

        tick_window = Window(
            content=self._tick_control, width=6, height=1,
        )

        root = HSplit(
            [
                VSplit(
                    [
                        Frame(self.main_area, title="MUD Stream"),
                        Frame(self.capture_area, title="Capture", width=48),
                    ]
                ),
                VSplit([tick_window, Frame(self.input_area, title="Input")]),
            ]
        )
        self._app = Application(
            layout=Layout(root, focused_element=self.input_area),
            key_bindings=kb,
            full_screen=True,
            mouse_support=True,
        )
        self._task = asyncio.create_task(self._app.run_async())
        self._tick_task = asyncio.create_task(self._tick_refresh_loop())

        # When the app exits for any reason, unblock any waiting read_line().
        def _on_done(task: asyncio.Task) -> None:
            # Consume the exception so asyncio does not emit
            # "Task exception was never retrieved" on non-TTY startup failures.
            if not task.cancelled():
                try:
                    task.exception()
                except Exception:
                    pass
            self._queue.put_nowait(None)
        self._task.add_done_callback(_on_done)

        # Yield once so the task can begin initializing.
        await asyncio.sleep(0)

        # If it died immediately (e.g. stdin is not a TTY), surface the error
        # so the caller can fall back to classic mode.
        if self._task.done():
            exc = self._task.exception() if not self._task.cancelled() else None
            raise exc or RuntimeError("Split UI exited immediately (not a TTY?)")

    async def stop(self) -> None:
        if self._tick_task is not None:
            self._tick_task.cancel()
            try:
                await self._tick_task
            except (asyncio.CancelledError, Exception):
                pass
        if self._app is not None:
            try:
                self._app.exit()
            except Exception:
                pass
        await self._queue.put(None)
        if self._task is not None:
            try:
                await self._task
            except Exception:
                pass

    async def read_line(self) -> Optional[str]:
        line = await self._queue.get()
        return line

    def append_main(self, text: str) -> None:
        buf = self.main_area.buffer
        stripped = _ANSI_RE.sub("", text)
        new_text = _trim_lines(buf.text + stripped)

        # Keep raw ANSI text in sync for the color lexer
        self._main_ansi_text = _trim_lines(self._main_ansi_text + text)
        self._ansi_lexer.set_lines(self._main_ansi_text)

        # Cursor at start of last line: scrolls vertically to bottom
        # without horizontal scrolling that clips line beginnings.
        cursor = new_text.rfind("\n") + 1
        buf.set_document(Document(new_text, cursor), bypass_readonly=True)
        if self._app is not None and self._app.is_running:
            self._app.invalidate()

    def append_capture(self, text: str) -> None:
        buf = self.capture_area.buffer
        new_text = _trim_lines(buf.text + _ANSI_RE.sub("", text))
        buf.set_document(Document(new_text, len(new_text)), bypass_readonly=True)
        if self._app is not None and self._app.is_running:
            self._app.invalidate()

    async def _tick_refresh_loop(self) -> None:
        """Refresh the tick countdown label every second."""
        while True:
            try:
                await asyncio.sleep(1)
                if self._app is not None and self._app.is_running:
                    self._app.invalidate()
            except asyncio.CancelledError:
                break

    def _on_accept(self, buffer) -> bool:
        self._queue.put_nowait(buffer.text)
        return False  # prompt_toolkit resets the buffer automatically

    def _copy_selected_text_to_clipboard(self) -> bool:
        """Copy active selection (if any) to macOS clipboard via pbcopy."""
        for area in (self.main_area, self.capture_area, self.input_area):
            buf = area.buffer
            if buf.selection_state is None:
                continue
            data = buf.copy_selection(_cut=False)
            if not data.text:
                return False
            try:
                subprocess.run(["pbcopy"], input=data.text, text=True, check=False)
                return True
            except Exception:
                return False
        return False
