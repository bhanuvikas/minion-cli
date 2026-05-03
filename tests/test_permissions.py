"""Tests for minion/permissions.py — the tiered allow-list trust system."""

import threading
from pathlib import Path

import pytest

from minion.permissions import (
    PermissionStore,
    _suggest_command_patterns,
    _suggest_path_patterns,
    _suggest_url_patterns,
    split_compound,
    suggest_patterns_for_tool,
)


# ─── TestSplitCompound ────────────────────────────────────────────────────────

class TestSplitCompound:
    def test_simple_command_unchanged(self):
        assert split_compound("ls") == ["ls"]

    def test_no_operators_returns_list_of_one(self):
        assert split_compound("python -m pytest") == ["python -m pytest"]

    def test_double_ampersand(self):
        assert split_compound("cd /tmp && ls") == ["cd /tmp", "ls"]

    def test_semicolon(self):
        assert split_compound("cd /tmp; ls") == ["cd /tmp", "ls"]

    def test_triple_compound(self):
        result = split_compound("cd /tmp && echo hi; pwd")
        assert result == ["cd /tmp", "echo hi", "pwd"]

    def test_no_split_on_pipe(self):
        assert split_compound("ls | grep foo") == ["ls | grep foo"]

    def test_respects_single_quotes(self):
        # Semicolon inside single quotes must not split
        assert split_compound("echo 'hello; world'") == ["echo 'hello; world'"]

    def test_respects_double_quotes(self):
        # && inside double quotes must not split
        assert split_compound('echo "x && y"') == ['echo "x && y"']

    def test_empty_parts_dropped(self):
        # Double semicolons produce an empty middle part — should be dropped
        assert split_compound("ls;; pwd") == ["ls", "pwd"]

    def test_whitespace_stripped(self):
        assert split_compound("  ls  &&  pwd  ") == ["ls", "pwd"]


# ─── TestSuggestCommandPatterns ───────────────────────────────────────────────

class TestSuggestCommandPatterns:
    def test_single_token_returns_exact_only(self):
        patterns = _suggest_command_patterns("ls")
        assert patterns == ["ls"]

    def test_two_tokens(self):
        patterns = _suggest_command_patterns("git status")
        assert patterns == ["git status", "git *"]

    def test_exact_always_first(self):
        patterns = _suggest_command_patterns("pytest tests/")
        assert patterns[0] == "pytest tests/"

    def test_five_tokens_capped_at_five_patterns(self):
        cmd = "python -m pytest tests/ -v --tb=short"
        patterns = _suggest_command_patterns(cmd)
        assert len(patterns) <= 5
        assert patterns[0] == cmd

    def test_relaxation_order(self):
        patterns = _suggest_command_patterns("python -m pytest")
        # Strict first, then progressively looser
        assert patterns[0] == "python -m pytest"
        assert patterns[1] == "python -m *"
        assert patterns[2] == "python *"

    def test_unmatched_quotes_fallback_to_exact(self):
        cmd = "echo 'unclosed"
        patterns = _suggest_command_patterns(cmd)
        assert patterns == [cmd]


# ─── TestSuggestUrlPatterns ───────────────────────────────────────────────────

class TestSuggestUrlPatterns:
    def test_url_with_deep_path(self):
        url = "https://docs.python.org/3/library/os.html"
        patterns = _suggest_url_patterns(url)
        assert patterns[0] == url
        assert "https://docs.python.org/3/library/*" in patterns
        assert "https://docs.python.org/*" in patterns

    def test_url_root_path(self):
        url = "https://docs.python.org"
        patterns = _suggest_url_patterns(url)
        assert patterns[0] == url
        assert "https://docs.python.org/*" in patterns

    def test_exact_always_first(self):
        url = "https://example.com/a/b/c"
        patterns = _suggest_url_patterns(url)
        assert patterns[0] == url


# ─── TestSuggestPathPatterns ──────────────────────────────────────────────────

class TestSuggestPathPatterns:
    def test_file_with_extension(self):
        patterns = _suggest_path_patterns("src/main.py")
        assert patterns[0] == "src/main.py"
        assert "src/*.py" in patterns
        assert "src/*" in patterns

    def test_file_no_extension(self):
        patterns = _suggest_path_patterns("Makefile")
        assert patterns[0] == "Makefile"
        # No extension and no meaningful parent — only exact
        assert len(patterns) == 1

    def test_root_level_file(self):
        patterns = _suggest_path_patterns("/tmp/test.py")
        assert patterns[0] == "/tmp/test.py"
        assert "/tmp/*.py" in patterns
        assert "/tmp/*" in patterns

    def test_exact_always_first(self):
        patterns = _suggest_path_patterns("tests/test_foo.py")
        assert patterns[0] == "tests/test_foo.py"


# ─── TestSuggestPatternsForTool ───────────────────────────────────────────────

class TestSuggestPatternsForTool:
    def test_dispatches_run_shell(self):
        patterns = suggest_patterns_for_tool("run_shell", "pytest tests/")
        assert patterns[0] == "pytest tests/"

    def test_dispatches_web_fetch(self):
        patterns = suggest_patterns_for_tool("web_fetch", "https://example.com/page")
        assert patterns[0] == "https://example.com/page"

    def test_dispatches_write_file(self):
        patterns = suggest_patterns_for_tool("write_file", "src/foo.py")
        assert patterns[0] == "src/foo.py"

    def test_dispatches_edit_file(self):
        patterns = suggest_patterns_for_tool("edit_file", "src/bar.py")
        assert patterns[0] == "src/bar.py"

    def test_unknown_tool_exact_only(self):
        patterns = suggest_patterns_for_tool("unknown_tool", "some value")
        assert patterns == ["some value"]


