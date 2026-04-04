# input: pytest fixtures, monkeypatch env, and temporary filesystem paths
# output: regression tests for SDK upload config precedence and persistence
# pos: verifies aistatus.config file/env/runtime config behavior
# >>> 一旦我被更新，务必更新我的开头注释，以及所属文件夹的 CLAUDE.md <<<

from __future__ import annotations

from pathlib import Path

from aistatus.config import AIStatusConfig, _load_from_file, configure, get_config
import aistatus.config as config_module


def _reset_config_state(monkeypatch, tmp_path: Path) -> Path:
    config_path = tmp_path / ".aistatus" / "config.yaml"
    monkeypatch.setattr(config_module, "CONFIG_PATH", config_path)
    monkeypatch.setattr(config_module, "_config", None)
    for key in ("AISTATUS_UPLOAD", "AISTATUS_NAME", "AISTATUS_ORG", "AISTATUS_EMAIL"):
        monkeypatch.delenv(key, raising=False)
    return config_path


class TestConfigLoading:
    def test_load_from_file_reads_persisted_values(self, monkeypatch, tmp_path):
        config_path = _reset_config_state(monkeypatch, tmp_path)
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(
            "upload:\n"
            "  enabled: true\n"
            "  name: Alice\n"
            "  organization: Lab X\n"
            "  email: alice@example.com\n",
            encoding="utf-8",
        )

        config = _load_from_file()

        assert config == AIStatusConfig(
            upload_enabled=True,
            name="Alice",
            organization="Lab X",
            email="alice@example.com",
        )

    def test_env_overrides_file_values(self, monkeypatch, tmp_path):
        config_path = _reset_config_state(monkeypatch, tmp_path)
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(
            "upload:\n"
            "  enabled: false\n"
            "  name: File Name\n"
            "  organization: File Org\n"
            "  email: file@example.com\n",
            encoding="utf-8",
        )
        monkeypatch.setenv("AISTATUS_UPLOAD", "yes")
        monkeypatch.setenv("AISTATUS_NAME", "Env Name")
        monkeypatch.setenv("AISTATUS_ORG", "Env Org")
        monkeypatch.setenv("AISTATUS_EMAIL", "env@example.com")

        config = _load_from_file()

        assert config == AIStatusConfig(
            upload_enabled=True,
            name="Env Name",
            organization="Env Org",
            email="env@example.com",
        )

    def test_get_config_returns_defaults_when_no_sources_exist(self, monkeypatch, tmp_path):
        _reset_config_state(monkeypatch, tmp_path)

        config = get_config()

        assert config == AIStatusConfig()


class TestConfigPersistence:
    def test_configure_persists_values_and_updates_cache(self, monkeypatch, tmp_path):
        config_path = _reset_config_state(monkeypatch, tmp_path)

        configure(
            upload=True,
            name="Alice",
            organization="Lab X",
            email="alice@example.com",
        )

        assert get_config() == AIStatusConfig(
            upload_enabled=True,
            name="Alice",
            organization="Lab X",
            email="alice@example.com",
        )
        assert config_path.exists()
        assert "enabled: true" in config_path.read_text(encoding="utf-8")
        assert "name: Alice" in config_path.read_text(encoding="utf-8")
        assert "organization: Lab X" in config_path.read_text(encoding="utf-8")
        assert "email: alice@example.com" in config_path.read_text(encoding="utf-8")

    def test_configure_overrides_env_values_in_current_process(self, monkeypatch, tmp_path):
        _reset_config_state(monkeypatch, tmp_path)
        monkeypatch.setenv("AISTATUS_NAME", "Env Name")
        monkeypatch.setenv("AISTATUS_UPLOAD", "false")

        configure(upload=True, name="Runtime Name")

        config = get_config()

        assert config.upload_enabled is True
        assert config.name == "Runtime Name"
