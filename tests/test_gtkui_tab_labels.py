import unittest

from ssh_manager.gtkui.tab_labels import (
    SessionLabelInfo,
    TabLabelMode,
    address_text,
    base_label,
    session_tab_labels,
)


def info(key, alias="apzkrfpspst0103.fns", hostname="10.60.110.98", port=22, custom=""):
    return SessionLabelInfo(key=key, alias=alias, hostname=hostname, port=port, custom_name=custom)


class BaseLabelTests(unittest.TestCase):
    def test_modes(self) -> None:
        session = info("a")
        self.assertEqual(base_label(session, "name"), "apzkrfpspst0103.fns")
        self.assertEqual(base_label(session, "ip"), "10.60.110.98")
        self.assertEqual(base_label(session, "name_ip"), "apzkrfpspst0103.fns · 10.60.110.98")

    def test_port_other_than_22(self) -> None:
        session = info("a", port=2222)
        self.assertEqual(base_label(session, "ip"), "10.60.110.98:2222")
        self.assertEqual(base_label(session, "name_ip"), "apzkrfpspst0103.fns · 10.60.110.98:2222")
        self.assertEqual(base_label(session, "name"), "apzkrfpspst0103.fns")

    def test_alias_missing_falls_back_to_address(self) -> None:
        session = info("a", alias="", hostname="db.internal", port=2200)
        for mode in TabLabelMode:
            with self.subTest(mode=mode):
                self.assertEqual(base_label(session, mode), "db.internal:2200")

    def test_alias_equal_to_hostname_is_not_repeated(self) -> None:
        session = info("a", alias="10.60.110.74", hostname="10.60.110.74")
        self.assertEqual(base_label(session, "name_ip"), "10.60.110.74")

    def test_ipv6_literal(self) -> None:
        self.assertEqual(address_text("fe80::1", 2222), "[fe80::1]:2222")
        self.assertEqual(address_text("fe80::1", 22), "fe80::1")
        self.assertEqual(base_label(info("a", alias="v6", hostname="2001:db8::7", port=2022), "ip"), "[2001:db8::7]:2022")

    def test_unknown_mode_is_name(self) -> None:
        self.assertIs(TabLabelMode.coerce("bogus"), TabLabelMode.NAME)
        self.assertIs(TabLabelMode.coerce(None), TabLabelMode.NAME)
        self.assertIs(TabLabelMode.coerce("IP"), TabLabelMode.IP)

    def test_very_long_names_are_kept_whole(self) -> None:
        long_alias = "a" * 300 + ".example.internal"
        self.assertEqual(base_label(info("a", alias=long_alias), "name"), long_alias)  # the tab ellipsizes


class DisambiguationTests(unittest.TestCase):
    def test_duplicates_get_counters(self) -> None:
        labels, counters = session_tab_labels([info("a"), info("b"), info("c")])
        self.assertEqual(labels, ["apzkrfpspst0103.fns", "apzkrfpspst0103.fns (2)", "apzkrfpspst0103.fns (3)"])
        self.assertEqual(counters, {"a": 1, "b": 2, "c": 3})

    def test_distinct_hosts_need_no_counter(self) -> None:
        labels, _ = session_tab_labels([info("a"), info("b", alias="web-01", hostname="10.0.0.1")])
        self.assertEqual(labels, ["apzkrfpspst0103.fns", "web-01"])

    def test_closing_the_first_keeps_the_second_stable(self) -> None:
        _labels, counters = session_tab_labels([info("a"), info("b")])
        labels, counters = session_tab_labels([info("b")], assigned=counters)
        self.assertEqual(labels, ["apzkrfpspst0103.fns (2)"])

    def test_freed_counters_are_reused(self) -> None:
        _labels, counters = session_tab_labels([info("a"), info("b"), info("c")])
        _labels, counters = session_tab_labels([info("a"), info("c")], assigned=counters)  # close (2)
        labels, counters = session_tab_labels([info("a"), info("c"), info("d")], assigned=counters)
        self.assertEqual(labels, ["apzkrfpspst0103.fns", "apzkrfpspst0103.fns (3)", "apzkrfpspst0103.fns (2)"])

    def test_the_first_tab_label_never_changes(self) -> None:
        labels, counters = session_tab_labels([info("a")])
        self.assertEqual(labels, ["apzkrfpspst0103.fns"])
        labels, counters = session_tab_labels([info("a"), info("b")], assigned=counters)
        self.assertEqual(labels[0], "apzkrfpspst0103.fns")

    def test_counters_are_per_label_and_mode(self) -> None:
        sessions = [info("a"), info("b", port=2222), info("c")]
        self.assertEqual(session_tab_labels(sessions, "name")[0],
                         ["apzkrfpspst0103.fns", "apzkrfpspst0103.fns (2)", "apzkrfpspst0103.fns (3)"])
        self.assertEqual(session_tab_labels(sessions, "ip")[0],
                         ["10.60.110.98", "10.60.110.98:2222", "10.60.110.98 (2)"])

    def test_renamed_tab_overrides_and_takes_no_counter(self) -> None:
        labels, counters = session_tab_labels([info("a"), info("b", custom="prod shell"), info("c")])
        self.assertEqual(labels, ["apzkrfpspst0103.fns", "prod shell", "apzkrfpspst0103.fns (2)"])
        self.assertNotIn("b", counters)


if __name__ == "__main__":
    unittest.main()


class TabLabelSettingTests(unittest.TestCase):
    def test_ui_state_round_trip_and_old_keys(self) -> None:
        from ssh_manager.gtkui.controllers.ui_state import UiState

        self.assertEqual(UiState().tab_label_mode, "name")
        self.assertEqual(UiState.from_dict({"tab_label_mode": "name_ip"}).tab_label_mode, "name_ip")
        for bad in ("bogus", 7, None, ["ip"]):
            with self.subTest(value=bad):
                self.assertEqual(UiState.from_dict({"tab_label_mode": bad}).tab_label_mode, "name")
        # The old active content tab (Terminal/Details/Activity) is ignored, whatever its type.
        for old in (2, "details", None, [1], {"x": 1}):
            with self.subTest(page_tab=old):
                state = UiState.from_dict({"page_tab": old, "active_tab": old})
                self.assertNotIn("page_tab", state.to_dict())
        self.assertEqual(UiState.from_dict(UiState(tab_label_mode="ip").to_dict()).tab_label_mode, "ip")


class PaneLabelTests(unittest.TestCase):
    def test_pane_suffixes_only_when_split(self) -> None:
        from ssh_manager.gtkui.tab_labels import pane_labels

        self.assertEqual(pane_labels("web-01", 1), ["web-01"])
        self.assertEqual(pane_labels("web-01 (2)", 2), ["web-01 (2) · pane 1", "web-01 (2) · pane 2"])
        self.assertEqual(pane_labels("x", 0), ["x"])
