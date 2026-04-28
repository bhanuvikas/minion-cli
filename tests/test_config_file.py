"""Tests for minion/config_file.py — TOML config loading and priority merging."""

from __future__ import annotations

from pathlib import Path

import pytest

from minion.config_file import (
    MinionConfig,
    load_config,
    format_config,
)


def _write(tmp_path: Path, content: str) -> Path:
    p = tmp_path / "config.toml"
    p.write_text(content, encoding="utf-8")
    return p


class TestLoadConfigDefaults:
    def test_no_file_returns_defaults(self, tmp_path):
        cfg = load_config(path=tmp_path / "nonexistent.toml")
        assert isinstance(cfg, MinionConfig)
        assert cfg.llm.provider is None
        assert cfg.llm.model is None
        assert cfg.agent.reflect_depth == 0
        assert cfg.agent.verbose is False
        assert cfg.agent.debug is False
        assert cfg.agent.agents_enabled is True
        assert cfg.agent.max_subagent_depth == 2
        assert cfg.memory.enabled is True
        assert cfg.memory.top_k == 5
        assert cfg.memory.similarity_threshold == 0.70
        assert cfg.memory.consolidation_threshold == 0.70
        assert cfg.memory.extraction_trigger == "substantial"
        assert cfg.memory.extraction_min_words == 50
        assert cfg.a2a.auth_token == ""
        assert cfg.tracing.enabled is True

    def test_empty_toml_returns_defaults(self, tmp_path):
        p = _write(tmp_path, "")
        cfg = load_config(path=p)
        assert cfg.agent.reflect_depth == 0

    def test_malformed_toml_returns_defaults(self, tmp_path):
        p = _write(tmp_path, "not valid toml ][[[")
        cfg = load_config(path=p)
        assert cfg.agent.reflect_depth == 0


class TestLoadConfigLLM:
    def test_provider_and_model(self, tmp_path):
        p = _write(tmp_path, '[llm]\nprovider = "openai"\nmodel = "gpt-4o"')
        cfg = load_config(path=p)
        assert cfg.llm.provider == "openai"
        assert cfg.llm.model == "gpt-4o"

    def test_partial_llm_section(self, tmp_path):
        p = _write(tmp_path, '[llm]\nprovider = "anthropic"')
        cfg = load_config(path=p)
        assert cfg.llm.provider == "anthropic"
        assert cfg.llm.model is None  # not set → None


class TestLoadConfigAgent:
    def test_reflect_depth(self, tmp_path):
        p = _write(tmp_path, "[agent]\nreflect_depth = 2")
        cfg = load_config(path=p)
        assert cfg.agent.reflect_depth == 2

    def test_verbose_and_debug(self, tmp_path):
        p = _write(tmp_path, "[agent]\nverbose = true\ndebug = true")
        cfg = load_config(path=p)
        assert cfg.agent.verbose is True
        assert cfg.agent.debug is True

    def test_agents_disabled(self, tmp_path):
        p = _write(tmp_path, "[agent]\nagents_enabled = false")
        cfg = load_config(path=p)
        assert cfg.agent.agents_enabled is False

    def test_max_subagent_depth(self, tmp_path):
        p = _write(tmp_path, "[agent]\nmax_subagent_depth = 5")
        cfg = load_config(path=p)
        assert cfg.agent.max_subagent_depth == 5


class TestLoadConfigMemory:
    def test_top_k(self, tmp_path):
        p = _write(tmp_path, "[memory]\ntop_k = 10")
        cfg = load_config(path=p)
        assert cfg.memory.top_k == 10

    def test_thresholds(self, tmp_path):
        p = _write(tmp_path, "[memory]\nsimilarity_threshold = 0.85\nconsolidation_threshold = 0.90")
        cfg = load_config(path=p)
        assert cfg.memory.similarity_threshold == pytest.approx(0.85)
        assert cfg.memory.consolidation_threshold == pytest.approx(0.90)

    def test_extraction_trigger_valid(self, tmp_path):
        for trigger in ("substantial", "every_5", "manual", "always"):
            p = _write(tmp_path, f'[memory]\nextraction_trigger = "{trigger}"')
            cfg = load_config(path=p)
            assert cfg.memory.extraction_trigger == trigger

    def test_extraction_trigger_invalid_falls_back(self, tmp_path):
        p = _write(tmp_path, '[memory]\nextraction_trigger = "never_heard_of_this"')
        cfg = load_config(path=p)
        assert cfg.memory.extraction_trigger == "substantial"

    def test_memory_disabled(self, tmp_path):
        p = _write(tmp_path, "[memory]\nenabled = false")
        cfg = load_config(path=p)
        assert cfg.memory.enabled is False

    def test_extraction_min_words(self, tmp_path):
        p = _write(tmp_path, "[memory]\nextraction_min_words = 100")
        cfg = load_config(path=p)
        assert cfg.memory.extraction_min_words == 100


class TestLoadConfigA2A:
    def test_auth_token(self, tmp_path):
        p = _write(tmp_path, '[a2a]\nauth_token = "secret123"')
        cfg = load_config(path=p)
        assert cfg.a2a.auth_token == "secret123"


class TestLoadConfigTracing:
    def test_tracing_disabled(self, tmp_path):
        p = _write(tmp_path, "[tracing]\nenabled = false")
        cfg = load_config(path=p)
        assert cfg.tracing.enabled is False


class TestLoadConfigFull:
    def test_full_config(self, tmp_path):
        content = """
[llm]
provider = "anthropic"
model = "claude-opus-4-7"

[agent]
reflect_depth = 1
verbose = true
debug = false
agents_enabled = true
max_subagent_depth = 3

[memory]
enabled = true
top_k = 8
similarity_threshold = 0.75
consolidation_threshold = 0.80
extraction_trigger = "always"
extraction_min_words = 30

[a2a]
auth_token = "tok_abc"

[tracing]
enabled = false
"""
        p = _write(tmp_path, content)
        cfg = load_config(path=p)
        assert cfg.llm.provider == "anthropic"
        assert cfg.llm.model == "claude-opus-4-7"
        assert cfg.agent.reflect_depth == 1
        assert cfg.agent.verbose is True
        assert cfg.agent.max_subagent_depth == 3
        assert cfg.memory.top_k == 8
        assert cfg.memory.extraction_trigger == "always"
        assert cfg.a2a.auth_token == "tok_abc"
        assert cfg.tracing.enabled is False

    def test_unknown_keys_ignored(self, tmp_path):
        p = _write(tmp_path, "[llm]\nunknown_future_key = true\nprovider = 'anthropic'")
        cfg = load_config(path=p)
        assert cfg.llm.provider == "anthropic"


class TestFormatConfig:
    def test_format_shows_all_sections(self, tmp_path):
        cfg = load_config(path=tmp_path / "nonexistent.toml")
        text = format_config(cfg)
        assert "[llm]" in text
        assert "[agent]" in text
        assert "[memory]" in text
        assert "[a2a]" in text
        assert "[tracing]" in text

    def test_format_auth_token_masked(self, tmp_path):
        p = _write(tmp_path, '[a2a]\nauth_token = "supersecret"')
        cfg = load_config(path=p)
        text = format_config(cfg)
        assert "supersecret" not in text
        assert "(set)" in text

    def test_format_empty_auth_token(self, tmp_path):
        cfg = load_config(path=tmp_path / "nonexistent.toml")
        text = format_config(cfg)
        assert "(not set)" in text
