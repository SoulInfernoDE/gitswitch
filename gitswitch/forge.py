"""Forgejo, Gitea and GitLab accounts.

gh cannot manage these, so Git-Switch keeps them itself and follows gh's
model: tokens live only in the system keyring, a small file lists the accounts
without any secret, and git asks a credential helper for the token of the
active account.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import sys
import urllib.error
import urllib.parse
import uuid
from pathlib import Path

from . import core
from .i18n import _

FORGEJO, GITLAB = "forgejo", "gitlab"
KIND = FORGEJO
KINDS = (FORGEJO, GITLAB)

# What differs between the server families: API root, how a token is presented, and the user fields.
_API_ROOT = {FORGEJO: "/api/v1", GITLAB: "/api/v4"}
_AUTH_SCHEME = {FORGEJO: "token", GITLAB: "Bearer"}
_LOGIN_FIELD = {FORGEJO: "login", GITLAB: "username"}
_NAME_FIELD = {FORGEJO: "full_name", GITLAB: "name"}


# --- account list (no secrets) ----------------------------------------------


def accounts_file() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return Path(base) / "gitswitch" / "forges.json"


def _load() -> dict[str, dict]:
    """Return ``{host: {"users": [...], "active": name, "kind": FORGEJO | GITLAB}}``."""
    try:
        stored = json.loads(accounts_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    hosts = {}
    for host, entry in stored.items() if isinstance(stored, dict) else ():
        users = entry.get("users") if isinstance(entry, dict) else None
        if isinstance(users, list) and users:
            kind = entry.get("kind") if entry.get("kind") in KINDS else FORGEJO
            hosts[host] = {"users": [str(user) for user in users], "active": str(entry.get("active") or ""), "kind": kind}
    return hosts


def _save(hosts: dict[str, dict]) -> None:
    path = accounts_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(hosts, indent=2) + "\n", encoding="utf-8")
    temporary.chmod(0o600)
    os.replace(temporary, path)


def list_accounts() -> list[core.Account]:
    return [
        core.Account(host, user, user == entry["active"], entry["kind"])
        for host, entry in _load().items()
        for user in entry["users"]
    ]


# --- tokens (system keyring only) --------------------------------------------


def _keyring():
    """Return libsecret and the schema under which tokens are filed."""
    try:
        import gi

        gi.require_version("Secret", "1")
        from gi.repository import Secret
    except (ImportError, ValueError):
        raise core.GhError(
            _("No system keyring is available (libsecret is missing), so the token cannot be stored safely.")
        ) from None
    attributes = {"host": Secret.SchemaAttributeType.STRING, "user": Secret.SchemaAttributeType.STRING}
    return Secret, Secret.Schema.new("dev.gitswitch.Token", Secret.SchemaFlags.NONE, attributes)


def _keyring_call(action):
    from gi.repository import GLib

    try:
        return action()
    except GLib.Error as error:
        raise core.GhError(_("The system keyring refused the request: {detail}").format(detail=error.message)) from None


def store_token(host: str, user: str, token: str) -> None:
    secret, schema = _keyring()
    label = f"Git-Switch: {user}@{host}"
    attributes = {"host": host, "user": user}
    stored = _keyring_call(
        lambda: secret.password_store_sync(schema, attributes, secret.COLLECTION_DEFAULT, label, token, None)
    )
    if not stored:
        raise core.GhError(_("The token could not be stored in the system keyring."))


def lookup_token(host: str, user: str) -> str:
    secret, schema = _keyring()
    return _keyring_call(lambda: secret.password_lookup_sync(schema, {"host": host, "user": user}, None)) or ""


def clear_token(host: str, user: str) -> None:
    secret, schema = _keyring()
    _keyring_call(lambda: secret.password_clear_sync(schema, {"host": host, "user": user}, None))


# --- server API -------------------------------------------------------------


def host_kind(host: str) -> str:
    """Kind of a known host; Forgejo for hosts without accounts."""
    return _load().get(host, {}).get("kind", FORGEJO)


def token_page(host: str, kind: str = FORGEJO) -> str:
    if kind == GITLAB:
        return f"https://{host}/-/user_settings/personal_access_tokens?name=Git-Switch&scopes=read_user,write_repository"
    return f"https://{host}/user/settings/applications"


def _profile(host: str, user: str, token: str, kind: str = FORGEJO) -> core.Profile:
    if not token:
        return core.Profile(valid=False, login=user)
    try:
        data, _headers = core.api_get(f"https://{host}{_API_ROOT[kind]}/user", f"{_AUTH_SCHEME[kind]} {token}")
    except urllib.error.HTTPError as error:
        return core.Profile(valid=False if error.code in (401, 403) else None, login=user)
    except (OSError, ValueError):
        return core.Profile(valid=None, login=user)
    if not isinstance(data, dict) or not data.get(_LOGIN_FIELD[kind]):
        return core.Profile(valid=None, login=user)
    return core.Profile(
        valid=True,
        login=data[_LOGIN_FIELD[kind]],
        name=data.get(_NAME_FIELD[kind]) or "",
        user_id=data.get("id") or 0,
        avatar_url=data.get("avatar_url") or "",
    )


def fetch_profile(account: core.Account) -> core.Profile:
    try:
        token = lookup_token(account.host, account.user)
    except core.GhError:
        return core.Profile(valid=None, login=account.user)
    return _profile(account.host, account.user, token, account.kind)


# --- API access for tools ---------------------------------------------------

API_METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE")
# Endpoints that hand out new credentials stay closed, so no secret can end up in a tool result.
_CREDENTIAL_PATH = re.compile(
    r"/(tokens|oauth2|personal_access_tokens|access_tokens|impersonation_tokens|deploy_tokens|runners)(/|\?|$)"
)
_REPOSITORY = re.compile(r"[\w.-]+/[\w.-]+")


def _api_account(host: str | None, kind: str = FORGEJO) -> core.Account:
    active = [a for a in list_accounts() if a.active and a.kind == kind and host in (None, "", a.host)]
    if not active:
        if kind == GITLAB:
            raise core.GhError(_("No GitLab account is signed in for this host."))
        raise core.GhError(_("No Forgejo or Gitea account is signed in for this host."))
    if len(active) > 1:
        raise core.GhError(_("Several servers of this kind are known; pass a hostname."))
    return active[0]


def _api_call(account: core.Account, method: str, path: str, data: bytes | None, content_type: str, timeout: int):
    token = lookup_token(account.host, account.user)
    if not token:
        raise core.GhError(_("No token for {user} in the system keyring; sign in again.").format(user=account.user))
    try:
        url = f"https://{account.host}{_API_ROOT[account.kind]}{path}"
        return core.api_request(url, f"{_AUTH_SCHEME[account.kind]} {token}", method, data, content_type, timeout)[0]
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", "replace")[:500].strip()
        raise core.GhError(f"HTTP {error.code} {error.reason}: {detail}".rstrip(": ")) from None
    except (OSError, ValueError) as error:
        raise core.GhError(_("Request to {host} failed: {detail}").format(host=account.host, detail=error)) from None


def api(method: str, path: str, body: object = None, host: str | None = None, kind: str = FORGEJO) -> object:
    """Call the API of the active account's server; ``path`` is relative to the API root (``/api/v1``, ``/api/v4``)."""
    method = method.upper()
    if method not in API_METHODS:
        raise core.GhError(_("Unsupported method: {method}").format(method=method))
    account = _api_account(host, kind)
    path = _relative_path(path, account)
    segments = path.split("?", 1)[0].split("/")
    if not path.startswith("/") or path.startswith("//") or ".." in segments or any(c.isspace() for c in path):
        raise core.GhError(_("The path must start with a single / and stay below the API root."))
    if _CREDENTIAL_PATH.search(path):
        raise core.GhError(_("Endpoints that create or list credentials are not available here."))
    data = None if body is None else json.dumps(body).encode()
    return _api_call(account, method, path, data, "application/json", 60)


