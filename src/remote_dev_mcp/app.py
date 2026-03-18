from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import FastMCP

from .config import AppConfig
from .service import RemoteDevService


def create_mcp_app(config: AppConfig, service: RemoteDevService) -> FastMCP:
    mcp = FastMCP(
        name=config.server.name,
        instructions=config.server.instructions,
        host=config.server.host,
        port=config.server.port,
        log_level=config.server.log_level,
        json_response=True,
        stateless_http=True,
    )

    @mcp.tool()
    def list_actions() -> dict[str, Any]:
        """List all configured actions and their parameter schema."""

        return service.list_actions()

    @mcp.tool()
    def run_action(action: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """Run a configured action inside tmux and return the created session metadata."""

        return service.run_action(action, params=params)

    @mcp.tool()
    def get_session(session_id: str) -> dict[str, Any]:
        """Read the latest known session state."""

        return service.get_session(session_id)

    @mcp.tool()
    def list_sessions(
        limit: int = 20,
        statuses: list[str] | None = None,
        action: str | None = None,
        host: str | None = None,
        refresh: bool = False,
    ) -> dict[str, Any]:
        """List known sessions from local state, optionally refreshing status from the target host."""

        return service.list_sessions(
            limit=limit,
            statuses=statuses,
            action=action,
            host=host,
            refresh=refresh,
        )

    @mcp.tool()
    def read_session_output(
        session_id: str,
        offset: int = 0,
        max_bytes: int | None = None,
        tail_lines: int | None = None,
    ) -> dict[str, Any]:
        """Read session output by byte offset or by tailing the last N lines."""

        return service.read_session_output(
            session_id=session_id,
            offset=offset,
            max_bytes=max_bytes,
            tail_lines=tail_lines,
        )

    @mcp.tool()
    def stop_session(session_id: str) -> dict[str, Any]:
        """Stop a running tmux-backed session."""

        return service.stop_session(session_id)

    return mcp
