import gettext
import json
import os
import subprocess
import tempfile
import unittest
import urllib.error
from datetime import datetime, timedelta, timezone
from email.message import Message
from pathlib import Path
from unittest import mock

from gitswitch import core, expiry, forge, leaks, mcp, update

# Kept before any test replaces it with a fake.
REAL_GATHER = leaks.gather

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
        patcher = mock.patch.dict(os.environ, {"GH_CONFIG_DIR": self.dir.name, "XDG_CONFIG_HOME": self.dir.name})
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
        self.assertEqual(result, {"host": "github.com", "user": "Bob", "kind": "github", "git_identity": None})

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

    def test_server_family(self):
        answers = {
            "/api/v3/meta": (200, {"installed_version": "3.12"}),
            "/api/v1/version": (200, {"version": "11.0"}),
            "/api/v4/version": (401, {"message": "401 Unauthorized"}),
            "signin-forgejo": (403, {"message": "Only signed in user is allowed to call APIs."}),
        }

        def serving(*paths, using=None):
            def probe(url):
                for path in paths:
                    if url.endswith(path):
                        return answers[using or path]
                return 404, None
            return mock.patch.object(core, "_probe", side_effect=probe)

        with mock.patch.object(core, "_probe") as probe:
            self.assertEqual(core.server_family("github.com"), "github")
            self.assertEqual(core.server_family("gitlab.com"), "gitlab")
        probe.assert_not_called()
        for path, family in (("/api/v3/meta", "github"), ("/api/v1/version", "forgejo"), ("/api/v4/version", "gitlab")):
            with serving(path):
                self.assertEqual(core.server_family("git.example.com"), family)
        with serving("/api/v1/version", using="signin-forgejo"):
            self.assertEqual(core.server_family("git.example.com"), "forgejo")
        with serving():
            self.assertEqual(core.server_family("git.example.com"), "")
            core.check_github_server("git.example.com")
        with serving("/api/v4/version"), self.assertRaises(core.GhError):
            core.check_github_server("git.example.com")

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


