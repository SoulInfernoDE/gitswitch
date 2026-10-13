"""Command line entry point: GUI by default, plus a few scriptable commands."""

from __future__ import annotations

import argparse
import json
import sys

from . import __version__, core
from .i18n import _


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="gitswitch", description=_("Switch between GitHub CLI accounts."))
    parser.add_argument("--version", action="version", version=f"gitswitch {__version__}")
    commands = parser.add_subparsers(dest="command")
    commands.add_parser("gui", help=_("open the window (default)"))
    commands.add_parser("list", help=_("print the logged-in accounts as JSON"))
    switch = commands.add_parser("switch", help=_("activate an account"))
    switch.add_argument("user")
    switch.add_argument("--hostname")
    commands.add_parser("mcp", help=_("run the MCP server on stdio"))
    credential = commands.add_parser("git-credential", help=_("answer git's credential requests for Forgejo accounts"))
    credential.add_argument("operation")
    args = parser.parse_args(argv)

    if args.command == "mcp":
        from . import mcp

        return mcp.main()
    if args.command == "git-credential":
        from . import forge

        sys.stdout.write(forge.credential_helper(args.operation, sys.stdin.read()))
        return 0
    if args.command in ("list", "switch"):
        try:
            if args.command == "list":
                result = core.describe_accounts()
            else:
                result = core.switch_account(args.user, args.hostname)
        except core.GhError as error:
            print(_("gitswitch: {error}").format(error=error), file=sys.stderr)
            return 1
        print(json.dumps(result, indent=2))
        return 0

    from . import gui

    return gui.main()


if __name__ == "__main__":
    sys.exit(main())
