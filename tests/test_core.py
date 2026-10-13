import gettext
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from gitswitch import core, mcp

HOSTS = """\
github.com:
    git_protocol: https
    users:
        alice:
        Bob:
            oauth_token: gho_example
    user: alice
ghe.example.com:
    oauth_token: gho_legacy
    user: carol
"""


class HostsFileTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        Path(self.dir.name, "hosts.yml").write_text(HOSTS)
        patcher = mock.patch.dict(os.environ, {"GH_CONFIG_DIR": self.dir.name})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_parse_accounts(self):
        self.assertEqual(
            core.list_accounts(),
            [
                core.Account("github.com", "alice", True),
                core.Account("github.com", "Bob", False),
                core.Account("ghe.example.com", "carol", True),
            ],
        )
        self.assertEqual(core.git_protocol("github.com"), "https")
        self.assertEqual(core.git_protocol("ghe.example.com"), "")

    def test_missing_file(self):
        os.remove(Path(self.dir.name, "hosts.yml"))
        self.assertEqual(core.list_accounts(), [])

    def test_find_account(self):
        self.assertEqual(core.find_account("bob").user, "Bob")
        self.assertEqual(core.find_account("carol", "ghe.example.com").host, "ghe.example.com")
        with self.assertRaises(core.GhError):
            core.find_account("carol", "github.com")
        with self.assertRaises(core.GhError):
            core.find_account("nobody")

    def test_switch_runs_gh(self):
        with mock.patch.object(core, "_run") as run:
            result = core.switch_account("bob", sync_identity=False)
        run.assert_called_once_with(["gh", "auth", "switch", "--hostname", "github.com", "--user", "Bob"])
        self.assertEqual(result, {"host": "github.com", "user": "Bob", "git_identity": None})

    def test_flow_commands(self):
        login = core.LoginFlow("github.com", None, None)
        self.assertEqual(
            login.command(), ["gh", "auth", "login", "--hostname", "github.com", "--web", "--git-protocol", "https"]
        )
        refresh = core.LoginFlow("github.com", None, None, refresh=True, scopes="workflow", remove_scopes="delete_repo")
        self.assertEqual(
            refresh.command(),
            ["gh", "auth", "refresh", "--hostname", "github.com", "--remove-scopes", "delete_repo", "--scopes", "workflow"],
        )

    def test_scope_catalog_covers_required_scopes(self):
        names = [name for name, _description in core.scope_catalog()]
        self.assertEqual(len(names), len(set(names)))
        self.assertTrue(set(core.REQUIRED_SCOPES) <= set(names))

    def test_normalize_host(self):
        self.assertEqual(core.normalize_host(" https://GitHub.Example.com/login "), "github.example.com")
        self.assertEqual(core.normalize_host("github.com"), "github.com")
        self.assertEqual(core.normalize_host("  "), "")

    def test_login_with_token_passes_token_on_stdin(self):
        with mock.patch.object(core.subprocess, "run") as run:
            run.return_value = mock.Mock(returncode=0, stdout="", stderr="")
            core.login_with_token("github.com", " secret ")
        args, kwargs = run.call_args
        self.assertEqual(args[0], ["gh", "auth", "login", "--hostname", "github.com", "--with-token"])
        self.assertEqual(kwargs["input"], "secret\n")
        self.assertNotIn("stdin", kwargs)

    def test_noreply_email(self):
        profile = core.Profile(valid=True, login="alice", user_id=42)
        self.assertEqual(core.noreply_email("github.com", profile), "42+alice@users.noreply.github.com")


class CatalogTest(unittest.TestCase):
    LOCALE_DIR = Path(core.__file__).parent / "locale"

    def test_german_catalog_is_complete(self):
        german = gettext.translation("gitswitch", self.LOCALE_DIR, languages=["de"])
        self.assertEqual(german.gettext("Switch"), "Wechseln")
        template = (self.LOCALE_DIR / "gitswitch.pot").read_text(encoding="utf-8")
        catalog = (self.LOCALE_DIR / "de" / "LC_MESSAGES" / "gitswitch.po").read_text(encoding="utf-8")
        self.assertEqual(template.count("\nmsgid "), catalog.count("\nmsgid "))
        self.assertNotIn('msgstr ""\n\n', catalog.split("\n\n", 1)[1] + "\n")

    def test_english_is_the_fallback(self):
        other = gettext.translation("gitswitch", self.LOCALE_DIR, languages=["xx"], fallback=True)
        self.assertEqual(other.gettext("Switch"), "Switch")


class McpTest(unittest.TestCase):
    def call(self, method, params=None, **extra):
        return mcp.handle({"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}, **extra})

    def test_initialize_and_list(self):
        result = self.call("initialize", {"protocolVersion": "2024-11-05"})["result"]
        self.assertEqual(result["protocolVersion"], "2024-11-05")
        names = [tool["name"] for tool in self.call("tools/list")["result"]["tools"]]
        self.assertEqual(names, ["list_github_accounts", "switch_github_account"])

    def test_notification_has_no_response(self):
        self.assertIsNone(mcp.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}))

    def test_switch_tool(self):
        switched = {"host": "github.com", "user": "Bob", "git_identity": None}
        with mock.patch.object(core, "switch_account", return_value=switched) as switch:
            result = self.call("tools/call", {"name": "switch_github_account", "arguments": {"user": "bob"}})["result"]
        switch.assert_called_once_with("bob", None)
        self.assertEqual(json.loads(result["content"][0]["text"]), switched)

    def test_tool_error(self):
        with mock.patch.object(core, "switch_account", side_effect=core.GhError("boom")):
            result = self.call("tools/call", {"name": "switch_github_account", "arguments": {"user": "x"}})["result"]
        self.assertTrue(result["isError"])
        self.assertEqual(self.call("nope")["error"]["code"], -32601)


if __name__ == "__main__":
    unittest.main()
