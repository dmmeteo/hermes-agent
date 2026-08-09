"""Packet 1 remediation — provider resolution and setup findings.

F2   exact argv: no copilot args/env may leak into the Claude adapter spawn
F5   model.claude_code_acp.command actually works; managed ~/.hermes/node is
     resolved; the package pin lives in exactly one constant
F6   the interactive `hermes model` picker persists the choice
F13  a provider that omits spec keys fails loudly instead of silently
     inheriting copilot's
"""

from __future__ import annotations

import pytest

from hermes_cli.auth import (
    PROVIDER_REGISTRY,
    AuthError,
    get_external_process_provider_status,
    resolve_external_process_provider_credentials,
)

CLAUDE_ACP = "claude-code-acp"


@pytest.fixture
def adapter_on_path(monkeypatch, tmp_path):
    """A discoverable `claude-agent-acp`, with every copilot var cleared."""
    adapter = tmp_path / "claude-agent-acp"
    adapter.write_text("#!/bin/sh\n", encoding="utf-8")
    adapter.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path))
    for var in ("HERMES_CLAUDE_ACP_COMMAND", "HERMES_CLAUDE_ACP_ARGS",
                "HERMES_COPILOT_ACP_COMMAND", "COPILOT_CLI_PATH",
                "HERMES_COPILOT_ACP_ARGS"):
        monkeypatch.delenv(var, raising=False)
    return adapter


class TestExactArgv:
    """F2 — the adapter takes no arguments; copilot's must not leak in."""

    def test_status_reports_no_args(self, adapter_on_path):
        assert get_external_process_provider_status(CLAUDE_ACP)["args"] == []

    def test_resolved_credentials_carry_no_args(self, adapter_on_path):
        creds = resolve_external_process_provider_credentials(CLAUDE_ACP)
        assert creds["args"] == []
        assert creds["command"] == str(adapter_on_path)

    def test_copilot_command_env_vars_do_not_leak(self, monkeypatch, tmp_path):
        """An explicitly empty command_env_vars tuple must not fall back."""
        decoy = tmp_path / "copilot-decoy"
        decoy.write_text("#!/bin/sh\n", encoding="utf-8")
        decoy.chmod(0o755)
        monkeypatch.setenv("COPILOT_CLI_PATH", str(decoy))
        monkeypatch.setenv("HERMES_COPILOT_ACP_COMMAND", str(decoy))
        monkeypatch.delenv("HERMES_CLAUDE_ACP_COMMAND", raising=False)

        status = get_external_process_provider_status(CLAUDE_ACP)
        assert status["command"] != str(decoy)
        assert status["command"] == "claude-agent-acp"

    def test_copilot_args_env_var_does_not_leak(self, adapter_on_path, monkeypatch):
        monkeypatch.setenv("HERMES_COPILOT_ACP_ARGS", "--acp --stdio")
        assert get_external_process_provider_status(CLAUDE_ACP)["args"] == []

    def test_runtime_argv_is_exactly_the_bare_command(self, adapter_on_path):
        from agent.claude_code_runtime import resolve_adapter_argv
        from types import SimpleNamespace

        argv = resolve_adapter_argv(SimpleNamespace(acp_command="", acp_args=[]))
        assert argv == [str(adapter_on_path)]


class TestConfigCommandOverride:
    """F5 — the config key named in the error message must actually work."""

    def test_config_command_wins_over_path(self, monkeypatch, tmp_path):
        from_path = tmp_path / "claude-agent-acp"
        from_path.write_text("#!/bin/sh\n", encoding="utf-8")
        from_path.chmod(0o755)
        configured = tmp_path / "configured-adapter"
        configured.write_text("#!/bin/sh\n", encoding="utf-8")
        configured.chmod(0o755)

        monkeypatch.setenv("PATH", str(tmp_path))
        monkeypatch.delenv("HERMES_CLAUDE_ACP_COMMAND", raising=False)
        monkeypatch.setattr(
            "hermes_cli.auth.load_config",
            lambda: {"model": {"claude_code_acp": {"command": str(configured)}}},
        )

        status = get_external_process_provider_status(CLAUDE_ACP)
        assert status["command"] == str(configured)
        assert status["resolved_command"] == str(configured)

    def test_env_var_wins_over_config(self, monkeypatch, tmp_path):
        configured = tmp_path / "configured-adapter"
        configured.write_text("#!/bin/sh\n", encoding="utf-8")
        configured.chmod(0o755)
        override = tmp_path / "env-adapter"
        override.write_text("#!/bin/sh\n", encoding="utf-8")
        override.chmod(0o755)

        monkeypatch.setenv("HERMES_CLAUDE_ACP_COMMAND", str(override))
        monkeypatch.setattr(
            "hermes_cli.auth.load_config",
            lambda: {"model": {"claude_code_acp": {"command": str(configured)}}},
        )
        assert get_external_process_provider_status(CLAUDE_ACP)["command"] == str(override)

    def test_config_args_are_honoured(self, monkeypatch, tmp_path):
        configured = tmp_path / "configured-adapter"
        configured.write_text("#!/bin/sh\n", encoding="utf-8")
        configured.chmod(0o755)
        monkeypatch.delenv("HERMES_CLAUDE_ACP_COMMAND", raising=False)
        monkeypatch.delenv("HERMES_CLAUDE_ACP_ARGS", raising=False)
        monkeypatch.setattr(
            "hermes_cli.auth.load_config",
            lambda: {"model": {"claude_code_acp": {
                "command": str(configured), "args": ["--verbose"]}}},
        )
        assert get_external_process_provider_status(CLAUDE_ACP)["args"] == ["--verbose"]

    def test_broken_config_does_not_crash_resolution(self, monkeypatch, tmp_path):
        monkeypatch.setenv("PATH", str(tmp_path))
        monkeypatch.delenv("HERMES_CLAUDE_ACP_COMMAND", raising=False)
        monkeypatch.setattr(
            "hermes_cli.auth.load_config",
            lambda: (_ for _ in ()).throw(RuntimeError("config is broken")),
        )
        # Falls back to the default command rather than propagating.
        assert get_external_process_provider_status(CLAUDE_ACP)["command"] == "claude-agent-acp"


