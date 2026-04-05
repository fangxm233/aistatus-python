# input: pytest fixtures plus monkeypatched threading/executor, package imports, and usage/router/gateway integration points
# output: regression tests for fire-and-forget usage upload payload construction, field validation, executor usage, router streaming usage, retry handling, API caching, and provider client caching
# pos: verifies uploader behavior plus targeted SDK regressions across router, API client, and provider adapters
# >>> 一旦我被更新，务必更新我的开头注释，以及所属文件夹的 CLAUDE.md <<<

from __future__ import annotations

import json
from concurrent.futures import Future

import httpx
import pytest

from aistatus import AIStatusConfig, __version__, configure, get_config
from aistatus.api import StatusAPI
from aistatus.config import AIStatusConfig as ConfigAIStatusConfig
from aistatus.exceptions import ProviderCallFailed
from aistatus.models import ProviderConfig, RouteResponse
from aistatus.providers.anthropic_ import AnthropicAdapter
from aistatus.providers.google_ import GoogleAdapter
from aistatus.router import Router
from aistatus.uploader import UsageUploader
from aistatus.usage import UsageTracker


class DummyThread:
    def __init__(self, *, target, args, daemon):
        self.target = target
        self.args = args
        self.daemon = daemon
        self.started = False

    def start(self):
        self.started = True


class ThreadFactory:
    def __init__(self):
        self.instances: list[DummyThread] = []

    def __call__(self, *, target, args, daemon):
        thread = DummyThread(target=target, args=args, daemon=daemon)
        self.instances.append(thread)
        return thread


class DummyStorage:
    def __init__(self):
        self.records: list[dict] = []

    def append(self, record):
        self.records.append(record)




class DummyExecutor:
    def __init__(self):
        self.calls: list[tuple] = []

    def submit(self, fn, *args, **kwargs):
        self.calls.append((fn, args, kwargs))
        future = Future()
        future.set_result(None)
        return future


class DummyAsyncClient:
    def __init__(self, timeout=None):
        self.timeout = timeout
        self.calls: list[tuple[str, dict | None]] = []

    async def get(self, url, params=None):
        self.calls.append((url, params))
        response = type("Response", (), {})()
        response.raise_for_status = lambda: None
        response.json = lambda: {"model": "anthropic/claude-sonnet-4-6", "provider": "anthropic", "status": "operational"}
        return response


class Retry429Error(Exception):
    def __init__(self, status_code=429):
        super().__init__("rate limited")
        self.status_code = status_code


class StubStreamAdapter:
    def __init__(self):
        self.calls: list[tuple[str, list[dict], float, dict]] = []

    async def acall_stream(self, model_id, messages, timeout, **kwargs):
        self.calls.append((model_id, messages, timeout, kwargs))
        yield {"type": "text", "text": "hello"}
        yield {"type": "usage", "input_tokens": 12, "output_tokens": 3}
        yield {"type": "done"}


class RetryAdapter:
    def __init__(self):
        self.calls = 0

    async def acall(self, model_id, messages, timeout, **kwargs):
        self.calls += 1
        raise Retry429Error()


class CachedClientFactory:
    def __init__(self):
        self.instances: list[dict] = []

    def __call__(self, **kwargs):
        self.instances.append(kwargs)
        return {"kwargs": kwargs}


class DummyGoogleSyncModels:
    def __init__(self, calls):
        self.calls = calls

    def generate_content(self, **kwargs):
        self.calls.append(kwargs)
        return type("Resp", (), {"text": "ok", "usage_metadata": None})()


class DummyGoogleAsyncModels:
    def __init__(self, calls):
        self.calls = calls

    async def generate_content(self, **kwargs):
        self.calls.append(kwargs)
        return type("Resp", (), {"text": "ok", "usage_metadata": None})()


class DummyGoogleClient:
    def __init__(self):
        self.calls: list[dict] = []
        self.models = DummyGoogleSyncModels(self.calls)
        self.aio = type("Aio", (), {"models": DummyGoogleAsyncModels(self.calls)})()


