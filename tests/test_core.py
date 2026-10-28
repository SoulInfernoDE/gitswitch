import gettext
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from gitswitch import core, forge, leaks, mcp, update

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
        self.api_get = mock.Mock(return_value=(user, {}))
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
        self.api_get.assert_called_once_with("https://gitlab.com/api/v4/user", "Bearer glpat-secret")
        self.assertEqual(forge.fetch_profile(self.account).name, "Dana")
        self.assertEqual(json.loads(forge.accounts_file().read_text())["gitlab.com"]["kind"], forge.GITLAB)

    def test_noreply_address(self):
        profile = core.Profile(valid=True, login="dana", user_id=42)
        self.assertEqual(core.noreply_email("gitlab.com", profile, forge.GITLAB), "42-dana@users.noreply.gitlab.com")

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
