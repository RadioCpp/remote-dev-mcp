from __future__ import annotations

import argparse
from dataclasses import replace
from pathlib import Path

from .config import ConfigError, load_config
from .service import RemoteDevService
from .session_store import SessionStore


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Config-driven MCP server for remote dev workflows")
    parser.add_argument(
        "--config",
        default="config.toml",
        help="Path to the TOML config file",
    )
    parser.add_argument(
        "--check-config",
        action="store_true",
        help="Validate the config and print a short summary without starting the server",
    )
    parser.add_argument(
        "--cleanup-sessions",
        action="store_true",
        help="Delete old session records, optionally removing remote log/status files too",
    )
    parser.add_argument(
        "--max-age-hours",
        type=float,
        default=168.0,
        help="Age threshold in hours for --cleanup-sessions",
    )
    parser.add_argument(
        "--cleanup-status",
        action="append",
        dest="cleanup_statuses",
        help="Status to include in --cleanup-sessions. Repeat to pass multiple values.",
    )
    parser.add_argument(
        "--max-delete",
        type=int,
        default=100,
        help="Maximum number of sessions to delete during --cleanup-sessions",
    )
    parser.add_argument(
        "--remove-remote-state",
        action="store_true",
        help="Also delete remote log/status files during --cleanup-sessions",
    )
    parser.add_argument(
        "--diagnose-host",
        help="Run connectivity and tool diagnostics for a configured host and exit",
    )
    parser.add_argument(
        "--transport",
        choices=["stdio", "streamable-http", "sse"],
        help="Override the configured MCP transport",
    )
    parser.add_argument("--host", help="Override HTTP bind host for non-stdio transports")
    parser.add_argument("--port", type=int, help="Override HTTP bind port for non-stdio transports")
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    try:
        config = load_config(Path(args.config))
    except ConfigError as exc:
        raise SystemExit(str(exc)) from exc
    server_config = config.server
    if args.transport:
        server_config = replace(server_config, transport=args.transport)
    if args.host:
        server_config = replace(server_config, host=args.host)
    if args.port:
        server_config = replace(server_config, port=args.port)
    config = replace(config, server=server_config)
    if args.check_config:
        print(_describe_config(config))
        return
    store = SessionStore(server_config.state_dir / "sessions.sqlite3")
    service = RemoteDevService(config, store)
    if args.cleanup_sessions:
        result = service.cleanup_sessions(
            max_age_hours=args.max_age_hours,
            statuses=args.cleanup_statuses,
            remove_remote_state=args.remove_remote_state,
            max_delete=args.max_delete,
        )
        print(_describe_cleanup(result))
        return
    if args.diagnose_host:
        result = service.diagnose_host(args.diagnose_host)
        print(_describe_diagnostics(result))
        raise SystemExit(0 if result["ok"] else 1)
    try:
        from .app import create_mcp_app
    except ModuleNotFoundError as exc:
        if exc.name == "mcp":
            raise SystemExit(
                "The 'mcp' package is required to run the server. Install project dependencies first."
            ) from exc
        raise
    app = create_mcp_app(config, service)
    app.run(transport=server_config.transport)


def _describe_config(config) -> str:
    action_lines = []
    for action_name in sorted(config.actions):
        action = config.actions[action_name]
        action_lines.append(
            f"- {action.name}: host={action.host} runner={action.runner} timeout={action.timeout_sec}s params={len(action.params)}"
        )
    host_lines = []
    for host_name in sorted(config.hosts):
        host = config.hosts[host_name]
        destination = host.destination or "local"
        host_lines.append(f"- {host.name}: transport={host.transport} destination={destination}")
    lines = [
        f"Config OK: {config.server.name}",
        f"State dir: {config.server.state_dir}",
        f"Default host: {config.server.default_host or '<none>'}",
        "Hosts:",
        *host_lines,
        f"Actions: {len(config.actions)}",
        *action_lines,
    ]
    return "\n".join(lines)


def _describe_cleanup(result) -> str:
    lines = [
        f"Deleted sessions: {result['deleted_count']}",
        f"Skipped sessions: {result['skipped_count']}",
        f"Errors: {result['error_count']}",
    ]
    if result["deleted_session_ids"]:
        lines.append("Deleted IDs:")
        lines.extend(f"- {session_id}" for session_id in result["deleted_session_ids"])
    if result["errors"]:
        lines.append("Cleanup errors:")
        lines.extend(f"- {item['session_id']}: {item['error']}" for item in result["errors"])
    return "\n".join(lines)


def _describe_diagnostics(result) -> str:
    lines = [
        f"Host: {result['host']}",
        f"Transport: {result['transport']}",
        f"Destination: {result['destination']}",
        f"Overall: {'OK' if result['ok'] else 'FAILED'}",
        "Checks:",
    ]
    for check in result["checks"]:
        status = "OK" if check["ok"] else ("WARN" if not check["required"] else "FAIL")
        lines.append(f"- [{status}] {check['name']}: {check['detail']}")
    return "\n".join(lines)


if __name__ == "__main__":
    main()
