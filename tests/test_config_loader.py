"""Tests for dbk.config_loader."""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import pytest

from dbk.config_loader import (
    DEFAULT_CONFIG_DIR,
    DEFAULT_CONFIG_PATH,
    TOMLConfig,
    TOMLError,
)


class TestTOMLConfigSingleton:
    def test_get_instance_returns_same_object(self) -> None:
        TOMLConfig.reset_instance()
        a = TOMLConfig.get_instance()
        b = TOMLConfig.get_instance()
        assert a is b
        TOMLConfig.reset_instance()

    def test_reset_instance_returns_new_object(self) -> None:
        TOMLConfig.reset_instance()
        a = TOMLConfig.get_instance()
        TOMLConfig.reset_instance()
        b = TOMLConfig.get_instance()
        assert a is not b
        TOMLConfig.reset_instance()


class TestTOMLConfigInit:
    def test_explicit_config_path(self) -> None:
        cfg = TOMLConfig(config_path="/some/path.toml")
        assert cfg.config_path == Path("/some/path.toml")

    def test_explicit_config_dir(self) -> None:
        cfg = TOMLConfig(config_dir="/some/dir")
        assert cfg.config_path == Path("/some/dir/config.toml")

    def test_default_path(self) -> None:
        cfg = TOMLConfig()
        assert cfg.config_path == DEFAULT_CONFIG_PATH

    def test_both_config_dir_and_path_raises(self) -> None:
        with pytest.raises(ValueError):
            TOMLConfig(config_dir="/a", config_path="/b")


class TestTOMLConfigExists:
    def test_exists_false_when_file_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg = TOMLConfig(config_dir=Path(tmpdir))
            assert cfg.exists is False

    def test_exists_true_when_file_present(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "config.toml"
            path.write_text("[dbk]\n")
            cfg = TOMLConfig(config_path=path)
            assert cfg.exists is True


class TestTOMLConfigGet:
    def test_get_top_level_key(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "config.toml"
            path.write_text('[dbk]\nprovider = "anthropic"\n')
            cfg = TOMLConfig(config_path=path)
            assert cfg.get("dbk", "provider") == "anthropic"

    def test_get_nested_key(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "config.toml"
            path.write_text('[providers]\ntimeout = 30\n')
            cfg = TOMLConfig(config_path=path)
            assert cfg.get("providers", "timeout") == 30

    def test_get_missing_key_returns_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "config.toml"
            path.write_text("[dbk]\n")
            cfg = TOMLConfig(config_path=path)
            assert cfg.get("dbk", "missing") is None
            assert cfg.get("dbk", "missing", default="fallback") == "fallback"

    def test_get_int(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "config.toml"
            path.write_text('[dbk]\ncollect_interval_sec = 120\n')
            cfg = TOMLConfig(config_path=path)
            assert cfg.get_int("dbk", "collect_interval_sec") == 120

    def test_get_int_invalid_returns_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "config.toml"
            path.write_text('[dbk]\ncollect_interval_sec = "bad"\n')
            cfg = TOMLConfig(config_path=path)
            assert cfg.get_int("dbk", "collect_interval_sec", default=999) == 999

    def test_get_bool_true(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "config.toml"
            path.write_text('[dbk]\nverbose = true\n')
            cfg = TOMLConfig(config_path=path)
            assert cfg.get_bool("dbk", "verbose") is True

    def test_get_bool_false(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "config.toml"
            path.write_text('[dbk]\nverbose = false\n')
            cfg = TOMLConfig(config_path=path)
            assert cfg.get_bool("dbk", "verbose") is False

    def test_get_bool_string_true(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "config.toml"
            path.write_text('[dbk]\nverbose = "true"\n')
            cfg = TOMLConfig(config_path=path)
            assert cfg.get_bool("dbk", "verbose") is True

    def test_get_bool_string_one(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "config.toml"
            path.write_text('[dbk]\nverbose = "1"\n')
            cfg = TOMLConfig(config_path=path)
            assert cfg.get_bool("dbk", "verbose") is True

    def test_get_bool_missing_returns_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "config.toml"
            path.write_text("[dbk]\n")
            cfg = TOMLConfig(config_path=path)
            assert cfg.get_bool("dbk", "missing", default=True) is True
            assert cfg.get_bool("dbk", "missing", default=False) is False

    def test_as_dict(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "config.toml"
            path.write_text('[dbk]\nprovider = "openai"\n[other]\nval = 1\n')
            cfg = TOMLConfig(config_path=path)
            d = cfg.as_dict()
            assert d["dbk"] == {"provider": "openai"}
            assert d["other"] == {"val": 1}

    def test_as_dict_empty_when_file_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg = TOMLConfig(config_dir=Path(tmpdir))
            assert cfg.as_dict() == {}

    def test_empty_file_returns_empty_dict(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "config.toml"
            path.write_text("")
            cfg = TOMLConfig(config_path=path)
            assert cfg.as_dict() == {}


class TestTOMLConfigEnvOverride:
    def test_env_var_overrides_toml(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "config.toml"
            path.write_text('[dbk]\nprovider = "anthropic"\n')
            cfg = TOMLConfig(config_path=path)
            # Env var DBK_DBK_PROVIDER takes precedence.
            with pytest.MonkeyPatch.context() as mp:
                mp.setenv("DBK_DBK_PROVIDER", "openai")
                val = cfg.get("dbk", "provider")
                assert val == "openai"


class TestTOMLError:
    def test_invalid_toml_raises_toml_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "config.toml"
            path.write_text("not valid toml [[[\n")
            cfg = TOMLConfig(config_path=path)
            with pytest.raises(TOMLError) as exc_info:
                cfg.get("dbk")
            assert str(path) in str(exc_info.value)

    def test_exists_false_on_parse_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "config.toml"
            path.write_text("bad [[[\n")
            cfg = TOMLConfig(config_path=path)
            assert cfg.exists is False


class TestTOMLConfigRepr:
    def test_repr(self) -> None:
        cfg = TOMLConfig(config_path="/a/b.toml")
        assert "/a/b.toml" in repr(cfg)
