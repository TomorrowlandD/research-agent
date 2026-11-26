from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from agents.artifacts import CandidateLedger, candidate_label, canonical_id


class CandidateLedgerTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(TemporaryDirectory()))
        self.ledger = CandidateLedger(self.root, "abc")
        self.paper = {"title": "Study", "url": "https://arxiv.org/abs/2401.12345v1"}

    def test_canonical_identifiers(self):
        for url in (self.paper["url"], "https://export.arxiv.org/pdf/2401.12345v2.pdf",
                    "https://doi.org/10.48550/arXiv.2401.12345"):
            self.assertEqual(canonical_id(url), "arxiv:2401.12345")
        self.assertEqual(canonical_id("https://arxiv.org/pdf/hep-th/9901001v2"), "arxiv:hep-th/9901001")
        for url in ("https://doi.org/10.1234/ABC.7", "https://example.org/doi/full/10.1234/Abc.7?utm_term=test"):
            self.assertEqual(canonical_id(url), "doi:10.1234/abc.7")
        self.assertEqual(canonical_id("https://EXAMPLE.org:443/Case/?id=7&utm_SOURCE=x&fbclid=y#anchor"),
                         "url:https://example.org/Case?id=7")
        self.assertNotEqual(canonical_id("https://example.org/?id=7"), canonical_id("https://example.org/?id=8"))

    def test_decisions_survive_reopen_and_entities_stay_unique(self):
        row, known = self.ledger.observe([self.paper])[0]
        self.assertFalse(known)
        key = row["canonical_id"]
        updated = self.ledger.update(key, "rejected", "无代码")
        self.assertEqual(self.ledger.update(key, "rejected", "无代码"), updated)
        reopened = CandidateLedger(self.root, "abc")
        row, known = reopened.observe([{**self.paper, "url": "https://arxiv.org/pdf/2401.12345"}])[0]
        self.assertEqual(row, updated)
        self.assertTrue(known)
        self.assertIn("状态: rejected | 原因: 无代码", candidate_label(row, known))
        selected = reopened.update(key, "selected", "筛选条件已变更")
        self.assertEqual(selected["first_seen_at"], updated["first_seen_at"])
        self.assertEqual(reopened.list_candidates("selected"), [selected])
        self.assertFalse(reopened.list_candidates("rejected"))
        self.assertEqual([json.loads(line) for line in reopened.path.read_text().splitlines()], [selected])
        for cwd, sid in [(self.root, "other"), (self.root / "other", "abc")]:
            self.assertFalse(CandidateLedger(cwd, sid).list_candidates())

    def test_concurrent_observations_and_failed_replace(self):
        def add(i):
            CandidateLedger(self.root, "abc").observe([self.paper, {"title": str(i), "url": f"https://test.org/{i}"}])
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(add, range(12)))
        self.assertEqual(len(self.ledger.list_candidates()), 13)
        before = self.ledger.path.read_bytes()
        with patch("pathlib.Path.replace", side_effect=OSError("disk full")), self.assertRaises(OSError):
            self.ledger.update("arxiv:2401.12345", "rejected", "no code")
        self.assertEqual(before, self.ledger.path.read_bytes())

    def test_invalid_updates_preserve_existing_ledger(self):
        self.ledger.observe([self.paper])
        for key, status, reason in [("unknown", "selected", "ok"), ("arxiv:2401.12345", "seen", "why"),
                                    ("arxiv:2401.12345", "rejected", " ")]:
            with self.assertRaises(ValueError):
                self.ledger.update(key, status, reason)
        self.assertEqual(len(self.ledger.list_candidates("seen")), 1)