# ─── TestPatternMatching ──────────────────────────────────────────────────────

class TestPatternMatching:
    def _store_with_rule(self, tool, pattern):
        store = PermissionStore(project_cwd=None)
        store.add_rule(tool, pattern, "session")
        return store

    def test_exact_match(self):
        store = self._store_with_rule("run_shell", "pytest tests/")
        assert store.is_trusted("run_shell", "pytest tests/")

    def test_wildcard_suffix_matches(self):
        store = self._store_with_rule("run_shell", "pytest *")
        assert store.is_trusted("run_shell", "pytest tests/ -v")

    def test_wildcard_no_match(self):
        store = self._store_with_rule("run_shell", "pytest *")
        assert not store.is_trusted("run_shell", "rm -rf /")

    def test_fnmatch_glob(self):
        store = self._store_with_rule("edit_file", "tests/*")
        assert store.is_trusted("edit_file", "tests/test_foo.py")
        assert not store.is_trusted("edit_file", "src/foo.py")


# ─── TestPermissionStore ──────────────────────────────────────────────────────

class TestPermissionStore:
    def test_empty_store_returns_untrusted(self):
        store = PermissionStore(project_cwd=None)
        assert not store.is_trusted("run_shell", "ls")

    def test_session_rule_trusted(self):
        store = PermissionStore(project_cwd=None)
        store.add_rule("run_shell", "ls", "session")
        assert store.is_trusted("run_shell", "ls")

    def test_session_rule_wrong_tool_untrusted(self):
        store = PermissionStore(project_cwd=None)
        store.add_rule("run_shell", "ls", "session")
        assert not store.is_trusted("web_fetch", "ls")

    def test_compound_all_trusted(self):
        store = PermissionStore(project_cwd=None)
        store.add_rule("run_shell", "cd *", "session")
        store.add_rule("run_shell", "pytest *", "session")
        assert store.is_trusted("run_shell", "cd /tmp && pytest tests/")

    def test_compound_one_untrusted_returns_false(self):
        store = PermissionStore(project_cwd=None)
        store.add_rule("run_shell", "cd *", "session")
        # pytest has no rule → compound is untrusted
        assert not store.is_trusted("run_shell", "cd /tmp && pytest tests/")

    def test_add_project_rule_writes_file(self, tmp_path):
        store = PermissionStore(project_cwd=tmp_path)
        store.add_rule("run_shell", "pytest *", "project")
        perm_file = tmp_path / ".minion" / "permissions.toml"
        assert perm_file.exists()
        text = perm_file.read_text()
        assert "pytest *" in text

    def test_add_global_rule_writes_file(self, tmp_path):
        global_path = tmp_path / "permissions.toml"
        store = PermissionStore(project_cwd=None)
        store._global_path = global_path
        store.add_rule("run_shell", "git *", "global")
        assert global_path.exists()
        assert "git *" in global_path.read_text()

    def test_load_from_file(self, tmp_path):
        perm_file = tmp_path / ".minion" / "permissions.toml"
        perm_file.parent.mkdir(parents=True)
        perm_file.write_text('[allow]\nrun_shell = ["pytest *"]\n')
        store = PermissionStore(project_cwd=tmp_path)
        assert store.is_trusted("run_shell", "pytest tests/ -v")

    def test_project_gitignore_created(self, tmp_path):
        store = PermissionStore(project_cwd=tmp_path)
        store.add_rule("run_shell", "pytest *", "project")
        gitignore = tmp_path / ".minion" / ".gitignore"
        assert gitignore.exists()
        assert "permissions.toml" in gitignore.read_text().splitlines()

    def test_project_gitignore_not_duplicated(self, tmp_path):
        store = PermissionStore(project_cwd=tmp_path)
        store.add_rule("run_shell", "pytest *", "project")
        store.add_rule("run_shell", "git *", "project")
        gitignore = tmp_path / ".minion" / ".gitignore"
        lines = gitignore.read_text().splitlines()
        assert lines.count("permissions.toml") == 1

    def test_add_rule_thread_safe(self):
        store = PermissionStore(project_cwd=None)
        errors: list[Exception] = []

        def _add(i: int) -> None:
            try:
                store.add_rule("run_shell", f"cmd_{i}", "session")
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=_add, args=(i,)) for i in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors
        assert len(store._session.get("run_shell", [])) == 20

    def test_session_rule_not_persisted(self, tmp_path):
        store = PermissionStore(project_cwd=tmp_path)
        store.add_rule("run_shell", "pytest *", "session")
        perm_file = tmp_path / ".minion" / "permissions.toml"
        assert not perm_file.exists()

    def test_duplicate_rules_not_added(self):
        store = PermissionStore(project_cwd=None)
        store.add_rule("run_shell", "pytest *", "session")
        store.add_rule("run_shell", "pytest *", "session")
        assert store._session["run_shell"].count("pytest *") == 1
