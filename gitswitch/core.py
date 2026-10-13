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

DEFAULT_HOST = "github.com"
_CODE_RE = re.compile(r"\b[A-Z0-9]{4}-[A-Z0-9]{4}\b")


class GhError(RuntimeError):
    """Raised when ``gh`` or ``git`` is missing or exits with an error."""


@dataclass(frozen=True)
class Account:
    host: str
    user: str
    active: bool


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


def _run(args: list[str], check: bool = True, timeout: int = 30) -> subprocess.CompletedProcess:
    try:
        proc = subprocess.run(
            args, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL
        )
    except FileNotFoundError:
        raise GhError(f"'{args[0]}' was not found in PATH") from None
    except subprocess.TimeoutExpired:
        raise GhError(f"'{' '.join(args[:3])}' timed out") from None
    if check and proc.returncode != 0:
        message = (proc.stderr or proc.stdout).strip()
        raise GhError(message or f"'{' '.join(args[:3])}' failed")
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
    try:
        return parse_hosts(hosts_file().read_text(encoding="utf-8"))
    except OSError:
        return []


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


def fetch_profile(account: Account) -> Profile:
    """Check the stored token of ``account`` and fetch its public profile."""
    proc = _run(["gh", "auth", "token", "--hostname", account.host, "--user", account.user], check=False)
    token = proc.stdout.strip()
    if proc.returncode != 0 or not token:
        return Profile(valid=False, login=account.user)
    request = urllib.request.Request(
        _api_url(account.host),
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "User-Agent": f"gitswitch/{__version__}",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            data = json.load(response)
            scopes = response.headers.get("X-OAuth-Scopes", "")
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


def noreply_email(host: str, profile: Profile) -> str:
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
        raise GhError(f"Cannot read the profile of {account.user}; git identity left unchanged")
    name, email = profile.name or profile.login, noreply_email(account.host, profile)
    _run(["git", "config", "--global", "user.name", name])
    _run(["git", "config", "--global", "user.email", email])
    return {"name": name, "email": email}


# --- settings ---------------------------------------------------------------


def _settings_file() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return Path(base) / "gitswitch" / "config.json"


def load_settings() -> dict:
    settings = {"sync_git_identity": False}
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
        known = ", ".join(f"{a.user}@{a.host}" for a in list_accounts()) or "none"
        raise GhError(f"No logged-in account '{user}'. Known accounts: {known}")
    if len(matches) > 1:
        raise GhError(f"'{user}' exists on several hosts; pass a hostname")
    return matches[0]


def switch_account(user: str, host: str | None = None, sync_identity: bool | None = None) -> dict:
    """Run ``gh auth switch`` and, if enabled, update the global git identity."""
    account = find_account(user, host)
    _run(["gh", "auth", "switch", "--hostname", account.host, "--user", account.user])
    result: dict = {"host": account.host, "user": account.user, "git_identity": None}
    if sync_identity is None:
        sync_identity = bool(load_settings().get("sync_git_identity"))
    if sync_identity:
        try:
            result["git_identity"] = sync_git_identity(account)
        except GhError as error:
            result["warning"] = str(error)
    return result


def logout_account(account: Account) -> None:
    _run(["gh", "auth", "logout", "--hostname", account.host, "--user", account.user])


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


class LoginFlow:
    """Drive ``gh auth login --web`` without a terminal.

    ``on_code`` receives the one-time device code, ``on_done`` the exit status
    and collected output. Both are called from a worker thread.
    """

    def __init__(self, host: str, on_code, on_done):
        self.host, self._on_code, self._on_done = host, on_code, on_done
        self._proc: subprocess.Popen | None = None

    def start(self) -> None:
        args = ["gh", "auth", "login", "--hostname", self.host, "--web"]
        if git_protocol(self.host):
            args += ["--git-protocol", git_protocol(self.host)]
        try:
            self._proc = subprocess.Popen(
                args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True
            )
        except FileNotFoundError:
            raise GhError("'gh' was not found in PATH") from None
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
        raise GhError(f"{path} does not exist; is Claude Code installed?")
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
