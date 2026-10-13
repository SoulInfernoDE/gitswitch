"""Tiny built-in translation table (English source strings, German catalog)."""

import locale
import os

_DE = {
    "Add account": "Konto hinzufügen",
    "Refresh": "Aktualisieren",
    "Switch": "Wechseln",
    "Active": "Aktiv",
    "Checking token…": "Token wird geprüft …",
    "Token invalid – sign in again": "Token ungültig – bitte neu anmelden",
    "Token could not be checked": "Token konnte nicht geprüft werden",
    "Sign in again": "Neu anmelden",
    "Open profile": "Profil öffnen",
    "Sign out": "Abmelden",
    "Sign out {user}?": "{user} abmelden?",
    "The stored token for this account on {host} will be removed.": (
        "Das gespeicherte Token dieses Kontos auf {host} wird entfernt."
    ),
    "Cancel": "Abbrechen",
    "Switched to {user}": "Zu {user} gewechselt",
    "Signed out {user}": "{user} abgemeldet",
    "Options": "Optionen",
    "Sync Git identity": "Git-Identität mitwechseln",
    "Set global user.name and user.email when switching. Current: {identity}": (
        "Globales user.name und user.email beim Wechsel setzen. Aktuell: {identity}"
    ),
    "not set": "nicht gesetzt",
    "Git identity set to {identity}": "Git-Identität auf {identity} gesetzt",
    "Claude Code integration": "Claude-Code-Integration",
    "Register the MCP server so Claude Code can switch accounts": (
        "MCP-Server registrieren, damit Claude Code das Konto wechseln kann"
    ),
    "Claude Code was not found – copy the setup command instead": (
        "Claude Code wurde nicht gefunden – stattdessen Einrichtungsbefehl kopieren"
    ),
    "Copy command": "Befehl kopieren",
    "Copied to clipboard": "In die Zwischenablage kopiert",
    "MCP server registered in Claude Code": "MCP-Server in Claude Code registriert",
    "MCP server removed from Claude Code": "MCP-Server aus Claude Code entfernt",
    "Sound effects": "Soundeffekte",
    "About": "Info",
    "Switch between GitHub CLI accounts": "Zwischen GitHub-CLI-Konten wechseln",
    "No accounts yet": "Noch keine Konten",
    "Sign in to GitHub to get started.": "Melde dich bei GitHub an, um loszulegen.",
    "GitHub CLI not found": "GitHub CLI nicht gefunden",
    "Install gh from cli.github.com and start Git-Switch again.": (
        "Installiere gh von cli.github.com und starte Git-Switch erneut."
    ),
    "Host": "Host",
    "Sign in": "Anmelden",
    "Sign in to {host}": "Bei {host} anmelden",
    "Starting sign-in…": "Anmeldung wird gestartet …",
    "Enter this code in the browser window that just opened:": (
        "Gib diesen Code im soeben geöffneten Browserfenster ein:"
    ),
    "Copy code": "Code kopieren",
    "Signed in to {host}": "Bei {host} angemeldet",
    "Sign-in failed": "Anmeldung fehlgeschlagen",
}


def _language() -> str:
    for variable in ("LANGUAGE", "LC_ALL", "LC_MESSAGES", "LANG"):
        if os.environ.get(variable):
            return os.environ[variable]
    return locale.getlocale()[0] or ""


_CATALOG = _DE if _language().lower().startswith("de") else {}


def _(text: str) -> str:
    return _CATALOG.get(text, text)
