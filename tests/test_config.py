"""Tests for the .env read/write utility in minion/config.py.

We only test the pure data-manipulation logic (update_env_values).
The interactive questionary flow is not tested here — it requires a TTY
and is covered by manual testing.
"""

import os
import pytest
from pathlib import Path

from minion.config import update_env_values, PROVIDER_KEY_MAP, PROVIDERS


class TestUpdateEnvValues:
    def test_updates_existing_key(self, tmp_path, monkeypatch):
        env_file = tmp_path / ".env"
        env_file.write_text("MINION_PROVIDER=anthropic\nANTHROPIC_API_KEY=old-key\n")
        monkeypatch.chdir(tmp_path)

        update_env_values({"ANTHROPIC_API_KEY": "new-key"})

        content = env_file.read_text()
        assert "ANTHROPIC_API_KEY=new-key" in content
        assert "old-key" not in content

    def test_preserves_comments(self, tmp_path, monkeypatch):
        env_file = tmp_path / ".env"
        env_file.write_text(
            "# This is a comment\nMINION_PROVIDER=anthropic\n# Another comment\n"
        )
        monkeypatch.chdir(tmp_path)

        update_env_values({"MINION_PROVIDER": "openai"})

        content = env_file.read_text()
        assert "# This is a comment" in content
        assert "# Another comment" in content

    def test_preserves_unrelated_keys(self, tmp_path, monkeypatch):
        env_file = tmp_path / ".env"
        env_file.write_text("MINION_PROVIDER=anthropic\nOPENAI_API_KEY=sk-abc\n")
        monkeypatch.chdir(tmp_path)

        update_env_values({"MINION_PROVIDER": "openai"})

        content = env_file.read_text()
        assert "OPENAI_API_KEY=sk-abc" in content

    def test_appends_new_key(self, tmp_path, monkeypatch):
        env_file = tmp_path / ".env"
        env_file.write_text("MINION_PROVIDER=anthropic\n")
        monkeypatch.chdir(tmp_path)

        update_env_values({"MINION_MODEL": "claude-opus-4-5"})

        content = env_file.read_text()
        assert "MINION_MODEL=claude-opus-4-5" in content

    def test_updates_multiple_keys_at_once(self, tmp_path, monkeypatch):
        env_file = tmp_path / ".env"
        env_file.write_text("MINION_PROVIDER=anthropic\nMINION_MODEL=claude-sonnet-4-5\n")
        monkeypatch.chdir(tmp_path)

        update_env_values({"MINION_PROVIDER": "openai", "MINION_MODEL": "gpt-4o"})

        content = env_file.read_text()
        assert "MINION_PROVIDER=openai" in content
        assert "MINION_MODEL=gpt-4o" in content

    def test_creates_env_from_example(self, tmp_path, monkeypatch):
        """When .env doesn't exist but .env.example does, it should be created."""
        example = tmp_path / ".env.example"
        example.write_text("MINION_PROVIDER=anthropic\n")
        monkeypatch.chdir(tmp_path)

        update_env_values({"MINION_PROVIDER": "openai"})

        assert (tmp_path / ".env").exists()

    def test_blank_lines_preserved(self, tmp_path, monkeypatch):
        env_file = tmp_path / ".env"
        env_file.write_text("KEY_A=1\n\nKEY_B=2\n")
        monkeypatch.chdir(tmp_path)

        update_env_values({"KEY_A": "10"})

        content = env_file.read_text()
        assert "\n\n" in content  # blank line preserved


class TestProviderConstants:
    def test_all_providers_have_key_mapping(self):
        for provider in PROVIDERS:
            assert provider in PROVIDER_KEY_MAP

    def test_key_names_are_uppercase_env_vars(self):
        for key in PROVIDER_KEY_MAP.values():
            assert key == key.upper()
            assert "API_KEY" in key
