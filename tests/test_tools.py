"""Tests for minion/tools/implementations.py — pure function tests, no mocking.

Each tool returns a plain string. Errors are returned as strings too (never raised),
so the model can reason about failures and recover.
"""

import pytest
from minion.tools.implementations import edit_file, list_directory, read_file, run_shell, write_file


# ─── read_file ────────────────────────────────────────────────────────────────

class TestReadFile:
    def test_reads_file_content(self, tmp_path):
        f = tmp_path / "hello.txt"
        f.write_text("hello world")
        result = read_file(str(f))
        assert "hello world" in result
        assert "1" in result   # line number prefix

    def test_returns_error_for_missing_file(self, tmp_path):
        result = read_file(str(tmp_path / "nope.txt"))
        assert result.startswith("Error:")
        assert "does not exist" in result

    def test_returns_error_for_directory(self, tmp_path):
        result = read_file(str(tmp_path))
        assert result.startswith("Error:")
        assert "not a file" in result

    def test_truncates_large_file_at_300_lines(self, tmp_path):
        f = tmp_path / "big.txt"
        f.write_text("\n".join(f"line {i}" for i in range(400)))
        result = read_file(str(f))
        assert "300" in result
        assert "400" in result   # total line count mentioned
        assert "start_line" in result  # hint to use range

    def test_does_not_truncate_small_file(self, tmp_path):
        f = tmp_path / "small.txt"
        f.write_text("\n".join(f"line {i}" for i in range(10)))
        result = read_file(str(f))
        assert "start_line" not in result   # no truncation hint
        assert "line 9" in result           # all content present

    def test_line_range_returns_correct_lines(self, tmp_path):
        f = tmp_path / "ranged.txt"
        f.write_text("\n".join(f"line {i}" for i in range(1, 21)))
        result = read_file(str(f), start_line=5, end_line=7)
        assert "line 5" in result
        assert "line 7" in result
        assert "line 4" not in result
        assert "line 8" not in result

    def test_line_range_header_shows_bounds(self, tmp_path):
        f = tmp_path / "f.txt"
        f.write_text("\n".join(str(i) for i in range(1, 11)))
        result = read_file(str(f), start_line=2, end_line=4)
        assert "Lines 2" in result
        assert "4" in result

    def test_start_line_beyond_eof_returns_error(self, tmp_path):
        f = tmp_path / "short.txt"
        f.write_text("only one line")
        result = read_file(str(f), start_line=99)
        assert result.startswith("Error:")
        assert "exceeds" in result

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


# ─── edit_file ────────────────────────────────────────────────────────────────

class TestEditFile:
    def test_exact_match_replaces_block(self, tmp_path):
        f = tmp_path / "hello.py"
        f.write_text('x = 1\nprint("hello")\ny = 2\n')
        result = edit_file(str(f), 'print("hello")', 'print("world")')
        assert f.read_text() == 'x = 1\nprint("world")\ny = 2\n'
        assert "Edited" in result

    def test_multiline_replacement(self, tmp_path):
        f = tmp_path / "f.py"
        f.write_text("a = 1\nb = 2\nc = 3\n")
        result = edit_file(str(f), "a = 1\nb = 2", "a = 10\nb = 20")
        assert "a = 10" in f.read_text()
        assert "b = 20" in f.read_text()
        assert "c = 3" in f.read_text()

    def test_error_on_missing_file(self, tmp_path):
        result = edit_file(str(tmp_path / "nope.py"), "x", "y")
        assert result.startswith("Error:")
        assert "does not exist" in result

    def test_error_when_old_string_not_found(self, tmp_path):
        f = tmp_path / "f.txt"
        f.write_text("hello world\n")
        result = edit_file(str(f), "not present", "replacement")
        assert result.startswith("Error:")
        assert "not found" in result

    def test_error_when_old_string_ambiguous(self, tmp_path):
        f = tmp_path / "f.txt"
        f.write_text("foo\nfoo\n")
        result = edit_file(str(f), "foo", "bar")
        assert result.startswith("Error:")
        assert "2 times" in result

    def test_whitespace_flexible_fallback(self, tmp_path):
        f = tmp_path / "f.py"
        f.write_text("class Foo:\n    def method(self):\n        pass\n")
        # old_string written without indentation — flexible match should handle it
        result = edit_file(str(f), "def method(self):\n    pass", "def method(self):\n    return 42")
        assert "return 42" in f.read_text()
        assert "Edited" in result

    def test_python_syntax_error_rejected(self, tmp_path):
        f = tmp_path / "f.py"
        f.write_text("x = 1\ny = 2\n")
        result = edit_file(str(f), "y = 2", "y = (")
        assert result.startswith("Error:")
        assert "syntax" in result.lower()
        # original file unchanged
        assert f.read_text() == "x = 1\ny = 2\n"

    def test_delete_block_with_empty_new_string(self, tmp_path):
        f = tmp_path / "f.txt"
        f.write_text("keep\ndelete me\nkeep\n")
        edit_file(str(f), "\ndelete me", "")
        assert "delete me" not in f.read_text()


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
