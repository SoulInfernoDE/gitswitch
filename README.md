<img src="data/dev.gitswitch.GitSwitch.svg" width="96" align="right" alt="gitswitch logo">

# gitswitch

A small GTK app around `gh auth switch`: see every account the
[GitHub CLI](https://cli.github.com) is logged in to and switch between them
with one click – from the window, from the command line, or from Claude Code
through the bundled MCP server.

![gitswitch window](docs/screenshot.png)

## Features

- Lists all accounts per host, with avatar, active marker and a live check
  whether the stored token still works
- One-click switching (`gh auth switch`)
- Add accounts or re-authenticate expired ones with the browser device flow –
  no terminal needed
- Sign out of single accounts
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
sudo apt install ./gitswitch_0.1.0_all.deb
```

**Other distributions** – install PyGObject, GTK 4 and libadwaita from your
package manager, then:

```bash
pipx install --system-site-packages gitswitch-0.1.0-py3-none-any.whl
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
packaging/build-deb.sh
```

## License

MIT
