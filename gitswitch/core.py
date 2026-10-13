"""Everything that talks to ``gh``, ``git`` and the GitHub API.

This module has no GUI dependencies so that the MCP server and the command
line can use it on their own.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from pathlib import Path

from . import __version__
from .i18n import _

DEFAULT_HOST = "github.com"
_CODE_RE = re.compile(r"\b[A-Z0-9]{4}-[A-Z0-9]{4}\b")


class GhError(RuntimeError):
    """Raised when ``gh`` or ``git`` is missing or exits with an error."""


@dataclass(frozen=True)
class Account:
    host: str
    user: str
    active: bool
    kind: str = "github"


@dataclass
class Profile:
    """Result of validating an account's token against the GitHub API.

    ``valid`` is ``None`` if the check itself failed (e.g. no network).
    """

    valid: bool | None
    login: str = ""
    name: str = ""
    user_id: int = 0
    avatar_url: str = ""
    scopes: list[str] = field(default_factory=list)


# --- running commands -------------------------------------------------------


def _run(
    args: list[str], check: bool = True, timeout: int = 30, stdin_text: str | None = None
) -> subprocess.CompletedProcess:
    try:
        # subprocess.run() rejects ``input`` together with ``stdin``.
        source = {"stdin": subprocess.DEVNULL} if stdin_text is None else {"input": stdin_text}
        proc = subprocess.run(args, capture_output=True, text=True, timeout=timeout, **source)
    except FileNotFoundError:
        raise GhError(_("'{command}' was not found in PATH").format(command=args[0])) from None
    except subprocess.TimeoutExpired:
        raise GhError(_("'{command}' timed out").format(command=" ".join(args[:3]))) from None
    if check and proc.returncode != 0:
        message = (proc.stderr or proc.stdout).strip()
        raise GhError(message or _("'{command}' failed").format(command=" ".join(args[:3])))
    return proc


def gh_available() -> bool:
    return shutil.which("gh") is not None


# --- reading gh's configuration ---------------------------------------------


def gh_config_dir() -> Path:
    if os.environ.get("GH_CONFIG_DIR"):
        return Path(os.environ["GH_CONFIG_DIR"])
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return Path(base) / "gh"


def hosts_file() -> Path:
    return gh_config_dir() / "hosts.yml"


def _parse_yaml(text: str) -> dict:
    """Parse the small YAML subset gh uses for hosts.yml (nested mappings)."""
    root: dict = {}
    stack: list[tuple[int, dict]] = [(-1, root)]
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        key, sep, value = line.partition(":")
        if not sep:
            continue
        indent = len(raw) - len(raw.lstrip(" "))
        while stack[-1][0] >= indent:
            stack.pop()
        value = value.strip().strip("\"'")
        if value:
            stack[-1][1][key.strip()] = value
        else:
            child: dict = {}
            stack[-1][1][key.strip()] = child
            stack.append((indent, child))
    return root


def parse_hosts(text: str) -> list[Account]:
    accounts = []
    for host, entry in _parse_yaml(text).items():
        if not isinstance(entry, dict):
            continue
        active = entry.get("user", "")
        active = active if isinstance(active, str) else ""
        users = entry.get("users")
        # gh < 2.40 stored a single account per host without a "users" map.
        names = list(users) if isinstance(users, dict) else ([active] if active else [])
        accounts += [Account(host, name, name == active) for name in names]
    return accounts


def list_accounts() -> list[Account]:
    """All accounts: those of gh plus the Forgejo/Gitea ones Git-Switch keeps itself."""
    from . import forge

    try:
        accounts = parse_hosts(hosts_file().read_text(encoding="utf-8"))
    except OSError:
        accounts = []
    return accounts + forge.list_accounts()


def git_protocol(host: str) -> str:
    try:
        entry = _parse_yaml(hosts_file().read_text(encoding="utf-8")).get(host, {})
    except OSError:
        return ""
    value = entry.get("git_protocol", "") if isinstance(entry, dict) else ""
    return value if isinstance(value, str) else ""


# --- GitHub API -------------------------------------------------------------


def _api_url(host: str) -> str:
    return "https://api.github.com/user" if host == DEFAULT_HOST else f"https://{host}/api/v3/user"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Never follow redirects, so a token is only ever sent to the host it belongs to."""

    def redirect_request(self, *_args, **_kwargs):
        return None


_api_opener = urllib.request.build_opener(_NoRedirect)