class TestManagedNodeResolution:
    """F5 / M2 — find a managed install without stubbing the resolver.

    The adapter must be located by a plain directory scan of the managed Node
    dirs. Routing a third-party binary through ``find_node_executable`` drags
    in ``node_tool_runnable``, which spawns ``<path> --version`` and can
    trigger a full managed-Node re-download — from a provider *status* check
    reachable via ``hermes doctor`` and the dashboard.
    """

    @pytest.fixture
    def managed_home(self, monkeypatch, tmp_path):
        home = tmp_path / "hermes-home"
        bin_dir = home / "node" / "bin"
        bin_dir.mkdir(parents=True)
        monkeypatch.setenv("HERMES_HOME", str(home))
        empty = tmp_path / "empty"
        empty.mkdir()
        monkeypatch.setenv("PATH", str(empty))
        monkeypatch.delenv("HERMES_CLAUDE_ACP_COMMAND", raising=False)
        return bin_dir

    def test_real_managed_install_is_discovered(self, managed_home):
        adapter = managed_home / "claude-agent-acp"
        adapter.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        adapter.chmod(0o755)

        status = get_external_process_provider_status(CLAUDE_ACP)
        assert status["configured"] is True
        assert status["resolved_command"] == str(adapter)

    def test_absent_managed_install_is_not_configured(self, managed_home):
        assert get_external_process_provider_status(CLAUDE_ACP)["configured"] is False

    def test_status_check_never_probes_the_node_healer(self, managed_home, monkeypatch):
        """A status check must not shell out or heal the managed Node tree."""
        import hermes_constants

        adapter = managed_home / "claude-agent-acp"
        adapter.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        adapter.chmod(0o755)

        def _boom(*args, **kwargs):
            raise AssertionError("provider status must not run the Node healer")

        monkeypatch.setattr(hermes_constants, "heal_hermes_managed_node", _boom)
        monkeypatch.setattr(hermes_constants, "node_tool_runnable", _boom)

        status = get_external_process_provider_status(CLAUDE_ACP)
        assert status["resolved_command"] == str(adapter)

    def test_credentials_resolution_uses_the_managed_install(self, managed_home):
        adapter = managed_home / "claude-agent-acp"
        adapter.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        adapter.chmod(0o755)

        creds = resolve_external_process_provider_credentials(CLAUDE_ACP)
        assert creds["command"] == str(adapter)


class TestSinglePinConstant:
    """F5 — the version pin must exist in exactly one place."""

    def test_probe_imports_the_shared_pin(self):
        import scripts.claude_acp_probe as probe
        from hermes_cli.auth import CLAUDE_CODE_ACP_PACKAGE

        assert probe.PINNED_ADAPTER is CLAUDE_CODE_ACP_PACKAGE

    def test_install_hint_names_the_managed_path(self, monkeypatch, tmp_path):
        home = tmp_path / "hermes-home"
        (home / "node" / "bin").mkdir(parents=True)
        monkeypatch.setenv("HERMES_HOME", str(home))
        monkeypatch.setenv("PATH", str(tmp_path))
        monkeypatch.delenv("HERMES_CLAUDE_ACP_COMMAND", raising=False)

        with pytest.raises(AuthError) as excinfo:
            resolve_external_process_provider_credentials(CLAUDE_ACP)
        message = str(excinfo.value)
        # Point at the Hermes-managed installer, not raw global npm advice.
        assert "hermes model" in message or "hermes claude-code" in message
        # `hermes tools` is a toolset toggle UI, not an installer — the plan's
        # original wording was wrong and must not come back.
        assert "hermes tools" not in message


