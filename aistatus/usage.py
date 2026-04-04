# input: route responses, optional usage uploader, pricing lookup, and usage storage backend
# output: persisted usage records, aggregated usage summaries, and optional async upload fan-out
# pos: central usage tracking layer shared by router and gateway request flows
# >>> 一旦我被更新，务必更新我的开头注释，以及所属文件夹的 CLAUDE.md <<<

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Protocol

from .models import RouteResponse
from .pricing import CostCalculator
from .usage_storage import UsageStorage


class UsageUploadSink(Protocol):
    def upload(self, record: dict[str, Any]) -> None: ...


class UsageTracker:
    def __init__(
        self,
        storage: UsageStorage | None = None,
        cost_calculator: CostCalculator | None = None,
        uploader: UsageUploadSink | None = None,
    ):
        self.storage = storage or UsageStorage()
        self.cost_calculator = cost_calculator or CostCalculator()
        self.uploader = uploader

    def calculate_cost(self, response: RouteResponse) -> float:
        if response.cost_usd:
            return round(response.cost_usd, 8)
        # Use cache-aware cost if cache tokens are present
        if response.cache_creation_input_tokens or response.cache_read_input_tokens:
            return round(
                self.cost_calculator.calculate_cost_with_cache(
                    provider=response.provider_used,
                    model=response.model_used,
                    input_tokens=response.input_tokens,
                    output_tokens=response.output_tokens,
                    cache_creation_input_tokens=response.cache_creation_input_tokens,
                    cache_read_input_tokens=response.cache_read_input_tokens,
                ),
                8,
            )
        return round(
            self.cost_calculator.calculate_cost(
                provider=response.provider_used,
                model=response.model_used,
                input_tokens=response.input_tokens,
                output_tokens=response.output_tokens,
            ),
            8,
        )

    def record(self, response: RouteResponse, latency_ms: int) -> dict[str, Any]:
        provider = response.provider_used
        model = response.model_used
        cost = self.calculate_cost(response)
        record: dict[str, Any] = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "provider": provider,
            "model": model,
            "in": response.input_tokens,
            "out": response.output_tokens,
            "cost": round(cost, 8),
            "fallback": response.was_fallback,
            "latency_ms": latency_ms,
        }
        if response.cache_creation_input_tokens:
            record["cache_creation_in"] = response.cache_creation_input_tokens
        if response.cache_read_input_tokens:
            record["cache_read_in"] = response.cache_read_input_tokens
        self.storage.append(record)
        self._upload(record)
        return record

    def record_usage(
        self,
        *,
        provider: str,
        model: str,
        input_tokens: int,
        output_tokens: int,
        cache_creation_input_tokens: int = 0,
        cache_read_input_tokens: int = 0,
        latency_ms: int,
        fallback: bool,
        cost: float | None = None,
        billing_mode: str | None = None,
    ) -> dict[str, Any]:
        record_cost = cost
        if record_cost is None:
            if cache_creation_input_tokens or cache_read_input_tokens:
                record_cost = self.cost_calculator.calculate_cost_with_cache(
                    provider, model, input_tokens, output_tokens,
                    cache_creation_input_tokens, cache_read_input_tokens,
                )
            else:
                record_cost = self.cost_calculator.calculate_cost(provider, model, input_tokens, output_tokens)
        record: dict[str, Any] = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "provider": provider,
            "model": model,
            "in": input_tokens,
            "out": output_tokens,
            "cost": round(record_cost, 8),
            "fallback": fallback,
            "latency_ms": latency_ms,
        }
        if billing_mode:
            record["billing_mode"] = billing_mode
        if cache_creation_input_tokens:
            record["cache_creation_in"] = cache_creation_input_tokens
        if cache_read_input_tokens:
            record["cache_read_in"] = cache_read_input_tokens
        self.storage.append(record)
        self._upload(record)
        return record

    def summary(self, period: str = "month", all_projects: bool = False) -> dict[str, Any]:
        records = self.storage.read(period=period, all_projects=all_projects)
        total_requests = len(records)
        total_input = sum(int(r.get("in", 0) or 0) for r in records)
        total_output = sum(int(r.get("out", 0) or 0) for r in records)
        total_cost = round(sum(float(r.get("cost", 0.0) or 0.0) for r in records), 8)
        avg_latency = round(
            sum(int(r.get("latency_ms", 0) or 0) for r in records) / total_requests,
            2,
        ) if total_requests else 0.0
        fallback_count = sum(1 for r in records if r.get("fallback"))
        return {
            "period": period,
            "all_projects": all_projects,
            "requests": total_requests,
            "input_tokens": total_input,
            "output_tokens": total_output,
            "cost_usd": total_cost,
            "avg_latency_ms": avg_latency,
            "fallback_requests": fallback_count,
        }

    def by_provider(self, period: str = "month", all_projects: bool = False) -> list[dict[str, Any]]:
        return self._group_by("provider", period, all_projects)

    def by_model(self, period: str = "month", all_projects: bool = False) -> list[dict[str, Any]]:
        return self._group_by("model", period, all_projects)

    def cost_breakdown(self, period: str = "month", all_projects: bool = False) -> dict[str, Any]:
        return {
            "summary": self.summary(period=period, all_projects=all_projects),
            "providers": self.by_provider(period=period, all_projects=all_projects),
            "models": self.by_model(period=period, all_projects=all_projects),
        }

    def export_csv(self, output_path: str, period: str = "month", all_projects: bool = False) -> None:
        records = self.storage.read(period=period, all_projects=all_projects)
        self.storage.export_csv(records, output_path)

    def export_json(self, output_path: str, period: str = "month", all_projects: bool = False) -> None:
        payload = {
            "summary": self.summary(period=period, all_projects=all_projects),
            "records": self.storage.read(period=period, all_projects=all_projects),
        }
        self.storage.export_json(payload, output_path)

    def _group_by(self, key: str, period: str, all_projects: bool) -> list[dict[str, Any]]:
        buckets: dict[str, dict[str, Any]] = defaultdict(lambda: {
            key: "",
            "requests": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "cost_usd": 0.0,
            "avg_latency_ms": 0.0,
            "fallback_requests": 0,
        })
        latency_sums: dict[str, int] = defaultdict(int)

        for record in self.storage.read(period=period, all_projects=all_projects):
            bucket_key = str(record.get(key, "unknown"))
            bucket = buckets[bucket_key]
            bucket[key] = bucket_key
            bucket["requests"] += 1
            bucket["input_tokens"] += int(record.get("in", 0) or 0)
            bucket["output_tokens"] += int(record.get("out", 0) or 0)
            bucket["cost_usd"] = round(bucket["cost_usd"] + float(record.get("cost", 0.0) or 0.0), 8)
            bucket["fallback_requests"] += 1 if record.get("fallback") else 0
            latency_sums[bucket_key] += int(record.get("latency_ms", 0) or 0)

        rows: list[dict[str, Any]] = []
        for bucket_key, bucket in buckets.items():
            requests = bucket["requests"]
            bucket["avg_latency_ms"] = round(latency_sums[bucket_key] / requests, 2) if requests else 0.0
            rows.append(bucket)
        rows.sort(key=lambda row: (-row["cost_usd"], row[key]))
        return rows

    def _upload(self, record: dict[str, Any]) -> None:
        if self.uploader is not None:
            self.uploader.upload(record)
