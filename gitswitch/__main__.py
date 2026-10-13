"""Command line entry point: GUI by default, plus a few scriptable commands."""

from __future__ import annotations

import argparse
import json
import sys

from . import __version__, core


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="gitswitch", description="Switch between GitHub CLI accounts.")
    parser.add_argument("--version", action="version", version=f"gitswitch {__version__}")
    commands = parser.add_subparsers(dest="command")
    commands.add_parser("gui", help="open the window (default)")
    commands.add_parser("list", help="print the logged-in accounts as JSON")
    switch = commands.add_parser("switch", help="activate an account")
    switch.add_argument("user")
    switch.add_argument("--hostname")
    commands.add_parser("mcp", help="run the MCP server on stdio")
    args = parser.parse_args(argv)

    if args.command == "mcp":
        from . import mcp

        return mcp.main()
    if args.command in ("list", "switch"):
        try:
            if args.command == "list":
                result = core.describe_accounts()
            else:
                result = core.switch_account(args.user, args.hostname)
        except core.GhError as error:
            print(f"gitswitch: {error}", file=sys.stderr)
            return 1
        print(json.dumps(result, indent=2))
        return 0

    from . import gui

    return gui.main()


if __name__ == "__main__":
    sys.exit(main())
