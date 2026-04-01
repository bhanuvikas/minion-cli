"""Tests for minion/context/ — manifest detection, file tree, ProjectContext.

All tests use tmp_path (pytest fixture) to create real but isolated directories.
No network calls, no LLM calls.
"""

import pytest
from pathlib import Path

from minion.context.manifest import detect_project, ProjectManifest
from minion.context.filetree import build_file_tree, IgnoreRules, ALWAYS_IGNORE
from minion.context.project import build_project_context, ProjectContext


# ─── Manifest detection ───────────────────────────────────────────────────────

class TestDetectPython:
    def test_detects_python_from_pyproject_toml(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text(
            '[project]\nname = "myapp"\nrequires-python = ">=3.12"\n'
            'dependencies = ["flask", "requests"]\n'
        )
        m = detect_project(tmp_path)
        assert m is not None
        assert "Python" in m.language
        assert m.framework == "Flask"

    def test_detects_python_version_from_requires_python(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text(
            '[project]\nname = "app"\nrequires-python = ">=3.11"\n'
        )
        m = detect_project(tmp_path)
        assert m is not None
        assert "3.11" in m.language

    def test_detects_poetry_style_deps(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text(
            '[tool.poetry]\nname = "app"\n'
            '[tool.poetry.dependencies]\npython = "^3.12"\nfastapi = "*"\n'
        )
        m = detect_project(tmp_path)
        assert m is not None
        assert m.framework == "FastAPI"

    def test_detects_python_from_setup_py(self, tmp_path):
        (tmp_path / "setup.py").write_text("from setuptools import setup\nsetup(name='app')")
        m = detect_project(tmp_path)
        assert m is not None
        assert "Python" in m.language

    def test_finds_python_entry_point(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text('[project]\nname = "app"\n')
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "main.py").write_text("# entry")
        m = detect_project(tmp_path)
        assert m is not None
        assert m.entry_point == "src/main.py"


class TestDetectNode:
    def test_detects_javascript_from_package_json(self, tmp_path):
        (tmp_path / "package.json").write_text(
            '{"name": "app", "dependencies": {"express": "^4.0.0"}}'
        )
        m = detect_project(tmp_path)
        assert m is not None
        assert m.language == "JavaScript"
        assert m.framework == "Express"

    def test_detects_typescript_when_tsconfig_present(self, tmp_path):
        (tmp_path / "package.json").write_text('{"name": "app"}')
        (tmp_path / "tsconfig.json").write_text("{}")
        m = detect_project(tmp_path)
        assert m is not None
        assert m.language == "TypeScript"

    def test_detects_typescript_from_dev_deps(self, tmp_path):
        (tmp_path / "package.json").write_text(
            '{"name": "app", "devDependencies": {"typescript": "^5.0"}}'
        )
        m = detect_project(tmp_path)
        assert m is not None
        assert m.language == "TypeScript"

    def test_detects_nextjs_framework(self, tmp_path):
        (tmp_path / "package.json").write_text(
            '{"name": "app", "dependencies": {"next": "^14.0", "react": "^18.0"}}'
        )
        m = detect_project(tmp_path)
        assert m is not None
        assert m.framework == "Next.js"


class TestDetectGo:
    def test_detects_go_from_go_mod(self, tmp_path):
        (tmp_path / "go.mod").write_text("module github.com/user/app\n\ngo 1.21\n")
        m = detect_project(tmp_path)
        assert m is not None
        assert "Go" in m.language
        assert "1.21" in m.language

    def test_finds_go_entry_point(self, tmp_path):
        (tmp_path / "go.mod").write_text("module app\ngo 1.21\n")
        (tmp_path / "main.go").write_text("package main")
        m = detect_project(tmp_path)
        assert m is not None
        assert m.entry_point == "main.go"


class TestDetectRust:
    def test_detects_rust_from_cargo_toml(self, tmp_path):
        (tmp_path / "Cargo.toml").write_text(
            '[package]\nname = "app"\nversion = "0.1.0"\nedition = "2021"\n'
        )
        m = detect_project(tmp_path)
        assert m is not None
        assert "Rust" in m.language
        assert "2021" in m.language


class TestDetectUnknown:
    def test_returns_none_for_unrecognised_project(self, tmp_path):
        (tmp_path / "README.txt").write_text("hello")
        assert detect_project(tmp_path) is None

    def test_returns_none_for_empty_directory(self, tmp_path):
        assert detect_project(tmp_path) is None


class TestManifestToText:
    def test_language_always_present(self):
        m = ProjectManifest(language="Python 3.12")
        assert "Python 3.12" in m.to_text()

    def test_framework_appended_to_language_line(self):
        m = ProjectManifest(language="Python", framework="Flask")
        text = m.to_text()
        assert "Python" in text
        assert "Flask" in text
        # Both on same line
        assert "Python" in text.splitlines()[0]
        assert "Flask" in text.splitlines()[0]

    def test_entry_point_on_own_line(self):
        m = ProjectManifest(language="Go", entry_point="main.go")
        lines = m.to_text().splitlines()
        assert any("main.go" in line for line in lines)

    def test_key_deps_truncated_to_six(self):
        m = ProjectManifest(language="JS", key_deps=[f"pkg{i}" for i in range(10)])
        text = m.to_text()
        # At most 6 packages shown in the deps line
        deps_line = next((l for l in text.splitlines() if "Key deps" in l), "")
        shown = [p.strip() for p in deps_line.split(":")[1].split(",")] if ":" in deps_line else []
        assert len(shown) <= 6


# ─── File tree ────────────────────────────────────────────────────────────────

class TestIgnoreRules:
    def test_always_ignore_names_are_excluded(self, tmp_path):
        for name in ALWAYS_IGNORE:
            rules = IgnoreRules()
            entry = tmp_path / name
            assert rules.is_ignored(entry, tmp_path)

    def test_regular_file_not_ignored_by_default(self, tmp_path):
        rules = IgnoreRules()
        assert not rules.is_ignored(tmp_path / "main.py", tmp_path)

    def test_loads_minionignore_patterns(self, tmp_path):
        (tmp_path / ".minionignore").write_text("secrets/\n*.log\n")
        rules = IgnoreRules.load(tmp_path)
        assert rules.is_ignored(tmp_path / "secrets", tmp_path)
        assert rules.is_ignored(tmp_path / "app.log", tmp_path)

    def test_loads_gitignore_patterns(self, tmp_path):
        (tmp_path / ".gitignore").write_text("generated/\n")
        rules = IgnoreRules.load(tmp_path)
        assert rules.is_ignored(tmp_path / "generated", tmp_path)

    def test_both_ignore_files_are_additive(self, tmp_path):
        (tmp_path / ".gitignore").write_text("from_git/\n")
        (tmp_path / ".minionignore").write_text("from_minion/\n")
        rules = IgnoreRules.load(tmp_path)
        assert rules.is_ignored(tmp_path / "from_git", tmp_path)
        assert rules.is_ignored(tmp_path / "from_minion", tmp_path)

    def test_comment_lines_are_skipped(self, tmp_path):
        (tmp_path / ".minionignore").write_text("# this is a comment\nreal_dir/\n")
        rules = IgnoreRules.load(tmp_path)
        assert not rules.is_ignored(tmp_path / "this is a comment", tmp_path)
        assert rules.is_ignored(tmp_path / "real_dir", tmp_path)

    def test_missing_ignore_files_are_tolerated(self, tmp_path):
        rules = IgnoreRules.load(tmp_path)   # no .gitignore / .minionignore
        assert rules.patterns == []


class TestBuildFileTree:
    def test_shows_files_and_dirs(self, tmp_path):
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "main.py").write_text("x")
        (tmp_path / "README.md").write_text("x")
        tree = build_file_tree(tmp_path)
        assert "src/" in tree
        assert "main.py" in tree
        assert "README.md" in tree

    def test_respects_max_depth(self, tmp_path):
        deep = tmp_path / "a" / "b" / "c" / "d"
        deep.mkdir(parents=True)
        (deep / "hidden.py").write_text("x")
        tree = build_file_tree(tmp_path, max_depth=2)
        assert "hidden.py" not in tree

    def test_excludes_always_ignore_dirs(self, tmp_path):
        (tmp_path / "__pycache__").mkdir()
        (tmp_path / "__pycache__" / "mod.cpython-312.pyc").write_text("x")
        tree = build_file_tree(tmp_path)
        assert "__pycache__" not in tree

    def test_excludes_minionignore_patterns(self, tmp_path):
        (tmp_path / ".minionignore").write_text("secrets/\n")
        (tmp_path / "secrets").mkdir()
        (tmp_path / "secrets" / "key.txt").write_text("x")
        tree = build_file_tree(tmp_path)
        assert "secrets" not in tree

    def test_empty_directory_returns_placeholder(self, tmp_path):
        result = build_file_tree(tmp_path)
        assert result == "(empty)"

    def test_truncation_message_when_entry_cap_reached(self, tmp_path):
        """When _MAX_ENTRIES is hit, a '... N more entries' line is appended."""
        from unittest.mock import patch
        for i in range(5):
            (tmp_path / f"file{i}.py").write_text("x")
        with patch("minion.context.filetree._MAX_ENTRIES", 2):
            tree = build_file_tree(tmp_path)
        assert "more entries not shown" in tree

    def test_per_directory_file_cap_shows_truncation(self, tmp_path):
        """Files beyond _MAX_FILES_PER_DIR in one dir are counted as truncated."""
        from unittest.mock import patch
        for i in range(5):
            (tmp_path / f"f{i}.py").write_text("x")
        with patch("minion.context.filetree._MAX_FILES_PER_DIR", 2):
            tree = build_file_tree(tmp_path)
        assert "more entries not shown" in tree


class TestIgnoreRulesRelativePath:
    def test_matches_relative_path_pattern(self, tmp_path):
        """A pattern like 'src/generated' should match via relative path, not bare name."""
        (tmp_path / ".minionignore").write_text("src/generated\n")
        src = tmp_path / "src"
        src.mkdir()
        generated = src / "generated"
        generated.mkdir()
        rules = IgnoreRules.load(tmp_path)
        # bare name "generated" does not match "src/generated", but relative path does
        assert rules.is_ignored(generated, tmp_path)


# ─── ProjectContext ───────────────────────────────────────────────────────────

class TestBuildProjectContext:
    def test_detects_manifest_when_fingerprint_present(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text('[project]\nname = "app"\n')
        ctx = build_project_context(tmp_path)
        assert ctx.manifest is not None
        assert "Python" in ctx.manifest.language

    def test_manifest_is_none_for_unknown_project(self, tmp_path):
        ctx = build_project_context(tmp_path)
        assert ctx.manifest is None

    def test_reads_minion_md_when_present(self, tmp_path):
        (tmp_path / "MINION.md").write_text("Run tests with: pytest")
        ctx = build_project_context(tmp_path)
        assert ctx.minion_md is not None
        assert "pytest" in ctx.minion_md

    def test_minion_md_is_none_when_absent(self, tmp_path):
        ctx = build_project_context(tmp_path)
        assert ctx.minion_md is None

    def test_file_tree_is_populated(self, tmp_path):
        (tmp_path / "app.py").write_text("x")
        ctx = build_project_context(tmp_path)
        assert "app.py" in ctx.file_tree

    def test_cwd_is_set(self, tmp_path):
        ctx = build_project_context(tmp_path)
        assert ctx.cwd == tmp_path


class TestProjectContextToPromptBlock:
    def test_includes_project_context_header(self, tmp_path):
        ctx = build_project_context(tmp_path)
        block = ctx.to_prompt_block()
        assert "## Project Context" in block

    def test_includes_manifest_text_when_present(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text(
            '[project]\nname = "app"\ndependencies = ["flask"]\n'
        )
        ctx = build_project_context(tmp_path)
        block = ctx.to_prompt_block()
        assert "Python" in block
        assert "Flask" in block

    def test_includes_minion_md_section_when_present(self, tmp_path):
        (tmp_path / "MINION.md").write_text("Always use type hints.")
        ctx = build_project_context(tmp_path)
        block = ctx.to_prompt_block()
        assert "Project Instructions" in block
        assert "Always use type hints." in block

    def test_no_minion_md_section_when_absent(self, tmp_path):
        ctx = build_project_context(tmp_path)
        block = ctx.to_prompt_block()
        assert "Project Instructions" not in block

    def test_label_includes_framework(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text(
            '[project]\nname = "app"\ndependencies = ["django"]\n'
        )
        ctx = build_project_context(tmp_path)
        assert "Django" in ctx.label

    def test_label_falls_back_to_dir_name(self, tmp_path):
        ctx = build_project_context(tmp_path)
        assert ctx.label == tmp_path.name