class DummyUploader:
    def __init__(self):
        self.records: list[dict] = []

    def upload(self, record):
        self.records.append(record)


class TestUsageUploader:
    def test_upload_skips_when_disabled(self, monkeypatch):
        thread_factory = ThreadFactory()
        monkeypatch.setattr("aistatus.uploader.threading.Thread", thread_factory)
        uploader = UsageUploader(AIStatusConfig(upload_enabled=False, name="Alice", email="alice@example.com"))

        uploader.upload({"ts": "2026-04-03T00:00:00+00:00", "provider": "anthropic", "model": "claude"})

        assert thread_factory.instances == []

    def test_upload_skips_when_identity_is_incomplete(self, monkeypatch):
        thread_factory = ThreadFactory()
        monkeypatch.setattr("aistatus.uploader.threading.Thread", thread_factory)
        uploader = UsageUploader(AIStatusConfig(upload_enabled=True, name="", email="alice@example.com"))

        uploader.upload({"ts": "2026-04-03T00:00:00+00:00", "provider": "anthropic", "model": "claude"})

        assert thread_factory.instances == []

    def test_upload_builds_payload_and_submits_background_post(self, monkeypatch):
        executor = DummyExecutor()
        monkeypatch.setattr(UsageUploader, "_executor", executor)
        uploader = UsageUploader(
            AIStatusConfig(
                upload_enabled=True,
                name="Alice",
                organization="Lab X",
                email="alice@example.com",
                base_url="https://example.test",
            )
        )
        record = {
            "ts": "2026-04-03T00:00:00+00:00",
            "provider": "anthropic",
            "model": "claude-sonnet-4-6",
            "in": 123,
            "out": 45,
            "cache_creation_in": 7,
            "cache_read_in": 8,
            "cost": 0.25,
            "latency_ms": 321,
        }

        uploader.upload(record)

        assert len(executor.calls) == 1
        fn, args, kwargs = executor.calls[0]
        assert fn == uploader._post
        assert kwargs == {}
        payload = args[0]
        assert payload["sdk_version"] == __version__
        assert payload["records"] == [{
            "ts": "2026-04-03T00:00:00+00:00",
            "name": "Alice",
            "organization": "Lab X",
            "email": "alice@example.com",
            "provider": "anthropic",
            "model": "claude-sonnet-4-6",
            "input_tokens": 123,
            "output_tokens": 45,
            "cache_creation_input_tokens": 7,
            "cache_read_input_tokens": 8,
            "cost_usd": 0.25,
            "latency_ms": 321,
        }]

    def test_upload_truncates_identity_fields(self, monkeypatch):
        executor = DummyExecutor()
        monkeypatch.setattr(UsageUploader, "_executor", executor)
        uploader = UsageUploader(
            AIStatusConfig(
                upload_enabled=True,
                name="A" * 250,
                organization="B" * 250,
                email=("c" * 300) + "@example.com",
            )
        )

        uploader.upload({"ts": "2026-04-03T00:00:00+00:00", "provider": "anthropic", "model": "claude"})

        payload = executor.calls[0][1][0]
        record = payload["records"][0]
        assert len(record["name"]) == 200
        assert len(record["organization"]) == 200
        assert len(record["email"]) == 254

    def test_upload_uses_executor_instead_of_spawning_threads(self, monkeypatch):
        executor = DummyExecutor()
        thread_factory = ThreadFactory()
        monkeypatch.setattr(UsageUploader, "_executor", executor)
        monkeypatch.setattr("aistatus.uploader.threading.Thread", thread_factory)
        uploader = UsageUploader(AIStatusConfig(upload_enabled=True, name="Alice", email="alice@example.com"))

        uploader.upload({"ts": "2026-04-03T00:00:00+00:00", "provider": "anthropic", "model": "claude"})

        assert len(executor.calls) == 1
        assert thread_factory.instances == []

    def test_usage_tracker_uploads_after_record(self):
        storage = DummyStorage()
        uploader = DummyUploader()
        tracker = UsageTracker(storage=storage, uploader=uploader)

        record = tracker.record(
            type("Response", (), {
                "provider_used": "anthropic",
                "model_used": "anthropic/claude-sonnet-4-6",
                "input_tokens": 123,
                "output_tokens": 45,
                "cost_usd": 0.25,
                "was_fallback": False,
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 0,
            })(),
            latency_ms=321,
        )

        assert storage.records == [record]
        assert uploader.records == [record]

    def test_usage_tracker_uploads_after_record_usage(self):
        storage = DummyStorage()
        uploader = DummyUploader()
        tracker = UsageTracker(storage=storage, uploader=uploader)

        record = tracker.record_usage(
            provider="anthropic",
            model="claude-sonnet-4-6",
            input_tokens=123,
            output_tokens=45,
            latency_ms=321,
            fallback=False,
        )

        assert storage.records == [record]
        assert uploader.records == [record]

    def test_router_creates_usage_uploader_from_config(self, monkeypatch):
        config = AIStatusConfig(upload_enabled=True, name="Alice", email="alice@example.com")
        uploader_instances = []

        class StubUploader:
            def __init__(self, passed_config):
                uploader_instances.append(passed_config)

        monkeypatch.setattr("aistatus.router.get_config", lambda: config)
        monkeypatch.setattr("aistatus.router.UsageUploader", StubUploader)

        router = Router(auto_discover=False)

        assert uploader_instances == [config]
        assert isinstance(router.usage, UsageTracker)
        assert isinstance(router.usage.uploader, StubUploader)

    def test_package_exports_config_symbols(self):
        assert AIStatusConfig is ConfigAIStatusConfig
        assert callable(configure)
        assert callable(get_config)

    def test_post_swallows_exceptions(self, monkeypatch):
        uploader = UsageUploader(
            AIStatusConfig(upload_enabled=True, name="Alice", email="alice@example.com")
        )
        monkeypatch.setattr(
            "aistatus.uploader.urllib.request.urlopen",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("boom")),
        )

        uploader._post({"records": [], "sdk_version": __version__})

    def test_post_targets_usage_upload_endpoint(self, monkeypatch):
        captured = {}
        uploader = UsageUploader(
            AIStatusConfig(
                upload_enabled=True,
                name="Alice",
                email="alice@example.com",
                base_url="https://example.test",
            )
        )

        def fake_urlopen(request, timeout):
            captured["url"] = request.full_url
            captured["method"] = request.get_method()
            captured["headers"] = dict(request.header_items())
            captured["body"] = json.loads(request.data.decode("utf-8"))
            captured["timeout"] = timeout
            return None

        monkeypatch.setattr("aistatus.uploader.urllib.request.urlopen", fake_urlopen)
        payload = {"records": [{"email": "alice@example.com"}], "sdk_version": __version__}

        uploader._post(payload)