class ForgeTest(unittest.TestCase):
    """Forgejo accounts with the keyring and the server replaced by fakes."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.keyring = {}
        profile = core.Profile(valid=True, login="dana", name="Dana", user_id=7)
        patches = [
            mock.patch.dict(os.environ, {"GH_CONFIG_DIR": self.dir.name, "XDG_CONFIG_HOME": self.dir.name}),
            mock.patch.object(forge, "store_token", lambda host, user, token: self.keyring.__setitem__((host, user), token)),
            mock.patch.object(forge, "lookup_token", lambda host, user: self.keyring.get((host, user), "")),
            mock.patch.object(forge, "clear_token", lambda host, user: self.keyring.pop((host, user), None)),
            mock.patch.object(forge, "_profile", return_value=profile),
            mock.patch.object(core, "_run"),
            mock.patch.object(leaks, "gather", return_value=leaks.Context()),
        ]
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)
        self.account = forge.add_account("https://Git.Example.com/", " s3cret ")

    def request(self, **fields):
        return "".join(f"{key}={value}\n" for key, value in fields.items())

    def test_add_account(self):
        self.assertEqual(self.account, core.Account("git.example.com", "dana", True, forge.KIND))
        self.assertEqual(core.list_accounts(), [self.account])
        self.assertEqual(self.keyring, {("git.example.com", "dana"): "s3cret"})
        git_calls = [call.args[0] for call in core._run.call_args_list]
        self.assertEqual(git_calls[0][-2:], ["credential.https://git.example.com.helper", ""])
        self.assertTrue(git_calls[1][-1].endswith(" git-credential"))

    def test_token_is_not_written_to_disk(self):
        for path in Path(self.dir.name).rglob("*"):
            if path.is_file():
                self.assertNotIn("s3cret", path.read_text())
        self.assertEqual(forge.accounts_file().stat().st_mode & 0o777, 0o600)
        self.assertNotIn("s3cret", json.dumps(core.describe_accounts(check_tokens=False)))

    def test_credential_helper_answers_only_matching_https_requests(self):
        answer = forge.credential_helper("get", self.request(protocol="https", host="git.example.com"))
        self.assertEqual(answer, "username=dana\npassword=s3cret\n")
        for operation, fields in (
            ("get", {"protocol": "http", "host": "git.example.com"}),
            ("get", {"protocol": "https", "host": "evil.example.com"}),
            ("get", {"protocol": "https", "host": "git.example.com.evil.example"}),
            ("get", {"protocol": "https", "host": "git.example.com", "username": "someone-else"}),
            ("store", {"protocol": "https", "host": "git.example.com"}),
            ("erase", {"protocol": "https", "host": "git.example.com"}),
        ):
            self.assertEqual(forge.credential_helper(operation, self.request(**fields)), "", fields)

    def test_switch_and_logout(self):
        with mock.patch.object(forge, "_profile", return_value=core.Profile(valid=True, login="erin", user_id=8)):
            forge.add_account("git.example.com", "other")
        self.assertEqual([a.user for a in core.list_accounts() if a.active], ["erin"])
        result = core.switch_account("dana", sync_identity=False)
        self.assertEqual(result["kind"], forge.KIND)
        self.assertEqual([a.user for a in core.list_accounts() if a.active], ["dana"])
        core.logout_account(core.find_account("dana"))
        self.assertEqual([(a.user, a.active) for a in core.list_accounts()], [("erin", True)])
        self.assertNotIn(("git.example.com", "dana"), self.keyring)
        core.logout_account(core.find_account("erin"))
        self.assertEqual(core.list_accounts(), [])
        self.assertEqual(self.keyring, {})

    def test_rejected_token_is_not_stored(self):
        with mock.patch.object(forge, "_profile", return_value=core.Profile(valid=False)):
            with self.assertRaises(core.GhError):
                forge.add_account("git.example.com", "bad")
        self.assertNotIn("bad", self.keyring.values())

    def test_api_goes_to_the_account_host_with_its_token(self):
        with mock.patch.object(core, "api_request", return_value=({"ok": True}, {})) as request:
            self.assertEqual(forge.api("post", "/repos/dana/x/issues", {"title": "t"}), {"ok": True})
        url, authorization, method, data = request.call_args.args[:4]
        self.assertEqual((url, authorization, method), ("https://git.example.com/api/v1/repos/dana/x/issues", "token s3cret", "POST"))
        self.assertEqual(json.loads(data), {"title": "t"})

    def test_api_accepts_the_path_with_or_without_its_root(self):
        with mock.patch.object(core, "api_request", return_value=({}, {})) as request:
            for path in ("/repos/dana/x", "repos/dana/x", "/api/v1/repos/dana/x", "https://git.example.com/api/v1/repos/dana/x"):
                forge.api("GET", path)
                self.assertEqual(request.call_args.args[0], "https://git.example.com/api/v1/repos/dana/x", path)

    def test_api_rejects_escaping_paths_and_credential_endpoints(self):
        with mock.patch.object(core, "api_request") as request:
            for method, path in (
                ("GET", "//evil.example/api"),
                ("GET", "/../../login"),
                ("GET", "/repos/dana/x /y"),
                ("GET", "/users/dana/tokens"),
                ("POST", "/user/applications/oauth2"),
                ("TRACE", "/user"),
            ):
                with self.assertRaises(core.GhError, msg=path):
                    forge.api(method, path)
            with self.assertRaises(core.GhError):
                forge.api("GET", "/user", host="other.example.com")
        request.assert_not_called()

    def test_mcp_tools_reach_the_api(self):
        with mock.patch.object(forge, "api", return_value=[]) as api:
            call = {"name": "forgejo_api_get", "arguments": {"path": "/user/repos"}}
            mcp.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": call})
            call = {"name": "forgejo_api_write", "arguments": {"method": "GET", "path": "/user"}}
            result = mcp.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": call})["result"]
        api.assert_called_once_with("GET", "/user/repos", None, None, forge.FORGEJO)
        self.assertTrue(result["isError"])

    def test_noreply_address(self):
        profile = core.Profile(valid=True, login="dana", user_id=7)
        self.assertEqual(core.noreply_email("git.example.com:3000", profile, forge.KIND), "dana@noreply.git.example.com")


class GitLabTest(unittest.TestCase):
    """GitLab accounts share the Forgejo machinery but differ in API root, auth header and fields."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.keyring = {}
        user = {"username": "dana", "name": "Dana", "id": 42, "avatar_url": "https://a/x.png"}
        self.token_info = {"name": "Git-Switch", "expires_at": "2026-12-31"}

        def api_get(url, _authorization):
            if url.endswith("/personal_access_tokens/self"):
                if self.token_info is None:
                    raise urllib.error.HTTPError(url, 403, "Forbidden", {}, None)
                return self.token_info, {}
            return user, {}

        self.api_get = mock.Mock(side_effect=api_get)
        patches = [
            mock.patch.dict(os.environ, {"GH_CONFIG_DIR": self.dir.name, "XDG_CONFIG_HOME": self.dir.name}),
            mock.patch.object(forge, "store_token", lambda host, user, token: self.keyring.__setitem__((host, user), token)),
            mock.patch.object(forge, "lookup_token", lambda host, user: self.keyring.get((host, user), "")),
            mock.patch.object(forge, "clear_token", lambda host, user: self.keyring.pop((host, user), None)),
            mock.patch.object(core, "api_get", self.api_get),
            mock.patch.object(core, "_run"),
            mock.patch.object(leaks, "gather", return_value=leaks.Context()),
        ]
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)
        self.account = forge.add_account("gitlab.com", "glpat-secret", forge.GITLAB)

    def test_profile_comes_from_api_v4_with_a_bearer_token(self):
        self.assertEqual(self.account, core.Account("gitlab.com", "dana", True, forge.GITLAB))
        self.assertEqual(self.api_get.call_args_list[0], mock.call("https://gitlab.com/api/v4/user", "Bearer glpat-secret"))
        self.assertEqual(forge.fetch_profile(self.account).name, "Dana")
        self.assertEqual(json.loads(forge.accounts_file().read_text())["gitlab.com"]["kind"], forge.GITLAB)

    def test_noreply_address(self):
        profile = core.Profile(valid=True, login="dana", user_id=42)
        self.assertEqual(core.noreply_email("gitlab.com", profile, forge.GITLAB), "42-dana@users.noreply.gitlab.com")

    def test_expiry_comes_from_the_token_itself(self):
        self.api_get.assert_any_call("https://gitlab.com/api/v4/personal_access_tokens/self", "Bearer glpat-secret")
        end_of_year = datetime(2026, 12, 31, tzinfo=timezone.utc)
        self.assertEqual(core.fetch_profile(self.account).expires, end_of_year)
        # Remembered when the account was added, without the token.
        stored = expiry.store_file().read_text()
        self.assertIn("2026-12-31T00:00:00Z", stored)
        self.assertNotIn("glpat-secret", stored)
        self.token_info = {"name": "Git-Switch", "expires_at": None}
        self.assertEqual(core.fetch_profile(self.account).expires, expiry.NEVER)
        # A token that may not read its own details still works; its expiry is just unknown.
        self.token_info = None
        profile = core.fetch_profile(self.account)
        self.assertTrue(profile.valid)
        self.assertIsNone(profile.expires)

    def test_credential_helper_and_kinds_stay_apart(self):
        request = "protocol=https\nhost=gitlab.com\n"
        self.assertEqual(forge.credential_helper("get", request), "username=dana\npassword=glpat-secret\n")
        with self.assertRaises(core.GhError):
            forge.add_account("gitlab.com", "x", forge.FORGEJO)
        with self.assertRaises(core.GhError):
            forge.api("GET", "/user")  # no Forgejo account
        with self.assertRaises(core.GhError):
            forge.api("POST", "/user/personal_access_tokens", kind=forge.GITLAB)

    def test_api_uses_gitlab_root(self):
        with mock.patch.object(core, "api_request", return_value=([], {})) as request:
            call = {"name": "gitlab_api_get", "arguments": {"path": "/projects?membership=true"}}
            mcp.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": call})
        url, authorization = request.call_args.args[:2]
        self.assertEqual((url, authorization), ("https://gitlab.com/api/v4/projects?membership=true", "Bearer glpat-secret"))

    def test_path_forms_all_reach_api_v4(self):
        with mock.patch.object(core, "api_request", return_value=({}, {})) as request:
            for path in ("/user", "user", "/api/v4/user", "api/v4/user", "https://gitlab.com/api/v4/user"):
                forge.api("GET", path, kind=forge.GITLAB)
                self.assertEqual(request.call_args.args[0], "https://gitlab.com/api/v4/user", path)
            forge.api("GET", "/api/v4?x=1", kind=forge.GITLAB)
            self.assertEqual(request.call_args.args[0], "https://gitlab.com/api/v4/?x=1")
            for path in ("https://evil.example/api/v4/user", "http://gitlab.com/api/v4/user", "/api/v4/../../x"):
                with self.assertRaises(core.GhError, msg=path):
                    forge.api("GET", path, kind=forge.GITLAB)

    def test_forgejo_tool_on_a_gitlab_host_uses_api_v4(self):
        with mock.patch.object(core, "api_request", return_value=({}, {})) as request:
            call = {"name": "forgejo_api_get", "arguments": {"path": "/user", "hostname": "gitlab.com"}}
            result = mcp.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": call})["result"]
        self.assertFalse(result.get("isError"))
        self.assertEqual(request.call_args.args[0], "https://gitlab.com/api/v4/user")

    def test_public_gitlab_is_not_a_private_server(self):
        accounts = [self.account, core.Account("gitlab.example.com", "dana", True, forge.GITLAB)]
        with mock.patch.object(core, "list_accounts", return_value=accounts), \
                mock.patch.object(core, "get_git_identity", return_value=("", "")):
            context = REAL_GATHER()
        self.assertEqual(context.hosts, {"gitlab.example.com"})
        self.assertIn("glpat-secret", context.tokens)