def api_request(
    url: str,
    authorization: str,
    method: str = "GET",
    data: bytes | None = None,
    content_type: str = "application/json",
    timeout: int = 10,
) -> tuple[object, object]:
    """Authenticated HTTPS request; returns the body (decoded if it is JSON) and the response headers."""
    if not url.startswith("https://"):
        raise ValueError("refusing to send a token over an unencrypted connection")
    headers = {"Authorization": authorization, "Accept": "application/json", "User-Agent": f"gitswitch/{__version__}"}
    if data is not None:
        headers["Content-Type"] = content_type
    request = urllib.request.Request(url, data=data, method=method, headers=headers)
    with _api_opener.open(request, timeout=timeout) as response:
        text = response.read().decode("utf-8", "replace")
        try:
            return json.loads(text), response.headers
        except ValueError:
            return text, response.headers


def api_get(url: str, authorization: str) -> tuple[object, object]:
    return api_request(url, authorization)


def fetch_profile(account: Account) -> Profile:
    """Check the stored token of ``account`` and fetch its public profile."""
    if account.kind != "github":
        from . import forge

        return forge.fetch_profile(account)
    proc = _run(["gh", "auth", "token", "--hostname", account.host, "--user", account.user], check=False)
    token = proc.stdout.strip()
    if proc.returncode != 0 or not token:
        return Profile(valid=False, login=account.user)
    try:
        data, headers = api_get(_api_url(account.host), f"Bearer {token}")
        scopes = headers.get("X-OAuth-Scopes", "")
    except urllib.error.HTTPError as error:
        return Profile(valid=False if error.code == 401 else None, login=account.user)
    except (OSError, ValueError):
        return Profile(valid=None, login=account.user)
    return Profile(
        valid=True,
        login=data.get("login") or account.user,
        name=data.get("name") or "",
        user_id=data.get("id") or 0,
        avatar_url=data.get("avatar_url") or "",
        scopes=[scope.strip() for scope in scopes.split(",") if scope.strip()],
    )


def fetch_avatar(url: str, size: int = 96) -> bytes:
    separator = "&" if "?" in url else "?"
    with urllib.request.urlopen(f"{url}{separator}s={size}", timeout=10) as response:
        return response.read()


# --- git identity -----------------------------------------------------------


def noreply_email(host: str, profile: Profile, kind: str = "github") -> str:
    """Address that hides the real one; Forgejo and Gitea use ``login@noreply.<host>`` by default."""
    if kind != "github":
        return f"{profile.login}@noreply.{host.split(':')[0]}"
    return f"{profile.user_id}+{profile.login}@users.noreply.{host}"


def get_git_identity() -> tuple[str, str]:
    def read(key: str) -> str:
        return _run(["git", "config", "--global", "--get", key], check=False).stdout.strip()

    try:
        return read("user.name"), read("user.email")
    except GhError:
        return "", ""


def sync_git_identity(account: Account, profile: Profile | None = None) -> dict:
    """Point the global git identity at ``account`` using its noreply address."""
    profile = profile or fetch_profile(account)
    if not profile.valid or not profile.user_id:
        raise GhError(_("Cannot read the profile of {user}; git identity left unchanged").format(user=account.user))
    name, email = profile.name or profile.login, noreply_email(account.host, profile, account.kind)
    _run(["git", "config", "--global", "user.name", name])
    _run(["git", "config", "--global", "user.email", email])
    return {"name": name, "email": email}


# --- settings ---------------------------------------------------------------


def _settings_file() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return Path(base) / "gitswitch" / "config.json"