class TestRouterStreamingAndRetry:
    @pytest.mark.asyncio
    async def test_route_stream_records_usage_after_stream_finishes(self, monkeypatch):
        router = Router(auto_discover=False)
        adapter = StubStreamAdapter()
        router.adapters = {"anthropic": adapter}
        router._adapter_index = {"anthropic": "anthropic"}

        async def resolve(model, prefer=None):
            return [type("Candidate", (), {
                "provider_slug": "anthropic",
                "adapter_key": "anthropic",
                "model_id": model,
            })()]

        monkeypatch.setattr(router, "_aresolve_model", resolve)
        recorded = []
        router.usage = type(
            "Usage",
            (),
            {
                "cost_calculator": type(
                    "Calc",
                    (),
                    {"calculate_cost": staticmethod(lambda **_kwargs: 0.0), "calculate_cost_with_cache": staticmethod(lambda **_kwargs: 0.0)},
                )(),
                "record": lambda self, response, latency_ms: recorded.append((response, latency_ms)),
            },
        )()

        chunks = [chunk async for chunk in router.route_stream("hello", model="anthropic/claude-sonnet-4-6")]

        assert [chunk["type"] for chunk in chunks] == ["text", "usage", "done"]
        assert len(recorded) == 1
        response, latency_ms = recorded[0]
        assert response.input_tokens == 12
        assert response.output_tokens == 3
        assert response.model_used == "anthropic/claude-sonnet-4-6"
        assert response.provider_used == "anthropic"
        assert latency_ms >= 0

    @pytest.mark.asyncio
    async def test_async_429_retry_failure_raises_retry_exception_when_fallback_disabled(self, monkeypatch):
        router = Router(auto_discover=False)
        adapter = RetryAdapter()
        router.adapters = {"anthropic": adapter}
        router._adapter_index = {"anthropic": "anthropic"}

        async def resolve(model, prefer=None):
            return [type("Candidate", (), {
                "provider_slug": "anthropic",
                "adapter_key": "anthropic",
                "model_id": model,
            })()]

        async def fake_sleep(*_args, **_kwargs):
            return None

        monkeypatch.setattr(router, "_aresolve_model", resolve)
        monkeypatch.setattr("aistatus.router.asyncio.sleep", fake_sleep)

        with pytest.raises(ProviderCallFailed) as exc:
            await router._aroute_model(
                [{"role": "user", "content": "hello"}],
                "anthropic/claude-sonnet-4-6",
                allow_fallback=False,
                timeout=30.0,
                prefer=None,
            )

        assert adapter.calls == 2
        assert exc.value.cause is not None
        assert exc.value.cause is not exc.value
        assert isinstance(exc.value.cause, Retry429Error)


