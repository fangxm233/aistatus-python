# input: upload config, usage records, stdlib threading/json/urllib, and package version metadata
# output: fire-and-forget POSTs of usage payloads to the aistatus upload API with silent failure semantics
# pos: bridges local usage tracking to remote leaderboard ingestion without blocking SDK request flows
# >>> 一旦我被更新，务必更新我的开头注释，以及所属文件夹的 CLAUDE.md <<<

from __future__ import annotations

import json
import threading
import urllib.request
from typing import Any

from . import __version__
from .config import AIStatusConfig


class UsageUploader:
    def __init__(self, config: AIStatusConfig):
        self.config = config
        self._base_url = config.base_url.rstrip("/")

    def upload(self, record: dict[str, Any]) -> None:
        if not self.config.upload_enabled:
            return
        if not self.config.name or not self.config.email:
            return
        payload = {
            "records": [
                {
                    "ts": record["ts"],
                    "name": self.config.name,
                    "organization": self.config.organization,
                    "email": self.config.email,
                    "provider": record["provider"],
                    "model": record["model"],
                    "input_tokens": record.get("in", 0),
                    "output_tokens": record.get("out", 0),
                    "cache_creation_input_tokens": record.get("cache_creation_in", 0),
                    "cache_read_input_tokens": record.get("cache_read_in", 0),
                    "cost_usd": record.get("cost", 0),
                    "latency_ms": record.get("latency_ms", 0),
                }
            ],
            "sdk_version": __version__,
        }
        threading.Thread(target=self._post, args=(payload,), daemon=True).start()

    def _post(self, payload: dict[str, Any]) -> None:
        try:
            urllib.request.urlopen(
                urllib.request.Request(
                    f"{self._base_url}/api/usage/upload",
                    data=json.dumps(payload).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                ),
                timeout=5,
            )
        except Exception:
            pass
