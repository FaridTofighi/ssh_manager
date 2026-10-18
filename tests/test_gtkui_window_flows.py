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


@unittest.skipUnless(HAS_DISPLAY, "GTK display not available")
class RelayTreeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.sandbox = WindowSandbox()
        self.win = self.sandbox.window

    def tearDown(self) -> None:
        self.sandbox.close()

    def test_row_geometry_matches_reference(self) -> None:
        from ssh_manager.gtkui.theme import density
        from ssh_manager.gtkui.views.session_tree import row_geometry

        with density.using("comfortable"):  # the Relay reference
            self.assertEqual(row_geometry("parent", 0), {"chevron": 26, "icon": 52, "text": 79})
            self.assertEqual(row_geometry("child", 1), {"chevron": 51, "icon": -1, "text": 77})
            self.assertEqual(row_geometry("host", 2), {"chevron": -1, "icon": 81, "text": 108})
        with density.using("compact"):
            self.assertEqual(row_geometry("parent", 0), {"chevron": 12, "icon": 30, "text": 52})
            self.assertEqual(row_geometry("child", 1), {"chevron": 28, "icon": -1, "text": 46})
            self.assertEqual(row_geometry("host", 2), {"chevron": -1, "icon": 46, "text": 66})

    def test_collapse_state_survives_rebuild_and_keyboard_expands(self) -> None:
        from gi.repository import Gdk, Gtk

        win = self.win
        view = win.tree_view
        self.assertFalse(view.get_show_expanders())
        view.set_row_collapsed("parent", "staging", "")
        win.tree.rebuild(refresh=False)
        staging = next(p for p in self._paths() if view.get_model()[p][1] == "staging")
        self.assertFalse(view.row_expanded(staging))

        view.set_cursor(staging, None, False)
        event = Gdk.Event.new(Gdk.EventType.KEY_PRESS)
        event.keyval = Gdk.KEY_Right
        event.state = 0
        self.assertTrue(view._on_key_press(view, event))
        self.assertTrue(view.row_expanded(staging))
        event.keyval = Gdk.KEY_Left
        view._on_key_press(view, event)
        self.assertFalse(view.row_expanded(staging))
        self.assertIsInstance(view.row_renderer, Gtk.CellRendererText)  # accessible text

    def _paths(self):
        model = self.win.tree_view.get_model()
        paths = []
        model.foreach(lambda m, path, it: paths.append(path.copy()) and False)
        return paths


