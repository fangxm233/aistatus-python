# input: pytest fixtures plus monkeypatched threading and urllib request behavior
# output: regression tests for fire-and-forget usage upload payload construction and silent failure
# pos: verifies aistatus.uploader async upload gating, payload mapping, and exception swallowing
# >>> 一旦我被更新，务必更新我的开头注释，以及所属文件夹的 CLAUDE.md <<<

from __future__ import annotations

import json

from aistatus.config import AIStatusConfig
from aistatus.uploader import UsageUploader


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
        assert payload["sdk_version"] == "0.0.3"
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

    def test_post_swallows_exceptions(self, monkeypatch):
        uploader = UsageUploader(
            AIStatusConfig(upload_enabled=True, name="Alice", email="alice@example.com")
        )
        monkeypatch.setattr(
            "aistatus.uploader.urllib.request.urlopen",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("boom")),
        )

        uploader._post({"records": [], "sdk_version": "0.0.3"})

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
        payload = {"records": [{"email": "alice@example.com"}], "sdk_version": "0.0.3"}

        uploader._post(payload)

        assert captured == {
            "url": "https://example.test/api/usage/upload",
            "method": "POST",
            "headers": {"Content-type": "application/json"},
            "body": payload,
            "timeout": 5,
        }
