"""End-to-end flows through the window controllers (dialogs answered by fakes)."""

import unittest
from unittest import mock

from tests.gtk_window_fixture import WindowSandbox, display_available

HAS_DISPLAY = display_available()


@unittest.skipUnless(HAS_DISPLAY, "GTK display not available")
class WindowFlowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.sandbox = WindowSandbox()
        self.win = self.sandbox.window
        self.toasts = []
        self.win._toasts.on_show(lambda message: self.toasts.append((message.level.value, message.text)))

    def tearDown(self) -> None:
        self.sandbox.close()

    def _aliases(self):
        return sorted(self.win.tree.host_lookup.keys())

    def test_tree_selection_details_and_menus_for_each_kind(self) -> None:
        win = self.win
        self.assertEqual(len(win.tree.host_lookup), 4)
        win.tree.select_host("prod", "web.conf", "web-01")
        self.assertEqual(win.tree.selected_host().alias, "web-01")
        content = win.tree.detail_content()
        self.assertEqual(content.title, "web-01")
        self.assertIn("deploy@web-01.internal", content.subtitle)
        host_menu = [item.label for section in win.tree.context_menu_spec() for item in section.items if item.visible]
        self.assertIn("_Connect", host_menu)
        self.assertNotIn("_Reconnect", host_menu)  # no session open yet

        win.tree.select_child_group("prod", "web.conf")
        self.assertIn("2 hosts", win.tree.detail_content().subtitle)
        self.assertTrue(any(i.label == "Connect _All" for s in win.tree.context_menu_spec() for i in s.items))
        win.tree.select_parent_group("prod")
        self.assertIn("3 hosts", win.tree.detail_content().subtitle)
        win.tree_view.get_selection().unselect_all()
        self.assertEqual(win.tree.detail_content().title, "Nothing selected")
        self.assertTrue(any(i.label == "New _Host…" for s in win.tree.context_menu_spec() for i in s.items))

    def test_search_filter_and_empty_state(self) -> None:
        win = self.win
        win._sidebar.search_entry.set_text("primary")
        win.tree.rebuild()
        self.assertEqual(self._aliases(), ["prod::db/primary.conf::db-primary"])
        win._sidebar.search_entry.set_text("nothing-matches")
        win.tree.rebuild()
        self.assertEqual(win._sidebar.stack.get_visible_child_name(), "no-results")
        win._sidebar.clear_all()
        win.tree.on_filter_text_changed("tag:web")
        self.assertEqual(self._aliases(), ["prod::web.conf::web-01"])
        self.assertEqual(win.tree.filter_query, "tag:web")

    def test_open_session_status_reconnect_menu_and_copy_ssh(self) -> None:
        win = self.win
        win.tree.select_host("prod", "web.conf", "web-01")
        with mock.patch("ssh_manager.gtk_terminal.TerminalTab._spawn"):
            win.sessions.connect_selected()
        self.assertEqual(win.terminal_tabs.tab_count(), 1)
        tab = win.terminal_tabs.current_tab()
        tab._set_status("Connected", True)
        self.assertEqual(win.tree.host_status["prod::web.conf::web-01"], "connected")
        menu = [item.label for section in win.tree.context_menu_spec() for item in section.items if item.visible]
        self.assertIn("_Reconnect", menu)
        win.sessions.copy_ssh_command()
        self.assertIn(("success", "SSH command copied to the clipboard."), self.toasts)

    def test_create_rename_delete_group_and_host_flows(self) -> None:
        win = self.win
        with mock.patch("ssh_manager.gtkui.controllers.group_actions.GroupActions.prompt_child_destination"), \
                mock.patch.object(type(win), "_prompt_text", side_effect=["ops"]):
            win.groups.create_parent_group()
        self.assertIn("ops", win.manager.tree)

        win.tree.select_parent_group("ops")
        with mock.patch.object(type(win), "_prompt_text", side_effect=["ops2"]):
            win.groups.rename_parent_group()
        self.assertIn("ops2", win.manager.tree)
        self.assertNotIn("ops", win.manager.tree)

        win.tree.select_parent_group("ops2")
        with mock.patch.object(type(win), "_confirm", return_value=True):
            win.hosts.delete_current_item()
        self.assertNotIn("ops2", win.manager.tree)

    def test_edit_and_delete_host_through_dialog_fakes(self) -> None:
        from ssh_manager.config import HostEntry

        win = self.win
        win.tree.select_host("prod", "web.conf", "web-02")
        edited = HostEntry(alias="web-02", options={"hostname": "web-02.example.com"}, parent_group="prod", child_group="web.conf")
        with mock.patch.object(type(win.hosts), "run_host_dialog", return_value=(edited, None)):
            win.hosts.edit_selected()
        host = win.tree.find_host_by_alias("web-02")
        self.assertEqual(host.option("hostname"), "web-02.example.com")
        win.tree.select_host("prod", "web.conf", "web-02")
        with mock.patch.object(type(win), "_confirm", return_value=True):
            win.hosts.delete_selected()
        self.assertIsNone(win.tree.find_host_by_alias("web-02"))

    def test_drag_and_drop_moves_host(self) -> None:
        win = self.win
        moved = win.dnd.handle_drop(
            {"kind": "host", "items": [{"kind": "host", "parent": "prod", "child": "web.conf", "alias": "web-02"}]},
            {"type": "child", "parent": "staging", "child": "hosts.conf"},
        )
        self.assertTrue(moved)
        self.assertEqual(win.tree.find_host_by_alias("web-02").parent_group, "staging")
        self.assertFalse(win.dnd.handle_drop({"kind": "bogus", "items": [1]}, {"type": "child"}))

    def test_layout_toggles_and_ui_state(self) -> None:
        win = self.win
        win.layout.toggle_sidebar()
        self.assertFalse(win.layout.sidebar_visible)
        self.assertTrue(win._header.sidebar_expand_button.get_visible())
        win.layout.toggle_maximized()
        self.assertTrue(win.layout.maximized)
        win.layout.toggle_maximized()
        win.layout.toggle_sidebar()
        self.assertTrue(win.layout.sidebar_visible)
        win._save_ui_state()
        saved = win._load_ui_state()
        self.assertTrue(saved["sidebar_visible"])
        self.assertIn("details_expanded", saved)

    def test_settings_apply_and_inventory_refresh(self) -> None:
        from ssh_manager.gtkui.dialogs.preferences import PreferencesValues
        from ssh_manager.gtkui.theme.loader import ThemeMode
        from ssh_manager.settings import TerminalAppearanceSettings

        win = self.win
        values = PreferencesValues(
            theme_mode=ThemeMode.LIGHT, terminal_font_scale=1.1, ui_font_size=13, remember_password_default=False,
            host_key_policy="auto-add", connect_timeout=5.0, auth_timeout=6.0, banner_timeout=7.0,
            use_paramiko_vte=False, master_password_disabled=True, allowed_ciphers=[], allowed_kex=[],
            secrets_policy={}, enable_audit_logging=False, audit_log_path="", git_watch_enabled=False,
            git_watch_path="", git_watch_interval=600, terminal_appearance=TerminalAppearanceSettings(),
        )
        win.app_dialogs.apply_preferences(values)
        self.assertEqual(win._settings.host_key_policy, "auto-add")
        self.assertEqual(win._ui.theme_mode, "light")
        self.assertIn(("success", "Settings saved."), self.toasts)
        win.inventory.refresh()
        self.assertEqual(len(win.tree.host_lookup), 4)
        win.inventory.check_git_now()
        self.assertTrue(any("Git update checks are off" in text for _level, text in self.toasts))

    def test_window_fits_minimum_width(self) -> None:
        win = self.win
        with mock.patch("ssh_manager.gtk_terminal.TerminalTab._spawn"):
            win.tree.select_host("prod", "web.conf", "web-01")
            win.sessions.connect_selected()
            win.tree.select_host("prod", "web.conf", "web-02")
            win.sessions.connect_selected()
        minimum = win.get_child().get_preferred_width()[0]
        self.assertLessEqual(minimum, 960, "content must fit the 960 px minimum window width")

    def test_quick_connect_opens_adhoc_session(self) -> None:
        win = self.win
        with mock.patch("ssh_manager.gtkui.dialogs.prompt.PromptDialog.ask", return_value="root@10.1.2.3:2200"), \
                mock.patch("ssh_manager.gtk_terminal.TerminalTab._spawn"):
            win.sessions.quick_connect()
        tab = win.terminal_tabs.current_tab()
        self.assertEqual(tab.host.option("hostname"), "10.1.2.3")
        self.assertEqual(tab.host.option("port"), "2200")
        self.assertEqual(tab.host.option("user"), "root")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()


