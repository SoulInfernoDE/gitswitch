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
- Add accounts or re-authenticate expired ones with the browser device flow –
  no terminal needed
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
- Soft sound effects for switching, sign-in and errors (can be turned off)
- English and German interface

## Install

Requires `gh` ≥ 2.40, `git`, Python ≥ 3.10, GTK 4 and libadwaita ≥ 1.4.

**Debian / Ubuntu / Linux Mint** – download the `.deb` from the
[releases page](../../releases) and install it:

```bash
sudo apt install ./gitswitch_0.2.3_all.deb
```

**Other distributions** – install PyGObject, GTK 4 and libadwaita from your
package manager, then:

```bash
pipx install --system-site-packages gitswitch-0.2.3-py3-none-any.whl
```

**From source** – `python3 -m gitswitch` inside a checkout.

## Usage

```bash
gitswitch                 # open the window
gitswitch list            # accounts as JSON
gitswitch switch <user>   # switch from the shell
```

## Claude Code integration

Turn on **Claude Code integration** in the window, or register the server
yourself:

```bash
claude mcp add --scope user gitswitch -- gitswitch-mcp
```

Restart Claude Code afterwards. Two tools become available:

| Tool | Purpose |
| --- | --- |
| `list_github_accounts` | Accounts per host, the active one, token validity and scopes |
| `switch_github_account` | Activate an account (`user`, optional `hostname`) |

Switching is global: it changes the account for every `gh` and `git` (HTTPS)
process on the machine, exactly like running `gh auth switch` yourself. The
server never returns tokens.

## Development

```bash
python3 -m unittest discover -s tests
packaging/make-sounds.py   # regenerate the sound effects
packaging/update-translations.sh   # extract program texts, update and compile the catalogs
packaging/build-deb.sh
```

Program texts live in gettext catalogs under `gitswitch/locale/`; English is
the source language and the fallback. To add a language, copy
`gitswitch.pot` to `<lang>/LC_MESSAGES/gitswitch.po`, translate it and run
`packaging/update-translations.sh`.

## License

MIT
