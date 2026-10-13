"""Minimal MCP server (stdio transport) exposing account switching as tools.

Implements just the parts of the Model Context Protocol that a tool-only
server needs, so no third-party SDK is required.
"""

from __future__ import annotations

import json
import sys

from . import __version__, core
from .i18n import _

PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")

TOOLS = [
    {
        "name": "list_github_accounts",
        "description": _(
            "List the accounts Git-Switch manages, per host: the GitHub accounts the GitHub CLI (gh) is "
            "logged in to and Forgejo or Gitea accounts, including which one is active and whether its "
            "token is still valid. Tokens are never returned."
        ),
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "switch_github_account",
        "description": _(
            "Switch the active account of a host: gh auth switch for GitHub, or the account git uses "
            "for a Forgejo or Gitea host. This applies to the whole machine, not just this session. "
            "If the user enabled it in Git-Switch, the global git user.name/user.email follow."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "user": {"type": "string", "description": _("Login name of the account to activate")},
                "hostname": {
                    "type": "string",
                    "description": _("Host name, only needed if the login exists on several hosts"),
                },
            },
            "required": ["user"],
            "additionalProperties": False,
        },
    },
]


def _call_tool(name: str, arguments: dict) -> dict:
    if name == "list_github_accounts":
        return {"accounts": core.describe_accounts()}
    if name == "switch_github_account":
        user = arguments.get("user")
        if not isinstance(user, str) or not user:
            raise core.GhError(_("'user' is required"))
        return core.switch_account(user, arguments.get("hostname"))
    raise core.GhError(_("Unknown tool: {name}").format(name=name))


def handle(message: dict) -> dict | None:
    """Handle one JSON-RPC message; returns the response or None for notifications."""
    method, params = message.get("method"), message.get("params") or {}
    if "id" not in message:
        return None

    def reply(result=None, error=None) -> dict:
        response = {"jsonrpc": "2.0", "id": message["id"]}
        response.update({"error": error} if error else {"result": result})
        return response

    if method == "initialize":
        requested = params.get("protocolVersion")
        return reply(
            {
                "protocolVersion": requested if requested in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0],
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "gitswitch", "version": __version__},
            }
        )
    if method == "ping":
        return reply({})
    if method == "tools/list":
        return reply({"tools": TOOLS})
    if method == "tools/call":
        try:
            result = _call_tool(params.get("name", ""), params.get("arguments") or {})
        except core.GhError as error:
            return reply({"content": [{"type": "text", "text": str(error)}], "isError": True})
        return reply({"content": [{"type": "text", "text": json.dumps(result, indent=2)}]})
    return reply(error={"code": -32601, "message": _("Method not found: {method}").format(method=method)})


def main() -> int:
    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            message = json.loads(line)
        except ValueError:
            response = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": _("Parse error")}}
        else:
            response = handle(message) if isinstance(message, dict) else None
        if response is not None:
            sys.stdout.write(json.dumps(response) + "\n")
            sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
