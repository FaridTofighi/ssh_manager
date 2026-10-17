import unittest
from pathlib import Path

from ssh_manager.config import GroupConfig, HostEntry
from ssh_manager.gtkui.constants import RowKind
from ssh_manager.gtkui.controllers.tree_controller import (
    TreeCallbacks,
    build_tree_rows,
    filter_host,
    parse_filter,
    search_text_for_host,
    sidebar_page_for,
)


def host(alias, parent, child, **options):
    meta = options.pop("meta", {})
    return HostEntry(alias=alias, options=options, metadata=meta, parent_group=parent, child_group=child)


def inventory():
    return {
        "prod": {
            "web.conf": GroupConfig("prod", "web.conf", Path("prod/web.conf"), [
                host("web-02", "prod", "web.conf", hostname="web-02.internal", tags="prod,web"),
                host("web-01", "prod", "web.conf", hostname="web-01.internal", user="deploy", tags="prod,web",
                     meta={"owner": "alice"}),
            ]),
            "db/primary.conf": GroupConfig("prod", "db/primary.conf", Path("prod/db/primary.conf"), [
                host("db-primary", "prod", "db/primary.conf", hostname="10.0.4.12", tags="prod,db"),
            ]),
        },
        "staging": {
            "hosts.conf": GroupConfig("staging", "hosts.conf", Path("staging/hosts.conf"), [
                host("stage-app", "staging", "hosts.conf", hostname="stage.internal"),
            ]),
            "empty.conf": GroupConfig("staging", "empty.conf", Path("staging/empty.conf"), []),
        },
        "empty-parent": {},
    }


def labels(rows, depth=0):
    out = []
    for row in rows:
        out.append(("  " * depth) + f"{row.kind.value}:{row.text}:{row.count if row.kind is not RowKind.HOST else ''}")
        out.extend(labels(row.children, depth + 1))
    return out


class FilterParsingTests(unittest.TestCase):
    def test_parse_filter(self) -> None:
        self.assertEqual(parse_filter(""), ([], ""))
        self.assertEqual(parse_filter("tag:Prod AND owner:alice"), ([("tag", "prod"), ("owner", "alice")], ""))
        terms, error = parse_filter("tag")
        self.assertIsNone(terms)
        self.assertIn("key:value", error)
        self.assertIsNone(parse_filter("tag:")[0])

    def test_filter_host_matches_tags_metadata_and_options(self) -> None:
        web = inventory()["prod"]["web.conf"].hosts[1]
        self.assertTrue(filter_host(web, [("tag", "web"), ("owner", "alice")]))
        self.assertTrue(filter_host(web, [("user", "deploy")]))
        self.assertFalse(filter_host(web, [("tag", "db")]))

    def test_search_text_includes_display_user(self) -> None:
        web = inventory()["prod"]["web.conf"].hosts[0]
        self.assertIn("root", search_text_for_host(web, "root"))


class TreeRowTests(unittest.TestCase):
    def test_unfiltered_tree_keeps_empty_groups_and_sorts(self) -> None:
        rows = build_tree_rows([(inventory(), False)])
        self.assertEqual(
            labels(rows),
            [
                "parent:empty-parent:0",
                "parent:prod:3",
                "  child-folder:db:1",
                "    child:primary.conf:1",
                "      host:db-primary:",
                "  child:web.conf:2",
                "    host:web-01:",
                "    host:web-02:",
                "parent:staging:1",
                "  child:empty.conf:0",
                "  child:hosts.conf:1",
                "    host:stage-app:",
            ],
        )

    def test_host_search_keeps_child_path_and_hides_unmatched_groups(self) -> None:
        rows = build_tree_rows([(inventory(), False)], query="primary")
        self.assertEqual(
            labels(rows),
            ["parent:prod:1", "  child-folder:db:1", "    child:primary.conf:1", "      host:db-primary:"],
        )

    def test_group_name_match_shows_all_hosts_and_empty_children(self) -> None:
        rows = build_tree_rows([(inventory(), False)], query="staging")
        self.assertEqual(
            labels(rows),
            ["parent:staging:1", "  child:empty.conf:0", "  child:hosts.conf:1", "    host:stage-app:"],
        )

    def test_filter_hides_groups_without_matching_hosts(self) -> None:
        rows = build_tree_rows([(inventory(), False)], terms=[("tag", "db")])
        self.assertEqual(
            labels(rows),
            ["parent:prod:1", "  child-folder:db:1", "    child:primary.conf:1", "      host:db-primary:"],
        )

    def test_shared_inventory_is_prefixed_and_marked(self) -> None:
        rows = build_tree_rows([(inventory(), False), ({"team": inventory()["staging"]}, True)])
        shared = [row for row in rows if row.shared]
        self.assertEqual([row.text for row in shared], ["[Shared] team"])
        host_rows = [r for r in shared[0].walk() if r.kind is RowKind.HOST]
        self.assertTrue(all(r.shared for r in host_rows))
        self.assertEqual(host_rows[0].key, "[Shared] team::hosts.conf::stage-app")

    def test_callbacks_supply_status_color_and_user(self) -> None:
        callbacks = TreeCallbacks(
            display_user=lambda h: "ops",
            status=lambda key: "connected" if key.endswith("::web-01") else "bogus",
            color=lambda parent, child, alias="": "#123456" if alias == "web-01" else "",
        )
        rows = build_tree_rows([(inventory(), False)], query="web-0", callbacks=callbacks)
        hosts = {r.alias: r for top in rows for r in top.walk() if r.kind is RowKind.HOST}
        self.assertEqual(hosts["web-01"].status, "connected")
        self.assertEqual(hosts["web-02"].status, "unknown")
        self.assertEqual(hosts["web-01"].color, "#123456")
        self.assertIn("ops@web-01.internal", hosts["web-01"].tooltip)
        self.assertIn("Connected", hosts["web-01"].tooltip)


class EmptyStateTests(unittest.TestCase):
    def test_empty_inventory(self) -> None:
        self.assertEqual(sidebar_page_for([({}, False)], [], False), "empty")

    def test_no_results_only_when_filtered(self) -> None:
        inv = [(inventory(), False)]
        rows = build_tree_rows(inv, query="does-not-exist")
        self.assertEqual(rows, [])
        self.assertEqual(sidebar_page_for(inv, rows, True), "no-results")
        self.assertEqual(sidebar_page_for(inv, build_tree_rows(inv), False), "tree")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
