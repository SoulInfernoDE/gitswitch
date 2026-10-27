<p align="center">
  <img src="gitswitch/icons/hicolor/scalable/apps/dev.gitswitch.GitSwitch.svg" width="128" alt="Git-Switch logo">
</p>

<h1 align="center">Git-Switch</h1>

<p align="center">
  Switch between GitHub CLI accounts with one click.<br>
  <b>English</b> · <a href="README.de.md">Deutsch</a>
</p>

<p align="center">
  <img src="docs/screenshot.png" width="480" alt="Git-Switch window">
</p>

A small GTK app around `gh auth switch`: see every account the
[GitHub CLI](https://cli.github.com) is logged in to and switch between them
with one click – from the window, from the command line, or from Claude Code
through the bundled MCP server.

## Features

- Lists all accounts per host, with avatar, active marker and a live check
  whether the stored token still works
- One-click switching (`gh auth switch`)
- Add accounts through a guided dialog: GitHub.com, GitHub Enterprise or a
  Forgejo/Gitea server, sign in in the browser or with an access token – no
  terminal needed
- Forgejo and Gitea accounts with the same safeguards gh applies to GitHub
  tokens (see below)
- Sign out of single accounts
- Shows the permissions (scopes) of each token; add or remove them by ticking
  boxes in a list with descriptions (`gh auth refresh`)
- Copy an account's token to the clipboard (`gh auth token`)
- Per host: choose HTTPS or SSH as git protocol and set up gh as git
  credential helper (`gh auth setup-git`)
- Optional: keep the global git `user.name` / `user.email` in sync with the
  active account (uses the account's GitHub noreply address)
- Follows changes made elsewhere (the `gh` CLI, the MCP server) immediately
- MCP server so Claude Code can list and switch accounts
- Leak check: finds tokens, addresses of your own servers, your real e-mail
  address and similar private data before they are published (see below)
- Looks for a newer version on start, in the window and in the MCP server
- Soft sound effects for switching, sign-in and errors (can be turned off)
- English and German interface

GitHub accounts are managed through the GitHub CLI; Forgejo and Gitea accounts
are managed by Git-Switch itself. GitLab is not supported.

## Install

Requires `gh` ≥ 2.40, `git`, Python ≥ 3.10, GTK 4 and libadwaita ≥ 1.4.
Forgejo/Gitea accounts additionally need libsecret and a running keyring
(GNOME Keyring, KWallet or similar).

**Debian / Ubuntu / Linux Mint** – download the `.deb` from the
[releases page](../../releases) and install it:

```bash
sudo apt install ./gitswitch_0.5.1_all.deb
```

**Other distributions** – install PyGObject, GTK 4 and libadwaita from your
package manager, then:

```bash
pipx install --system-site-packages gitswitch-0.5.1-py3-none-any.whl
```

**From source** – `python3 -m gitswitch` inside a checkout.

## Usage

```bash
gitswitch                 # open the window
gitswitch list            # accounts as JSON
gitswitch switch <user>   # switch from the shell
gitswitch leak-check      # check what the next push would publish
```

## Forgejo and Gitea

Choose **Forgejo or Gitea** in the add-account dialog, enter the server address
and paste an access token (permissions `read:user` and `write:repository`).
Git-Switch reads the account name from the server, so there is nothing else to
type.

Tokens are handled the way gh handles GitHub tokens:

- A token is stored in the system keyring only – never in a file, in the git
  configuration, in a command line or in a log. Without a keyring the account
  is not added; there is no plain-text fallback.
- git gets it through a credential helper (`gitswitch git-credential`) that is
  set up for that one host. It answers only HTTPS requests for exactly that
  host, with the token of the active account.
- Git-Switch sends a token only to its own server, only over HTTPS, and never
  follows redirects while doing so.
- Tokens are not shown in the window and not returned by the command line or
  the MCP server.
- With **Sync Git identity**, the address `login@noreply.<host>` is used, not
  your real one. If your server uses a different no-reply domain, commits are
  simply not linked to the account.

A user name in a remote URL (`https://name@git.example.com/…`) selects that
account for the repository regardless of which one is active.

## Claude Code integration

Turn on **Claude Code integration** in the window, or register the server
yourself:

```bash
claude mcp add --scope user gitswitch -- gitswitch-mcp
```

Restart Claude Code afterwards. These tools become available:

| Tool | Purpose |
| --- | --- |
| `list_github_accounts` | All accounts per host (GitHub, Forgejo, Gitea), the active one, token validity and scopes |
| `switch_github_account` | Activate an account (`user`, optional `hostname`) |
| `forgejo_api_get` | Read from the REST API of a Forgejo/Gitea server, like `gh api` for GitHub |
| `forgejo_api_write` | Change things through that API: issues, pull requests, releases, mirror sync … |
| `forgejo_upload_release_asset` | Attach a local file to a release |
| `check_for_leaks` | Check text, files or the next push for private data before publishing |

The names of the first two tools date from the GitHub-only versions; both
cover Forgejo and Gitea accounts too. Switching is global: it changes the account for every
`gh` and `git` (HTTPS) process on the machine. The server never returns tokens.

The Forgejo tools exist because there is no counterpart to the `gh` command for
these servers. They act as the active account of the server. Reading and
writing are separate tools so that reading can be allowed permanently while
changes still ask for confirmation. Requests only go to a signed-in server,
paths cannot leave its API, and endpoints that issue credentials are blocked.

## Leak check

Git-Switch knows what is private on your machine, so it can look for exactly
that in content that is about to be published:

| Severity | What is found |
| --- | --- |
| `secret` | The live tokens of the signed-in accounts, other access tokens, private keys |
| `private` | Addresses of your self-hosted servers, your real git e-mail address, paths in your home directory |
| `review` | Other e-mail addresses, the computer name, private network addresses, internal host names |

```bash
gitswitch leak-check                 # staged changes and commits not yet pushed
gitswitch leak-check README.md docs  # files or folders
```

The command exits with status 1 if it finds something, so it works as a git
`pre-push` hook. In Claude Code the MCP server tells the model to run
`check_for_leaks` before it commits, pushes or publishes anything, and the
Forgejo write tools refuse requests that contain a secret. Entries listed
under `leak_allow` in `~/.config/gitswitch/config.json` are ignored, for
example a server that is meant to be public.

The check reads text only: it cannot see what an image such as a screenshot
shows.

## Updates

On start the window and the MCP server ask GitHub for the latest release, at
most every 12 hours and without using any account. The window shows a banner;
the MCP server mentions it in its results and also notices when it is still
running an older version after an upgrade. Turn it off with **Check for
updates on start**. Nothing is installed automatically.

## Development

```bash
python3 -m unittest discover -s tests
packaging/make-sounds.py   # regenerate the sound effects
packaging/update-translations.sh   # extract program texts, update and compile the catalogs
packaging/build-deb.sh
packaging/release.sh <notes file> [<forgejo host> <forgejo owner/repo>]   # build, release on GitHub, copy to the mirror
packaging/mirror-releases.py <github owner/repo> <forgejo host> <forgejo owner/repo>   # only copy releases to a Forgejo mirror
```

Program texts live in gettext catalogs under `gitswitch/locale/`; English is
the source language and the fallback. To add a language, copy
`gitswitch.pot` to `<lang>/LC_MESSAGES/gitswitch.po`, translate it and run
`packaging/update-translations.sh`.

## License

MIT
