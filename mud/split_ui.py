"""
Split terminal UI for Mudder.
Left pane: main MUD stream.
Right pane: communication capture stream.
Bottom: input line.
"""

import asyncio
import re
from typing import Optional

from prompt_toolkit.application import Application
from prompt_toolkit.document import Document
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import HSplit, Layout, VSplit
from prompt_toolkit.widgets import Frame, TextArea

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[mKHJABCDsuhl]")


class SplitUI:
    def __init__(self) -> None:
        self._queue: asyncio.Queue[Optional[str]] = asyncio.Queue()
        self._app: Optional[Application] = None
        self._task: Optional[asyncio.Task] = None

        self.main_area = TextArea(
            text="",
            read_only=True,
            scrollbar=True,
            wrap_lines=False,
            focusable=False,
        )
        self.capture_area = TextArea(
            text="",
            read_only=True,
            scrollbar=True,
            wrap_lines=True,
            focusable=False,
        )
        self.input_area = TextArea(
            height=1,
            prompt="> ",
            multiline=False,
            wrap_lines=False,
        )
        self.input_area.accept_handler = self._on_accept

    async def start(self) -> None:
        kb = KeyBindings()

        @kb.add("c-c")
        def _exit(event) -> None:
            event.app.exit()

        root = HSplit(
            [
                VSplit(
                    [
                        Frame(self.main_area, title="MUD Stream"),
                        Frame(self.capture_area, title="Capture", width=48),
                    ]
                ),
                Frame(self.input_area, title="Input"),
            ]
        )
        self._app = Application(
            layout=Layout(root, focused_element=self.input_area),
            key_bindings=kb,
            full_screen=True,
        )
        self._task = asyncio.create_task(self._app.run_async())

        # When the app exits for any reason, unblock any waiting read_line().
        def _on_done(task: asyncio.Task) -> None:
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
        new_text = buf.text + _ANSI_RE.sub("", text)
        # Cursor at start of last line: scrolls vertically to bottom
        # without horizontal scrolling that clips line beginnings.
        cursor = new_text.rfind("\n") + 1
        buf.set_document(Document(new_text, cursor), bypass_readonly=True)
        if self._app is not None and self._app.is_running:
            self._app.invalidate()

    def append_capture(self, text: str) -> None:
        buf = self.capture_area.buffer
        new_text = buf.text + _ANSI_RE.sub("", text)
        buf.set_document(Document(new_text, len(new_text)), bypass_readonly=True)
        if self._app is not None and self._app.is_running:
            self._app.invalidate()

    def _on_accept(self, buffer) -> bool:
        self._queue.put_nowait(buffer.text)
        return False  # prompt_toolkit resets the buffer automatically
