from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


ScalarValue = str | int | float | bool
SUPPORTED_PARAM_TYPES = {"string", "int", "float", "bool", "enum", "path"}
SUPPORTED_TRANSPORTS = {"ssh", "local"}
SUPPORTED_RUNNERS = {"tmux"}
SUPPORTED_CONCURRENCY_POLICIES = {"allow", "deny_if_running", "replace"}
LOG_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}


class ConfigError(ValueError):
    """Raised when the server configuration is invalid."""


@dataclass(frozen=True)
class ParamConfig:
    name: str
    type: str = "string"
    description: str = ""
    required: bool = True
    has_default: bool = False
    default: Any = None
    values: tuple[str, ...] = ()
    pattern: str | None = None
    minimum: float | None = None
    maximum: float | None = None
    allow_empty: bool = False
    must_be_relative: bool = False
    roots: tuple[str, ...] = ()


@dataclass(frozen=True)
class HostConfig:
    name: str
    transport: str
    destination: str | None = None
    ssh_command: str = "ssh"
    ssh_args: tuple[str, ...] = ()
    tmux_command: str = "tmux"
    shell_path: str = "/bin/sh"
    remote_state_dir: str = "/tmp/remote-dev-mcp"
    allowed_cwd_roots: tuple[str, ...] = ()
    variables: dict[str, ScalarValue] = field(default_factory=dict)
    env: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class ActionConfig:
    name: str
    description: str
    host: str | None
    program: str
    args: tuple[str, ...]
    cwd: str
    env: dict[str, str] = field(default_factory=dict)
    runner: str = "tmux"
    timeout_sec: int = 3600
    allowed_exit_codes: tuple[int, ...] = (0,)
    ready_regex: str | None = None
    concurrency_policy: str = "allow"
    category: str | None = None
    tags: tuple[str, ...] = ()
    history_limit: int = 50000
    params: dict[str, ParamConfig] = field(default_factory=dict)


@dataclass(frozen=True)
class ServerConfig:
    name: str = "remote-dev-mcp"
    instructions: str = (
        "Use list_actions to discover allowed operations. "
        "Use run_action for builds, tests, and instances. "
        "Poll with get_session and read_session_output. "
        "Do not assume arbitrary shell access exists."
    )
    default_host: str | None = None
    state_dir: Path = Path("~/.remote-dev-mcp").expanduser()
    session_prefix: str = "rdmcp"
    default_read_max_bytes: int = 32768
    max_read_max_bytes: int = 131072
    host: str = "127.0.0.1"
    port: int = 8000
    transport: str = "stdio"
    log_level: str = "INFO"
    variables: dict[str, ScalarValue] = field(default_factory=dict)


@dataclass(frozen=True)
class AppConfig:
    server: ServerConfig
    hosts: dict[str, HostConfig]
    actions: dict[str, ActionConfig]