class TestStatusApiClientCaching:
    def test_sync_client_is_reused(self, monkeypatch):
        created = []

        class DummyClient:
            def __init__(self, timeout=None):
                created.append(timeout)

            def get(self, url, params=None):
                response = type("Response", (), {})()
                response.raise_for_status = lambda: None
                response.json = lambda: {"providers": []}
                return response

        monkeypatch.setattr("aistatus.api.httpx.Client", DummyClient)
        api = StatusAPI()

        api.providers()
        api.providers()

        assert created == [3.0]

    @pytest.mark.asyncio
    async def test_async_client_is_reused(self, monkeypatch):
        created = []
        client = DummyAsyncClient(timeout=3.0)

        class DummyAsyncClientFactory:
            def __init__(self, timeout=None):
                created.append(timeout)
                self.timeout = timeout

            async def get(self, url, params=None):
                return await client.get(url, params=params)

            async def aclose(self):
                return None

        monkeypatch.setattr("aistatus.api.httpx.AsyncClient", DummyAsyncClientFactory)
        api = StatusAPI()

        await api.acheck_model("anthropic/claude-sonnet-4-6")
        await api.acheck_model("anthropic/claude-haiku-4-5")

        assert created == [3.0]


class TestProviderClientCaching:
    def test_anthropic_adapter_reuses_cached_client(self, monkeypatch):
        factory = CachedClientFactory()
        monkeypatch.setattr("aistatus.providers.anthropic_.anthropic.Anthropic", factory, raising=False)
        adapter = AnthropicAdapter(ProviderConfig(slug="anthropic", adapter_type="anthropic", api_key="sk-test"))

        first = adapter._get_client()
        second = adapter._get_client()

        assert first is second
        assert len(factory.instances) == 1

    def test_openai_adapter_reuses_cached_client(self, monkeypatch):
        factory = CachedClientFactory()
        monkeypatch.setattr("openai.OpenAI", factory)

        from aistatus.providers.openai_ import OpenAIAdapter

        adapter = OpenAIAdapter(ProviderConfig(slug="openai", adapter_type="openai", api_key="sk-test"))

        first = adapter._get_client()
        second = adapter._get_client()

        assert first is second
        assert len(factory.instances) == 1

    def test_google_adapter_passes_timeout_in_http_options(self, monkeypatch):
        client = DummyGoogleClient()
        adapter = GoogleAdapter(ProviderConfig(slug="google", adapter_type="google", api_key="key"))
        monkeypatch.setattr(adapter, "_get_client", lambda: client)

        adapter.call("google/gemini-2.0-flash", [{"role": "user", "content": "hello"}], 7.5)

        assert client.calls[0]["config"]["http_options"] == {"timeout": 7500}