def load_settings() -> dict:
    settings = {"sync_git_identity": False, "sounds": True}
    try:
        stored = json.loads(_settings_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return settings
    if isinstance(stored, dict):
        settings.update(stored)
    return settings


def save_settings(settings: dict) -> None:
    path = _settings_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")


# --- actions ----------------------------------------------------------------


def find_account(user: str, host: str | None = None) -> Account:
    """Resolve ``user`` (case-insensitive) to a known account."""
    matches = [
        account
        for account in list_accounts()
        if account.user.lower() == user.lower() and host in (None, "", account.host)
    ]
    if not matches:
        known = ", ".join(f"{a.user}@{a.host}" for a in list_accounts()) or _("none")
        raise GhError(_("No logged-in account '{user}'. Known accounts: {known}").format(user=user, known=known))
    if len(matches) > 1:
        raise GhError(_("'{user}' exists on several hosts; pass a hostname").format(user=user))
    return matches[0]


def switch_account(user: str, host: str | None = None, sync_identity: bool | None = None) -> dict:
    """Activate an account (``gh auth switch`` for GitHub) and, if enabled, update the global git identity."""
    account = find_account(user, host)
    if account.kind == "github":
        _run(["gh", "auth", "switch", "--hostname", account.host, "--user", account.user])
    else:
        from . import forge

        forge.switch(account)
    result: dict = {"host": account.host, "user": account.user, "kind": account.kind, "git_identity": None}
    if sync_identity is None:
        sync_identity = bool(load_settings().get("sync_git_identity"))
    if sync_identity:
        try:
            result["git_identity"] = sync_git_identity(account)
        except GhError as error:
            result["warning"] = str(error)
    return result


def normalize_host(text: str) -> str:
    """Turn what people paste as a server address into a bare hostname."""
    host = text.strip().lower()
    host = host.split("://", 1)[-1]
    return host.split("/", 1)[0]


def _reachable(url: str) -> bool:
    request = urllib.request.Request(url, headers={"User-Agent": f"gitswitch/{__version__}"})
    try:
        with urllib.request.urlopen(request, timeout=8) as response:
            return response.status == 200
    except (OSError, ValueError):
        return False


def check_github_server(host: str) -> None:
    """Refuse servers gh cannot talk to before starting a sign-in that must fail.

    gh only speaks the GitHub API. Forgejo and Gitea are recognised by their
    own API; anything undetermined is left to gh.
    """
    if host == DEFAULT_HOST or _reachable(f"https://{host}/api/v3/meta"):
        return
    if _reachable(f"https://{host}/api/v1/version"):
        raise GhError(
            _(
                "{host} is a Forgejo or Gitea server, not GitHub Enterprise. "
                "Choose \"Forgejo or Gitea\" under \"Account on\" to add it."
            ).format(host=host)
        )


def token_page(host: str) -> str:
    """Page where a personal access token with the scopes gh needs can be created."""
    return f"https://{host}/settings/tokens/new?scopes=repo,read:org,gist,workflow&description=Git-Switch"


def device_page(host: str) -> str:
    return f"https://{host}/login/device"


def login_with_token(host: str, token: str) -> None:
    """Add an account from a personal access token (``gh auth login --with-token``)."""
    _run(["gh", "auth", "login", "--hostname", host, "--with-token"], stdin_text=token.strip() + "\n")


def logout_account(account: Account) -> None:
    if account.kind != "github":
        from . import forge

        return forge.logout(account)
    _run(["gh", "auth", "logout", "--hostname", account.host, "--user", account.user])


def get_token(account: Account) -> str:
    return _run(["gh", "auth", "token", "--hostname", account.host, "--user", account.user]).stdout.strip()


def set_git_protocol(host: str, protocol: str) -> None:
    _run(["gh", "config", "set", "git_protocol", protocol, "--host", host])


def setup_git(host: str) -> None:
    """Make git use gh as credential helper for ``host`` (``gh auth setup-git``)."""
    _run(["gh", "auth", "setup-git", "--hostname", host])


def describe_accounts(check_tokens: bool = True) -> list[dict]:
    """Plain-data view of all accounts, used by the CLI and the MCP server."""
    described = []
    for account in list_accounts():
        entry = asdict(account)
        if check_tokens:
            profile = fetch_profile(account)
            entry.update(token_valid=profile.valid, name=profile.name, scopes=profile.scopes)
        described.append(entry)
    return described


# Scopes gh always needs; ``gh auth refresh`` refuses to remove them.
REQUIRED_SCOPES = ("repo", "read:org", "gist")


def scope_catalog() -> list[tuple[str, str]]:
    """The OAuth scopes offered in the permissions dialog, with a short description."""
    return [
        ("repo", _("Full access to repositories")),
        ("read:org", _("Read organization and team membership")),
        ("gist", _("Create and edit gists")),
        ("workflow", _("Update GitHub Actions workflow files")),
        ("delete_repo", _("Delete repositories")),
        ("read:packages", _("Download packages")),
        ("write:packages", _("Upload packages")),
        ("delete:packages", _("Delete packages")),
        ("admin:public_key", _("Manage SSH keys")),
        ("admin:ssh_signing_key", _("Manage SSH signing keys")),
        ("admin:gpg_key", _("Manage GPG keys")),
        ("user", _("Read and edit profile data")),
        ("notifications", _("Read notifications")),
        ("project", _("Manage projects")),
        ("read:project", _("Read projects")),
        ("codespace", _("Create and manage codespaces")),
        ("admin:org", _("Manage organizations and teams")),
        ("admin:repo_hook", _("Manage repository webhooks")),
        ("security_events", _("Read and write security events")),
        ("write:discussion", _("Read and write team discussions")),
    ]


class LoginFlow:
    """Drive ``gh auth login --web`` or ``gh auth refresh`` without a terminal.

    With ``refresh`` the token of the active account is renewed, adding the
    comma separated ``scopes`` and dropping ``remove_scopes``. ``on_code`` receives the one-time device code, ``on_done`` the exit status
    and collected output. Both are called from a worker thread.
    """

    def __init__(self, host, on_code, on_done, refresh: bool = False, scopes: str = "", remove_scopes: str = ""):
        self.host, self._on_code, self._on_done = host, on_code, on_done
        self.refresh, self.scopes, self.remove_scopes = refresh, scopes, remove_scopes
        self._proc: subprocess.Popen | None = None

    def command(self) -> list[str]:
        if self.refresh:
            args = ["gh", "auth", "refresh", "--hostname", self.host]
            if self.remove_scopes:
                args += ["--remove-scopes", self.remove_scopes]
        else:
            args = ["gh", "auth", "login", "--hostname", self.host, "--web"]
            if git_protocol(self.host):
                args += ["--git-protocol", git_protocol(self.host)]
        if self.scopes:
            args += ["--scopes", self.scopes]
        return args

    def start(self) -> None:
        args = self.command()
        try:
            self._proc = subprocess.Popen(
                args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True
            )
        except FileNotFoundError:
            raise GhError(_("'{command}' was not found in PATH").format(command="gh")) from None
        threading.Thread(target=self._watch, daemon=True).start()

    def _watch(self) -> None:
        proc, output, code_seen = self._proc, [], False
        try:
            # Answers the "Press Enter to open the browser" prompt, if any.
            proc.stdin.write("\n")
            proc.stdin.close()
        except OSError:
            pass
        for line in proc.stdout:
            output.append(line)
            match = _CODE_RE.search(line)
            if match and not code_seen:
                code_seen = True
                self._on_code(match.group(0))
        self._on_done(proc.wait(), "".join(output).strip())

    def cancel(self) -> None:
        if self._proc and self._proc.poll() is None:
            self._proc.terminate()


# --- Claude Code integration ------------------------------------------------

MCP_NAME = "gitswitch"


def claude_cli() -> str | None:
    return shutil.which("claude")


def mcp_command() -> list[str]:
    """Command line that starts the MCP server of this installation."""
    script = shutil.which("gitswitch-mcp")
    return [script] if script else [shutil.which("python3") or "python3", "-m", "gitswitch.mcp"]


def claude_config_file() -> Path:
    base = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~")
    return Path(base) / ".claude.json"


def _read_claude_config() -> dict:
    try:
        config = json.loads(claude_config_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return config if isinstance(config, dict) else {}


def _write_mcp_entry(entry: dict | None) -> None:
    """Fallback for installations without the ``claude`` command line tool."""
    path = claude_config_file()
    if not path.exists():
        raise GhError(_("{path} does not exist; is Claude Code installed?").format(path=path))
    config = json.loads(path.read_text(encoding="utf-8"))
    servers = config.setdefault("mcpServers", {})
    if entry is None:
        servers.pop(MCP_NAME, None)
    else:
        servers[MCP_NAME] = entry
    temporary = path.with_name(path.name + ".gitswitch-tmp")
    temporary.write_text(json.dumps(config, indent=2), encoding="utf-8")
    temporary.chmod(path.stat().st_mode & 0o777)
    os.replace(temporary, path)


def mcp_available() -> bool:
    return claude_cli() is not None or claude_config_file().exists()


def mcp_registered() -> bool:
    servers = _read_claude_config().get("mcpServers")
    return isinstance(servers, dict) and MCP_NAME in servers


def mcp_register() -> None:
    command = mcp_command()
    if claude_cli():
        _run(["claude", "mcp", "add", "--scope", "user", MCP_NAME, "--", *command], timeout=60)
    else:
        _write_mcp_entry({"type": "stdio", "command": command[0], "args": command[1:], "env": {}})


def mcp_unregister() -> None:
    if claude_cli():
        _run(["claude", "mcp", "remove", "--scope", "user", MCP_NAME], timeout=60)
    else:
        _write_mcp_entry(None)
