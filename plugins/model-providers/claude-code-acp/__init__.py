"""Claude Code (ACP) provider profile.

Claude Code runs as an external `claude-agent-acp` subprocess driven over the
Agent Client Protocol — NOT a REST endpoint. ``api_mode="claude_code_acp"``
routes the whole turn to the delegated runtime in
``agent/claude_code_runtime.py``; the profile carries the identity and auth
metadata so Claude Code appears in the picker like any other provider.

Auth is the user's existing Claude subscription, held by the Claude Code
install itself, so there are no API-key env vars.
"""

from providers import register_provider
from providers.base import ProviderProfile


class ClaudeCodeACPProfile(ProviderProfile):
    """Claude Code over ACP — external process, no REST models endpoint."""

    def fetch_models(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        timeout: float = 8.0,
    ) -> list[str] | None:
        """Model selection belongs to the Claude Code session, not to Hermes."""
        return None


claude_code_acp = ClaudeCodeACPProfile(
    name="claude-code-acp",
    # NOTE: deliberately does NOT claim "claude-code" — the `anthropic`
    # profile already owns that alias, and stealing it would silently reroute
    # existing Anthropic API traffic through this runtime.
    aliases=("claude-acp", "claude-code-agent"),
    display_name="Claude Code",
    description="Claude Code (subscription, native tools)",
    api_mode="claude_code_acp",
    env_vars=(),  # Subscription auth lives in the Claude Code install
    base_url="acp://claude-code",
    auth_type="external_process",
    supports_health_check=False,  # No /models endpoint to probe
    # Pseudo-model: the Claude Code session picks the real model itself.
    fallback_models=("claude-code",),
)

register_provider(claude_code_acp)
