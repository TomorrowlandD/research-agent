from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from agents.event_log import SessionEventLog


class SessionEventLogTests(unittest.TestCase):
    def setUp(self):
        self.directory = Path(self.enterContext(TemporaryDirectory()))
        self.enterContext(patch("agents.session.SESSION_DIR", self.directory))
        self.path = self.directory / "session.events.jsonl"

    def rows(self):
        return [json.loads(line) for line in self.path.read_bytes().splitlines()]

    def test_resume_reads_long_last_line_and_preserves_prefix(self):
        original = "第一行\n" * 7000
        first = SessionEventLog("session", "openai")
        first.append("t1", "user_message", {"content": "start"})
        first.append("t1", "assistant_message", {"content": original})
        prefix = self.path.read_bytes()
        resumed = SessionEventLog("session", "openai")
        resumed.append("t2", "session_resumed", {})
        first.append("t3", "user_message", {"content": "again"})
        self.assertTrue(self.path.read_bytes().startswith(prefix))
        rows = self.rows()
        self.assertEqual([r["seq"] for r in rows], [1, 2, 3, 4])
        self.assertEqual(rows[1]["payload"]["content"], original)
        self.assertEqual([r["trace_id"] for r in rows], ["t1", "t1", "t2", "t3"])
        self.assertTrue(all(r["session_id"] == "session" and r["protocol"] == "openai" for r in rows))

    def test_independent_writers_serialize_sequence_allocation(self):
        def emit(i):
            SessionEventLog("session", "anthropic").append("t", "user_message", {"i": i})
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(emit, range(24)))
        rows = self.rows()
        self.assertEqual([r["seq"] for r in rows], list(range(1, 25)))
        self.assertEqual(sorted(r["payload"]["i"] for r in rows), list(range(24)))

    def test_tool_preview_references_existing_artifact(self):
        log = SessionEventLog("session", "openai")
        log.tool_result("t", "1", "read_file", "small", is_error=False)
        artifact = self.directory / "original.txt"
        artifact.write_text("large" * 20000)
        log.tool_result("t", "2", "read_file", "preview", is_error=False, artifact_path=artifact)
        small, large = [r["payload"] for r in self.rows()]
        self.assertEqual(small["content"], "small")
        self.assertEqual(large["content_preview"], "preview")
        self.assertEqual(large["artifact_path"], str(artifact))
        self.assertNotIn("content", large)

    def test_write_failure_warns_and_next_event_can_succeed(self):
        log = SessionEventLog("session", "openai")
        with patch("agents.event_log.os.open", side_effect=PermissionError("denied")), self.assertLogs(level="WARNING"):
            log.append("t", "user_message", {})
        log.append("t", "user_message", {})
        self.assertEqual([row["seq"] for row in self.rows()], [1])

    def test_damaged_tail_warns_and_preserves_evidence(self):
        self.path.write_bytes(b'{"seq":1}\n{"seq":')
        with self.assertLogs(level="WARNING"):
            SessionEventLog("session", "openai").append("t", "user_message", {})
        self.assertEqual(self.path.read_bytes(), b'{"seq":1}\n{"seq":')