class ExpiryTest(unittest.TestCase):
    NOW = datetime(2026, 11, 12, 12, 0, tzinfo=timezone.utc)

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        english = gettext.NullTranslations()
        patches = [
            mock.patch.dict(os.environ, {"GH_CONFIG_DIR": self.dir.name, "XDG_CONFIG_HOME": self.dir.name}),
            # The texts are compared in English, whatever the locale running the tests.
            mock.patch.object(expiry, "_", english.gettext),
            mock.patch.object(expiry, "ngettext", english.ngettext),
            mock.patch.object(mcp, "_", english.gettext),
            mock.patch.object(mcp, "ngettext", english.ngettext),
        ]
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)

    def describe(self, expires):
        return expiry.describe(expires, lambda moment: f"{moment:%Y-%m-%d}", self.NOW)

    def test_parse_the_formats_servers_send(self):
        utc = timezone.utc
        for text, expected in (
            ("2026-12-31 08:15:00 UTC", datetime(2026, 12, 31, 8, 15, tzinfo=utc)),
            ("2026-12-31 08:15:00 +0200", datetime(2026, 12, 31, 6, 15, tzinfo=utc)),
            ("2026-12-31T08:15:00Z", datetime(2026, 12, 31, 8, 15, tzinfo=utc)),
            ("2026-12-31T08:15:00-05:00", datetime(2026, 12, 31, 13, 15, tzinfo=utc)),
            ("2026-12-31", datetime(2026, 12, 31, tzinfo=utc)),
        ):
            self.assertEqual(expiry.parse(text), expected, text)
            self.assertEqual(expiry.parse(text).tzinfo, utc)
        for text in ("", "   ", "soon", None, 42):
            self.assertIsNone(expiry.parse(text), text)
        self.assertEqual(expiry.to_iso(datetime(2026, 12, 31, 8, 15, tzinfo=utc)), "2026-12-31T08:15:00Z")
        self.assertIsNone(expiry.to_iso(expiry.NEVER))
        self.assertIsNone(expiry.to_iso(None))

    def test_days_left_rounds_up(self):
        hours = timedelta(hours=1)
        self.assertEqual(expiry.days_left(self.NOW + 3 * hours, self.NOW), 1)
        self.assertEqual(expiry.days_left(self.NOW + timedelta(days=10), self.NOW), 10)
        self.assertEqual(expiry.days_left(self.NOW - 3 * hours, self.NOW), -1)
        self.assertIsNone(expiry.days_left(expiry.NEVER, self.NOW))
        self.assertIsNone(expiry.days_left(None, self.NOW))
        self.assertTrue(expiry.is_expired(self.NOW, self.NOW))
        self.assertFalse(expiry.is_expired(expiry.NEVER, self.NOW))

    def test_describe(self):
        days = timedelta(days=1)
        self.assertEqual(self.describe(expiry.NEVER), ("No expiry date", "ok"))
        self.assertIsNone(self.describe(None))
        self.assertEqual(self.describe(self.NOW + 30 * days), ("Valid until 2026-12-12", "ok"))
        self.assertEqual(self.describe(self.NOW + 15 * days), ("Valid until 2026-11-27", "ok"))
        self.assertEqual(self.describe(self.NOW + 14 * days), ("Expires in 14 days", "warning"))
        self.assertEqual(self.describe(self.NOW + timedelta(hours=2)), ("Expires in 1 day", "warning"))
        self.assertEqual(self.describe(self.NOW - 2 * days), ("Expired on 2026-11-10", "expired"))
        self.assertTrue(expiry.expires_soon(self.NOW + 7 * days, expiry.TOAST_DAYS, self.NOW))
        self.assertFalse(expiry.expires_soon(self.NOW + 8 * days, expiry.TOAST_DAYS, self.NOW))
        self.assertFalse(expiry.expires_soon(self.NOW - days, expiry.TOAST_DAYS, self.NOW))

    def test_german_plural(self):
        german = gettext.translation("gitswitch", CatalogTest.LOCALE_DIR, languages=["de"])
        with mock.patch.object(expiry, "ngettext", german.ngettext):
            self.assertEqual(self.describe(self.NOW + timedelta(hours=2))[0], "Läuft in 1 Tag ab")
            self.assertEqual(self.describe(self.NOW + timedelta(days=3))[0], "Läuft in 3 Tagen ab")

    def test_store_remembers_and_forgets(self):
        moment = datetime(2026, 12, 31, tzinfo=timezone.utc)
        expiry.remember("github.com", "alice", moment)
        expiry.remember("github.com", "bob", expiry.NEVER)
        expiry.remember("github.com", "carol", None)
        self.assertEqual(expiry.recall("github.com", "alice"), moment)
        self.assertEqual(expiry.recall("github.com", "bob"), expiry.NEVER)
        self.assertIsNone(expiry.recall("github.com", "carol"))
        self.assertEqual(expiry.store_file().stat().st_mode & 0o777, 0o600)
        expiry.forget("github.com", "alice")
        expiry.forget("github.com", "bob")
        self.assertEqual(json.loads(expiry.store_file().read_text()), {})

    def github(self, headers=None, error=None):
        """Fetch the profile of alice with gh and the GitHub API replaced by fakes."""
        message = Message()
        for key, value in (headers or {}).items():
            message[key] = value
        api = mock.Mock(return_value=({"login": "alice", "id": 1}, message), side_effect=error)
        token = mock.Mock(returncode=0, stdout="gho_secret\n")
        with mock.patch.object(core, "_run", return_value=token), mock.patch.object(core, "api_get", api):
            return core.fetch_profile(core.Account("github.com", "alice", True))

    def test_github_header_and_remembered_expiry(self):
        # Headers are case-insensitive; GitHub sends this one in lower case.
        profile = self.github({"github-authentication-token-expiration": "2026-11-01 08:00:00 UTC"})
        self.assertEqual(profile.expires, datetime(2026, 11, 1, 8, tzinfo=timezone.utc))
        self.assertNotIn("gho_secret", expiry.store_file().read_text())
        refused = urllib.error.HTTPError("https://api.github.com/user", 401, "Unauthorized", {}, None)
        profile = self.github(error=refused)
        self.assertFalse(profile.valid)
        self.assertEqual(profile.expires, datetime(2026, 11, 1, 8, tzinfo=timezone.utc))
        self.assertEqual(self.describe(profile.expires), ("Expired on 2026-11-01", "expired"))

    def test_github_oauth_token_never_expires(self):
        self.assertEqual(self.github().expires, expiry.NEVER)
        self.assertEqual(expiry.recall("github.com", "alice"), expiry.NEVER)

    def test_forgejo_tokens_have_no_expiry(self):
        user = {"login": "dana", "id": 7}
        with mock.patch.object(core, "api_get", return_value=(user, {})) as api:
            profile = forge._profile("git.example.com", "dana", "s3cret", forge.FORGEJO)
        self.assertEqual(profile.expires, expiry.NEVER)
        api.assert_called_once()

    def test_describe_accounts_and_logout(self):
        moment = datetime.now(timezone.utc) + timedelta(days=5, hours=1)
        profile = core.Profile(valid=True, login="alice", expires=moment)
        account = core.Account("github.com", "alice", True)
        with mock.patch.object(core, "list_accounts", return_value=[account]), \
                mock.patch.object(core, "fetch_profile", return_value=profile):
            entry = core.describe_accounts()[0]
        self.assertEqual(entry["expires"], expiry.to_iso(moment))
        self.assertEqual(entry["expires_in_days"], 6)
        self.assertFalse(entry["never_expires"])
        json.dumps(entry)
        expiry.remember("github.com", "alice", moment)
        with mock.patch.object(core, "_run"):
            core.logout_account(account)
        self.assertIsNone(expiry.recall("github.com", "alice"))

    def test_mcp_notices(self):
        now = datetime.now(timezone.utc)
        accounts = [
            {"user": "alice", "host": "github.com", "expires": expiry.to_iso(now - timedelta(days=2))},
            {"user": "bob", "host": "github.com", "expires": expiry.to_iso(now + timedelta(days=3, hours=1))},
            {"user": "carol", "host": "github.com", "expires": expiry.to_iso(now + timedelta(days=40))},
            {"user": "dana", "host": "git.example.com", "expires": None},
        ]
        notices = mcp.expiry_notices(accounts)
        self.assertEqual(len(notices), 2)
        self.assertIn("alice on github.com expired on", notices[0])
        self.assertIn("bob on github.com expires on", notices[1])
        self.assertIn("(in 4 days)", notices[1])
        with mock.patch.object(core, "describe_accounts", return_value=accounts), \
                mock.patch.object(mcp, "notices", return_value=[]):
            call = {"name": "list_github_accounts", "arguments": {}}
            result = mcp.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": call})["result"]
        self.assertEqual(json.loads(result["content"][0]["text"])["notices"], notices)


class ReplaceTokenTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        patcher = mock.patch.dict(os.environ, {"GH_CONFIG_DIR": self.dir.name, "XDG_CONFIG_HOME": self.dir.name,
                                               "XDG_CACHE_HOME": self.dir.name})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_forge_token_is_replaced_only_for_the_same_account(self):
        account = core.Account("git.example.com", "dana", True, forge.FORGEJO)
        keyring = {}
        with mock.patch.object(forge, "store_token", lambda h, u, t: keyring.__setitem__((h, u), t)):
            with mock.patch.object(forge, "_profile", return_value=core.Profile(valid=True, login="Dana")):
                core.replace_token(account, " new-token ")
            self.assertEqual(keyring, {("git.example.com", "dana"): "new-token"})
            for profile in (core.Profile(valid=True, login="erin"), core.Profile(valid=False), core.Profile(valid=None)):
                with mock.patch.object(forge, "_profile", return_value=profile), self.assertRaises(core.GhError):
                    core.replace_token(account, "other")
        self.assertEqual(keyring, {("git.example.com", "dana"): "new-token"})

    def test_github_token_goes_to_gh_and_the_active_account_stays(self):
        account = core.Account("github.com", "bob", False)
        others = [core.Account("github.com", "alice", True), account]
        with mock.patch.object(core, "api_get", return_value=({"login": "bob"}, {})), \
                mock.patch.object(core, "list_accounts", return_value=others), \
                mock.patch.object(core, "_run") as run:
            core.replace_token(account, "ghp_new")
        calls = [call.args[0] for call in run.call_args_list]
        self.assertEqual(calls[0], ["gh", "auth", "login", "--hostname", "github.com", "--with-token"])
        self.assertEqual(run.call_args_list[0].kwargs["stdin_text"], "ghp_new\n")
        self.assertEqual(calls[1], ["gh", "auth", "switch", "--hostname", "github.com", "--user", "alice"])

    def test_github_token_of_another_account_is_refused(self):
        account = core.Account("github.com", "bob", True)
        with mock.patch.object(core, "api_get", return_value=({"login": "mallory"}, {})), \
                mock.patch.object(core, "_run") as run, self.assertRaises(core.GhError):
            core.replace_token(account, "ghp_other")
        run.assert_not_called()


