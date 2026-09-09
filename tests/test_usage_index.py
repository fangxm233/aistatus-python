# input: usage JSONL files read repeatedly through UsageStorage and UsageTracker
# output: regression coverage that cached parsing stays correct and stops re-reading files
# pos: usage aggregation caching test suite
# >>> 一旦我被更新，务必更新我的开头注释，以及所属文件夹的 CLAUDE.md <<<
"""Tests for cached usage-file parsing.

Every usage query re-read and re-parsed the whole month file, and `cost_breakdown` did it three
times over. Parsed files are now reused while unchanged — the risk of which is staleness, so these
tests care mainly about the cache noticing writes.
"""

from __future__ import annotations

import json

from aistatus.usage import UsageTracker
from aistatus.usage_storage import UsageStorage


def _tracker(tmp_path) -> UsageTracker:
    return UsageTracker(storage=UsageStorage(base_dir=tmp_path, cwd="/test/proj"))


def _record(tracker: UsageTracker, model: str, cost: float, **kwargs) -> None:
    tracker.record_usage(
        provider=kwargs.get("provider", "anthropic"),
        model=model,
        input_tokens=kwargs.get("input_tokens", 100),
        output_tokens=kwargs.get("output_tokens", 20),
        latency_ms=kwargs.get("latency_ms", 50),
        fallback=kwargs.get("fallback", False),
        cost=cost,
    )


class TestCacheCorrectness:
    def test_repeated_reads_agree(self, tmp_path):
        tracker = _tracker(tmp_path)
        _record(tracker, "claude-opus-5", 1.0)
        _record(tracker, "gpt-5.6", 2.0, provider="openai")

        first = tracker.storage.read("all")
        assert tracker.storage.read("all") == first

    def test_a_new_record_is_seen_immediately(self, tmp_path):
        # The cache is keyed on file size and mtime; an append must invalidate it.
        tracker = _tracker(tmp_path)
        _record(tracker, "claude-opus-5", 1.0)
        assert len(tracker.storage.read("all")) == 1

        _record(tracker, "claude-opus-5", 1.0)
        assert len(tracker.storage.read("all")) == 2

    def test_an_external_write_is_seen(self, tmp_path):
        tracker = _tracker(tmp_path)
        _record(tracker, "claude-opus-5", 1.0)
        tracker.storage.read("all")

        # Another process appending to the same file, as the flock in `append` anticipates.
        month_file = next(p for p in tmp_path.rglob("*.jsonl"))
        with month_file.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({
                "ts": "2099-01-01T00:00:00Z", "provider": "openai", "model": "gpt-5.6",
                "in": 1, "out": 1, "cost": 0.5, "latency_ms": 1, "fallback": False,
            }) + "\n")

        models = {record["model"] for record in tracker.storage.read("all")}
        assert models == {"claude-opus-5", "gpt-5.6"}

    def test_aggregates_match_a_cold_reader(self, tmp_path):
        tracker = _tracker(tmp_path)
        for index in range(20):
            _record(tracker, f"model-{index % 3}", 0.5, provider=f"p{index % 2}")

        cold = UsageTracker(storage=UsageStorage(base_dir=tmp_path, cwd="/test/proj"))
        assert tracker.summary(period="all") == cold.summary(period="all")
        assert tracker.by_model(period="all") == cold.by_model(period="all")
        assert tracker.by_provider(period="all") == cold.by_provider(period="all")

    def test_period_filtering_is_not_cached_across_periods(self, tmp_path):
        # Only the parse is cached; the period filter must still be applied per query.
        tracker = _tracker(tmp_path)
        _record(tracker, "claude-opus-5", 1.0)

        month_file = next(p for p in tmp_path.rglob("*.jsonl"))
        with month_file.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({
                "ts": "2000-01-01T00:00:00Z", "provider": "anthropic", "model": "old",
                "in": 1, "out": 1, "cost": 0.1, "latency_ms": 1, "fallback": False,
            }) + "\n")

        assert len(tracker.storage.read("all")) == 2
        assert {r["model"] for r in tracker.storage.read("month")} == {"claude-opus-5"}


class TestReport:
    def test_matches_the_three_separate_queries(self, tmp_path):
        tracker = _tracker(tmp_path)
        for index in range(10):
            _record(tracker, f"model-{index % 3}", 0.25, provider=f"p{index % 2}")

        report = tracker.report(period="all")
        assert report["summary"] == tracker.summary(period="all")
        assert report["providers"] == tracker.by_provider(period="all")
        assert report["models"] == tracker.by_model(period="all")

    def test_cost_breakdown_is_the_same_report(self, tmp_path):
        tracker = _tracker(tmp_path)
        _record(tracker, "claude-opus-5", 1.0)
        assert tracker.cost_breakdown(period="all") == tracker.report(period="all")


class TestReadsAreAvoided:
    def test_an_unchanged_file_is_not_re_read(self, tmp_path, monkeypatch):
        tracker = _tracker(tmp_path)
        for index in range(5):
            _record(tracker, "claude-opus-5", 1.0)
        tracker.storage.read("all")  # warm

        reads = 0
        original = type(tmp_path).read_text

        def counting_read_text(self, *args, **kwargs):
            nonlocal reads
            if self.suffix == ".jsonl":
                reads += 1
            return original(self, *args, **kwargs)

        monkeypatch.setattr(type(tmp_path), "read_text", counting_read_text)
        tracker.report(period="all")

        # Three aggregations over one warm file used to mean three full re-parses.
        assert reads == 0

    def test_prewarm_parses_everything_once(self, tmp_path):
        tracker = _tracker(tmp_path)
        for index in range(7):
            _record(tracker, "claude-opus-5", 1.0)

        cold = UsageStorage(base_dir=tmp_path, cwd="/test/proj")
        assert cold.prewarm() == 7
        assert len(cold.read("all")) == 7
