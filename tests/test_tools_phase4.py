"""Tests for Phase 4 tool additions and runner enhancements.

Covers:
  - search_code (Python fallback — no rg dependency in tests)
  - get_file_outline (via implementations.py dispatch)
  - _resolve_mentions in runner.py
  - build_system_prompt with and without ProjectContext
"""

import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock

from minion.tools.implementations import search_code, get_file_outline
from minion.runner import _resolve_mentions
from minion.context.prompts import build_system_prompt, BASE_SYSTEM_PROMPT
from minion.context.project import build_project_context


# ─── search_code ─────────────────────────────────────────────────────────────

class TestSearchCode:
    def test_finds_pattern_in_python_file(self, tmp_path):
        (tmp_path / "app.py").write_text("def authenticate(user):\n    pass\n")
        # Force Python fallback by patching rg to be unavailable
        with patch("minion.tools.implementations._search_rg", return_value=None):
            result = search_code("def authenticate", path=str(tmp_path))
        assert "app.py" in result
        assert "authenticate" in result

    def test_returns_line_number(self, tmp_path):
        (tmp_path / "utils.py").write_text("x = 1\ndef target():\n    pass\n")
        with patch("minion.tools.implementations._search_rg", return_value=None):
            result = search_code("def target", path=str(tmp_path))
        assert ":2:" in result   # line 2

    def test_no_matches_returns_placeholder(self, tmp_path):
        (tmp_path / "empty.py").write_text("x = 1\n")
        with patch("minion.tools.implementations._search_rg", return_value=None):
            result = search_code("DEFINITELY_NOT_IN_ANY_FILE", path=str(tmp_path))
        assert "no matches" in result.lower()

    def test_invalid_regex_returns_error(self, tmp_path):
        with patch("minion.tools.implementations._search_rg", return_value=None):
            result = search_code("[invalid(regex", path=str(tmp_path))
        assert result.startswith("Error:")
        assert "pattern" in result.lower()

    def test_file_glob_restricts_search(self, tmp_path):
        (tmp_path / "app.py").write_text("TARGET = 1\n")
        (tmp_path / "app.js").write_text("TARGET = 2\n")
        with patch("minion.tools.implementations._search_rg", return_value=None):
            result = search_code("TARGET", path=str(tmp_path), file_glob="*.py")
        assert "app.py" in result
        assert "app.js" not in result

    def test_nonexistent_path_returns_error(self):
        with patch("minion.tools.implementations._search_rg", return_value=None):
            result = search_code("foo", path="/nonexistent/path/xyz")
        assert result.startswith("Error:")

    def test_searches_across_multiple_files(self, tmp_path):
        (tmp_path / "a.py").write_text("SHARED = 1\n")
        (tmp_path / "b.py").write_text("SHARED = 2\n")
        with patch("minion.tools.implementations._search_rg", return_value=None):
            result = search_code("SHARED", path=str(tmp_path))
        assert "a.py" in result
        assert "b.py" in result

    def test_rg_result_used_when_available(self, tmp_path):
        """When _search_rg returns a value, _search_python is not called."""
        with patch("minion.tools.implementations._search_rg", return_value="rg:1:match"), \
             patch("minion.tools.implementations._search_python") as mock_py:
            result = search_code("anything", path=str(tmp_path))
        assert result == "rg:1:match"
        mock_py.assert_not_called()

    def test_search_results_cap_shows_truncation_note(self, tmp_path):
        """When result count hits _MAX_SEARCH_RESULTS, a truncation note is appended."""
        for i in range(5):
            (tmp_path / f"f{i}.py").write_text("MATCH = 1\n")
        with patch("minion.tools.implementations._search_rg", return_value=None), \
             patch("minion.tools.implementations._MAX_SEARCH_RESULTS", 3):
            result = search_code("MATCH", path=str(tmp_path))
        assert "showing first 3 matches" in result

    def test_gitignore_patterns_respected_in_python_fallback(self, tmp_path):
        """Files matching .gitignore patterns are excluded from Python search results."""
        (tmp_path / ".gitignore").write_text(".context/\n")
        ignored_dir = tmp_path / ".context"
        ignored_dir.mkdir()
        (ignored_dir / "notes.md").write_text("MATCH = ignored\n")
        (tmp_path / "app.py").write_text("MATCH = visible\n")
        with patch("minion.tools.implementations._search_rg", return_value=None):
            result = search_code("MATCH", path=str(tmp_path))
        assert "app.py" in result
        assert ".context" not in result

    def test_binary_files_skipped_in_python_fallback(self, tmp_path):
        """Binary-extension files (.pyc etc.) are skipped during Python search."""
        (tmp_path / "mod.pyc").write_bytes(b"\x00MATCH\x00")
        (tmp_path / "app.py").write_text("NO_PATTERN_HERE\n")
        with patch("minion.tools.implementations._search_rg", return_value=None):
            result = search_code("MATCH", path=str(tmp_path))
        assert "no matches" in result.lower()

    def test_rg_returncode_one_means_no_matches(self, tmp_path):
        """rg exits 1 when there are no matches — should return the no-match message."""
        import subprocess
        mock_result = MagicMock()
        mock_result.returncode = 1
        with patch("subprocess.run", return_value=mock_result):
            from minion.tools.implementations import _search_rg
            result = _search_rg("anything", str(tmp_path), "*")
        assert result == "[no matches found]"

    def test_rg_returncode_other_falls_back(self, tmp_path):
        """rg exits with unexpected code → _search_rg returns None so Python runs."""
        import subprocess
        mock_result = MagicMock()
        mock_result.returncode = 2
        mock_result.stdout = ""
        with patch("subprocess.run", return_value=mock_result):
            from minion.tools.implementations import _search_rg
            result = _search_rg("anything", str(tmp_path), "*")
        assert result is None

    def test_rg_not_found_falls_back(self, tmp_path):
        """FileNotFoundError (rg not on PATH) → _search_rg returns None."""
        with patch("subprocess.run", side_effect=FileNotFoundError):
            from minion.tools.implementations import _search_rg
            result = _search_rg("anything", str(tmp_path), "*")
        assert result is None