class ApiTest(unittest.TestCase):
    def test_tokens_are_not_sent_over_plain_http(self):
        with self.assertRaises(ValueError):
            core.api_get("http://git.example.com/api/v1/user", "token x")

    def test_redirects_are_not_followed(self):
        self.assertIsNone(core._NoRedirect().redirect_request(None, None, 302, "Found", {}, "https://evil.example"))


class LeakTest(unittest.TestCase):
    def setUp(self):
        self.context = leaks.Context(
            tokens={"abcdef0123456789abcdef0123456789abcdef01": "dana@git.example.com"},
            hosts={"git.example.com"},
            emails={"dana@real-mail.example.net"},
            home="/home/dana",
            machine="danas-laptop",
        )

    def kinds(self, text):
        return [finding.kind for finding in leaks.scan_text(text, "t", self.context)]

    def test_repository_with_text_that_is_not_utf8(self):
        with tempfile.TemporaryDirectory() as folder:
            git = ["git", "-C", folder, "-c", "user.name=t", "-c", "user.email=t@example.com"]
            subprocess.run(["git", "init", "-q", folder], check=True)
            subprocess.run([*git, "commit", "-q", "--allow-empty", "-m", "init"], check=True)
            Path(folder, "notes.txt").write_bytes("Straße für https://git.example.com/dana/x\n".encode("cp1252"))
            subprocess.run([*git, "add", "notes.txt"], check=True)
            found = leaks.scan_repository(folder, self.context)
        self.assertEqual({f.kind for f in found}, {"private-server"})

    def test_mcp_survives_an_unexpected_error(self):
        call = {"name": "check_for_leaks", "arguments": {"text": "x"}}
        with mock.patch.object(mcp, "_call_tool", side_effect=UnicodeDecodeError("utf-8", b"\xdf", 0, 1, "invalid")):
            result = mcp.handle({"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": call})["result"]
        self.assertTrue(result["isError"])
        self.assertIn("UnicodeDecodeError", result["content"][0]["text"])

    def test_finds_what_is_private_here(self):
        self.assertEqual(self.kinds("see https://git.example.com/dana/x"), ["private-server"])
        self.assertEqual(self.kinds("token abcdef0123456789abcdef0123456789abcdef01"), ["live-token"])
        self.assertEqual(self.kinds("GH_TOKEN=ghp_" + "a" * 36), ["token"])
        self.assertEqual(self.kinds("-----BEGIN OPENSSH PRIVATE KEY-----"), ["private-key"])
        self.assertEqual(self.kinds("Dana <dana@real-mail.example.net>"), ["own-email"])
        self.assertEqual(self.kinds("mail bob@company.io"), ["email"])
        self.assertEqual(self.kinds("see /home/dana/projects"), ["home-path"])
        self.assertEqual(self.kinds("built on danas-laptop"), ["machine-name"])
        self.assertEqual(self.kinds("http://192.168.1.20:3000 and nas.local"), ["private-address", "internal-host"])

    def test_leaves_public_things_alone(self):
        for text in (
            "https://github.com/dana/x",
            "7+dana@users.noreply.github.com and dana@noreply.git.example.org",
            "git@github.com:dana/x.git",
            "commit 25cd9b7f51af22f3669200591f28d7438997f603",
            "listen on 127.0.0.1 or 8.8.8.8, version 1.2.3.4567",
            "someone@example.com",
        ):
            self.assertEqual(self.kinds(text), [], text)

    def test_secrets_are_masked_and_allow_list_is_honoured(self):
        finding = leaks.scan_text("x abcdef0123456789abcdef0123456789abcdef01", "t", self.context)[0]
        self.assertNotIn("abcdef0123456789abcdef", finding.excerpt)
        self.context.allow = ["git.example.com"]
        self.assertEqual(self.kinds("https://git.example.com/dana/x"), [])

    def test_repository_scan_covers_unpublished_commits(self):
        with tempfile.TemporaryDirectory() as directory:
            def git(*args):
                env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull}
                core.subprocess.run(["git", "-C", directory, *args], check=True, capture_output=True, env=env)

            git("init", "-q")
            Path(directory, "README.md").write_text("Mirror: https://git.example.com/dana/x\n")
            git("add", "-A")
            git("-c", "user.name=Dana", "-c", "user.email=dana@real-mail.example.net", "commit", "-q", "-m", "init")
            found = leaks.report(leaks.scan_repository(directory, self.context))["findings"]
        self.assertEqual({f["kind"] for f in found}, {"private-server", "own-email"})

    def restrict(self, repository="git.example.com/dana/notes"):
        rules = {"terms": ["Acme", "Wile E."], "files": ["ACME.md", ".acme"], "repository": repository}
        leaks._restricted_settings(self.context, {"restricted": rules})

    def test_restricted_terms_only_in_the_set_aside_repository(self):
        self.assertEqual(self.kinds("Acme built this"), [])
        self.restrict("git@git.example.com:dana/notes.git")
        self.assertEqual(self.context.restricted_repository, "git.example.com/dana/notes")
        self.assertEqual(self.kinds("Co-Authored-By: Acme Bot <bot@acme.invalid>"), ["restricted-attribution"])
        self.assertEqual(self.kinds("Generated with [Acme Tools](https://acme.invalid)"), ["restricted-attribution"])
        self.assertEqual(self.kinds("asked acme about it, and Wile E. too"), ["restricted-term"])
        self.assertEqual(self.kinds("an acmeish word"), [])
        self.assertTrue(leaks.restricted_allowed("git.example.com", "Dana/Notes", self.context))
        self.assertFalse(leaks.restricted_allowed("git.example.com", "dana/other", self.context))
        self.assertEqual(leaks.repository_id("https://Git.Example.com/dana/notes.git/"), "git.example.com/dana/notes")

    def test_repository_scan_honours_the_set_aside_repository(self):
        self.restrict()
        with tempfile.TemporaryDirectory() as directory:
            env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull}

            def git(*args):
                core.subprocess.run(["git", "-C", directory, *args], check=True, capture_output=True, env=env)

            git("init", "-q")
            Path(directory, "ACME.md").write_text("Notes for Acme\n")
            git("add", "-A")
            git("-c", "user.name=D", "-c", "user.email=d@users.noreply.github.com", "commit", "-q", "-m", "notes")
            git("remote", "add", "origin", "https://github.com/dana/public.git")
            kinds = {f["kind"] for f in leaks.report(leaks.scan_repository(directory, self.context))["findings"]}
            self.assertEqual(kinds, {"restricted-file", "restricted-term"})
            git("remote", "set-url", "origin", "https://git.example.com/dana/notes.git")
            self.assertEqual(leaks.report(leaks.scan_repository(directory, self.context))["findings"], [])

    def test_mcp_write_refuses_secrets(self):
        with mock.patch.object(leaks, "gather", return_value=self.context), mock.patch.object(forge, "api") as api:
            call = {
                "name": "forgejo_api_write",
                "arguments": {"method": "POST", "path": "/repos/a/b/issues", "body": {"body": "ghp_" + "b" * 36}},
            }
            result = mcp.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": call})["result"]
        self.assertTrue(result["isError"])
        api.assert_not_called()


