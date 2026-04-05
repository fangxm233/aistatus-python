# input: pytest, tempfile/path helpers, and aistatus.pricing CostCalculator
# output: regression coverage for Claude versioned pricing lookup and atomic pricing cache writes
# pos: SDK pricing lookup tests for versioned model IDs, base aliases, and cache persistence behavior
# >>> 一旦我被更新，务必更新我的开头注释，以及所属文件夹的 CLAUDE.md <<<

from __future__ import annotations

import json

from aistatus.pricing import CostCalculator


class TestPricingLookup:
    def test_candidate_queries_include_base_alias_for_versioned_claude_model(self):
        calculator = CostCalculator(ttl_seconds=0)

        queries = calculator._candidate_queries("claude-haiku-4-5-20251001")

        assert "claude-haiku-4-5-20251001" in queries
        assert "claude-haiku-4.5" in queries

    def test_pick_model_match_accepts_base_alias_for_versioned_claude_model(self):
        calculator = CostCalculator(ttl_seconds=0)

        match = calculator._pick_model_match(
            "anthropic",
            "claude-haiku-4-5-20251001",
            [
                {
                    "id": "anthropic/claude-haiku-4.5",
                    "pricing": {"prompt": 0.000001, "completion": 0.000005},
                }
            ],
        )



class TestPricingCacheWrites:
    def test_write_file_cache_uses_atomic_replace(self, tmp_path, monkeypatch):
        calculator = CostCalculator(ttl_seconds=0)
        calculator._cache_path = tmp_path / "pricing-cache.json"
        replaced = []
        monkeypatch.setattr("aistatus.pricing.os.replace", lambda src, dst: replaced.append((src, dst)))

        calculator._write_file_cache({"anthropic/claude": {"ts": 1, "pricing": {"input_per_million": 1.0}}})

        assert len(replaced) == 1
        src, dst = replaced[0]
        assert dst == calculator._cache_path
        assert src != dst