@unittest.skipUnless(HAS_DISPLAY, "GTK display not available")
class TopBarTests(unittest.TestCase):
    def setUp(self) -> None:
        self.sandbox = WindowSandbox()
        self.win = self.sandbox.window

    def tearDown(self) -> None:
        self.sandbox.close()

    def test_health_follows_unread_problems(self) -> None:
        from ssh_manager.gtkui.theme.tokens import Level

        win = self.win
        self.assertEqual(win._header.health_text, "All systems operational")
        win._notify("Authentication failed", Level.ERROR)
        self.assertEqual(win._header.health_text, "1 error")
        win._toasts.history.mark_all_read()
        self.assertEqual(win._header.health_text, "All systems operational")

    def test_workspace_switcher_and_sidebar_affordances(self) -> None:
        from pathlib import Path

        win = self.win
        self.assertEqual(win._header.workspace_name, Path(win.manager.base_dir).name)
        other = Path(self.sandbox.base).parent / "Lab"
        other.mkdir(exist_ok=True)
        win.inventory.load_workspace(other)
        self.assertEqual(win._header.workspace_name, "Lab")
        self.assertTrue(win._ui.recent_inventories)
        for name in ("open-folder", "documentation", "quit", "focus-search"):
            self.assertIsNotNone(win.lookup_action(name), name)
        self.assertIsNotNone(win._sidebar.add_menu)
        win.layout.toggle_sidebar()
        self.assertTrue(win._header.sidebar_expand_button.get_visible())
        win.layout.toggle_sidebar()
        self.assertFalse(win._header.sidebar_expand_button.get_visible())