class RestrictedPhraseTest(unittest.TestCase):
    def test_allowed_phrases_pass_but_attributions_do_not(self):
        context = leaks.Context()
        leaks._restricted_settings(context, {"restricted": {"terms": ["Acme"], "allow": ["Acme Studio", "acme-cli"]}})

        def kinds(line):
            return [finding.kind for finding in leaks.scan_line(line, "t", context)]

        self.assertEqual(kinds("Integration with Acme Studio"), [])
        self.assertEqual(kinds('run(["acme-cli", "add"])'), [])
        self.assertEqual(kinds("Written by Acme"), ["restricted-term"])
        self.assertEqual(kinds("Generated with Acme Studio"), ["restricted-attribution"])


class UpdateTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        patcher = mock.patch.dict(os.environ, {"XDG_CACHE_HOME": self.dir.name, "XDG_CONFIG_HOME": self.dir.name})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_newer_release_is_reported_and_cached(self):
        newer = {"version": "99.0.0", "url": "https://example.com/r"}
        with mock.patch.object(update, "_fetch_latest", return_value=newer) as fetch:
            self.assertEqual(update.check(), newer)
            self.assertEqual(update.check(), newer)
        fetch.assert_called_once()
        self.assertEqual(update.cached(), newer)
        self.assertTrue(any("99.0.0" in notice for notice in mcp.notices()))

    def test_same_version_and_failures_report_nothing(self):
        current = {"version": update.__version__, "url": "u"}
        with mock.patch.object(update, "_fetch_latest", return_value=current):
            self.assertIsNone(update.check())
        with mock.patch.object(update, "_fetch_latest", side_effect=OSError):
            self.assertIsNone(update.check(force=True))

    def test_switch_turns_the_check_off(self):
        core.save_settings({"check_updates": False})
        with mock.patch.object(update, "_fetch_latest") as fetch:
            self.assertIsNone(update.check())
        fetch.assert_not_called()

    def test_outdated_process_is_noticed(self):
        with mock.patch.object(update, "installed_version", return_value="99.0.0"):
            self.assertEqual(update.outdated_process(), "99.0.0")
        self.assertIsNone(update.outdated_process())


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
        self.assertEqual(
            names,
            [
                "list_github_accounts",
                "switch_github_account",
                "forgejo_api_get",
                "forgejo_api_write",
                "forgejo_upload_release_asset",
                "gitlab_api_get",
                "gitlab_api_write",
                "check_for_leaks",
            ],
        )

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