def load_config(path: str | Path) -> AppConfig:
    config_path = Path(path).expanduser()
    try:
        raw = tomllib.loads(config_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigError(f"Config file not found: {config_path}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"Invalid TOML in {config_path}: {exc}") from exc

    server = _parse_server(raw.get("server", {}), config_path)
    hosts = _parse_hosts(raw.get("hosts", {}))
    actions = _parse_actions(raw.get("actions", {}), hosts, server)
    return AppConfig(server=server, hosts=hosts, actions=actions)


def _parse_server(raw: dict[str, Any], config_path: Path) -> ServerConfig:
    name = _get_string(raw, "name", default="remote-dev-mcp")
    instructions = _get_string(raw, "instructions", default=ServerConfig.instructions)
    default_host = _get_optional_string(raw, "default_host")
    state_dir = Path(_get_string(raw, "state_dir", default="~/.remote-dev-mcp")).expanduser()
    if not state_dir.is_absolute():
        state_dir = (config_path.parent / state_dir).resolve()
    session_prefix = _get_string(raw, "session_prefix", default="rdmcp")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", session_prefix):
        raise ConfigError("server.session_prefix must match [A-Za-z0-9_-]{1,32}")
    default_read_max_bytes = _get_int(raw, "default_read_max_bytes", default=32768, minimum=1024)
    max_read_max_bytes = _get_int(raw, "max_read_max_bytes", default=131072, minimum=1024)
    if default_read_max_bytes > max_read_max_bytes:
        raise ConfigError("server.default_read_max_bytes cannot exceed server.max_read_max_bytes")
    host = _get_string(raw, "host", default="127.0.0.1")
    port = _get_int(raw, "port", default=8000, minimum=1, maximum=65535)
    transport = _get_string(raw, "transport", default="stdio")
    if transport not in {"stdio", "streamable-http", "sse"}:
        raise ConfigError("server.transport must be stdio, streamable-http, or sse")
    log_level = _get_string(raw, "log_level", default="INFO").upper()
    if log_level not in LOG_LEVELS:
        raise ConfigError(f"server.log_level must be one of: {', '.join(sorted(LOG_LEVELS))}")
    variables = _parse_scalar_mapping(raw.get("variables", {}), "server.variables")
    return ServerConfig(
        name=name,
        instructions=instructions,
        default_host=default_host,
        state_dir=state_dir,
        session_prefix=session_prefix,
        default_read_max_bytes=default_read_max_bytes,
        max_read_max_bytes=max_read_max_bytes,
        host=host,
        port=port,
        transport=transport,
        log_level=log_level,
        variables=variables,
    )


def _parse_hosts(raw: dict[str, Any]) -> dict[str, HostConfig]:
    if not isinstance(raw, dict) or not raw:
        raise ConfigError("Config must define at least one host under [hosts.<name>]")
    hosts: dict[str, HostConfig] = {}
    for name, host_raw in raw.items():
        if not isinstance(host_raw, dict):
            raise ConfigError(f"hosts.{name} must be a table")
        transport = _get_string(host_raw, "transport")
        if transport not in SUPPORTED_TRANSPORTS:
            raise ConfigError(f"hosts.{name}.transport must be one of: {', '.join(sorted(SUPPORTED_TRANSPORTS))}")
        destination = _get_optional_string(host_raw, "destination")
        if transport == "ssh" and not destination:
            raise ConfigError(f"hosts.{name}.destination is required when transport=ssh")
        ssh_command = _get_string(host_raw, "ssh_command", default="ssh")
        ssh_args = tuple(_get_string_list(host_raw, "ssh_args", default=[]))
        tmux_command = _get_string(host_raw, "tmux_command", default="tmux")
        shell_path = _get_string(host_raw, "shell_path", default="/bin/sh")
        remote_state_dir = _get_string(host_raw, "remote_state_dir", default="/tmp/remote-dev-mcp")
        allowed_cwd_roots = tuple(_get_string_list(host_raw, "allowed_cwd_roots", default=[]))
        variables = _parse_scalar_mapping(host_raw.get("variables", {}), f"hosts.{name}.variables")
        env = _parse_env_mapping(host_raw.get("env", {}), f"hosts.{name}.env")
        hosts[name] = HostConfig(
            name=name,
            transport=transport,
            destination=destination,
            ssh_command=ssh_command,
            ssh_args=ssh_args,
            tmux_command=tmux_command,
            shell_path=shell_path,
            remote_state_dir=remote_state_dir,
            allowed_cwd_roots=allowed_cwd_roots,
            variables=variables,
            env=env,
        )
    return hosts


def _parse_actions(
    raw: dict[str, Any], hosts: dict[str, HostConfig], server: ServerConfig
) -> dict[str, ActionConfig]:
    if not isinstance(raw, dict) or not raw:
        raise ConfigError("Config must define at least one action under [actions.<name>]")
    actions: dict[str, ActionConfig] = {}
    for name, action_raw in raw.items():
        if not isinstance(action_raw, dict):
            raise ConfigError(f"actions.{name} must be a table")
        description = _get_string(action_raw, "description")
        host = _get_optional_string(action_raw, "host") or server.default_host
        if not host:
            raise ConfigError(f"actions.{name}.host is required because server.default_host is not set")
        if host not in hosts:
            raise ConfigError(f"actions.{name}.host references unknown host '{host}'")
        program = _get_string(action_raw, "program")
        args = tuple(_get_string_list(action_raw, "args", default=[]))
        cwd = _get_string(action_raw, "cwd")
        env = _parse_env_mapping(action_raw.get("env", {}), f"actions.{name}.env")
        runner = _get_string(action_raw, "runner", default="tmux")
        if runner not in SUPPORTED_RUNNERS:
            raise ConfigError(f"actions.{name}.runner must be one of: {', '.join(sorted(SUPPORTED_RUNNERS))}")
        timeout_sec = _get_int(action_raw, "timeout_sec", default=3600, minimum=1)
        allowed_exit_codes = tuple(_get_int_list(action_raw, "allowed_exit_codes", default=[0]))
        ready_regex = _get_optional_string(action_raw, "ready_regex")
        concurrency_policy = _get_string(action_raw, "concurrency_policy", default="allow")
        if concurrency_policy not in SUPPORTED_CONCURRENCY_POLICIES:
            raise ConfigError(
                "actions."
                f"{name}.concurrency_policy must be one of: {', '.join(sorted(SUPPORTED_CONCURRENCY_POLICIES))}"
            )
        category = _get_optional_string(action_raw, "category")
        tags = tuple(_get_string_list(action_raw, "tags", default=[]))
        history_limit = _get_int(action_raw, "history_limit", default=50000, minimum=1000)
        params = _parse_params(action_raw.get("params", {}), action_name=name)
        actions[name] = ActionConfig(
            name=name,
            description=description,
            host=host,
            program=program,
            args=args,
            cwd=cwd,
            env=env,
            runner=runner,
            timeout_sec=timeout_sec,
            allowed_exit_codes=allowed_exit_codes,
            ready_regex=ready_regex,
            concurrency_policy=concurrency_policy,
            category=category,
            tags=tags,
            history_limit=history_limit,
            params=params,
        )
    return actions


def _parse_params(raw: dict[str, Any], action_name: str) -> dict[str, ParamConfig]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ConfigError(f"actions.{action_name}.params must be a table")
    params: dict[str, ParamConfig] = {}
    for name, param_raw in raw.items():
        if not isinstance(param_raw, dict):
            raise ConfigError(f"actions.{action_name}.params.{name} must be a table")
        param_type = _get_string(param_raw, "type", default="string")
        if param_type not in SUPPORTED_PARAM_TYPES:
            raise ConfigError(
                f"actions.{action_name}.params.{name}.type must be one of: {', '.join(sorted(SUPPORTED_PARAM_TYPES))}"
            )
        has_default = "default" in param_raw
        required = bool(param_raw.get("required", not has_default))
        default = param_raw.get("default")
        values = tuple(str(value) for value in param_raw.get("values", []))
        pattern = _get_optional_string(param_raw, "pattern")
        if pattern is not None:
            re.compile(pattern)
        minimum = _get_optional_number(param_raw, "min")
        maximum = _get_optional_number(param_raw, "max")
        if minimum is not None and maximum is not None and minimum > maximum:
            raise ConfigError(f"actions.{action_name}.params.{name}: min cannot exceed max")
        allow_empty = bool(param_raw.get("allow_empty", False))
        must_be_relative = bool(param_raw.get("must_be_relative", False))
        roots = tuple(_get_string_list(param_raw, "roots", default=[]))
        if param_type == "path":
            if must_be_relative and roots:
                raise ConfigError(
                    f"actions.{action_name}.params.{name}: roots cannot be combined with must_be_relative=true"
                )
            if not must_be_relative and not roots:
                raise ConfigError(
                    f"actions.{action_name}.params.{name}: path params must set roots or must_be_relative=true"
                )
        params[name] = ParamConfig(
            name=name,
            type=param_type,
            description=_get_string(param_raw, "description", default=""),
            required=required,
            has_default=has_default,
            default=default,
            values=values,
            pattern=pattern,
            minimum=minimum,
            maximum=maximum,
            allow_empty=allow_empty,
            must_be_relative=must_be_relative,
            roots=roots,
        )
    return params


def _parse_scalar_mapping(raw: Any, field_name: str) -> dict[str, ScalarValue]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ConfigError(f"{field_name} must be a table")
    result: dict[str, ScalarValue] = {}
    for key, value in raw.items():
        if not isinstance(key, str):
            raise ConfigError(f"{field_name} keys must be strings")
        if not isinstance(value, (str, int, float, bool)):
            raise ConfigError(f"{field_name}.{key} must be a scalar string/int/float/bool")
        result[key] = value
    return result


def _parse_string_mapping(raw: Any, field_name: str) -> dict[str, str]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ConfigError(f"{field_name} must be a table")
    result: dict[str, str] = {}
    for key, value in raw.items():
        if not isinstance(key, str) or not isinstance(value, str):
            raise ConfigError(f"{field_name} must map strings to strings")
        result[key] = value
    return result


def _parse_env_mapping(raw: Any, field_name: str) -> dict[str, str]:
    result = _parse_string_mapping(raw, field_name)
    for key in result:
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key) is None:
            raise ConfigError(f"{field_name}.{key} is not a valid environment variable name")
    return result


def _get_string(raw: dict[str, Any], key: str, default: str | None = None) -> str:
    if key not in raw:
        if default is None:
            raise ConfigError(f"Missing required string field: {key}")
        return default
    value = raw[key]
    if not isinstance(value, str) or value == "":
        raise ConfigError(f"{key} must be a non-empty string")
    return value


def _get_optional_string(raw: dict[str, Any], key: str) -> str | None:
    value = raw.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or value == "":
        raise ConfigError(f"{key} must be a non-empty string when provided")
    return value


def _get_string_list(raw: dict[str, Any], key: str, default: list[str]) -> list[str]:
    value = raw.get(key, default)
    if not isinstance(value, list) or not all(isinstance(item, str) and item != "" for item in value):
        raise ConfigError(f"{key} must be a list of non-empty strings")
    return value


def _get_int_list(raw: dict[str, Any], key: str, default: list[int]) -> list[int]:
    value = raw.get(key, default)
    if not isinstance(value, list) or not all(isinstance(item, int) for item in value):
        raise ConfigError(f"{key} must be a list of integers")
    return value


def _get_int(
    raw: dict[str, Any],
    key: str,
    default: int | None = None,
    minimum: int | None = None,
    maximum: int | None = None,
) -> int:
    if key not in raw:
        if default is None:
            raise ConfigError(f"Missing required integer field: {key}")
        value = default
    else:
        value = raw[key]
    if not isinstance(value, int):
        raise ConfigError(f"{key} must be an integer")
    if minimum is not None and value < minimum:
        raise ConfigError(f"{key} must be >= {minimum}")
    if maximum is not None and value > maximum:
        raise ConfigError(f"{key} must be <= {maximum}")
    return value


def _get_optional_number(raw: dict[str, Any], key: str) -> float | None:
    value = raw.get(key)
    if value is None:
        return None
    if not isinstance(value, (int, float)):
        raise ConfigError(f"{key} must be numeric")
    return float(value)
