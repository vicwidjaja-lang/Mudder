#!/usr/bin/env python3
"""
Mudder - A MUD client for dsl-mud.org (and other Diku-based MUDs).

Usage:
  python main.py                    # connect to dsl-mud.org:4000
  python main.py --host example.com --port 4000
  python main.py --rate-limit 0.8   # more conservative pacing

Once connected, type normally to send commands.
Prefix client meta-commands with #:
  #help           list all client commands
  #aliases        show aliases
  #triggers       show triggers
  #skills         show auto-skill config
  #autoskill on   enable the skill bot
  #quit           disconnect and exit
"""

import argparse
import asyncio
import logging
import os
import socket
import subprocess
import sys
import time
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Mudder MUD client",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--host",         default="dsl-mud.org",  help="Server hostname")
    parser.add_argument("--port",         default=4000, type=int, help="Server port")
    parser.add_argument("--config",       default="configs",       help="Config directory")
    parser.add_argument("--rate-limit",   default=0.5, type=float,
                        metavar="SECS",
                        help="Minimum seconds between outgoing commands (default 0.5)")
    parser.add_argument("--log",          default="WARNING",
                        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
                        help="Log level (default WARNING)")
    parser.add_argument("--bridge",       action="store_true",
                        help="Connect via local bridge server instead of directly to MUD")
    parser.add_argument("--bridge-host",  default="127.0.0.1",
                        help="Bridge server address (default 127.0.0.1)")
    parser.add_argument("--bridge-port",  default=4001, type=int,
                        help="Bridge server port (default 4001)")
    parser.add_argument("--bridge-client-idle-timeout", default=0.0, type=float,
                        metavar="SECS",
                        help="When auto-launching the bridge, set its client idle timeout "
                             "(default 0 disables disconnects)")
    parser.add_argument("--ui",           default="classic",
                        choices=["classic", "split"],
                        help="UI mode: classic prompt or split-pane capture view")
    parser.add_argument("--launch-bridge", action="store_true",
                        help="Auto-start bridge.py in the background (implies --bridge). "
                             "Bridge output is suppressed so only the TK UI is visible.")
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log),
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    _bridge_proc = None
    _launched_bridge = False
    if args.launch_bridge:
        args.bridge = True
        bridge_running = False
        try:
            with socket.create_connection((args.bridge_host, args.bridge_port), timeout=1):
                bridge_running = True
        except OSError:
            pass

        if bridge_running:
            print(f"[CLIENT] Using existing bridge at {args.bridge_host}:{args.bridge_port}")
        else:
            bridge_script = Path(__file__).parent / "bridge.py"
            _bridge_proc = subprocess.Popen(
                [
                    sys.executable, str(bridge_script),
                    "--quiet",
                    "--host",        args.host,
                    "--port",        str(args.port),
                    "--bridge-host", args.bridge_host,
                    "--bridge-port", str(args.bridge_port),
                    "--config",      args.config,
                    "--client-idle-timeout", str(args.bridge_client_idle_timeout),
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            _launched_bridge = True
            # Wait up to 10 s for the bridge to start listening
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                try:
                    with socket.create_connection(
                        (args.bridge_host, args.bridge_port), timeout=1
                    ):
                        break
                except OSError:
                    time.sleep(0.2)
            else:
                _bridge_proc.kill()
                print("ERROR: Bridge server did not start in time.", file=sys.stderr)
                sys.exit(1)

    from mud.app import MudApp
    if args.bridge:
        from mud.bridge_client import BridgeClient
        app = MudApp(
            host=args.host,
            port=args.port,
            config_dir=args.config,
            rate_limit=args.rate_limit,
            bridge_mode=True,
            ui_mode=args.ui,
        )
        app.client = BridgeClient(args.bridge_host, args.bridge_port)
    else:
        app = MudApp(
            host=args.host,
            port=args.port,
            config_dir=args.config,
            rate_limit=args.rate_limit,
            ui_mode=args.ui,
        )

    try:
        asyncio.run(app.run())
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
    finally:
        restart = getattr(app, '_restart_requested', False)
        if _bridge_proc is not None and _launched_bridge and not restart:
            _bridge_proc.terminate()
            try:
                _bridge_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                _bridge_proc.kill()
        if restart:
            os.execv(sys.executable, [sys.executable] + sys.argv)


if __name__ == "__main__":
    main()
