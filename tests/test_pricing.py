# input: pytest, aistatus.pricing CostCalculator
# output: regression coverage for Claude versioned pricing lookup
# pos: SDK pricing lookup tests for versioned and aliased model IDs
# >>> 一旦我被更新，务必更新我的开头注释，以及所属文件夹的 CLAUDE.md <<<

from __future__ import annotations

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

        assert match is not None
        assert match["id"] == "anthropic/claude-haiku-4.5"