def _relative_path(path: str, account: core.Account) -> str:
    """Accept the API path with or without its root, or as a full URL of the account's server."""
    root, text = _API_ROOT[account.kind], path.strip()
    if "://" in text:
        origin = f"https://{account.host}"
        if not text.lower().startswith(origin.lower() + "/"):
            raise core.GhError(_("The URL must belong to {host} and use HTTPS.").format(host=account.host))
        text = text[len(origin):]
    if not text.startswith("/"):
        text = "/" + text
    if text == root or text.startswith((root + "/", root + "?")):
        text = text[len(root):]
    return text if text.startswith("/") else "/" + text


def upload_release_asset(repository: str, tag: str, file: str, host: str | None = None) -> object:
    """Attach a local file to the release of ``tag`` in ``owner/name``."""
    source = Path(file).expanduser()
    if not _REPOSITORY.fullmatch(repository):
        raise core.GhError(_("The repository must be given as owner/name."))
    if not source.is_file():
        raise core.GhError(_("{path} is not a file.").format(path=source))
    account = _api_account(host)
    release = api("GET", f"/repos/{repository}/releases/tags/{urllib.parse.quote(tag, safe='')}", host=account.host)
    boundary = uuid.uuid4().hex
    head = (
        f'--{boundary}\r\nContent-Disposition: form-data; name="attachment"; filename="{source.name}"\r\n'
        "Content-Type: application/octet-stream\r\n\r\n"
    ).encode()
    data = head + source.read_bytes() + f"\r\n--{boundary}--\r\n".encode()
    path = f"/repos/{repository}/releases/{release['id']}/assets?name={urllib.parse.quote(source.name)}"
    return _api_call(account, "POST", path, data, f"multipart/form-data; boundary={boundary}", 300)


