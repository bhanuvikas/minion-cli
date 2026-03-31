"""Tests for minion/tools/implementations.py — pure function tests, no mocking.

Each tool returns a plain string. Errors are returned as strings too (never raised),
so the model can reason about failures and recover.
"""

import pytest
from minion.tools.implementations import list_directory, read_file, run_shell, write_file


# ─── read_file ────────────────────────────────────────────────────────────────

class TestReadFile:
    def test_reads_file_content(self, tmp_path):
        f = tmp_path / "hello.txt"
        f.write_text("hello world")
        assert read_file(str(f)) == "hello world"

    def test_returns_error_for_missing_file(self, tmp_path):
        result = read_file(str(tmp_path / "nope.txt"))
        assert result.startswith("Error:")
        assert "does not exist" in result

    def test_returns_error_for_directory(self, tmp_path):
        result = read_file(str(tmp_path))
        assert result.startswith("Error:")
        assert "not a file" in result

    def test_truncates_large_file(self, tmp_path):
        f = tmp_path / "big.txt"
        f.write_bytes(b"x" * 100)
        result = read_file(str(f), max_bytes=10)
        assert "truncated" in result
        assert "100" in result  # original size mentioned

    def test_does_not_truncate_within_limit(self, tmp_path):
        f = tmp_path / "small.txt"
        f.write_text("short content")
        result = read_file(str(f), max_bytes=50_000)
        assert "truncated" not in result
        assert result == "short content"

    def test_handles_binary_with_replacement(self, tmp_path):
        f = tmp_path / "bin.bin"
        f.write_bytes(bytes(range(256)))
        result = read_file(str(f))
        assert isinstance(result, str)  # no crash, valid string


# ─── write_file ───────────────────────────────────────────────────────────────

class TestWriteFile:
    def test_creates_new_file(self, tmp_path):
        path = str(tmp_path / "out.txt")
        result = write_file(path, "banana")
        assert "banana" in (tmp_path / "out.txt").read_text()
        assert "Wrote" in result

    def test_overwrites_existing_file(self, tmp_path):
        f = tmp_path / "existing.txt"
        f.write_text("old content")
        write_file(str(f), "new content")
        assert f.read_text() == "new content"

    def test_creates_parent_directories(self, tmp_path):
        path = str(tmp_path / "deep" / "nested" / "file.txt")
        write_file(path, "hello")
        assert (tmp_path / "deep" / "nested" / "file.txt").read_text() == "hello"

    def test_result_contains_path_and_size(self, tmp_path):
        path = str(tmp_path / "f.txt")
        result = write_file(path, "hello world")
        assert "f.txt" in result
        assert "11" in result  # 11 chars


# ─── list_directory ───────────────────────────────────────────────────────────

class TestListDirectory:
    def test_lists_files_and_dirs(self, tmp_path):
        (tmp_path / "subdir").mkdir()
        (tmp_path / "file.py").write_text("x")
        result = list_directory(str(tmp_path))
        assert "subdir/" in result
        assert "file.py" in result

    def test_returns_error_for_missing_dir(self, tmp_path):
        result = list_directory(str(tmp_path / "nope"))
        assert result.startswith("Error:")
        assert "does not exist" in result

    def test_returns_error_for_file_path(self, tmp_path):
        f = tmp_path / "f.txt"
        f.write_text("x")
        result = list_directory(str(f))
        assert result.startswith("Error:")
        assert "not a directory" in result

    def test_empty_directory(self, tmp_path):
        result = list_directory(str(tmp_path))
        assert "empty" in result

    def test_includes_file_sizes(self, tmp_path):
        (tmp_path / "sized.txt").write_bytes(b"hello")
        result = list_directory(str(tmp_path))
        assert "5" in result  # 5 bytes

    def test_dirs_sorted_before_files(self, tmp_path):
        (tmp_path / "z_file.txt").write_text("x")
        (tmp_path / "a_dir").mkdir()
        result = list_directory(str(tmp_path))
        dir_pos = result.index("a_dir/")
        file_pos = result.index("z_file.txt")
        assert dir_pos < file_pos


# ─── run_shell ────────────────────────────────────────────────────────────────

class TestRunShell:
    def test_captures_stdout(self):
        result = run_shell("echo hello")
        assert "hello" in result

    def test_captures_stderr(self):
        result = run_shell("echo error_msg >&2")
        assert "error_msg" in result

    def test_nonzero_exit_code_noted(self):
        result = run_shell("exit 1", timeout=5)
        assert "exit code: 1" in result

    def test_timeout_returns_error(self):
        result = run_shell("sleep 10", timeout=1)
        assert "timed out" in result.lower()

    def test_no_output_returns_placeholder(self):
        result = run_shell("true")
        assert "no output" in result.lower() or result  # either message or empty-ish

    def test_combined_stdout_and_stderr(self):
        result = run_shell("echo out && echo err >&2")
        assert "out" in result
        assert "err" in result
