#!/usr/bin/env python3
"""
bridge.py — MUD Bridge Server entry point.

Holds one shared telnet connection to the MUD and fans output to all
local clients (main.py --bridge, mud_mcp_server with use_bridge=True).

Usage:
  python bridge.py                        # connect to dsl-mud.org:4000, listen on 127.0.0.1:4001
  python bridge.py --host example.com --port 4000
  python bridge.py --bridge-port 4002     # use a different local port
  python bridge.py --log DEBUG            # verbose logging

Workflow:
  Terminal 1: python bridge.py            # connects to MUD, auto-login trigger fires
  Terminal 2: python main.py --bridge     # human plays via bridge
  Claude Desktop: mud_connect(use_bridge=True)  # Claude plays same session
"""

import argparse
import asyncio
import logging
import sys
from pathlib import Path

_HERE = Path(__file__).parent.resolve()
sys.path.insert(0, str(_HERE))


def main() -> None:
    parser = argparse.ArgumentParser(
        description="MUD Bridge Server — shared connection fan-out",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--host",         default="dsl-mud.org",  help="MUD server hostname")
    parser.add_argument("--port",         default=4000, type=int, help="MUD server port")
    parser.add_argument("--bridge-host",  default="127.0.0.1",    help="Bridge listen address (default 127.0.0.1)")
    parser.add_argument("--bridge-port",  default=4001, type=int, help="Bridge listen port (default 4001)")
    parser.add_argument("--config",       default="configs",       help="Config directory (default configs)")
    parser.add_argument("--rate-limit",   default=0.5, type=float,
                        metavar="SECS",
                        help="Minimum seconds between outgoing MUD commands (default 0.5)")
    parser.add_argument("--log",          default="INFO",
                        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
                        help="Log level (default INFO)")
    parser.add_argument("--quiet",        action="store_true",
                        help="Suppress MUD output from bridge terminal (useful when running in background)")
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log),
        format="%(asctime)s [BRIDGE] %(name)s %(levelname)s %(message)s",
    )

    from mud.bridge import MudBridgeServer

    bridge = MudBridgeServer(
        mud_host=args.host,
        mud_port=args.port,
        bridge_host=args.bridge_host,
        bridge_port=args.bridge_port,
        config_dir=args.config,
        rate_limit=args.rate_limit,
        quiet=args.quiet,
    )

    try:
        asyncio.run(bridge.start())
    except KeyboardInterrupt:
        print("\n[BRIDGE] Interrupted.", file=sys.stderr)


if __name__ == "__main__":
    main()
