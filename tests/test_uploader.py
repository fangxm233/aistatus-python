# input: pytest fixtures plus monkeypatched threading, package imports, and usage/router/gateway integration points
# output: regression tests for fire-and-forget usage upload payload construction and uploader wiring
# pos: verifies aistatus.uploader async upload gating plus SDK integration with usage tracker, router, gateway, and exports
# >>> 一旦我被更新，务必更新我的开头注释，以及所属文件夹的 CLAUDE.md <<<

from __future__ import annotations

import json

from aistatus import AIStatusConfig, __version__, configure, get_config
from aistatus.config import AIStatusConfig as ConfigAIStatusConfig
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

    def test_upload_builds_payload_and_starts_daemon_thread(self, monkeypatch):
        thread_factory = ThreadFactory()
        monkeypatch.setattr("aistatus.uploader.threading.Thread", thread_factory)
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

        assert len(thread_factory.instances) == 1
        thread = thread_factory.instances[0]
        assert thread.target == uploader._post
        assert thread.daemon is True
        assert thread.started is True
        payload = thread.args[0]
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

        assert captured == {
            "url": "https://example.test/api/usage/upload",
            "method": "POST",
            "headers": {"Content-type": "application/json"},
            "body": payload,
            "timeout": 5,
        }
