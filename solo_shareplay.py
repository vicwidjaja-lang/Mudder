#!/usr/bin/env python3
"""
solo_shareplay.py

Standalone non-terminal shared client for Mudder bridge sessions.
Two input lanes ("You" and "Agent") send commands into the same bridge
connection and share one live output stream.
"""

from __future__ import annotations

import argparse
import asyncio
import queue
import re
import threading
import tkinter as tk
from tkinter import ttk
from tkinter.scrolledtext import ScrolledText
from typing import Optional

from mud.bridge_client import BridgeClient

ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


class SoloSharePlayApp:
    def __init__(self, host: str, port: int) -> None:
        self.host = host
        self.port = port

        self.root = tk.Tk()
        self.root.title("Mudder Solo SharePlay")
        self.root.geometry("1120x760")
        self.root.configure(bg="#0a1118")
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        self._queue: queue.Queue[tuple[str, str]] = queue.Queue()
        self._loop = asyncio.new_event_loop()
        self._loop_thread = threading.Thread(target=self._run_loop, daemon=True)
        self._client: Optional[BridgeClient] = None
        self._read_task: Optional[asyncio.Task[None]] = None
        self._closing = False

        self.status_var = tk.StringVar(value=f"Disconnected ({self.host}:{self.port})")
        self.you_var = tk.StringVar()
        self.agent_var = tk.StringVar()
        self.you_entry: Optional[ttk.Entry] = None
        self.agent_entry: Optional[ttk.Entry] = None

        self._build_ui()
        self._loop_thread.start()
        self.root.after(80, self._drain_queue)
        self.root.after(150, self.connect)

    def _build_ui(self) -> None:
        style = ttk.Style()
        style.theme_use("clam")
        style.configure("Header.TFrame", background="#0f1f2e")
        style.configure("Header.TLabel", background="#0f1f2e", foreground="#d9e7ff", font=("Menlo", 11, "bold"))
        style.configure("Pane.TFrame", background="#0a1118")
        style.configure("Lane.TFrame", background="#0f1b28")
        style.configure("Lane.TLabel", background="#0f1b28", foreground="#b9ccdf", font=("Menlo", 10, "bold"))
        style.configure("Action.TButton", font=("Menlo", 10, "bold"))

        header = ttk.Frame(self.root, style="Header.TFrame", padding=(12, 10))
        header.pack(fill=tk.X)
        ttk.Label(header, text="Mudder Solo SharePlay", style="Header.TLabel").pack(side=tk.LEFT)
        ttk.Label(header, textvariable=self.status_var, style="Header.TLabel").pack(side=tk.LEFT, padx=(16, 0))
        ttk.Button(header, text="Reconnect", style="Action.TButton", command=self.connect).pack(side=tk.RIGHT)

        main = ttk.Frame(self.root, style="Pane.TFrame", padding=(10, 10))
        main.pack(fill=tk.BOTH, expand=True)

        self.stream = ScrolledText(
            main,
            wrap=tk.WORD,
            font=("Menlo", 11),
            bg="#0f141a",
            fg="#e8f3ff",
            insertbackground="#e8f3ff",
            relief=tk.FLAT,
            padx=10,
            pady=10,
            height=30,
        )
        self.stream.configure(takefocus=0)
        self.stream.pack(fill=tk.BOTH, expand=True)
        self.stream.insert(tk.END, "Connecting to bridge...\n")
        # Keep widget in normal state for cross-Tk rendering reliability.
        # Route any typing/paste attempts back into the input field.
        self.stream.bind("<Key>", self._route_keys_to_input)
        self.stream.bind("<<Paste>>", self._route_keys_to_input)

        lanes = ttk.Frame(main, style="Pane.TFrame")
        lanes.pack(fill=tk.X, pady=(10, 0))

        you_lane = ttk.Frame(lanes, style="Lane.TFrame", padding=(10, 8))
        you_lane.pack(fill=tk.X, pady=(0, 8))
        ttk.Label(you_lane, text="You", style="Lane.TLabel").pack(anchor=tk.W)
        self.you_entry = ttk.Entry(you_lane, textvariable=self.you_var, font=("Menlo", 12))
        self.you_entry.pack(fill=tk.X, pady=(4, 0))
        self.you_entry.bind("<Return>", lambda _evt: self.send_from_lane("you"))
        ttk.Button(you_lane, text="Send", style="Action.TButton", command=lambda: self.send_from_lane("you")).pack(anchor=tk.E, pady=(6, 0))

        agent_lane = ttk.Frame(lanes, style="Lane.TFrame", padding=(10, 8))
        agent_lane.pack(fill=tk.X)
        ttk.Label(agent_lane, text="Agent", style="Lane.TLabel").pack(anchor=tk.W)
        self.agent_entry = ttk.Entry(agent_lane, textvariable=self.agent_var, font=("Menlo", 12))
        self.agent_entry.pack(fill=tk.X, pady=(4, 0))
        self.agent_entry.bind("<Return>", lambda _evt: self.send_from_lane("agent"))
        ttk.Button(agent_lane, text="Send", style="Action.TButton", command=lambda: self.send_from_lane("agent")).pack(anchor=tk.E, pady=(6, 0))

        quick = ttk.Frame(main, style="Pane.TFrame")
        quick.pack(fill=tk.X, pady=(10, 0))
        for cmd in ["look", "score", "inventory", "north", "south", "east", "west", "recall"]:
            ttk.Button(quick, text=cmd, command=lambda c=cmd: self.send_direct(c)).pack(side=tk.LEFT, padx=(0, 6))

        self.root.after(50, self._focus_you)
        self.root.bind_all("<Button-1>", self._focus_input_on_click, add="+")
        self.root.bind_all("<Key>", self._route_keys_to_input, add="+")

    def _focus_you(self) -> None:
        if self.you_entry is not None:
            self.you_entry.focus_set()

    def _focus_input_on_click(self, event) -> None:
        target = event.widget
        if target in (self.you_entry, self.agent_entry):
            return
        if self.you_entry is not None:
            self.root.after_idle(self.you_entry.focus_set)

    def _route_keys_to_input(self, event) -> str | None:
        target = event.widget
        if target in (self.you_entry, self.agent_entry):
            return None
        if self.you_entry is None:
            return None
        if event.keysym == "Return":
            self.send_from_lane("you")
            return "break"
        if event.keysym in ("BackSpace", "Delete"):
            self.you_var.set(self.you_var.get()[:-1])
            return "break"
        if event.char and event.char.isprintable():
            self.you_var.set(self.you_var.get() + event.char)
            return "break"
        return None

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def connect(self) -> None:
        self._enqueue_local("SYS", f"Connecting to bridge {self.host}:{self.port} ...")
        fut = asyncio.run_coroutine_threadsafe(self._connect_async(), self._loop)
        fut.add_done_callback(lambda _: None)

    async def _connect_async(self) -> None:
        try:
            await self._disconnect_async()
            client = BridgeClient(self.host, self.port)
            client.on_data(self._on_data)
            await client.connect()
            self._client = client
            self._read_task = asyncio.create_task(client.read_loop())
            self._queue.put(("status", f"Connected ({self.host}:{self.port})"))
            self._enqueue_local("SYS", "Bridge connected.")
            # Request a fresh prompt/menu line so the stream is not blank on first connect.
            await client.send("")
        except Exception as exc:
            self._queue.put(("status", f"Disconnected ({self.host}:{self.port})"))
            self._enqueue_local("ERR", f"Bridge connect failed: {exc}")

    async def _disconnect_async(self) -> None:
        if self._read_task is not None:
            self._read_task.cancel()
            try:
                await self._read_task
            except asyncio.CancelledError:
                pass
            self._read_task = None
        if self._client is not None:
            try:
                await self._client.disconnect()
            except Exception:
                pass
            self._client = None

    def _on_data(self, text: str) -> None:
        clean = ANSI_RE.sub("", text)
        self._queue.put(("stream", clean))

    def send_from_lane(self, lane: str) -> None:
        raw = self.you_var.get() if lane == "you" else self.agent_var.get()
        # Preserve intentional blank-enter sends (needed by DSL menus/pagers).
        cmd = raw.rstrip("\r\n")
        if lane == "you":
            self.you_var.set("")
            self._enqueue_local("YOU", cmd if cmd else "<ENTER>")
            self._focus_you()
        else:
            self.agent_var.set("")
            self._enqueue_local("AGENT", cmd if cmd else "<ENTER>")
            if self.agent_entry is not None:
                self.agent_entry.focus_set()
        self.send_direct(cmd)

    def send_direct(self, command: str) -> None:
        fut = asyncio.run_coroutine_threadsafe(self._send_async(command), self._loop)
        fut.add_done_callback(lambda _: None)

    async def _send_async(self, command: str) -> None:
        if self._client is None or not self._client.connected:
            self._enqueue_local("ERR", "Not connected to bridge.")
            return
        ok = await self._client.send(command)
        if not ok:
            self._enqueue_local("ERR", f"Failed to send: {command}")
        else:
            self._queue.put(("status", f"Connected ({self.host}:{self.port})"))

    def _enqueue_local(self, lane: str, text: str) -> None:
        self._queue.put(("local", f"[{lane}] {text}\n"))

    def _drain_queue(self) -> None:
        while True:
            try:
                kind, payload = self._queue.get_nowait()
            except queue.Empty:
                break

            if kind == "status":
                self.status_var.set(payload)
                continue

            self.stream.insert(tk.END, payload)
            self.stream.see(tk.END)

        if not self._closing:
            self.root.after(80, self._drain_queue)

    def _on_close(self) -> None:
        self._closing = True
        try:
            fut = asyncio.run_coroutine_threadsafe(self._disconnect_async(), self._loop)
            fut.result(timeout=1.5)
        except Exception:
            pass
        try:
            self._loop.call_soon_threadsafe(self._loop.stop)
        except Exception:
            pass
        self.root.destroy()

    def run(self) -> None:
        self.root.mainloop()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Mudder Solo SharePlay GUI client")
    parser.add_argument("--bridge-host", default="127.0.0.1", help="Bridge host (default 127.0.0.1)")
    parser.add_argument("--bridge-port", default=4001, type=int, help="Bridge port (default 4001)")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    app = SoloSharePlayApp(args.bridge_host, args.bridge_port)
    app.run()


if __name__ == "__main__":
    main()
