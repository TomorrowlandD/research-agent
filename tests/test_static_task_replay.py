"""Exercise the portable static-task recording through its offline entry point."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "tests" / "fixtures" / "static_task_replay.py"
RECORDING = SCRIPT.with_suffix(".json")


class StaticTaskReplayTests(unittest.TestCase):
    def replay(self, path):
        return subprocess.run(
            [sys.executable, "-s", "-B", str(SCRIPT), "replay", str(path)],
            cwd=ROOT, env={**os.environ, "PYTHONPATH": str(ROOT)},
            capture_output=True, text=True, encoding="utf-8", timeout=45,
        )

    def test_saved_task_replays_in_a_fresh_process(self):
        result = self.replay(RECORDING)
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout.splitlines()[-1])
        self.assertEqual(report["answer"], "NL-4827")
        self.assertEqual(report["sequence"], ["model", "web_fetch", "model"])
        self.assertEqual(report["model_responses_replayed"], 2)
        self.assertEqual(report["upstream_http_requests"], 0)

    def test_changed_tool_result_invalidates_next_model_input(self):
        bundle = json.loads(RECORDING.read_text(encoding="utf-8"))
        bundle["snapshot"]["result"]["content"] = "North Lake: OTHER"
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "changed.json"
            path.write_text(json.dumps(bundle), encoding="utf-8")
            result = self.replay(path)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Model input differs from recording at turn 2", result.stderr)
