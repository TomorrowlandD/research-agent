import json
from pathlib import Path
import tempfile
import unittest

from fixtures.web_comparison_report import metrics, render, verify


class WebComparisonTests(unittest.TestCase):
    def test_inclusive_percentiles_and_total_tokens(self):
        rows = [{"seconds": t, "main_tokens": {"input": 10, "output": 2},
                 "side_tokens": {"input": 3, "output": 1}, "tool_calls": 2, "passed": t != 9}
                for t in (1, 3, 9)]
        m = metrics(rows)
        self.assertEqual((m["passed"], m["p50"], m["input"], m["output"]), (2, 3, 13, 3))
        self.assertAlmostEqual(m["p95"], 8.4)

    def test_correct_answer_without_web_evidence_cannot_pass(self):
        row = {"case": "static", "expected": "NL-4827", "answer": "NL-4827", "passed": True,
               "tool_calls": 0, "seconds": 1}
        with self.assertRaisesRegex(ValueError, "Inconsistent outcome"):
            verify(row, [])

    def test_missing_or_duplicate_paired_observations_fail(self):
        rows = [{"repeat": i, "case": case, "arm": arm}
                for i in (1, 2) for case in ("static", "interactive") for arm in ("P0", "P1")]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "raw.jsonl"
            for broken in (rows[:-1], rows + rows[:1]):
                path.write_text("\n".join(map(json.dumps, broken)), encoding="utf-8")
                with self.assertRaisesRegex(ValueError, "Incomplete or duplicated"):
                    render(path)