@unittest.skipUnless(HAS_DISPLAY, "GTK display not available")
class HostPageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.sandbox = WindowSandbox()
        self.win = self.sandbox.window
        self.page = self.win.host_page

    def tearDown(self) -> None:
        self.sandbox.close()

    def _pump(self) -> None:
        from gi.repository import Gtk

        for _ in range(50):
            if not Gtk.events_pending():
                break
            Gtk.main_iteration_do(False)

    def test_host_selection_fills_every_band(self) -> None:
        win = self.win
        win.tree.select_host("prod", "web.conf", "web-01")
        content = self.page.view.content
        self.assertEqual(content.kind, "host")
        self.assertEqual([c.label for c in content.crumbs], ["Hosts", "web-01"])
        self.assertEqual(content.primary_label, "_Connect")
        self.assertEqual([caption for caption, _v, _m in content.info], ["Connection", "Auth", "Proxy"])
        self.assertTrue(content.info[0][1].endswith(":22"))
        self.assertEqual([caption for caption, _v, _l in content.stats], ["Last connected", "Latency"])
        self.assertEqual(content.stats[0][1], "Never")
        self.assertTrue(content.copy_text.startswith("ssh "))
        self.assertEqual(self.page.terminal_page.get_visible_child_name(), "ready")
        self.assertEqual(self.page.terminal_page.ready.title.get_text(), "Ready to connect")
        self.assertIn("web-01", self.page.terminal_page.ready.body_text)

    def test_groups_overview_and_breadcrumb_navigation(self) -> None:
        win = self.win
        win.tree.select_child_group("prod", "web.conf")
        content = self.page.view.content
        self.assertEqual(content.kind, "group")
        self.assertEqual(content.primary_label, "Connect _all")
        self.assertEqual([c.label for c in content.crumbs], ["Hosts", "prod", "web.conf"])
        self.page.on_crumb(("parent", "prod"))
        self.assertEqual(self.page.view.content.title, "prod")
        self.page.on_crumb(())
        self.assertEqual(self.page.view.content.kind, "empty")
        self.assertEqual(self.page.terminal_page.ready.title.get_text(), "Select a host")

    def test_details_panel_shortcuts_and_focus_mode(self) -> None:
        from gi.repository import Gdk

        from ssh_manager.gtkui.views.host_page import PanelPage

        win = self.win
        view = self.page.view
        win.tree.select_host("prod", "web.conf", "web-01")
        self.assertFalse(view.panel.is_open)
        win.lookup_action("toggle-details").activate(None)  # Ctrl+I / F9
        self.assertTrue(view.panel.is_open)
        self.assertIs(view.panel.page, PanelPage.DETAILS)
        self.assertEqual(view.panel.title.get_text(), "web-01")
        # The panel is an overlay: the session area keeps its place and size request.
        self.assertIs(view.panel.get_parent(), view.overlay)
        self.assertIs(view.terminal_page.get_parent(), view.overlay)
        event = Gdk.Event.new(Gdk.EventType.KEY_PRESS)
        event.keyval = Gdk.KEY_Escape
        event.state = 0
        self.assertTrue(win._on_window_key_press(win, event))  # Esc closes it
        self.assertFalse(view.panel.is_open)
        win.lookup_action("host-activity").activate(None)
        self.assertTrue(view.panel.is_open)
        self.assertIs(view.panel.page, PanelPage.ACTIVITY)
        view.page_bar.info.clicked()  # ⓘ toggles (here: back to Details)
        self.assertIs(view.panel.page, PanelPage.DETAILS)
        view.page_bar.info.clicked()
        self.assertFalse(view.panel.is_open)

        win.layout.toggle_maximized()  # focus mode
        self.assertFalse(view.header.get_visible())
        self.assertFalse(view.info_bar.get_visible())
        self.assertFalse(win._header.get_visible())
        win.layout.toggle_maximized()
        self.assertTrue(view.header.get_visible())
        self.assertTrue(view.info_bar.get_visible())
        self.assertTrue(win._header.get_visible())
        self.assertIsNone(win.lookup_action("page-tab"))  # Ctrl+1/2/3 are gone

    def test_no_content_tabs_in_the_workspace(self) -> None:
        from gi.repository import Gtk

        def labels(widget):
            found = []
            if isinstance(widget, Gtk.Label):
                found.append(widget.get_text())
            if isinstance(widget, Gtk.Button) and widget.get_label():
                found.append(widget.get_label())
            if isinstance(widget, Gtk.Container):
                children = []
                widget.forall(children.append)
                for child in children:
                    found += labels(child)
            return found

        win = self.win
        win.tree.select_host("prod", "web.conf", "web-01")
        with mock.patch("ssh_manager.gtk_terminal.TerminalTab._spawn"):
            win.sessions.connect_selected()
        view = self.page.view
        workspace = labels(win.terminal_tabs._strip) + labels(view.page_bar) + labels(view.header)
        for name in ("Terminal", "Details", "Activity"):
            self.assertNotIn(name, workspace)

    def test_duplicate_sessions_get_distinct_stable_labels_and_tooltips(self) -> None:
        win = self.win
        notebook = win.terminal_tabs
        win.tree.select_host("prod", "web.conf", "web-01")
        with mock.patch("ssh_manager.gtk_terminal.TerminalTab._spawn"):
            win.sessions.connect_selected()
            first = notebook.current_tab()
            win.sessions.duplicate_current_session()
            second = notebook.current_tab()
        self.assertIsNot(first, second)
        self.assertEqual(notebook.labels(), ["web-01", "web-01 (2)"])
        strip_titles = [tab.title for tab in notebook._strip.tabs()]
        self.assertEqual(strip_titles, ["web-01", "web-01 (2)"])
        tooltip = notebook._tab_labels[second].tooltip()
        self.assertIn("web-01", tooltip)
        self.assertRegex(tooltip, r"\S+@web-01\.internal:22")
        self.assertIn("Duplicate session", tooltip)
        notebook.close_tab(first)
        self.assertEqual(notebook.labels(), ["web-01 (2)"])  # stays stable

        # Label setting and renamed tabs.
        notebook.set_label_mode("ip")
        self.assertEqual(notebook.labels(), ["web-01.internal (2)"])
        notebook.set_label_mode("name_ip")
        self.assertEqual(notebook.labels(), ["web-01 · web-01.internal (2)"])
        second.title = "my shell"
        notebook._refresh_labels()
        self.assertEqual(notebook.labels(), ["my shell"])

    def test_activity_popover_filters_to_this_host(self) -> None:
        win = self.win
        win.tree.select_host("prod", "web.conf", "web-01")
        key = win.creds.host_key(win.tree.selected_host())
        self.page.history.record_event(key, "error", "Connection failed")
        button = win._header.history_button
        button.filter_host.set_active(True)
        texts = [row.get_children()[0].get_children()[1].get_text() for row in button._list.get_children()]
        self.assertEqual(texts, ["Connection failed"])
        button.filter_all.set_active(True)
        win.tree_view.get_selection().unselect_all()
        button.filter_host.set_active(True)
        self.assertEqual(button._list.get_children(), [])
        self.assertIn("Select a host", button._placeholder_label.get_text())

    def test_strip_reorder_and_many_tabs(self) -> None:
        win = self.win
        notebook = win.terminal_tabs
        win.tree.select_host("prod", "web.conf", "web-01")
        with mock.patch("ssh_manager.gtk_terminal.TerminalTab._spawn"):
            for _ in range(16):
                win.sessions.connect_selected()
        self.assertEqual(len(notebook._strip.tabs()), 16)
        self.assertEqual(notebook.labels()[:3], ["web-01", "web-01 (2)", "web-01 (3)"])
        first = notebook._tab_labels[notebook.tabs()[0]]
        notebook._on_strip_reorder(first, 3)  # drag the first tab onto the fourth
        self.assertEqual(notebook.labels()[:4], ["web-01 (2)", "web-01 (3)", "web-01 (4)", "web-01"])
        self.assertEqual([t.title for t in notebook._strip.tabs()][:4], notebook.labels()[:4])
        menu_items = []
        notebook._tabs_menu_button.set_active(True)
        popup = notebook._tabs_menu_button.get_popup()
        menu_items = [child for child in popup.get_children()]
        notebook._tabs_menu_button.set_active(False)
        self.assertEqual(len(menu_items), 16)  # the all-sessions list

    def test_host_bar_follows_the_active_session(self) -> None:
        win = self.win
        notebook = win.terminal_tabs
        with mock.patch("ssh_manager.gtk_terminal.TerminalTab._spawn"):
            win.tree.select_host("prod", "web.conf", "web-01")
            win.sessions.connect_selected()
            web01 = notebook.current_tab()
            win.tree.select_host("prod", "db/primary.conf", "db-primary")
            win.sessions.connect_selected()
            db = notebook.current_tab()
        self.page.refresh()
        self.assertEqual(self.page.view.content.title, db.host.alias)
        notebook.focus_tab(web01)
        self.page.refresh()
        self.assertEqual(self.page.view.content.title, "web-01")
        self.assertEqual(self.page.view.content.primary_label, "_Reconnect")
        # Selecting another host in the tree does not switch the session in front.
        win.tree.select_host("staging", "hosts.conf", "stage-app")
        self.page.refresh()
        self.assertIs(notebook.current_tab(), web01)
        self.assertEqual(self.page.view.content.title, "web-01")

    def test_status_changes_feed_history_and_sessions_dock_status_bar(self) -> None:
        win = self.win
        win.tree.select_host("prod", "web.conf", "web-01")
        host = win.tree.selected_host()
        key = win.creds.host_key(host)
        win.tree.set_host_status_for_key(key, "connected")
        win.tree.set_host_status_for_key(key, "unknown")
        events = [e.text for e in self.page.history.events(key)]
        self.assertEqual(events, ["Disconnected", "Connected"])
        self.assertNotEqual(self.page.view.content.stats[0][1], "Never")

        tab = win.terminal_tabs.new_terminal(host, autostart=False)
        self._pump()
        self.assertEqual(self.page.terminal_page.get_visible_child_name(), "sessions")
        self.assertIs(tab.statusbar.get_parent(), self.page.view.info_bar.live)
        self.assertTrue(win.terminal_tabs.has_session_for(host))
        self.page.refresh()
        self.assertEqual(self.page.view.content.primary_label, "_Reconnect")
        win.terminal_tabs.close_tab(tab)
        self._pump()
        self.assertEqual(self.page.terminal_page.get_visible_child_name(), "ready")
        self.assertEqual(self.page.view.info_bar.live.get_children(), [])

    def test_latency_uses_cache_and_skips_proxied_hosts(self) -> None:
        win = self.win
        from ssh_manager.config import HostEntry

        proxied = HostEntry(alias="p", options={"hostname": "10.0.0.1", "proxyjump": "bastion"})
        self.assertIsNone(self.page.latency_key(proxied))
        self.page.latency.enabled = False  # no real sockets in tests
        win.tree.select_host("prod", "web.conf", "web-01")
        self.assertEqual(self.page.view.content.stats[1][1], "—")
        self.page.latency.enabled = True
        host = win.tree.selected_host()
        self.page.inject_latency(host, 180.0)
        self.page.refresh()
        self.assertEqual(self.page.view.content.stats[1][1:], ("180 ms", "warn"))