# ─── get_file_outline (via implementations dispatch) ─────────────────────────

class TestGetFileOutlineImpl:
    def test_returns_outline_for_python_file(self, tmp_path):
        f = tmp_path / "auth.py"
        f.write_text("class Auth:\n    def login(self):\n        pass\n")
        result = get_file_outline(str(f))
        assert "Auth" in result
        assert "login" in result

    def test_returns_error_for_missing_file(self, tmp_path):
        result = get_file_outline(str(tmp_path / "ghost.py"))
        assert result.startswith("Error:")


# ─── _resolve_mentions ────────────────────────────────────────────────────────

class TestResolveMentions:
    def test_no_mentions_returns_prompt_unchanged(self, tmp_path):
        result = _resolve_mentions("just a plain prompt", tmp_path)
        assert result == "just a plain prompt"

    def test_resolves_file_mention(self, tmp_path):
        (tmp_path / "main.py").write_text("print('hello')")
        result = _resolve_mentions("explain @main.py", tmp_path)
        assert "print('hello')" in result
        assert "Contents of main.py" in result

    def test_resolves_path_with_directory(self, tmp_path):
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "auth.py").write_text("# auth module")
        result = _resolve_mentions("review @src/auth.py please", tmp_path)
        assert "auth module" in result

    def test_missing_mention_injects_error_inline(self, tmp_path):
        result = _resolve_mentions("see @ghost.py", tmp_path)
        assert "@ghost.py: file not found" in result

    def test_does_not_match_bare_decorator(self, tmp_path):
        """@property and @classmethod are Python decorators, not file mentions."""
        result = _resolve_mentions("use @property for this", tmp_path)
        assert result == "use @property for this"

    def test_does_not_match_at_without_extension_or_slash(self, tmp_path):
        result = _resolve_mentions("email me @bhanu", tmp_path)
        assert result == "email me @bhanu"

    def test_deduplicates_repeated_mentions(self, tmp_path):
        (tmp_path / "utils.py").write_text("# utils")
        result = _resolve_mentions("@utils.py and @utils.py again", tmp_path)
        # Should only append the file contents once
        assert result.count("Contents of utils.py") == 1

    def test_original_prompt_preserved_before_appended_content(self, tmp_path):
        (tmp_path / "f.py").write_text("x = 1")
        result = _resolve_mentions("explain @f.py", tmp_path)
        assert result.startswith("explain @f.py")

    def test_directory_mention_returns_not_a_file_error(self, tmp_path):
        (tmp_path / "src").mkdir()
        result = _resolve_mentions("look at @src", tmp_path)
        # src has no extension, so won't match the mention pattern
        # (dirs without extensions aren't matched)
        assert result == "look at @src"

    def test_mention_of_existing_directory_with_path_reports_not_a_file(self, tmp_path):
        """@dir/subdir where subdir is a real directory (not a file) shows an error."""
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "utils").mkdir()
        # @src/utils matches the regex (has a slash) but is a directory
        result = _resolve_mentions("look at @src/utils", tmp_path)
        assert "not a file" in result

    def test_resolves_dotfile_mention(self, tmp_path):
        """@.gitignore and other dotfiles should be resolved."""
        (tmp_path / ".gitignore").write_text("*.pyc\n.venv/\n")
        result = _resolve_mentions("explain @.gitignore", tmp_path)
        assert "*.pyc" in result
        assert "Contents of .gitignore" in result

    def test_resolves_dotfile_with_extension(self, tmp_path):
        """@.env.example has both a leading dot and an extension."""
        (tmp_path / ".env.example").write_text("API_KEY=your_key_here\n")
        result = _resolve_mentions("see @.env.example", tmp_path)
        assert "API_KEY" in result

    def test_missing_dotfile_injects_error(self, tmp_path):
        result = _resolve_mentions("see @.gitignore", tmp_path)
        assert "@.gitignore: file not found" in result


# ─── build_system_prompt ─────────────────────────────────────────────────────

class TestBuildSystemPrompt:
    def test_without_context_returns_base_prompt(self):
        result = build_system_prompt()
        assert result == BASE_SYSTEM_PROMPT

    def test_with_none_context_returns_base_prompt(self):
        assert build_system_prompt(None) == BASE_SYSTEM_PROMPT

    def test_with_context_appends_project_block(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text('[project]\nname = "app"\n')
        ctx = build_project_context(tmp_path)
        result = build_system_prompt(ctx)
        assert BASE_SYSTEM_PROMPT in result
        assert "## Project Context" in result

    def test_context_appears_after_base_prompt(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text('[project]\nname = "app"\n')
        ctx = build_project_context(tmp_path)
        result = build_system_prompt(ctx)
        base_end = result.index(BASE_SYSTEM_PROMPT) + len(BASE_SYSTEM_PROMPT)
        context_start = result.index("## Project Context")
        assert context_start > base_end

    def test_minion_md_injected_when_present(self, tmp_path):
        (tmp_path / "MINION.md").write_text("Always write tests.")
        ctx = build_project_context(tmp_path)
        result = build_system_prompt(ctx)
        assert "Always write tests." in result

    def test_system_prompt_longer_with_context(self, tmp_path):
        (tmp_path / "MINION.md").write_text("Extra instructions here.")
        ctx = build_project_context(tmp_path)
        result_with = build_system_prompt(ctx)
        result_without = build_system_prompt(None)
        assert len(result_with) > len(result_without)
