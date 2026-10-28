"""Minimal MCP server (stdio transport) exposing account switching as tools.

Implements just the parts of the Model Context Protocol that a tool-only
server needs, so no third-party SDK is required.
"""

from __future__ import annotations

import json
import re
import sys
import threading

from . import __version__, core, forge, leaks, update
from .i18n import _

PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")

TOOLS = [
    {
        "name": "list_github_accounts",
        "description": _(
            "List the accounts Git-Switch manages, per host: the GitHub accounts the GitHub CLI (gh) is "
            "logged in to and Forgejo, Gitea or GitLab accounts, including which one is active and whether its "
            "token is still valid. Tokens are never returned."
        ),
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "switch_github_account",
        "description": _(
            "Switch the active account of a host: gh auth switch for GitHub, or the account git uses "
            "for a Forgejo, Gitea or GitLab host. This applies to the whole machine, not just this session. "
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
    {
        "name": "gitlab_api_get",
        "description": _(
            "Read from the REST API of a GitLab server as the active account, like 'gh api' for GitHub. The path "
            "is relative to /api/v4, for example /user, /projects?membership=true, "
            "/projects/OWNER%2FNAME/issues?state=opened, /projects/OWNER%2FNAME/merge_requests, "
            "/projects/OWNER%2FNAME/pipelines or /projects/OWNER%2FNAME/repository/files/PATH/raw?ref=main. "
            "Projects can be addressed by ID or by their URL-encoded path. Lists are paged with ?page= and "
            "?per_page=. The token is never returned."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": _("API path below /api/v4, starting with /")},
                "hostname": {"type": "string", "description": _("Server, only needed if several are signed in")},
            },
            "required": ["path"],
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "gitlab_api_write",
        "description": _(
            "Change something through the REST API of a GitLab server as the active account: create or edit "
            "issues, notes, merge requests, releases and projects, merge a merge request "
            "(PUT /projects/OWNER%2FNAME/merge_requests/IID/merge) or start a pipeline. The path is relative to "
            "/api/v4. DELETE requests remove data permanently. Needs a token with the api scope."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "method": {"type": "string", "enum": ["POST", "PUT", "PATCH", "DELETE"]},
                "path": {"type": "string", "description": _("API path below /api/v4, starting with /")},
                "body": {"type": "object", "description": _("JSON body of the request")},
                "hostname": {"type": "string", "description": _("Server, only needed if several are signed in")},
            },
            "required": ["method", "path"],
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": False, "destructiveHint": True},
    },
    {
        "name": "check_for_leaks",
        "description": _(
            "Check content for private data before it is published: live tokens of the signed-in accounts, "
            "other access tokens and private keys, addresses of the user's self-hosted servers, the real git "
            "e-mail address, paths in the home directory, the computer name and internal network addresses. "
            "Call it before committing, pushing, creating a release, opening an issue or pull request, or "
            "putting text into a README. Pass 'repository' to check what the next push would publish, "
            "'paths' for files or folders, or 'text' for a draft. Findings of severity 'secret' must be "
            "removed; discuss 'private' and 'review' findings with the user instead of publishing them."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": _("Text to check, for example a draft README or message")},
                "paths": {"type": "array", "items": {"type": "string"}, "description": _("Files or folders to check")},
                "repository": {
                    "type": "string",
                    "description": _("Git working copy; checks staged changes and commits not yet on the upstream"),
                },
            },
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": True},
    },
]

# Larger results are cut so one call cannot flood the conversation.
MAX_RESULT_CHARS = 60000


def instructions() -> str:
    """Guidance the client shows the model for the whole session."""
    text = _(
        "Before publishing anything on the user's behalf (commit, push, release, issue, pull request, README), "
        "run check_for_leaks on it and resolve the findings with the user."
    )
    rules = core.load_settings().get("restricted")
    terms = ", ".join(str(term) for term in (rules or {}).get("terms") or []) if isinstance(rules, dict) else ""
    if terms:
        text += "\n" + _(
            "Do not mention {terms} in anything you publish, and add no Co-Authored-By or 'Generated with' lines "
            "naming them."
        ).format(terms=terms)
        if rules.get("repository"):
            text += " " + _("Such notes belong only in the repository {repository}.").format(repository=rules["repository"])
    return "\n".join([text, *notices()])


def notices() -> list[str]:
    """Things the user should hear about: a newer release, or this server being outdated."""
    found = []
    installed = update.outdated_process()
    if installed:
        found.append(
            _("Git-Switch was updated to {version}; restart the client so this MCP server runs the new version.").format(
                version=installed
            )
        )
    latest = update.cached() if core.load_settings().get("check_updates") else None
    if latest:
        found.append(_("Git-Switch {version} is available: {url}").format(**latest))
    return found


def _refuse_secrets(*values: object) -> None:
    """Stop a write that would carry a token or private key to a server."""
    text = "\n".join(json.dumps(value, ensure_ascii=False) for value in values if value is not None)
    secrets = [f for f in leaks.scan_text(text, "request") if f.severity == leaks.SECRET]
    if secrets:
        raise core.GhError(_("Not sent: the request contains a secret ({detail})").format(detail=secrets[0].detail))


def _refuse_restricted(host: str | None, repository: str, findings: list) -> None:
    """Keep the user's restricted terms out of every repository except the one set aside for them."""
    restricted = [f for f in findings if f.kind.startswith("restricted-")]
    if not restricted:
        return
    server = forge._api_account(host).host
    if not repository or not leaks.restricted_allowed(server, repository):
        raise core.GhError(_("Not sent: {detail}").format(detail=restricted[0].detail))


def _call_tool(name: str, arguments: dict) -> object:
    if name == "list_github_accounts":
        result = {"accounts": core.describe_accounts()}
        if notices():
            result["notices"] = notices()
        return result
    if name == "check_for_leaks":
        context, found = leaks.gather(), []
        if isinstance(arguments.get("text"), str):
            found += leaks.scan_text(arguments["text"], "text", context)
        if isinstance(arguments.get("paths"), list):
            found += leaks.scan_paths([str(path) for path in arguments["paths"]], context)
        if isinstance(arguments.get("repository"), str):
            found += leaks.scan_repository(arguments["repository"], context)
        return leaks.report(found)
    if name == "switch_github_account":
        user = arguments.get("user")
        if not isinstance(user, str) or not user:
            raise core.GhError(_("'user' is required"))
        return core.switch_account(user, arguments.get("hostname"))
    if name in ("forgejo_api_get", "forgejo_api_write", "forgejo_upload_release_asset", "gitlab_api_get", "gitlab_api_write"):
        text = {key: value for key, value in arguments.items() if isinstance(value, str)}
        host = text.get("hostname")
        if name == "forgejo_upload_release_asset":
            if leaks.report(leaks.scan_paths([text.get("file", "")]))["counts"][leaks.SECRET]:
                raise core.GhError(_("Not sent: the file contains a secret."))
            _refuse_restricted(host, text.get("repository", ""), leaks.scan_paths([text.get("file", "")]))
            return forge.upload_release_asset(text.get("repository", ""), text.get("tag", ""), text.get("file", ""), host)
        kind = forge.GITLAB if name.startswith("gitlab_") else forge.FORGEJO
        if host and forge.host_kind(host) in forge.KINDS and host in {a.host for a in forge.list_accounts()}:
            # A known host decides which API it speaks, whichever of the two tool families was called.
            kind = forge.host_kind(host)
        method = "GET" if name.endswith("_api_get") else text.get("method", "").upper()
        if name.endswith("_api_write") and method not in ("POST", "PUT", "PATCH", "DELETE"):
            raise core.GhError(_("Unsupported method: {method}").format(method=method))
        _refuse_secrets(text.get("path"), arguments.get("body"))
        if name == "forgejo_api_write":
            target = re.match(r"/repos/([^/?]+/[^/?]+)", text.get("path", ""))
            body = json.dumps(arguments.get("body"), ensure_ascii=False) if arguments.get("body") is not None else ""
            _refuse_restricted(host, target.group(1) if target else "", leaks.scan_text(body, "request"))
        return forge.api(method, text.get("path", ""), arguments.get("body"), host, kind)
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
                "instructions": instructions(),
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
    # Refreshes the cached answer in the background; the result shows up as a notice.
    threading.Thread(target=update.check, daemon=True).start()
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
