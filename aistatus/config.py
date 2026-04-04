# input: process env vars and ~/.aistatus/config.yaml persisted upload settings
# output: AIStatusConfig plus configure/get_config helpers for SDK upload identity
# pos: manages SDK-wide persistent upload config with runtime/env/file/default precedence
# >>> 一旦我被更新，务必更新我的开头注释，以及所属文件夹的 CLAUDE.md <<<

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import yaml

CONFIG_PATH = Path.home() / ".aistatus" / "config.yaml"
TRUTHY_VALUES = {"1", "true", "yes", "on"}


@dataclass
class AIStatusConfig:
    upload_enabled: bool = False
    name: str = ""
    organization: str = ""
    email: str = ""
    base_url: str = "https://aistatus.cc"


_config: AIStatusConfig | None = None


def _env_to_bool(value: str) -> bool:
    return value.strip().lower() in TRUTHY_VALUES


def _load_from_file() -> AIStatusConfig:
    config = AIStatusConfig()
    if CONFIG_PATH.exists():
        raw = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) or {}
        upload = raw.get("upload", {})
        config.upload_enabled = bool(upload.get("enabled", False))
        config.name = str(upload.get("name", ""))
        config.organization = str(upload.get("organization", ""))
        config.email = str(upload.get("email", ""))

    if "AISTATUS_UPLOAD" in os.environ:
        config.upload_enabled = _env_to_bool(os.environ["AISTATUS_UPLOAD"])
    if "AISTATUS_NAME" in os.environ:
        config.name = os.environ["AISTATUS_NAME"]
    if "AISTATUS_ORG" in os.environ:
        config.organization = os.environ["AISTATUS_ORG"]
    if "AISTATUS_EMAIL" in os.environ:
        config.email = os.environ["AISTATUS_EMAIL"]
    return config


def _save_to_file(config: AIStatusConfig) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "upload": {
            "enabled": config.upload_enabled,
            "name": config.name,
            "organization": config.organization,
            "email": config.email,
        }
    }
    CONFIG_PATH.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")


def get_config() -> AIStatusConfig:
    global _config
    if _config is None:
        _config = _load_from_file()
    return _config


def configure(
    *,
    upload: bool | None = None,
    name: str | None = None,
    organization: str | None = None,
    email: str | None = None,
) -> AIStatusConfig:
    config = get_config()
    if upload is not None:
        config.upload_enabled = upload
    if name is not None:
        config.name = name
    if organization is not None:
        config.organization = organization
    if email is not None:
        config.email = email
    _save_to_file(config)
    return config
