"""Minimal MCP Streamable HTTP client.

We talk to ``crypto-quant-signal-mcp`` at ``ALGOVAULT_MCP_URL`` (default
``http://127.0.0.1:3000/mcp`` — loopback on the same Hetzner host, no
Caddy/Cloudflare round-trip). Auth is the D1-C internal-bypass header
(``X-AlgoVault-Internal-Key``) — server-side this maps to ``tier:'internal'``
which bypasses the quota counter; bot-side enforces user-level quota
in its own SQLite.

Each call opens a fresh session (initialize → notifications/initialized →
tools/call → close). Sessions are light on the server (UUID + transport
in a Map). Cron-fire frequency caps wall-clock at one batch per minute
worst case, so the per-call session overhead is negligible.

The server responds with either ``application/json`` (single response)
or ``text/event-stream`` (SSE). We handle both. SSE format: ``data: <json>\\n\\n``
per JSON-RPC message; we read the first non-empty data: line.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from typing import Any

import httpx


log = logging.getLogger(__name__)

DEFAULT_MCP_URL = "http://127.0.0.1:3000/mcp"
PROTOCOL_VERSION = "2024-11-05"
HTTP_TIMEOUT = 30.0


class McpError(RuntimeError):
    """Raised when the MCP server returns an error or the transport fails."""


@dataclass
class McpClientConfig:
    url: str = DEFAULT_MCP_URL
    internal_bypass_key: str = ""
    client_name: str = "algovault-bot"
    client_version: str = "0.1"
    #: OPS-HL-INTERACTIVE-PRIORITY-W1 — mark this client's calls as background work.
    #: Sends ``X-AlgoVault-Priority: background``; signal-MCP then runs the request's
    #: venue fan-out in its BATCH rate-limit lane, waiting for capacity instead of
    #: consuming Hyperliquid's interactive reserve (which exists for LIVE callers).
    #: Set it ONLY on scheduled/cron work — never on a path a human is waiting on,
    #: because the batch lane may wait up to ~5 minutes.
    background: bool = False


def _parse_response(resp: httpx.Response) -> dict[str, Any]:
    """Return the first JSON-RPC payload found in the response.

    application/json → JSON.parse the body.
    text/event-stream → find the first ``data: ...`` line and JSON.parse it.
    """
    ctype = resp.headers.get("content-type", "")
    body = resp.text
    if "text/event-stream" in ctype:
        for raw in body.splitlines():
            line = raw.strip()
            if line.startswith("data:"):
                payload = line[len("data:") :].strip()
                if payload and payload != "[DONE]":
                    return json.loads(payload)
        raise McpError(f"No data: line found in SSE body: {body[:200]!r}")
    if ctype.startswith("application/json"):
        return json.loads(body)
    # Fallback: try JSON, then SSE
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        for raw in body.splitlines():
            line = raw.strip()
            if line.startswith("data:"):
                return json.loads(line[len("data:") :].strip())
        raise McpError(
            f"Unexpected MCP response shape (status={resp.status_code} ctype={ctype!r}): "
            f"{body[:200]!r}"
        )


class McpClient:
    """One-shot Streamable HTTP MCP client.

    Use as a context manager:

        with McpClient(config) as cli:
            result = cli.call_tool("get_trade_call", {...})
    """

    def __init__(self, config: McpClientConfig) -> None:
        self.config = config
        self._http = httpx.Client(timeout=HTTP_TIMEOUT)
        self._session_id: str | None = None
        self._next_id = 0

    def __enter__(self) -> "McpClient":
        self._initialize()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:  # noqa: ANN001
        try:
            self._close_session()
        finally:
            self._http.close()

    def _headers(self) -> dict[str, str]:
        h: dict[str, str] = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if self.config.internal_bypass_key:
            h["X-AlgoVault-Internal-Key"] = self.config.internal_bypass_key
        if self.config.background:
            # Honoured server-side for tier:'internal' callers only.
            h["X-AlgoVault-Priority"] = "background"
        if self._session_id:
            h["Mcp-Session-Id"] = self._session_id
        return h

    def _next_request_id(self) -> int:
        self._next_id += 1
        return self._next_id

    def _post(self, payload: dict[str, Any]) -> tuple[httpx.Response, dict[str, Any] | None]:
        resp = self._http.post(self.config.url, headers=self._headers(), json=payload)
        # Capture Mcp-Session-Id from response (init returns it; subsequent reuse)
        sid = resp.headers.get("mcp-session-id") or resp.headers.get("Mcp-Session-Id")
        if sid and not self._session_id:
            self._session_id = sid
        if resp.status_code == 202:
            # Notifications/responses without a body
            return resp, None
        if resp.status_code >= 400:
            raise McpError(
                f"HTTP {resp.status_code} from MCP server: {resp.text[:300]!r}"
            )
        return resp, _parse_response(resp)

    def _initialize(self) -> None:
        payload = {
            "jsonrpc": "2.0",
            "id": self._next_request_id(),
            "method": "initialize",
            "params": {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {
                    "name": self.config.client_name,
                    "version": self.config.client_version,
                },
            },
        }
        _, body = self._post(payload)
        if not body or "result" not in body:
            raise McpError(f"initialize returned no result: {body!r}")
        # Send the post-init notification (no response expected).
        notif = {
            "jsonrpc": "2.0",
            "method": "notifications/initialized",
            "params": {},
        }
        # 202 No Content is the normal response.
        self._http.post(self.config.url, headers=self._headers(), json=notif)

    def _close_session(self) -> None:
        if not self._session_id:
            return
        try:
            # MCP spec: DELETE /mcp with Mcp-Session-Id closes the session.
            self._http.delete(self.config.url, headers=self._headers())
        except httpx.HTTPError:
            pass

    def read_resource(self, uri: str) -> dict[str, Any]:
        """Read an MCP resource and return its parsed contents.

        Used by OPS-TRADE-CALL-CLUSTER-W1 CH4 coverage nudge to consume
        `performance://signal-performance` for per-venue+TF signal volume
        without adding a new producer/consumer edge (the bot↔signal-MCP
        edge already exists; this just widens the surface used).
        """
        payload = {
            "jsonrpc": "2.0",
            "id": self._next_request_id(),
            "method": "resources/read",
            "params": {"uri": uri},
        }
        _, body = self._post(payload)
        if not body or "result" not in body:
            raise McpError(f"resources/read returned no result: {body!r}")
        contents = body["result"].get("contents") or []
        if contents and isinstance(contents[0], dict):
            text = contents[0].get("text", "")
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                return {"_raw_text": text}
        return body["result"]

    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Invoke an MCP tool and return its parsed result.

        Tool results are wrapped in
        ``{"content": [{"type": "text", "text": "<JSON>"}]}``. We unwrap one
        layer and JSON-parse the inner text — that's the actual tool output.
        """
        payload = {
            "jsonrpc": "2.0",
            "id": self._next_request_id(),
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        }
        _, body = self._post(payload)
        if not body or "result" not in body:
            raise McpError(f"tools/call returned no result: {body!r}")
        result = body["result"]
        # Unwrap MCP content envelope.
        content = result.get("content") or []
        if content and isinstance(content[0], dict) and content[0].get("type") == "text":
            text = content[0].get("text", "")
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                # Tool returned plain text (e.g. an error string)
                return {"_raw_text": text}
        return result


def from_env(background: bool = False) -> McpClient:
    """Build an McpClient using the bot's standard env vars.

    ``background=True`` marks every call from this client as deferrable work
    (OPS-HL-INTERACTIVE-PRIORITY-W1). Use it for cron/scheduled paths only — see
    ``McpClientConfig.background``.
    """
    cfg = McpClientConfig(
        url=os.environ.get("ALGOVAULT_MCP_URL", DEFAULT_MCP_URL),
        internal_bypass_key=os.environ.get("ALGOVAULT_INTERNAL_BYPASS_KEY", "").strip(),
        background=background,
    )
    if not cfg.internal_bypass_key or cfg.internal_bypass_key == "__C3_PLACEHOLDER__":
        raise McpError(
            "ALGOVAULT_INTERNAL_BYPASS_KEY not configured — cron will not be able "
            "to reach signal-MCP without bypassing the IP-hash quota counter."
        )
    return McpClient(cfg)