# --- actions ----------------------------------------------------------------


def add_account(host: str, token: str, kind: str = FORGEJO) -> core.Account:
    """Validate ``token`` against ``host``, file it in the keyring and activate the account."""
    host, token = core.normalize_host(host), token.strip()
    known = _load().get(host)
    if known and known["kind"] != kind:
        raise core.GhError(_("{host} is already set up as a different kind of server.").format(host=host))
    profile = _profile(host, "", token, kind)
    if profile.valid is False:
        raise core.GhError(_("{host} rejected the token.").format(host=host))
    if not profile.valid:
        if kind == GITLAB:
            raise core.GhError(_("{host} did not answer like a GitLab server over HTTPS.").format(host=host))
        raise core.GhError(_("{host} did not answer like a Forgejo or Gitea server over HTTPS.").format(host=host))
    store_token(host, profile.login, token)
    hosts = _load()
    entry = hosts.setdefault(host, {"users": [], "active": "", "kind": kind})
    if profile.login not in entry["users"]:
        entry["users"].append(profile.login)
    entry["active"] = profile.login
    _save(hosts)
    setup_git(host)
    return core.Account(host, profile.login, True, kind)


def switch(account: core.Account) -> None:
    hosts = _load()
    hosts[account.host]["active"] = account.user
    _save(hosts)


def logout(account: core.Account) -> None:
    clear_token(account.host, account.user)
    hosts = _load()
    entry = hosts.get(account.host)
    if not entry:
        return
    entry["users"] = [user for user in entry["users"] if user != account.user]
    if not entry["users"]:
        del hosts[account.host]
        remove_git_setup(account.host)
    elif entry["active"] == account.user:
        entry["active"] = entry["users"][0]
    _save(hosts)


# --- git credential helper --------------------------------------------------


def _helper_key(host: str) -> str:
    return f"credential.https://{host}.helper"


def _helper_command() -> str:
    script = shutil.which("gitswitch")
    command = shlex.quote(script) if script else f"{shlex.quote(sys.executable)} -m gitswitch"
    return f"!{command} git-credential"


def setup_git(host: str) -> None:
    """Make git ask Git-Switch for credentials of ``host``, the way gh does for GitHub."""
    # The empty entry stops git from also asking helpers configured for all hosts.
    core._run(["git", "config", "--global", "--replace-all", _helper_key(host), ""])
    core._run(["git", "config", "--global", "--add", _helper_key(host), _helper_command()])


def remove_git_setup(host: str) -> None:
    core._run(["git", "config", "--global", "--unset-all", _helper_key(host)], check=False)


def credential_helper(operation: str, request: str) -> str:
    """Answer ``git credential`` requests; only ``get`` over HTTPS for a known host yields a token."""
    if operation != "get":
        return ""
    fields = dict(line.split("=", 1) for line in request.splitlines() if "=" in line)
    entry = _load().get(fields.get("host", ""))
    if fields.get("protocol") != "https" or not entry:
        return ""
    # A user name in the remote URL selects that account instead of the active one.
    user = fields.get("username") or entry["active"]
    if user not in entry["users"]:
        return ""
    try:
        token = lookup_token(fields["host"], user)
    except core.GhError:
        return ""
    return f"username={user}\npassword={token}\n" if token else ""
