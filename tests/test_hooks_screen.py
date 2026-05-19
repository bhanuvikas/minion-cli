"""Tests for hooks TUI screen helpers and persist utilities."""

from __future__ import annotations

from pathlib import Path

import pytest

from minion.hooks.manifest import load_manifest
from minion.hooks.persist import create_hook_yaml, update_hook_yaml


# ── Helper to import name-check logic ─────────────────────────────────────────

def _check_name_available_in_tier(name: str, tier_dir: Path) -> tuple[bool, str]:
    """Replicate HooksScreen._check_name_available() without instantiating the screen."""
    import re
    _SLUG_RE = re.compile(r'^[a-z][a-z0-9-]*$')
    if not _SLUG_RE.match(name):
        return False, "name must match [a-z][a-z0-9-]+"
    path = tier_dir / f"{name}.yaml"
    if path.exists():
        return False, "✗ name taken — pick another"
    return True, f"✓ available  {path}"


# ── Test 1: YAML round-trip ───────────────────────────────────────────────────

def test_yaml_round_trip(tmp_path: Path) -> None:
    """create_hook_yaml + load_manifest round-trips all fields correctly."""
    path = tmp_path / "test-hook.yaml"
    create_hook_yaml(
        path,
        name="test-hook",
        event="PreToolUse",
        command="echo hello",
        tools=["run_shell"],
        description="test description",
        timeout=15,
        blocking=True,
    )
    manifest = load_manifest(path, source="user")
    assert manifest.name == "test-hook"
    assert manifest.event == "PreToolUse"
    assert manifest.command == "echo hello"
    assert manifest.tools == ["run_shell"]
    assert manifest.description == "test description"
    assert manifest.timeout == 15
    assert manifest.blocking is True
    assert manifest.source == "user"
    assert manifest.source_path == path


def test_yaml_round_trip_minimal(tmp_path: Path) -> None:
    """create_hook_yaml with only required fields; optional fields absent from file."""
    path = tmp_path / "minimal.yaml"
    create_hook_yaml(path, name="minimal", event="StopTurn", command="./hook.sh")
    manifest = load_manifest(path, source="project")
    assert manifest.name == "minimal"
    assert manifest.event == "StopTurn"
    assert manifest.command == "./hook.sh"
    assert manifest.tools is None
    assert manifest.description == ""
    assert manifest.timeout == 30


def test_blocking_none_omitted(tmp_path: Path) -> None:
    """blocking=None (auto) must not appear in the YAML file."""
    import yaml
    path = tmp_path / "auto-blocking.yaml"
    create_hook_yaml(path, name="auto-blocking", event="PostToolUse", command="./hook.sh", blocking=None)
    raw = yaml.safe_load(path.read_text())
    assert "blocking" not in raw


def test_blocking_false_written(tmp_path: Path) -> None:
    """blocking=False must be stored in YAML and survive round-trip."""
    path = tmp_path / "no-block.yaml"
    create_hook_yaml(path, name="no-block", event="PreToolUse", command="./hook.sh", blocking=False)
    manifest = load_manifest(path)
    assert manifest.blocking is False


# ── Test 2: Name validation ───────────────────────────────────────────────────

import re as _re
_SLUG_RE = _re.compile(r'^[a-z][a-z0-9-]*$')


@pytest.mark.parametrize("name,expected", [
    ("my-hook",     True),
    ("a",           True),
    ("hook123",     True),
    ("log-tools-v2", True),
    ("",            False),   # empty
    ("MyHook",      False),   # uppercase
    ("1hook",       False),   # digit-first
    ("-hook",       False),   # dash-first
    ("hook_name",   False),   # underscore not allowed
    ("hook name",   False),   # space not allowed
    ("HOOK",        False),   # all uppercase
])
def test_name_slug_validation(name: str, expected: bool) -> None:
    if expected:
        assert _SLUG_RE.match(name), f"{name!r} should be valid"
    else:
        assert not _SLUG_RE.match(name), f"{name!r} should be invalid"


# ── Test 3: Name availability check ──────────────────────────────────────────

def test_name_available(tmp_path: Path) -> None:
    """Name is available when no YAML file exists in the tier dir."""
    tier_dir = tmp_path / ".minion" / "hooks"
    tier_dir.mkdir(parents=True)
    ok, msg = _check_name_available_in_tier("my-hook", tier_dir)
    assert ok is True
    assert "available" in msg


def test_name_taken(tmp_path: Path) -> None:
    """Name is taken when a YAML file already exists."""
    tier_dir = tmp_path / ".minion" / "hooks"
    tier_dir.mkdir(parents=True)
    (tier_dir / "my-hook.yaml").write_text("name: my-hook\n")
    ok, msg = _check_name_available_in_tier("my-hook", tier_dir)
    assert ok is False
    assert "taken" in msg


def test_name_invalid_slug_not_available(tmp_path: Path) -> None:
    """Invalid slug returns (False, validation msg) without hitting filesystem."""
    tier_dir = tmp_path / ".minion" / "hooks"
    # Do NOT create the dir — should fail before filesystem check
    ok, msg = _check_name_available_in_tier("BAD-name", tier_dir)
    assert ok is False
    assert "must match" in msg


# ── Test 4: update_hook_yaml ──────────────────────────────────────────────────

def test_update_hook_yaml(tmp_path: Path) -> None:
    """update_hook_yaml preserves unmodified fields and updates specified ones."""
    path = tmp_path / "log-tools.yaml"
    create_hook_yaml(
        path, name="log-tools", event="PostToolUse", command="./old.sh",
        description="old desc", timeout=60,
    )
    update_hook_yaml(path, updates={"command": "./new.sh", "timeout": 120})
    manifest = load_manifest(path)
    assert manifest.command == "./new.sh"
    assert manifest.timeout == 120
    assert manifest.description == "old desc"
    assert manifest.name == "log-tools"


def test_update_hook_yaml_removes_key_on_none(tmp_path: Path) -> None:
    """update_hook_yaml with value=None removes the key from YAML."""
    import yaml
    path = tmp_path / "remove-test.yaml"
    create_hook_yaml(
        path, name="remove-test", event="StopTurn", command="./hook.sh",
        description="some desc",
    )
    update_hook_yaml(path, updates={"description": None})
    raw = yaml.safe_load(path.read_text())
    assert "description" not in raw