@unittest.skipUnless(HAS_DISPLAY, "GTK display not available")
class WorkspaceStripTests(unittest.TestCase):
    def setUp(self) -> None:
        self.sandbox = WindowSandbox()
        self.win = self.sandbox.window

    def tearDown(self) -> None:
        self.sandbox.close()

    def test_bulk_bar_follows_multi_selection(self) -> None:
        win = self.win
        selection = win.tree_view.get_selection()
        for key in list(win.tree.host_iters)[:2]:
            selection.select_iter(win.tree.host_iters[key])
        self.assertTrue(win._sidebar.bulk_revealer.get_reveal_child())
        self.assertIn("2 hosts selected", win._sidebar.bulk_label.get_text())
        self.assertEqual(win.host_page.view.content.kind, "multi")
        win._sidebar.on_bulk_clear()
        self.assertFalse(win._sidebar.bulk_revealer.get_reveal_child())

    def test_toolbar_moves_between_strip_and_row(self) -> None:
        win = self.win
        notebook = win.terminal_tabs
        hosts = [win.tree.host_lookup[key] for key in list(win.tree.host_lookup)[:2]]
        for host in hosts:
            notebook.new_terminal(host, autostart=False)
        self.assertTrue(notebook.toolbar_in_strip)
        notebook.set_tile_mode(True)
        self.assertFalse(notebook.toolbar_in_strip)
        for tab in notebook.tabs():
            self.assertIsNot(tab.statusbar.get_parent(), win.host_page.view.info_bar.live)
        notebook.set_tile_mode(False)
        self.assertTrue(notebook.toolbar_in_strip)

    def test_latency_preference_round_trip(self) -> None:
        from ssh_manager.gtkui.dialogs.preferences import PreferencesDialog

        win = self.win
        dialog = PreferencesDialog(win, win._theme_mode, 1.0, 11, win._settings, latency_probe=False)
        values = dialog.values()
        dialog.destroy()
        self.assertFalse(values.latency_probe)
        win.app_dialogs.apply_preferences(values)
        self.assertFalse(win._ui.latency_probe)
        self.assertFalse(win.host_page.latency.enabled)
        self.assertFalse(win._load_ui_state().get("latency_probe", True))
