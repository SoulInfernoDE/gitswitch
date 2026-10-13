"""GTK 4 / libadwaita window for switching GitHub CLI accounts."""

from __future__ import annotations

import shlex
import sys
import threading
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk  # noqa: E402

from . import __version__, core, sound  # noqa: E402
from .i18n import _  # noqa: E402

APP_ID = ICON_NAME = "dev.gitswitch.GitSwitch"


def run_async(func, callback) -> None:
    """Run ``func`` in a thread and deliver ``(result, error)`` on the main loop."""

    def worker():
        try:
            result, error = func(), None
        except Exception as exc:  # shown to the user by the callback
            result, error = None, exc
        GLib.idle_add(lambda: callback(result, error) and False)

    threading.Thread(target=worker, daemon=True).start()


class Window(Adw.ApplicationWindow):
    def __init__(self, app: Adw.Application):
        super().__init__(application=app, title="Git-Switch", default_width=460, default_height=800)
        self.settings = core.load_settings()
        self.profiles: dict[core.Account, core.Profile] = {}
        self.avatars: dict[str, Gdk.Texture] = {}
        self.host_groups: list[Adw.PreferencesGroup] = []
        self.generation = 0
        self.reload_source = 0
        self.busy_toggle = False

        header = Adw.HeaderBar()
        add_button = Gtk.Button(icon_name="list-add-symbolic", tooltip_text=_("Add account"))
        add_button.connect("clicked", lambda *_args: self.ask_host())
        header.pack_start(add_button)
        refresh_button = Gtk.Button(icon_name="view-refresh-symbolic", tooltip_text=_("Refresh"))
        refresh_button.connect("clicked", lambda *_args: self.reload(recheck=True))
        about_button = Gtk.Button(icon_name="help-about-symbolic", tooltip_text=_("About"))
        about_button.connect("clicked", lambda *_args: self.show_about())
        header.pack_end(about_button)
        header.pack_end(refresh_button)

        self.page = Adw.PreferencesPage()
        logo = Adw.PreferencesGroup()
        logo.add(Gtk.Image(icon_name=ICON_NAME, pixel_size=128, halign=Gtk.Align.CENTER))
        self.page.add(logo)
        self.status = Adw.StatusPage(icon_name=ICON_NAME)
        self.stack = Gtk.Stack()
        self.stack.add_named(self.page, "accounts")
        self.stack.add_named(self.status, "status")
        self.toasts = Adw.ToastOverlay(child=self.stack)

        view = Adw.ToolbarView(content=self.toasts)
        view.add_top_bar(header)
        self.set_content(view)

        self.build_options()
        for name, handler, parameter in (
            ("login", self.on_login_action, "s"),
            ("logout", self.on_logout_action, "(ss)"),
            ("profile", self.on_profile_action, "(ss)"),
            ("copy-token", self.on_copy_token_action, "(ss)"),
            ("scopes", self.on_scopes_action, "s"),
        ):
            action = Gio.SimpleAction.new(name, GLib.VariantType.new(parameter))
            action.connect("activate", handler)
            self.add_action(action)

        # Follow changes made elsewhere: the gh CLI itself or the MCP server.
        self.monitor = Gio.File.new_for_path(str(core.hosts_file())).monitor_file(
            Gio.FileMonitorFlags.WATCH_MOVES, None
        )
        self.monitor.connect("changed", self.on_hosts_changed)
        self.reload()

    # --- helpers -----------------------------------------------------------

    def toast(self, text: str, sound_name: str | None = None) -> None:
        self.toasts.add_toast(Adw.Toast(title=text, use_markup=False))
        if sound_name and self.settings["sounds"]:
            sound.play(sound_name)

    def fail(self, error) -> None:
        self.toast(str(error), "error")

    def copy(self, text: str) -> None:
        self.get_clipboard().set(text)
        self.toast(_("Copied to clipboard"))

    def show_about(self) -> None:
        Adw.AboutWindow(
            transient_for=self,
            application_name="Git-Switch",
            application_icon=ICON_NAME,
            version=__version__,
            comments=_("Switch between GitHub CLI accounts"),
            website="https://github.com/SoulInfernoDE/gitswitch",
            issue_url="https://github.com/SoulInfernoDE/gitswitch/issues",
            license_type=Gtk.License.MIT_X11,
        ).present()

    def on_hosts_changed(self, *_args) -> None:
        if self.reload_source:
            GLib.source_remove(self.reload_source)
        self.reload_source = GLib.timeout_add(300, self.on_reload_timeout)

    def on_reload_timeout(self) -> bool:
        self.reload_source = 0
        self.reload()
        self.update_identity_row()
        return GLib.SOURCE_REMOVE

    # --- account list ------------------------------------------------------

    def reload(self, recheck: bool = False) -> None:
        self.generation += 1
        if recheck:
            self.profiles.clear()
        for group in self.host_groups:
            self.page.remove(group)
        self.host_groups.clear()

        if not core.gh_available():
            self.status.set_title(_("GitHub CLI not found"))
            self.status.set_description(_("Install gh from cli.github.com and start Git-Switch again."))
            self.stack.set_visible_child_name("status")
            return
        accounts = core.list_accounts()
        if not accounts:
            self.status.set_title(_("No accounts yet"))
            self.status.set_description(_("Sign in to GitHub to get started."))
            button = Gtk.Button(label=_("Add account"), halign=Gtk.Align.CENTER)
            button.add_css_class("pill")
            button.add_css_class("suggested-action")
            button.connect("clicked", lambda *_args: self.ask_host())
            self.status.set_child(button)
            self.stack.set_visible_child_name("status")
            return
        self.stack.set_visible_child_name("accounts")

        groups: dict[str, Adw.PreferencesGroup] = {}
        for account in accounts:
            if account.host not in groups:
                group = groups[account.host] = self.build_host_group(account.host)
                self.host_groups.append(group)
            groups[account.host].add(self.build_row(account))
        # Keep the options group below the accounts.
        self.page.remove(self.options)
        for group in self.host_groups:
            self.page.add(group)
        self.page.add(self.options)

    def build_host_group(self, host: str) -> Adw.PreferencesGroup:
        """Group for one host; its menu holds the per-host git settings of gh."""
        group = Adw.PreferencesGroup(title=host)
        actions = Gio.SimpleActionGroup()
        protocol = Gio.SimpleAction.new_stateful(
            "protocol", GLib.VariantType.new("s"), GLib.Variant("s", core.git_protocol(host) or "https")
        )

        def on_protocol(action, value):
            def done(_result, error):
                if error:
                    self.fail(error)
                    return
                action.set_state(value)
                self.toast(_("Git protocol for {host}: {protocol}").format(host=host, protocol=value.unpack().upper()))

            run_async(lambda: core.set_git_protocol(host, value.unpack()), done)

        def on_setup_git(*_args):
            def done(_result, error):
                if error:
                    self.fail(error)
                else:
                    self.toast(_("Git now uses gh to authenticate with {host}").format(host=host))

            run_async(lambda: core.setup_git(host), done)

        protocol.connect("activate", on_protocol)
        setup = Gio.SimpleAction.new("setup-git", None)
        setup.connect("activate", on_setup_git)
        actions.add_action(protocol)
        actions.add_action(setup)
        group.insert_action_group("host", actions)

        protocols = Gio.Menu()
        protocols.append("HTTPS", "host.protocol::https")
        protocols.append("SSH", "host.protocol::ssh")
        menu = Gio.Menu()
        menu.append_section(_("Git protocol"), protocols)
        menu.append(_("Use gh as Git credential helper"), "host.setup-git")
        button = Gtk.MenuButton(
            icon_name="emblem-system-symbolic", menu_model=menu, valign=Gtk.Align.CENTER, tooltip_text=_("Git settings")
        )
        button.add_css_class("flat")
        group.set_header_suffix(button)
        return group

    def build_row(self, account: core.Account) -> Adw.ActionRow:
        row = Adw.ActionRow(title=account.user, use_markup=False)
        avatar = Adw.Avatar(size=40, text=account.user, show_initials=True)
        row.add_prefix(avatar)

        if account.active:
            check = Gtk.Image(icon_name="object-select-symbolic")
            check.add_css_class("accent")
            row.add_suffix(check)
        else:
            button = Gtk.Button(label=_("Switch"), valign=Gtk.Align.CENTER)
            button.connect("clicked", lambda *_args: self.on_row_button(account))
            row.add_suffix(button)
            row.set_activatable_widget(button)
            row.button = button

        target = GLib.Variant("(ss)", (account.host, account.user))
        menu = Gio.Menu()
        items = [
            (_("Open profile"), "win.profile", target),
            (_("Copy token"), "win.copy-token", target),
            (_("Sign in again"), "win.login", GLib.Variant("s", account.host)),
            (_("Sign out"), "win.logout", target),
        ]
        if account.active:
            # gh can only refresh the token of the active account.
            items.insert(2, (_("Change permissions…"), "win.scopes", GLib.Variant("s", account.host)))
        for label, action, value in items:
            item = Gio.MenuItem.new(label, None)
            item.set_action_and_target_value(action, value)
            menu.append_item(item)
        more = Gtk.MenuButton(icon_name="view-more-symbolic", menu_model=menu, valign=Gtk.Align.CENTER)
        more.add_css_class("flat")
        row.add_suffix(more)

        generation = self.generation

        def show(profile, error=None):
            if generation != self.generation:
                return
            if error or profile is None:
                profile = core.Profile(valid=None, login=account.user)
            self.profiles[account] = profile
            self.show_profile(row, avatar, account, profile)

        if account in self.profiles:
            show(self.profiles[account])
        else:
            row.set_subtitle(_("Checking token…"))
            run_async(lambda: core.fetch_profile(account), show)
        return row

    def show_profile(self, row, avatar, account: core.Account, profile: core.Profile) -> None:
        if profile.valid is False:
            row.set_subtitle(_("Token invalid – sign in again"))
            if not account.active:
                row.button.set_label(_("Sign in again"))
            return
        if profile.valid is None:
            row.set_subtitle(_("Token could not be checked"))
            return
        parts = [profile.name] if profile.name else []
        if account.active:
            parts.append(_("Active"))
        lines = [" · ".join(parts)] if parts else []
        if profile.scopes:
            lines.append(_("Permissions: {scopes}").format(scopes=", ".join(profile.scopes)))
        row.set_subtitle("\n".join(lines))

        url = profile.avatar_url
        if not url:
            return
        if url in self.avatars:
            avatar.set_custom_image(self.avatars[url])
            return

        def loaded(data, error):
            if error or not data:
                return
            try:
                self.avatars[url] = Gdk.Texture.new_from_bytes(GLib.Bytes.new(data))
            except GLib.Error:
                return
            avatar.set_custom_image(self.avatars[url])

        run_async(lambda: core.fetch_avatar(url), loaded)

    # --- actions -----------------------------------------------------------

    def on_row_button(self, account: core.Account) -> None:
        profile = self.profiles.get(account)
        if profile is not None and profile.valid is False:
            self.login(account.host)
        else:
            self.switch(account)

    def switch(self, account: core.Account) -> None:
        def done(result, error):
            if error:
                self.fail(error)
                return
            self.toast(result.get("warning") or _("Switched to {user}").format(user=account.user), "switch")
            self.reload()
            self.update_identity_row()

        run_async(lambda: core.switch_account(account.user, account.host, self.settings["sync_git_identity"]), done)

    def on_profile_action(self, _action, parameter) -> None:
        host, user = parameter.unpack()
        Gio.AppInfo.launch_default_for_uri(f"https://{host}/{user}", None)

    def on_copy_token_action(self, _action, parameter) -> None:
        host, user = parameter.unpack()

        def done(token, error):
            if error:
                self.fail(error)
            else:
                self.copy(token)

        run_async(lambda: core.get_token(core.Account(host, user, False)), done)

    def on_scopes_action(self, _action, parameter) -> None:
        host = parameter.unpack()
        entry = Adw.EntryRow(title=_("Additional permissions, e.g. workflow, delete_repo"), activates_default=True)
        box = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        box.add_css_class("boxed-list")
        box.append(entry)
        dialog = Adw.MessageDialog(
            transient_for=self,
            heading=_("Change permissions"),
            body=_("Renews the token in the browser. Leave the field empty to keep the current permissions."),
            extra_child=box,
        )
        dialog.add_response("cancel", _("Cancel"))
        dialog.add_response("refresh", _("Continue"))
        dialog.set_response_appearance("refresh", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("refresh")

        def on_response(_dialog, response):
            if response == "refresh":
                scopes = ",".join(scope.strip() for scope in entry.get_text().split(",") if scope.strip())
                self.login(host, refresh=True, scopes=scopes)

        dialog.connect("response", on_response)
        dialog.present()

    def on_login_action(self, _action, parameter) -> None:
        self.login(parameter.unpack())

    def on_logout_action(self, _action, parameter) -> None:
        host, user = parameter.unpack()
        account = next((a for a in core.list_accounts() if (a.host, a.user) == (host, user)), None)
        if account is None:
            return
        dialog = Adw.MessageDialog(
            transient_for=self,
            heading=_("Sign out {user}?").format(user=user),
            body=_("The stored token for this account on {host} will be removed.").format(host=host),
        )
        dialog.add_response("cancel", _("Cancel"))
        dialog.add_response("logout", _("Sign out"))
        dialog.set_response_appearance("logout", Adw.ResponseAppearance.DESTRUCTIVE)

        def done(_result, error):
            if error:
                self.fail(error)
            else:
                self.toast(_("Signed out {user}").format(user=user), "signout")
            self.reload(recheck=True)

        def on_response(_dialog, response):
            if response == "logout":
                run_async(lambda: core.logout_account(account), done)

        dialog.connect("response", on_response)
        dialog.present()

    # --- sign-in -----------------------------------------------------------

    def ask_host(self) -> None:
        entry = Adw.EntryRow(title=_("Host"), text=core.DEFAULT_HOST, activates_default=True)
        box = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        box.add_css_class("boxed-list")
        box.append(entry)
        dialog = Adw.MessageDialog(transient_for=self, heading=_("Add account"), extra_child=box)
        dialog.add_response("cancel", _("Cancel"))
        dialog.add_response("login", _("Sign in"))
        dialog.set_response_appearance("login", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("login")

        def on_response(_dialog, response):
            host = entry.get_text().strip()
            if response == "login" and host:
                self.login(host)

        dialog.connect("response", on_response)
        dialog.present()

    def login(self, host: str, refresh: bool = False, scopes: str = "") -> None:
        code_label = Gtk.Label(selectable=True)
        code_label.add_css_class("title-1")
        copy_button = Gtk.Button(label=_("Copy code"), halign=Gtk.Align.CENTER, sensitive=False)
        copy_button.connect("clicked", lambda *_args: self.copy(code_label.get_text()))
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        box.append(code_label)
        box.append(copy_button)
        dialog = Adw.MessageDialog(
            transient_for=self,
            heading=(_("Change permissions") if refresh else _("Sign in to {host}").format(host=host)),
            body=_("Starting sign-in…"),
            extra_child=box,
        )
        dialog.add_response("cancel", _("Cancel"))
        state = {"open": True}

        def on_code(code):
            code_label.set_text(code)
            copy_button.set_sensitive(True)
            dialog.set_body(_("Enter this code in the browser window that just opened:"))

        def on_done(status, output):
            if not state["open"]:
                return
            state["open"] = False
            dialog.close()
            if status == 0:
                done_text = _("Token renewed for {host}") if refresh else _("Signed in to {host}")
                self.toast(done_text.format(host=host), "success")
            else:
                last_line = output.splitlines()[-1] if output else ""
                failed = _("Sign-in failed")
                text = _("{label}: {detail}").format(label=failed, detail=last_line) if last_line else failed
                self.toast(text, "error")
            self.reload(recheck=True)

        flow = core.LoginFlow(
            host,
            lambda code: GLib.idle_add(lambda: on_code(code) and False),
            lambda status, output: GLib.idle_add(lambda: on_done(status, output) and False),
            refresh=refresh,
            scopes=scopes,
        )

        def on_response(_dialog, _response):
            if state["open"]:
                state["open"] = False
                flow.cancel()

        dialog.connect("response", on_response)
        try:
            flow.start()
        except core.GhError as error:
            self.fail(error)
            return
        dialog.present()

    # --- options -----------------------------------------------------------

    def build_options(self) -> None:
        self.options = Adw.PreferencesGroup(title=_("Options"))
        self.identity_row = Adw.SwitchRow(
            title=_("Sync Git identity"), active=bool(self.settings["sync_git_identity"]), use_markup=False
        )
        self.identity_row.connect("notify::active", self.on_identity_toggled)
        self.options.add(self.identity_row)
        self.update_identity_row()

        if core.mcp_available():
            self.mcp_row = Adw.SwitchRow(
                title=_("Claude Code integration"),
                subtitle=_("Register the MCP server so Claude Code can switch accounts"),
                active=core.mcp_registered(),
            )
            self.mcp_row.connect("notify::active", self.on_mcp_toggled)
        else:
            self.mcp_row = Adw.ActionRow(
                title=_("Claude Code integration"),
                subtitle=_("Claude Code was not found – copy the setup command instead"),
            )
            button = Gtk.Button(label=_("Copy command"), valign=Gtk.Align.CENTER)
            command = shlex.join(["claude", "mcp", "add", "--scope", "user", core.MCP_NAME, "--", *core.mcp_command()])
            button.connect("clicked", lambda *_args: self.copy(command))
            self.mcp_row.add_suffix(button)
        self.options.add(self.mcp_row)

        sound_row = Adw.SwitchRow(title=_("Sound effects"), active=bool(self.settings["sounds"]))
        sound_row.connect("notify::active", self.on_sounds_toggled)
        self.options.add(sound_row)
        self.page.add(self.options)

    def update_identity_row(self) -> None:
        name, email = core.get_git_identity()
        identity = f"{name} <{email}>" if name or email else _("not set")
        self.identity_row.set_subtitle(
            _("Set global user.name and user.email when switching. Current: {identity}").format(identity=identity)
        )

    def on_identity_toggled(self, row, _pspec) -> None:
        self.settings["sync_git_identity"] = row.get_active()
        core.save_settings(self.settings)
        if not row.get_active():
            return
        # Apply right away so the identity matches the account that is active now.
        accounts = [account for account in core.list_accounts() if account.active]
        accounts.sort(key=lambda account: account.host != core.DEFAULT_HOST)
        if not accounts:
            return

        def done(identity, error):
            if error:
                self.fail(error)
                return
            self.toast(_("Git identity set to {identity}").format(identity=f"{identity['name']} <{identity['email']}>"))
            self.update_identity_row()

        run_async(lambda: core.sync_git_identity(accounts[0], self.profiles.get(accounts[0])), done)

    def on_sounds_toggled(self, row, _pspec) -> None:
        self.settings["sounds"] = row.get_active()
        core.save_settings(self.settings)
        if row.get_active():
            sound.play("switch")

    def set_mcp_switch(self, active: bool) -> None:
        self.busy_toggle = True
        self.mcp_row.set_active(active)
        self.busy_toggle = False

    def on_mcp_toggled(self, row, _pspec) -> None:
        if self.busy_toggle:
            return
        register = row.get_active()
        row.set_sensitive(False)

        def done(_result, error):
            row.set_sensitive(True)
            if error:
                self.set_mcp_switch(not register)
                self.fail(error)
            elif register:
                self.toast(_("MCP server registered in Claude Code"))
            else:
                self.toast(_("MCP server removed from Claude Code"))

        run_async(core.mcp_register if register else core.mcp_unregister, done)


def _on_startup(_app) -> None:
    # The bundled copy makes the logo available even if no icon theme has it.
    icons = Path(__file__).resolve().parent / "icons"
    Gtk.IconTheme.get_for_display(Gdk.Display.get_default()).add_search_path(str(icons))
    Gtk.Window.set_default_icon_name(ICON_NAME)


def main() -> int:
    app = Adw.Application(application_id=APP_ID, flags=Gio.ApplicationFlags.DEFAULT_FLAGS)
    app.connect("startup", _on_startup)
    app.connect("activate", lambda app: (app.get_active_window() or Window(app)).present())
    return app.run(sys.argv[:1])
