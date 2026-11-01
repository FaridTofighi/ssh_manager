"""Run Command results: grouping identical outputs, the diff against the majority, exports (no GTK)."""

import csv
import io
import json
import unittest

from ophion.multirun import report as P
from ophion.multirun.engine import HostResult

SECRET = "Report-sentinel-pw"


def result(alias, stdout="", status="success", exit_code=0, stderr="", group="G"):
    return HostResult(key=f"k-{alias}", alias=alias, group=group, status=status, exit_code=exit_code,
                      duration=1.25, stdout=stdout, stderr=stderr)


class GroupingTests(unittest.TestCase):
    def test_identical_outputs_group_and_the_majority_comes_first(self) -> None:
        results = [result(f"h{i}", "6.1.0\nok  \n\n") for i in range(31)]
        results += [result("odd", "5.4.0\nok\n"), result("trail", "6.1.0\nok\n"),
                    result("down", "", status="unreachable", exit_code=None), result("q", status="queued")]
        groups = P.group_by_output(results)
        self.assertEqual([len(g.keys) for g in groups], [32, 1])  # trailing whitespace does not count
        self.assertEqual(groups[0].text, "6.1.0\nok")
        self.assertEqual(groups[0].label, "Same output on 32 hosts")
        self.assertEqual(groups[1].label, "Output of 1 host")
        self.assertEqual(P.majority(groups).text, "6.1.0\nok")
        self.assertEqual(P.group_by_output([]), [])

    def test_diff_against_the_majority(self) -> None:
        diff = P.unified_diff("6.1.0\nok", "5.4.0\nok", "majority (32 hosts)", "odd")
        self.assertIn("--- majority (32 hosts)", diff)
        self.assertIn("+++ odd", diff)
        self.assertIn("-6.1.0", diff)
        self.assertIn("+5.4.0", diff)
        self.assertEqual(P.unified_diff("same", "same", "a", "b"), "")


class ExportTests(unittest.TestCase):
    def rows(self):
        return [result("web", 'line "one"\nline, two\n', stderr="warn\n"),
                result("ünï", "=cmd|' /C calc'!A0\n", status="failed", exit_code=2),
                result("plus", "+1\n-2\n@x\n")]

    def test_csv_quotes_newlines_utf8_and_formula_guard(self) -> None:
        text = P.to_csv(self.rows())
        parsed = list(csv.reader(io.StringIO(text)))
        self.assertEqual(parsed[0], ["host", "group", "status", "exit", "duration", "stdout", "stderr"])
        self.assertEqual(parsed[1][0], "web")
        self.assertEqual(parsed[1][5], 'line "one"\nline, two\n')
        self.assertEqual(parsed[2][0], "ünï")
        self.assertEqual(parsed[2][2], "Failed (exit 2)")
        self.assertEqual(parsed[2][5], "'=cmd|' /C calc'!A0\n")  # never a formula
        self.assertEqual(parsed[3][5], "'+1\n-2\n@x\n")
        self.assertEqual(P.cell("-5"), "'-5")
        self.assertEqual(P.cell("plain"), "plain")
        text.encode("utf-8")

    def test_json_text_and_tsv(self) -> None:
        data = json.loads(P.to_json(self.rows()))
        self.assertEqual(data[1]["host"], "ünï")
        self.assertEqual(data[1]["exit"], 2)
        self.assertEqual(data[0]["stderr"], "warn\n")
        text = P.to_text(self.rows())
        self.assertIn("=== web (G) — Success", text)
        self.assertIn("--- stderr ---\nwarn", text)
        tsv = P.to_tsv(self.rows())
        self.assertEqual(tsv.splitlines()[0], "host\tgroup\tstatus\texit\tduration\toutput")
        self.assertEqual(tsv.splitlines()[1].split("\t")[5], 'line "one"')

    def test_nothing_secret_is_added(self) -> None:
        rows = [result("web", "fine")]
        for export in (P.to_csv, P.to_json, P.to_text, P.to_tsv):
            self.assertNotIn(SECRET, export(rows))


if __name__ == "__main__":
    unittest.main()
