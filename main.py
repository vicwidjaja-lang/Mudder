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
import sys


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Mudder MUD client",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--host",       default="dsl-mud.org",  help="Server hostname")
    parser.add_argument("--port",       default=4000, type=int, help="Server port")
    parser.add_argument("--config",     default="configs",       help="Config directory")
    parser.add_argument("--rate-limit", default=0.5, type=float,
                        metavar="SECS",
                        help="Minimum seconds between outgoing commands (default 0.5)")
    parser.add_argument("--log",        default="WARNING",
                        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
                        help="Log level (default WARNING)")
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log),
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    from mud.app import MudApp
    app = MudApp(
        host=args.host,
        port=args.port,
        config_dir=args.config,
        rate_limit=args.rate_limit,
    )

    try:
        asyncio.run(app.run())
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)


if __name__ == "__main__":
    main()
