"""Minimal MCP server (stdio transport) exposing account switching as tools.

Implements just the parts of the Model Context Protocol that a tool-only
server needs, so no third-party SDK is required.
"""

from __future__ import annotations

import json
import sys

from . import __version__, core, forge
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
    {
        "name": "forgejo_api_get",
        "description": _(
            "Read from the REST API of a Forgejo or Gitea server as the active account, like 'gh api' for "
            "GitHub. The path is relative to /api/v1, for example /user/repos, /repos/OWNER/NAME/issues?state=open, "
            "/repos/OWNER/NAME/pulls, /repos/OWNER/NAME/releases, /repos/OWNER/NAME/actions/tasks or "
            "/repos/OWNER/NAME/contents/PATH. Lists are paged with ?page= and ?limit=. The token is never returned."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": _("API path below /api/v1, starting with /")},
                "hostname": {"type": "string", "description": _("Server, only needed if several are signed in")},
            },
            "required": ["path"],
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "forgejo_api_write",
        "description": _(
            "Change something through the REST API of a Forgejo or Gitea server as the active account: create or "
            "edit issues, comments, pull requests, releases and repositories, merge a pull request "
            "(POST /repos/OWNER/NAME/pulls/INDEX/merge), or sync a mirror (POST /repos/OWNER/NAME/mirror-sync). "
            "The path is relative to /api/v1. DELETE requests remove data permanently."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "method": {"type": "string", "enum": ["POST", "PUT", "PATCH", "DELETE"]},
                "path": {"type": "string", "description": _("API path below /api/v1, starting with /")},
                "body": {"type": "object", "description": _("JSON body of the request")},
                "hostname": {"type": "string", "description": _("Server, only needed if several are signed in")},
            },
            "required": ["method", "path"],
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": False, "destructiveHint": True},
    },
    {
        "name": "forgejo_upload_release_asset",
        "description": _(
            "Attach a local file to an existing release on a Forgejo or Gitea server. "
            "Create the release first with forgejo_api_write (POST /repos/OWNER/NAME/releases)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "repository": {"type": "string", "description": _("Repository as owner/name")},
                "tag": {"type": "string", "description": _("Tag of the release")},
                "file": {"type": "string", "description": _("Path of the local file to upload")},
                "hostname": {"type": "string", "description": _("Server, only needed if several are signed in")},
            },
            "required": ["repository", "tag", "file"],
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": False},
    },
]

# Larger results are cut so one call cannot flood the conversation.
MAX_RESULT_CHARS = 60000


def _call_tool(name: str, arguments: dict) -> object:
    if name == "list_github_accounts":
        return {"accounts": core.describe_accounts()}
    if name == "switch_github_account":
        user = arguments.get("user")
        if not isinstance(user, str) or not user:
            raise core.GhError(_("'user' is required"))
        return core.switch_account(user, arguments.get("hostname"))
    if name in ("forgejo_api_get", "forgejo_api_write", "forgejo_upload_release_asset"):
        text = {key: value for key, value in arguments.items() if isinstance(value, str)}
        host = text.get("hostname")
        if name == "forgejo_upload_release_asset":
            return forge.upload_release_asset(text.get("repository", ""), text.get("tag", ""), text.get("file", ""), host)
        method = "GET" if name == "forgejo_api_get" else text.get("method", "").upper()
        if name == "forgejo_api_write" and method not in ("POST", "PUT", "PATCH", "DELETE"):
            raise core.GhError(_("Unsupported method: {method}").format(method=method))
        return forge.api(method, text.get("path", ""), arguments.get("body"), host)
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
        text = json.dumps(result, indent=2, ensure_ascii=False)
        if len(text) > MAX_RESULT_CHARS:
            text = text[:MAX_RESULT_CHARS] + "\n" + _("[Result cut off; narrow the request or use ?limit= and ?page=.]")
        return reply({"content": [{"type": "text", "text": text}]})
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
