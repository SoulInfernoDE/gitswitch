<p align="center">
  <img src="gitswitch/icons/hicolor/scalable/apps/dev.gitswitch.GitSwitch.svg" width="128" alt="Git-Switch logo">
</p>

<h1 align="center">Git-Switch</h1>

<p align="center">
  Mit einem Klick zwischen GitHub-CLI-Konten wechseln.<br>
  <a href="README.md">English</a> · <b>Deutsch</b>
</p>

<p align="center">
  <img src="docs/screenshot.de.png" width="480" alt="Git-Switch-Fenster">
</p>

Eine kleine GTK-App rund um `gh auth switch`: Sie zeigt alle Konten, mit denen
die [GitHub CLI](https://cli.github.com) angemeldet ist, und wechselt mit einem
Klick zwischen ihnen – im Fenster, auf der Kommandozeile oder aus Claude Code
heraus über den mitgelieferten MCP-Server.

## Funktionen

- Listet alle Konten pro Host mit Avatar, Aktiv-Markierung und Live-Prüfung,
  ob das gespeicherte Token noch funktioniert
- Wechsel mit einem Klick (`gh auth switch`)
- Konten hinzufügen oder abgelaufene neu anmelden über den Browser – ganz ohne
  Terminal
- Einzelne Konten abmelden
- Zeigt die Berechtigungen (Scopes) jedes Tokens und kann weitere hinzufügen
  (`gh auth refresh`)
- Token eines Kontos in die Zwischenablage kopieren (`gh auth token`)
- Pro Host: HTTPS oder SSH als Git-Protokoll wählen und gh als
  Git-Anmeldehelfer einrichten (`gh auth setup-git`)
- Optional: globales git `user.name` / `user.email` folgt dem aktiven Konto
  (verwendet die GitHub-noreply-Adresse des Kontos)
- Übernimmt Änderungen von außen (`gh` CLI, MCP-Server) sofort
- MCP-Server, damit Claude Code Konten auflisten und wechseln kann
- Dezente Soundeffekte für Wechsel, Anmeldung und Fehler (abschaltbar)
- Oberfläche auf Deutsch und Englisch

## Installation

Benötigt `gh` ≥ 2.40, `git`, Python ≥ 3.10, GTK 4 und libadwaita ≥ 1.4.

**Debian / Ubuntu / Linux Mint** – das `.deb` von der
[Release-Seite](../../releases) herunterladen und installieren:

```bash
sudo apt install ./gitswitch_0.2.0_all.deb
```

**Andere Distributionen** – PyGObject, GTK 4 und libadwaita über den
Paketmanager installieren, dann:

```bash
pipx install --system-site-packages gitswitch-0.2.0-py3-none-any.whl
```

**Aus dem Quellcode** – `python3 -m gitswitch` in einem Checkout.

## Benutzung

```bash
gitswitch                 # Fenster öffnen
gitswitch list            # Konten als JSON
gitswitch switch <user>   # Wechsel aus der Shell
```

Die Sprache richtet sich nach der Systemsprache.

## Claude-Code-Integration

Im Fenster **Claude-Code-Integration** einschalten oder den Server selbst
registrieren:

```bash
claude mcp add --scope user gitswitch -- gitswitch-mcp
```

Danach Claude Code neu starten. Zwei Tools stehen dann zur Verfügung:

| Tool | Zweck |
| --- | --- |
| `list_github_accounts` | Konten pro Host, das aktive Konto, Token-Gültigkeit und Scopes |
| `switch_github_account` | Ein Konto aktivieren (`user`, optional `hostname`) |

Der Wechsel gilt global: Er ändert das Konto für jeden `gh`- und `git`-Prozess
(HTTPS) auf dem Rechner, genau wie ein eigenes `gh auth switch`. Der Server
gibt niemals Tokens zurück.

## Entwicklung

```bash
python3 -m unittest discover -s tests
packaging/make-sounds.py   # Soundeffekte neu erzeugen
packaging/build-deb.sh
```

## Lizenz

MIT