class TestSpecStrictness:
    """F13 — a new provider must not silently inherit copilot's identity."""

    def test_provider_missing_spec_keys_fails_loudly(self):
        from hermes_cli.auth import ProviderConfig, _external_process_spec

        bogus = ProviderConfig(
            id="some-new-agent",
            name="Some New Agent",
            auth_type="external_process",
            inference_base_url="acp://new",
            extra={},
        )
        with pytest.raises(ValueError) as excinfo:
            _external_process_spec(bogus)
        assert "some-new-agent" in str(excinfo.value)

    def test_copilot_spec_still_resolves(self):
        from hermes_cli.auth import _external_process_spec

        spec = _external_process_spec(PROVIDER_REGISTRY["copilot-acp"])
        assert spec["default_args"] == ["--acp", "--stdio"]
        assert spec["missing_code"] == "missing_copilot_cli"

    def test_claude_spec_is_fully_declared(self):
        from hermes_cli.auth import _external_process_spec

        spec = _external_process_spec(PROVIDER_REGISTRY[CLAUDE_ACP])
        assert spec["default_args"] == []
        assert spec["default_command"] == "claude-agent-acp"
        assert spec["missing_code"] == "missing_claude_acp_adapter"
        assert spec["remote_url_prefixes"] == ()


class TestModelPickerFlow:
    """F6 — selecting 'Claude Code' interactively must persist the choice."""

    def test_flow_persists_provider_and_model(self, monkeypatch, tmp_path):
        adapter = tmp_path / "claude-agent-acp"
        adapter.write_text("#!/bin/sh\n", encoding="utf-8")
        adapter.chmod(0o755)
        monkeypatch.setenv("PATH", str(tmp_path))
        monkeypatch.delenv("HERMES_CLAUDE_ACP_COMMAND", raising=False)

        saved: dict = {"model": {}}
        monkeypatch.setattr("hermes_cli.config.load_config", lambda: saved)
        monkeypatch.setattr(
            "hermes_cli.config.save_config",
            lambda cfg: saved.update(cfg),
        )

        from hermes_cli.model_setup_flows import _model_flow_claude_code_acp

        _model_flow_claude_code_acp(saved, "")

        assert saved["model"]["provider"] == CLAUDE_ACP
        assert saved["model"]["default"] == "claude-code"

    def test_flow_reports_missing_adapter_without_saving(self, monkeypatch, tmp_path, capsys):
        empty = tmp_path / "empty"
        empty.mkdir()
        home = tmp_path / "hermes-home"
        (home / "node" / "bin").mkdir(parents=True)
        monkeypatch.setenv("HERMES_HOME", str(home))
        monkeypatch.setenv("PATH", str(empty))
        monkeypatch.delenv("HERMES_CLAUDE_ACP_COMMAND", raising=False)

        saved: dict = {"model": {}}
        monkeypatch.setattr("hermes_cli.config.load_config", lambda: saved)
        monkeypatch.setattr("hermes_cli.config.save_config", lambda cfg: saved.update(cfg))

        from hermes_cli.model_setup_flows import _model_flow_claude_code_acp

        _model_flow_claude_code_acp(saved, "")

        assert saved["model"].get("provider") != CLAUDE_ACP
        out = capsys.readouterr().out
        assert "claude-agent-acp" in out

    def test_picker_dispatch_reaches_the_flow(self, monkeypatch):
        """Selecting the 'Claude Code' row must actually invoke its flow.

        Drives the real picker in `select_provider_and_model` with only the
        prompt stubbed, so the dispatch chain itself is under test.
        """
        import hermes_cli.main as main_mod

        called: list = []
        # No raising=False: if the symbol is ever deleted, this test must fail
        # rather than silently keep passing.
        monkeypatch.setattr(
            main_mod, "_model_flow_claude_code_acp",
            lambda config, current_model="": called.append(current_model),
        )
        monkeypatch.setattr(main_mod, "load_config", lambda: {"model": {}},
                            raising=False)
        monkeypatch.setattr("hermes_cli.config.load_config", lambda: {"model": {}})

        def pick(labels, default=0, title=""):
            # Rows are descriptions; Anthropic's mentions "Claude Code" too, so
            # anchor on the row that *starts* with the provider's own label.
            for index, label in enumerate(labels):
                if str(label).startswith("Claude Code"):
                    return index
            raise AssertionError(f"'Claude Code' row not offered: {labels}")

        monkeypatch.setattr(main_mod, "_prompt_provider_choice", pick)

        main_mod.select_provider_and_model(None)
        assert called, "picking 'Claude Code' did not reach its setup flow"
