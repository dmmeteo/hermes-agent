"""Packet 1 — Claude Code registers and resolves like an ordinary provider.

Also pins the copilot-acp behaviour that the shared external-process helpers
had to be generalised to support, so that refactor cannot silently regress it.
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


class TestProviderProfile:
    def test_profile_is_registered_with_the_acp_api_mode(self):
        from providers import get_provider_profile

        profile = get_provider_profile(CLAUDE_ACP)
        assert profile is not None
        assert profile.api_mode == "claude_code_acp"

    def test_profile_needs_no_api_key(self):
        from providers import get_provider_profile

        assert get_provider_profile(CLAUDE_ACP).env_vars == ()

    def test_profile_has_a_human_facing_display_name(self):
        from providers import get_provider_profile

        assert get_provider_profile(CLAUDE_ACP).display_name

    def test_does_not_steal_the_anthropic_claude_code_alias(self):
        """`anthropic` already owns the `claude-code` alias; stealing it would
        silently reroute existing users' Anthropic API traffic."""
        from providers import get_provider_profile

        assert get_provider_profile("claude-code").name == "anthropic"
        assert "claude-code" not in get_provider_profile(CLAUDE_ACP).aliases


class TestProviderRegistryEntry:
    def test_registered_as_an_external_process_provider(self):
        assert PROVIDER_REGISTRY[CLAUDE_ACP].auth_type == "external_process"

    def test_requires_no_api_key_env_vars(self):
        assert PROVIDER_REGISTRY[CLAUDE_ACP].api_key_env_vars == ()


class TestPresenceDetection:
    def test_reports_configured_when_the_adapter_is_on_path(self, monkeypatch, tmp_path):
        adapter = tmp_path / "claude-agent-acp"
        adapter.write_text("#!/bin/sh\n", encoding="utf-8")
        adapter.chmod(0o755)
        monkeypatch.setenv("PATH", str(tmp_path))

        status = get_external_process_provider_status(CLAUDE_ACP)
        assert status["configured"] is True
        assert status["resolved_command"] == str(adapter)

    def test_reports_not_configured_when_the_adapter_is_missing(self, monkeypatch, tmp_path):
        monkeypatch.setenv("PATH", str(tmp_path))
        status = get_external_process_provider_status(CLAUDE_ACP)
        assert status["configured"] is False

    def test_explicit_command_override_is_honoured(self, monkeypatch, tmp_path):
        custom = tmp_path / "my-adapter"
        custom.write_text("#!/bin/sh\n", encoding="utf-8")
        custom.chmod(0o755)
        monkeypatch.setenv("HERMES_CLAUDE_ACP_COMMAND", str(custom))

        status = get_external_process_provider_status(CLAUDE_ACP)
        assert status["command"] == str(custom)
        assert status["configured"] is True


class TestCredentialResolution:
    def test_resolves_command_when_present(self, monkeypatch, tmp_path):
        adapter = tmp_path / "claude-agent-acp"
        adapter.write_text("#!/bin/sh\n", encoding="utf-8")
        adapter.chmod(0o755)
        monkeypatch.setenv("PATH", str(tmp_path))

        creds = resolve_external_process_provider_credentials(CLAUDE_ACP)
        assert creds["command"] == str(adapter)
        assert creds["provider"] == CLAUDE_ACP

    def test_missing_adapter_raises_an_actionable_auth_error(self, monkeypatch, tmp_path):
        monkeypatch.setenv("PATH", str(tmp_path))
        monkeypatch.delenv("HERMES_CLAUDE_ACP_COMMAND", raising=False)

        with pytest.raises(AuthError) as excinfo:
            resolve_external_process_provider_credentials(CLAUDE_ACP)

        message = str(excinfo.value)
        assert "claude-agent-acp" in message
        assert "install" in message.lower()
        assert excinfo.value.code == "missing_claude_acp_adapter"


class TestRuntimeResolution:
    def test_runtime_resolution_selects_the_acp_api_mode(self, monkeypatch, tmp_path):
        adapter = tmp_path / "claude-agent-acp"
        adapter.write_text("#!/bin/sh\n", encoding="utf-8")
        adapter.chmod(0o755)
        monkeypatch.setenv("PATH", str(tmp_path))

        from hermes_cli.runtime_provider import resolve_runtime_provider

        runtime = resolve_runtime_provider(requested=CLAUDE_ACP)
        assert runtime["provider"] == CLAUDE_ACP
        assert runtime["api_mode"] == "claude_code_acp"
        assert runtime["command"] == str(adapter)


class TestCopilotAcpRegression:
    """The external-process helpers are now table-driven. copilot-acp must
    resolve exactly as it did before that refactor."""

    def test_copilot_defaults_are_unchanged(self, monkeypatch, tmp_path):
        copilot = tmp_path / "copilot"
        copilot.write_text("#!/bin/sh\n", encoding="utf-8")
        copilot.chmod(0o755)
        monkeypatch.setenv("PATH", str(tmp_path))
        for var in ("HERMES_COPILOT_ACP_COMMAND", "COPILOT_CLI_PATH",
                    "HERMES_COPILOT_ACP_ARGS"):
            monkeypatch.delenv(var, raising=False)

        status = get_external_process_provider_status("copilot-acp")
        assert status["command"] == "copilot"
        assert status["args"] == ["--acp", "--stdio"]
        assert status["configured"] is True

    def test_copilot_command_env_overrides_still_work(self, monkeypatch, tmp_path):
        custom = tmp_path / "custom-copilot"
        custom.write_text("#!/bin/sh\n", encoding="utf-8")
        custom.chmod(0o755)
        monkeypatch.setenv("COPILOT_CLI_PATH", str(custom))

        assert get_external_process_provider_status("copilot-acp")["command"] == str(custom)

    def test_copilot_args_env_override_still_works(self, monkeypatch):
        monkeypatch.setenv("HERMES_COPILOT_ACP_ARGS", "--acp --other")
        assert get_external_process_provider_status("copilot-acp")["args"] == [
            "--acp", "--other",
        ]

    def test_copilot_missing_cli_error_is_unchanged(self, monkeypatch, tmp_path):
        monkeypatch.setenv("PATH", str(tmp_path))
        for var in ("HERMES_COPILOT_ACP_COMMAND", "COPILOT_CLI_PATH",
                    "COPILOT_ACP_BASE_URL"):
            monkeypatch.delenv(var, raising=False)

        with pytest.raises(AuthError) as excinfo:
            resolve_external_process_provider_credentials("copilot-acp")
        assert excinfo.value.code == "missing_copilot_cli"

    def test_non_external_process_provider_still_rejected(self):
        with pytest.raises(AuthError) as excinfo:
            resolve_external_process_provider_credentials("anthropic")
        assert excinfo.value.code == "invalid_provider"
