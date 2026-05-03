# Prompt Caching — Baseline & Expected Improvement

## When this was recorded
2026-05-02 — after implementing caching improvements, before dashboard data updated.

---

## What was implemented

### Phase 1 — Caching infrastructure (commit `5f2cd1d`)
Added `cache_control: {"type": "ephemeral"}` to:
- Last tool in the tool definitions list (so full tool list gets cached)
- Static system prompt block (`system=` parameter)

Split system prompt into two parameters:
- `system=` — static base prompt (cached): BASE_SYSTEM_PROMPT + ProjectContext + SUBAGENT_GUIDANCE + A2A_GUIDANCE
- `system_dynamic=` — dynamic per-turn context (NOT cached): memory block + active plan block

### Phase 2 — Cache threshold fix + prompt quality (commit `1954226`)
Expanded prompts to push static prefix above the 2048-token minimum cacheable threshold.

---

## Token counts (before vs. after)

| Component | Before | After |
|---|---|---|
| Tools (8 tools) | ~1,461 | ~1,461 (unchanged) |
| BASE_SYSTEM_PROMPT | ~356 | ~599 |
| SUBAGENT_GUIDANCE | ~205 | ~409 |
| **Static prefix total** | **~2,047** | **~2,469** |
| Threshold (Haiku 4.5 + Sonnet 4.6) | 2,048 | 2,048 |
| **Margin** | **-1 (BELOW)** | **+421 (above)** |

Verified with:
```
Tools:             1,461 tokens
BASE_SYSTEM:         599 tokens
SUBAGENT_GUIDANCE:   409 tokens
Total:             2,469 tokens  ✓ PASS
```

---

## Dashboard snapshot before improvements

| Metric | Value |
|---|---|
| Cache read ratio | 6.5% |
| Write amortization | 4.21× |
| Cache read tokens | 619K |
| Total input tokens | ~9.7M |
| Model | Claude Haiku 4.5 |

### Why 6.5% was happening
The static prefix was **1 token below** the 2048-token minimum cacheable threshold.
`cache_control` markers were being sent on every request but Anthropic silently
no-ops them when the prefix is below threshold. The only turns that hit the cache
were ones where A2A guidance was also appended, pushing it just over 2048.

---

## Expected metrics after improvements

| Metric | Before | Expected after |
|---|---|---|
| Cache read ratio | 6.5% | 60–80% |
| Write amortization | 4.21× | similar or higher |
| Cache read tokens | 619K (period) | much higher per period |

**Why the jump:** Every turn after the first in a session will now be a cache read
(static prefix is stable at 2,469 tokens, well above threshold). Previously almost
no turns were cache reads.

Cache read tokens cost ~10% of normal input token price, so a 70% cache read ratio
means ~63% reduction in effective input token cost for the cached prefix portion.

---

## Files changed

- `minion/llm/anthropic.py` — `cache_control` on tools and system blocks
- `minion/llm/base.py` — `system_dynamic` param added to `stream()` / `async_stream()`
- `minion/llm/openai.py` — `system_dynamic` param (combined, no caching for OpenAI)
- `minion/runner.py` — threads `system_dynamic` through, fixes `system_prompt_tokens`
- `minion/repl.py` — extracts memory+plan into `system_dynamic`
- `minion/prompts.py` — BASE_SYSTEM_PROMPT expanded (+243 tokens)
- `minion/agents/__init__.py` — SUBAGENT_GUIDANCE expanded (+204 tokens)
- `minion/a2a/__init__.py` — A2A_REMOTE_GUIDANCE expanded (+3 lines)
- `minion/agents/builtin/*.yaml` — all 4 roles: no-clarifying-questions rule added
