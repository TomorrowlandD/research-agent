from __future__ import annotations

import asyncio
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from agents.tool_result import ToolFailure
from agents.tools import _grep_search, _read_file


class WindowsReadAndSearchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="researchagent-tools-", dir=Path(__file__).resolve().parent))
        # 每个测试文件单独清理，目录只在为空时删除，不使用递归删除。
        self.addCleanup(self.root.rmdir)
        windows = patch("agents.tools.IS_WIN", True)
        windows.start()
        self.addCleanup(windows.stop)

    def write_file(self, name: str, content: bytes) -> Path:
        path = self.root / name
        path.write_bytes(content)
        self.addCleanup(path.unlink)
        return path

    def search(self, pattern: str, path: Path, include: str | None = None) -> str:
        return asyncio.run(_grep_search({"pattern": pattern, "path": str(path), "include": include}))

    def test_utf8_and_bom_reading_and_chinese_search(self) -> None:
        for encoding in ("utf-8", "utf-8-sig"):
            with self.subTest(encoding=encoding):
                path = self.write_file(f"{encoding}.txt", "第一阶段\n测试代号：熊猫731\n".encode(encoding))
                result = _read_file({"file_path": str(path)})
                self.assertIn("   1 | 第一阶段", result)
                self.assertIn("   2 | 测试代号：熊猫731", result)
                self.assertNotIn("\ufeff", result)
                self.assertNotIn("\ufffd", result)
                self.assertIn(f"{path}:2:测试代号：熊猫731", self.search("熊猫731", path))

    def test_single_file_and_directory_find_same_implementation(self) -> None:
        path = self.write_file("agent.py", b'def _resolve_openai_request_options():\n    mode = "RESEARCH_DEEPSEEK_THINKING"\n')
        for pattern in ("RESEARCH_DEEPSEEK_THINKING", "_resolve_openai_request_options"):
            with self.subTest(pattern=pattern):
                single = self.search(pattern, path)
                directory = self.search(pattern, self.root, "agent.py")
                self.assertEqual(single, directory)
                self.assertIn(pattern, single)
                self.assertIn(str(path), single)

    def test_include_filter_applies_to_file_and_recursive_directory(self) -> None:
        nested = self.root / "nested"
        nested.mkdir()
        self.addCleanup(nested.rmdir)
        path = self.write_file("nested/agent.py", b"target\n")
        self.write_file("other.txt", b"target\n")
        result = self.search("target", self.root, "*.py")
        self.assertEqual(result, f"{path}:1:target")
        self.assertEqual(self.search("target", path, "*.txt"), "No matches found.")

    def test_no_match_is_distinct_from_missing_path_and_invalid_regex(self) -> None:
        path = self.write_file("plain.txt", b"hello\n")
        self.assertEqual(_read_file({"file_path": str(path)}), "   1 | hello\n   2 | ")
        self.assertEqual(self.search("missing", path), "No matches found.")
        with self.assertRaisesRegex(ToolFailure, "Error searching files:"):
            self.search("hello", self.root / "missing.txt")
        with self.assertRaisesRegex(ToolFailure, "Error searching files:"):
            self.search("[", path)

    def test_invalid_encoding_is_not_silently_replaced_or_reported_as_no_match(self) -> None:
        path = self.write_file("invalid.txt", b"\xff\xfe\x80")
        with self.assertRaisesRegex(ToolFailure, "Error reading file:"):
            _read_file({"file_path": str(path)})
        with self.assertRaisesRegex(ToolFailure, "Error searching files:") as error:
            self.search("target", path)
        self.assertIn(str(path), str(error.exception))

    def test_directory_permission_error_is_not_reported_as_no_match(self) -> None:
        with patch("agents.tools.os.listdir", side_effect=PermissionError("access denied")):
            with self.assertRaisesRegex(ToolFailure, "Error searching files:") as error:
                self.search("target", self.root)
        self.assertIn("access denied", str(error.exception))

    def test_file_permission_error_is_not_reported_as_no_match(self) -> None:
        path = self.write_file("denied.txt", b"target\n")
        with patch("agents.tools._read_tool_text", side_effect=PermissionError("access denied")):
            with self.assertRaisesRegex(ToolFailure, "Error searching files:") as error:
                self.search("target", path)
        self.assertIn("access denied", str(error.exception))

    def test_partial_search_returns_matches_and_reports_unreadable_files(self) -> None:
        path = self.write_file("good.txt", b"target\n")
        bad = self.write_file("bad.txt", b"\xff")
        result = self.search("target", self.root)
        self.assertIn(f"{path}:1:target", result)
        self.assertIn("Warning: search incomplete", result)
        self.assertIn(str(bad), result)

    def test_result_limits_and_default_hidden_directory_exclusions(self) -> None:
        self.write_file("many.txt", b"target\n" * 250)
        self.write_file(".hidden.txt", b"target\n")
        result = self.search("target", self.root)
        self.assertEqual(len([line for line in result.splitlines() if ":target" in line]), 100)
        self.assertIn("100 more matches", result)
        self.assertNotIn(".hidden.txt", result)


if __name__ == "__main__":
    unittest.main()
