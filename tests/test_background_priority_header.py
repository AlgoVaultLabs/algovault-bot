"""OPS-HL-INTERACTIVE-PRIORITY-W1 — the background-priority header contract.

The weekly scan-showcase cron fans out `scan_trade_calls` across every supported
venue. It was the measured source of 161 of 171 Hyperliquid interactive rate-limit
throws in a week — all inside a single minute (Mon 13:17 UTC). Nobody waits on a
weekly broadcast, so it now marks its calls background and signal-MCP runs them in
its BATCH lane, yielding Hyperliquid's interactive reserve to live callers.

The load-bearing invariant is the SPLIT: the showcase opts in, the live `/scan`
command does NOT. Both go through the same `mcp_client`, so a regression here would
silently park a human-facing command in a lane that may wait ~5 minutes.
"""

from __future__ import annotations

import inspect

from algovault_bot import adoption, handlers
from algovault_bot.mcp_client import McpClient, McpClientConfig

HEADER = "X-AlgoVault-Priority"


def _headers_for(**cfg_kwargs) -> dict[str, str]:
    cfg = McpClientConfig(internal_bypass_key="k" * 32, **cfg_kwargs)
    return McpClient(cfg)._headers()


def test_background_client_sends_the_priority_header():
    assert _headers_for(background=True)[HEADER] == "background"


def test_default_client_sends_no_priority_header():
    # Default must stay interactive — the header is opt-in, never implicit.
    assert HEADER not in _headers_for()
    assert HEADER not in _headers_for(background=False)


def test_background_does_not_disturb_the_internal_key_header():
    h = _headers_for(background=True)
    assert h["X-AlgoVault-Internal-Key"] == "k" * 32
    assert h["Content-Type"] == "application/json"


def test_from_env_defaults_to_interactive():
    from algovault_bot.mcp_client import from_env

    sig = inspect.signature(from_env)
    assert sig.parameters["background"].default is False


def test_showcase_scan_opts_into_background():
    # The showcase is the ONE path that should be deferrable.
    src = inspect.getsource(adoption._scan_one_venue)
    assert "from_env(background=True)" in src


def test_live_scan_command_stays_interactive():
    # THE REGRESSION GUARD: a human ran /scan and is watching the chat. If this ever
    # picks up background priority it could sit in the batch lane for minutes.
    src = inspect.getsource(handlers._scan_via_mcp_impl)
    assert "background" not in src
    assert "from_env()" in src
