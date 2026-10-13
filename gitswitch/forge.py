"""Forgejo and Gitea accounts.

gh cannot manage these, so Git-Switch keeps them itself and follows gh's
model: tokens live only in the system keyring, a small file lists the accounts
without any secret, and git asks a credential helper for the token of the
active account.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import sys
import urllib.error
from pathlib import Path

from . import core
from .i18n import _

KIND = "forgejo"


# --- account list (no secrets) ----------------------------------------------


def accounts_file() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return Path(base) / "gitswitch" / "forges.json"


def _load() -> dict[str, dict]:
    """Return ``{host: {"users": [...], "active": name}}``."""
    try:
        stored = json.loads(accounts_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    hosts = {}
    for host, entry in stored.items() if isinstance(stored, dict) else ():
        users = entry.get("users") if isinstance(entry, dict) else None
        if isinstance(users, list) and users:
            hosts[host] = {"users": [str(user) for user in users], "active": str(entry.get("active") or "")}
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
        core.Account(host, user, user == entry["active"], KIND)
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


def token_page(host: str) -> str:
    return f"https://{host}/user/settings/applications"


def _profile(host: str, user: str, token: str) -> core.Profile:
    if not token:
        return core.Profile(valid=False, login=user)
    try:
        data, _headers = core.api_get(f"https://{host}/api/v1/user", f"token {token}")
    except urllib.error.HTTPError as error:
        return core.Profile(valid=False if error.code in (401, 403) else None, login=user)
    except (OSError, ValueError):
        return core.Profile(valid=None, login=user)
    if not isinstance(data, dict) or not data.get("login"):
        return core.Profile(valid=None, login=user)
    return core.Profile(
        valid=True,
        login=data["login"],
        name=data.get("full_name") or "",
        user_id=data.get("id") or 0,
        avatar_url=data.get("avatar_url") or "",
    )


def fetch_profile(account: core.Account) -> core.Profile:
    try:
        token = lookup_token(account.host, account.user)
    except core.GhError:
        return core.Profile(valid=None, login=account.user)
    return _profile(account.host, account.user, token)


# --- actions ----------------------------------------------------------------


def add_account(host: str, token: str) -> core.Account:
    """Validate ``token`` against ``host``, file it in the keyring and activate the account."""
    host, token = core.normalize_host(host), token.strip()
    profile = _profile(host, "", token)
    if profile.valid is False:
        raise core.GhError(_("{host} rejected the token.").format(host=host))
    if not profile.valid:
        raise core.GhError(_("{host} did not answer like a Forgejo or Gitea server over HTTPS.").format(host=host))
    store_token(host, profile.login, token)
    hosts = _load()
    entry = hosts.setdefault(host, {"users": [], "active": ""})
    if profile.login not in entry["users"]:
        entry["users"].append(profile.login)
    entry["active"] = profile.login
    _save(hosts)
    setup_git(host)
    return core.Account(host, profile.login, True, KIND)


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
