from __future__ import annotations

import json
import posixpath
import re
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from .config import ActionConfig, AppConfig, HostConfig, ParamConfig
from .executor import ExecutionError, create_executor
from .session_store import SessionRecord, SessionStore
from .shell import is_within_roots, normalize_posix_path, shell_join, shell_quote
from .templates import TemplateError, render_mapping, render_sequence, render_template


class ServiceError(RuntimeError):
    """Raised when a tool request cannot be fulfilled."""


EXIT_TIMEOUT = 124
EXIT_CWD_UNAVAILABLE = 200
EXIT_ENV_INVALID = 201
TERMINAL_STATUSES = {"completed", "failed", "stopped", "lost"}
ACTIVE_STATUSES = {"starting", "running"}


@dataclass(frozen=True)
class PreparedInvocation:
    session_id: str
    action: ActionConfig
    host: HostConfig
    program: str
    args: list[str]
    cwd: str
    env: dict[str, str]
    tmux_session_name: str
    remote_log_path: str
    remote_status_path: str
    command_preview: str
    params: dict[str, Any]
    created_at: str


class RemoteDevService:
    def __init__(self, config: AppConfig, store: SessionStore):
        self._config = config
        self._store = store
        self._executor_cache: dict[str, Any] = {}
        self._tmux_checked: set[str] = set()

    def list_actions(self) -> dict[str, Any]:
        actions: list[dict[str, Any]] = []
        for action in sorted(self._config.actions.values(), key=lambda item: item.name):
            actions.append(
                {
                    "name": action.name,
                    "description": action.description,
                    "host": action.host,
                    "category": action.category,
                    "tags": list(action.tags),
                    "runner": action.runner,
                    "timeout_sec": action.timeout_sec,
                    "ready_regex": action.ready_regex,
                    "concurrency_policy": action.concurrency_policy,
                    "params": {
                        name: {
                            "type": param.type,
                            "description": param.description,
                            "required": param.required,
                            "default": param.default if param.has_default else None,
                            "values": list(param.values),
                            "pattern": param.pattern,
                            "min": param.minimum,
                            "max": param.maximum,
                            "allow_empty": param.allow_empty,
                            "must_be_relative": param.must_be_relative,
                            "roots": list(param.roots),
                        }
                        for name, param in sorted(action.params.items())
                    },
                }
            )
        return {"actions": actions}

    def list_sessions(
        self,
        limit: int = 20,
        statuses: list[str] | None = None,
        action: str | None = None,
        host: str | None = None,
        refresh: bool = False,
    ) -> dict[str, Any]:
        if limit <= 0:
            raise ServiceError("limit must be > 0")
        normalized_statuses = _normalize_statuses(statuses)
        records = self._store.list(
            limit=limit,
            statuses=normalized_statuses,
            action_name=action,
            host_name=host,
            newest_first=True,
        )
        if refresh:
            sessions = [self.get_session(record.session_id) for record in records]
        else:
            sessions = [self._record_to_summary(record) for record in records]
        return {"sessions": sessions}

    def run_action(self, action_name: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        action = self._get_action(action_name)
        host = self._get_host(action.host)
        invocation = self._prepare_invocation(action, host, params or {})
        executor = self._get_executor(host)
        self._ensure_tmux_available(host.name)
        replaced_session_ids = self._enforce_concurrency_policy(action, host, invocation.params)
        self._store.put(
            SessionRecord(
                session_id=invocation.session_id,
                action_name=action.name,
                host_name=host.name,
                tmux_session_name=invocation.tmux_session_name,
                remote_log_path=invocation.remote_log_path,
                remote_status_path=invocation.remote_status_path,
                cwd=invocation.cwd,
                command_preview=invocation.command_preview,
                env=invocation.env,
                args=invocation.args,
                allowed_exit_codes=list(action.allowed_exit_codes),
                created_at=invocation.created_at,
                status="starting",
                params=invocation.params,
                ready=False,
                ready_at=None,
            )
        )
        launch_script = self._build_tmux_launch_script(invocation)
        result = self._run_script(executor, host, launch_script, timeout_sec=30)
        if result.exit_code != 0:
            self._store.put(
                SessionRecord(
                    session_id=invocation.session_id,
                    action_name=action.name,
                    host_name=host.name,
                    tmux_session_name=invocation.tmux_session_name,
                    remote_log_path=invocation.remote_log_path,
                    remote_status_path=invocation.remote_status_path,
                    cwd=invocation.cwd,
                    command_preview=invocation.command_preview,
                    env=invocation.env,
                    args=invocation.args,
                    allowed_exit_codes=list(action.allowed_exit_codes),
                    created_at=invocation.created_at,
                    status="failed",
                    params=invocation.params,
                    note=(result.stderr or result.stdout).strip() or "Failed to start tmux session",
                )
            )
            raise ServiceError((result.stderr or result.stdout).strip() or "Failed to start remote session")
        session = self.get_session(invocation.session_id)
        if replaced_session_ids:
            session["replaced_session_ids"] = replaced_session_ids
        return session

    def get_session(self, session_id: str) -> dict[str, Any]:
        record = self._require_session(session_id)
        action = self._get_action(record.action_name)
        host = self._get_host(record.host_name)
        executor = self._get_executor(host)
        status_payload = self._read_remote_status(executor, host, record.remote_status_path)
        running = self._tmux_has_session(executor, host, record.tmux_session_name)
        if status_payload is None and not running and record.status not in TERMINAL_STATUSES:
            for _ in range(5):
                time.sleep(0.1)
                status_payload = self._read_remote_status(executor, host, record.remote_status_path)
                if status_payload is not None:
                    break
                running = self._tmux_has_session(executor, host, record.tmux_session_name)
                if running:
                    break
        log_size = self._read_log_size(executor, host, record.remote_log_path)
        local_status = record.status
        exit_code = record.exit_code
        finished_at = record.finished_at
        note = record.note
        ready = record.ready
        ready_at = record.ready_at
        if status_payload is not None:
            exit_code = _coerce_int(status_payload.get("exit_code"))
            finished_at = _coerce_string(status_payload.get("finished_at"))
            note = _coerce_string(status_payload.get("note")) or note
            payload_state = _coerce_string(status_payload.get("state"))
            if payload_state == "stopped":
                local_status = payload_state
            elif exit_code is not None and exit_code in record.allowed_exit_codes:
                local_status = "completed"
                if note == "Command failed":
                    note = None
            elif exit_code is not None:
                local_status = "failed"
            elif payload_state in {"completed", "failed"}:
                local_status = payload_state
            else:
                if running:
                    local_status = "running"
                else:
                    local_status = "failed"
                note = note or "Malformed status payload"
        elif running:
            local_status = "running"
        elif local_status not in TERMINAL_STATUSES:
            local_status = "lost"
        if action.ready_regex:
            if not ready and local_status != "lost":
                if self._log_matches_pattern(executor, host, record.remote_log_path, action.ready_regex):
                    ready = True
                    ready_at = ready_at or _utc_now()
        else:
            ready = False
            ready_at = None
        updated = SessionRecord(
            session_id=record.session_id,
            action_name=record.action_name,
            host_name=record.host_name,
            tmux_session_name=record.tmux_session_name,
            remote_log_path=record.remote_log_path,
            remote_status_path=record.remote_status_path,
            cwd=record.cwd,
            command_preview=record.command_preview,
            env=record.env,
            args=record.args,
            allowed_exit_codes=record.allowed_exit_codes,
            created_at=record.created_at,
            status=local_status,
            params=record.params,
            ready=ready,
            ready_at=ready_at,
            exit_code=exit_code,
            finished_at=finished_at,
            note=note,
        )
        self._store.put(updated)
        return {
            "session_id": updated.session_id,
            "action": updated.action_name,
            "host": updated.host_name,
            "status": updated.status,
            "created_at": updated.created_at,
            "finished_at": updated.finished_at,
            "exit_code": updated.exit_code,
            "cwd": updated.cwd,
            "command_preview": updated.command_preview,
            "params": updated.params,
            "tmux_session_name": updated.tmux_session_name,
            "log_size_bytes": log_size,
            "attach_command": executor.attach_command(host.tmux_command, updated.tmux_session_name),
            "note": updated.note,
            "ready_check": bool(action.ready_regex),
            "ready": updated.ready if action.ready_regex else None,
            "ready_at": updated.ready_at,
        }

    def read_session_output(
        self,
        session_id: str,
        offset: int = 0,
        max_bytes: int | None = None,
        tail_lines: int | None = None,
    ) -> dict[str, Any]:
        record = self._require_session(session_id)
        host = self._get_host(record.host_name)
        executor = self._get_executor(host)
        if offset < 0:
            raise ServiceError("offset must be >= 0")
        if tail_lines is not None and tail_lines <= 0:
            raise ServiceError("tail_lines must be > 0")
        limit = self._config.server.default_read_max_bytes if max_bytes is None else max_bytes
        if limit <= 0:
            raise ServiceError("max_bytes must be > 0")
        if limit > self._config.server.max_read_max_bytes:
            raise ServiceError(
                f"max_bytes exceeds configured cap of {self._config.server.max_read_max_bytes}"
            )
        log_size = self._read_log_size(executor, host, record.remote_log_path)
        if tail_lines is not None:
            script = (
                f"if [ -f {shell_quote(record.remote_log_path)} ]; then "
                f"tail -n {int(tail_lines)} {shell_quote(record.remote_log_path)}; fi"
            )
            content_bytes = self._run_script_bytes(executor, host, script, timeout_sec=15).stdout
            return {
                "session_id": record.session_id,
                "mode": "tail",
                "tail_lines": tail_lines,
                "content": content_bytes.decode("utf-8", errors="replace"),
                "next_offset": log_size,
                "log_size_bytes": log_size,
            }
        if offset > log_size:
            offset = log_size
        if offset == log_size:
            return {
                "session_id": record.session_id,
                "mode": "offset",
                "offset": offset,
                "content": "",
                "next_offset": offset,
                "log_size_bytes": log_size,
            }
        start_byte = offset + 1
        script = (
            f"if [ -f {shell_quote(record.remote_log_path)} ]; then "
            f"tail -c +{start_byte} {shell_quote(record.remote_log_path)} | head -c {int(limit)}; fi"
        )
        output = self._run_script_bytes(executor, host, script, timeout_sec=15).stdout
        next_offset = offset + len(output)
        return {
            "session_id": record.session_id,
            "mode": "offset",
            "offset": offset,
            "content": output.decode("utf-8", errors="replace"),
            "next_offset": next_offset,
            "log_size_bytes": log_size,
            "truncated": next_offset < log_size,
        }

    def stop_session(self, session_id: str) -> dict[str, Any]:
        current = self.get_session(session_id)
        if current["status"] in TERMINAL_STATUSES:
            return current
        record = self._require_session(session_id)
        host = self._get_host(record.host_name)
        executor = self._get_executor(host)
        stop_marker_path = f"{record.remote_status_path}.stop"
        kill_script = (
            f": > {shell_quote(stop_marker_path)}\n"
            f"{shell_quote(host.tmux_command)} has-session -t {shell_quote(record.tmux_session_name)} "
            f"2>/dev/null && {shell_quote(host.tmux_command)} kill-session -t {shell_quote(record.tmux_session_name)} || true"
        )
        self._run_script(executor, host, kill_script, timeout_sec=15)
        stopped = SessionRecord(
            session_id=record.session_id,
            action_name=record.action_name,
            host_name=record.host_name,
            tmux_session_name=record.tmux_session_name,
            remote_log_path=record.remote_log_path,
            remote_status_path=record.remote_status_path,
            cwd=record.cwd,
            command_preview=record.command_preview,
            env=record.env,
            args=record.args,
            allowed_exit_codes=record.allowed_exit_codes,
            created_at=record.created_at,
            status="stopped",
            params=record.params,
            ready=record.ready,
            ready_at=record.ready_at,
            exit_code=record.exit_code,
            finished_at=record.finished_at,
            note="Stopped by user request",
        )
        self._store.put(stopped)
        deadline = time.monotonic() + 5.0
        latest: dict[str, Any] | None = None
        while time.monotonic() < deadline:
            latest = self.get_session(session_id)
            if latest["status"] in TERMINAL_STATUSES:
                return latest
            time.sleep(0.1)
        return latest or self.get_session(session_id)

    def cleanup_sessions(
        self,
        max_age_hours: float = 168.0,
        statuses: list[str] | None = None,
        remove_remote_state: bool = False,
        max_delete: int = 100,
    ) -> dict[str, Any]:
        if max_age_hours < 0:
            raise ServiceError("max_age_hours must be >= 0")
        if max_delete <= 0:
            raise ServiceError("max_delete must be > 0")
        normalized_statuses = _normalize_statuses(statuses)
        cutoff = datetime.now(timezone.utc) - timedelta(hours=max_age_hours)
        candidates = self._store.list(
            limit=max_delete,
            statuses=None,
            newest_first=False,
        )
        deleted: list[str] = []
        skipped: list[str] = []
        errors: list[dict[str, str]] = []
        for record in candidates:
            if record.status not in TERMINAL_STATUSES:
                try:
                    self.get_session(record.session_id)
                except ServiceError as exc:
                    errors.append({"session_id": record.session_id, "error": str(exc)})
                    continue
                record = self._require_session(record.session_id)
            allowed_statuses = set(normalized_statuses) if normalized_statuses else TERMINAL_STATUSES
            if record.status not in allowed_statuses:
                skipped.append(record.session_id)
                continue
            reference_time = _parse_timestamp(record.finished_at) or _parse_timestamp(record.created_at)
            if reference_time is None or reference_time > cutoff:
                skipped.append(record.session_id)
                continue
            if remove_remote_state:
                try:
                    self._delete_remote_state(record)
                except ServiceError as exc:
                    errors.append({"session_id": record.session_id, "error": str(exc)})
                    continue
            self._store.delete(record.session_id)
            deleted.append(record.session_id)
        return {
            "deleted_count": len(deleted),
            "deleted_session_ids": deleted,
            "skipped_count": len(skipped),
            "skipped_session_ids": skipped,
            "error_count": len(errors),
            "errors": errors,
        }

    def diagnose_host(self, host_name: str) -> dict[str, Any]:
        host = self._get_host(host_name)
        executor = self._get_executor(host)
        checks: list[dict[str, Any]] = []
        connectivity = self._diagnose_script_check(
            executor,
            host,
            name="connectivity",
            script="printf 'ok'",
            required=True,
        )
        checks.append(connectivity)
        if not connectivity["ok"]:
            return {
                "host": host.name,
                "transport": host.transport,
                "destination": host.destination or "local",
                "ok": False,
                "checks": checks,
            }
        checks.append(
            self._diagnose_script_check(
                executor,
                host,
                name="shell_path",
                script=(
                    f"if [ -x {shell_quote(host.shell_path)} ]; then "
                    f"printf '%s' {shell_quote(host.shell_path)}; else exit 1; fi"
                ),
                required=True,
            )
        )
        checks.append(
            self._diagnose_argv_check(
                executor,
                host,
                name="tmux",
                argv=[host.tmux_command, "-V"],
                required=True,
            )
        )
        for command in ("grep", "sed", "tail", "wc", "tr", "date", "cat", "mv", "rm", "mkdir"):
            checks.append(self._diagnose_command_check(executor, host, command, required=True))
        checks.append(self._diagnose_command_check(executor, host, "setsid", required=False))
        for check_name, program in self._diagnostic_action_programs(host.name):
            checks.append(self._diagnose_program_check(executor, host, check_name, program, required=True))
        ok = all(check["ok"] or not check["required"] for check in checks)
        return {
            "host": host.name,
            "transport": host.transport,
            "destination": host.destination or "local",
            "ok": ok,
            "checks": checks,
        }

    def _prepare_invocation(
        self, action: ActionConfig, host: HostConfig, raw_params: dict[str, Any]
    ) -> PreparedInvocation:
        validated_params = self._validate_params(action, raw_params)
        created_at = _utc_now()
        session_id = uuid.uuid4().hex[:12]
        tmux_session_name = f"{self._config.server.session_prefix}-{session_id}"
        template_context: dict[str, Any] = {
            **self._config.server.variables,
            **host.variables,
            **validated_params,
            "session_id": session_id,
        }
        try:
            program = render_template(action.program, template_context)
            args = render_sequence(action.args, template_context)
            cwd = render_template(action.cwd, template_context)
            env = {**render_mapping(host.env, template_context), **render_mapping(action.env, template_context)}
        except TemplateError as exc:
            raise ServiceError(str(exc)) from exc
        cwd = normalize_posix_path(cwd)
        if host.allowed_cwd_roots and not is_within_roots(cwd, list(host.allowed_cwd_roots)):
            roots = ", ".join(host.allowed_cwd_roots)
            raise ServiceError(f"Resolved cwd '{cwd}' is outside allowed_cwd_roots: {roots}")
        remote_log_path = posixpath.join(host.remote_state_dir, "logs", f"{session_id}.log")
        remote_status_path = posixpath.join(host.remote_state_dir, "status", f"{session_id}.json")
        command_preview = shell_join([program, *args])
        return PreparedInvocation(
            session_id=session_id,
            action=action,
            host=host,
            program=program,
            args=args,
            cwd=cwd,
            env=env,
            tmux_session_name=tmux_session_name,
            remote_log_path=remote_log_path,
            remote_status_path=remote_status_path,
            command_preview=command_preview,
            params=validated_params,
            created_at=created_at,
        )

    def _enforce_concurrency_policy(
        self,
        action: ActionConfig,
        host: HostConfig,
        params: dict[str, Any],
    ) -> list[str]:
        if action.concurrency_policy == "allow":
            return []
        matches = self._find_active_conflicts(action, host, params)
        if not matches:
            return []
        if action.concurrency_policy == "deny_if_running":
            session_ids = ", ".join(record.session_id for record in matches)
            raise ServiceError(
                f"Action '{action.name}' already has an active session for the same params: {session_ids}"
            )
        replaced: list[str] = []
        for record in matches:
            self.stop_session(record.session_id)
            replaced.append(record.session_id)
        return replaced

    def _find_active_conflicts(
        self,
        action: ActionConfig,
        host: HostConfig,
        params: dict[str, Any],
    ) -> list[SessionRecord]:
        records = self._store.list(
            limit=200,
            statuses=None,
            action_name=action.name,
            host_name=host.name,
            newest_first=True,
        )
        conflicts: list[SessionRecord] = []
        for record in records:
            if record.params != params:
                continue
            if record.status not in TERMINAL_STATUSES:
                self.get_session(record.session_id)
                record = self._require_session(record.session_id)
            if record.status in ACTIVE_STATUSES:
                conflicts.append(record)
        return conflicts

    def _validate_params(self, action: ActionConfig, raw_params: dict[str, Any]) -> dict[str, Any]:
        unknown = set(raw_params) - set(action.params)
        if unknown:
            joined = ", ".join(sorted(unknown))
            raise ServiceError(f"Unknown params for action '{action.name}': {joined}")
        result: dict[str, Any] = {}
        for name, param in action.params.items():
            if name in raw_params:
                value = raw_params[name]
            elif param.has_default:
                value = param.default
            elif param.required:
                raise ServiceError(f"Missing required param '{name}' for action '{action.name}'")
            else:
                continue
            result[name] = self._validate_param_value(param, value)
        return result

    def _validate_param_value(self, param: ParamConfig, value: Any) -> Any:
        if param.type == "string":
            value = str(value)
            if value == "" and not param.allow_empty:
                raise ServiceError(f"Param '{param.name}' cannot be empty")
            if param.pattern and re.fullmatch(param.pattern, value) is None:
                raise ServiceError(f"Param '{param.name}' does not match pattern {param.pattern}")
            return value
        if param.type == "enum":
            value = str(value)
            if value not in param.values:
                raise ServiceError(f"Param '{param.name}' must be one of: {', '.join(param.values)}")
            return value
        if param.type == "int":
            if isinstance(value, str):
                try:
                    value = int(value)
                except ValueError as exc:
                    raise ServiceError(f"Param '{param.name}' must be an integer") from exc
            if not isinstance(value, int):
                raise ServiceError(f"Param '{param.name}' must be an integer")
            if param.minimum is not None and value < param.minimum:
                raise ServiceError(f"Param '{param.name}' must be >= {param.minimum:g}")
            if param.maximum is not None and value > param.maximum:
                raise ServiceError(f"Param '{param.name}' must be <= {param.maximum:g}")
            return value
        if param.type == "float":
            if isinstance(value, str):
                try:
                    value = float(value)
                except ValueError as exc:
                    raise ServiceError(f"Param '{param.name}' must be numeric") from exc
            if not isinstance(value, (int, float)):
                raise ServiceError(f"Param '{param.name}' must be numeric")
            value = float(value)
            if param.minimum is not None and value < param.minimum:
                raise ServiceError(f"Param '{param.name}' must be >= {param.minimum:g}")
            if param.maximum is not None and value > param.maximum:
                raise ServiceError(f"Param '{param.name}' must be <= {param.maximum:g}")
            return value
        if param.type == "bool":
            if isinstance(value, bool):
                return value
            if isinstance(value, str):
                lowered = value.strip().lower()
                if lowered in {"1", "true", "yes", "on"}:
                    return True
                if lowered in {"0", "false", "no", "off"}:
                    return False
            raise ServiceError(f"Param '{param.name}' must be boolean")
        if param.type == "path":
            value = normalize_posix_path(str(value))
            if value == "" and not param.allow_empty:
                raise ServiceError(f"Param '{param.name}' cannot be empty")
            if value in {"..", "."} or value.startswith("../"):
                raise ServiceError(f"Param '{param.name}' must not traverse parent directories")
            if param.must_be_relative and value.startswith("/"):
                raise ServiceError(f"Param '{param.name}' must be relative")
            if "\x00" in value:
                raise ServiceError(f"Param '{param.name}' contains an invalid null byte")
            if param.pattern and re.fullmatch(param.pattern, value) is None:
                raise ServiceError(f"Param '{param.name}' does not match pattern {param.pattern}")
            if param.roots:
                if not value.startswith("/"):
                    raise ServiceError(f"Param '{param.name}' must be absolute because roots are configured")
                if not is_within_roots(value, list(param.roots)):
                    roots = ", ".join(param.roots)
                    raise ServiceError(f"Param '{param.name}' is outside allowed roots: {roots}")
            return value
        raise ServiceError(f"Unsupported param type: {param.type}")

    def _build_tmux_launch_script(self, invocation: PreparedInvocation) -> str:
        log_dir = posixpath.dirname(invocation.remote_log_path)
        status_dir = posixpath.dirname(invocation.remote_status_path)
        inner_script = self._build_inner_action_script(invocation)
        command_string = shell_join([invocation.host.shell_path, "-lc", inner_script])
        lines = [
            "set -u",
            shell_join(["mkdir", "-p", log_dir, status_dir]),
            shell_join(
                [
                    invocation.host.tmux_command,
                    "new-session",
                    "-d",
                    "-s",
                    invocation.tmux_session_name,
                    "-c",
                    invocation.cwd,
                    command_string,
                ]
            ),
            shell_join(
                [
                    invocation.host.tmux_command,
                    "set-option",
                    "-t",
                    invocation.tmux_session_name,
                    "history-limit",
                    str(invocation.action.history_limit),
                ]
            ),
        ]
        return "\n".join(lines)

    def _build_inner_action_script(self, invocation: PreparedInvocation) -> str:
        log_path = shell_quote(invocation.remote_log_path)
        status_path = shell_quote(invocation.remote_status_path)
        status_tmp_path = shell_quote(f"{invocation.remote_status_path}.tmp")
        timeout_marker_path = shell_quote(f"{invocation.remote_status_path}.timeout")
        stop_marker_path = shell_quote(f"{invocation.remote_status_path}.stop")
        command = shell_join([invocation.program, *invocation.args])
        command_runner = shell_join([invocation.host.shell_path, "-lc", f"exec {command}"])
        env_lines = [f"export {key}={shell_quote(value)}" for key, value in sorted(invocation.env.items())]
        timeout_note = shell_quote(f"Timed out after {invocation.action.timeout_sec}s")
        header_lines = [
            "set -u",
            "umask 077",
            f"rm -f {status_path} {status_tmp_path} {timeout_marker_path} {stop_marker_path}",
            "rc=0",
            'status_state="failed"',
            'status_note=""',
            'watchdog_pid=""',
            'child_pid=""',
            'kill_mode="pid"',
            f"started_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)",
            "json_escape() {",
            "  printf '%s' \"$1\" | sed 's/\\\\/\\\\\\\\/g; s/\"/\\\\\"/g' | tr '\\n' ' '",
            "}",
            "write_status() {",
            '  note_json="null"',
            '  if [ -n "${status_note}" ]; then',
            '    note_json=$(printf \'"%s"\' "$(json_escape "${status_note}")")',
            "  fi",
            '  finished_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)',
            (
                "  printf '{\"session_id\": %s, \"action\": %s, \"exit_code\": %s, \"finished_at\": \"%s\", "
                "\"state\": \"%s\", \"note\": %s}\\n' "
                f"{shell_quote(json.dumps(invocation.session_id))} "
                f"{shell_quote(json.dumps(invocation.action.name))} "
                "\"${rc}\" \"${finished_at}\" \"${status_state}\" \"${note_json}\" > "
                f"{status_tmp_path}"
            ),
            f"  mv {status_tmp_path} {status_path}",
            "}",
            "cleanup() {",
            "  trap - EXIT INT TERM HUP",
            '  if [ -n "${watchdog_pid}" ]; then',
            '    kill "${watchdog_pid}" 2>/dev/null || true',
            '    wait "${watchdog_pid}" 2>/dev/null || true',
            "  fi",
            f"  if [ -f {stop_marker_path} ]; then",
            '    status_state="stopped"',
            '    status_note="Stopped by user request"',
            '    if [ "${rc}" -eq 0 ]; then',
            '      rc=143',
            "    fi",
            f"  elif [ -f {timeout_marker_path} ]; then",
            f"    rc={EXIT_TIMEOUT}",
            '    status_state="failed"',
            f"    status_note={timeout_note}",
            "  elif [ \"${rc}\" -eq 0 ]; then",
            '    status_state="completed"',
            '    status_note=""',
            '  elif [ -z "${status_note}" ]; then',
            '    status_state="failed"',
            '    status_note="Command failed"',
            "  fi",
            "  write_status",
            '  exit "${rc}"',
            "}",
            'trap \'rc=$?; cleanup\' EXIT',
            'trap \'rc=130; status_state="failed"; status_note="Interrupted"; exit "${rc}"\' INT',
            'trap \'rc=143; status_state="failed"; status_note="Terminated"; exit "${rc}"\' TERM HUP',
            (
                "printf '[remote-dev-mcp] session=%s action=%s started_at=%s\\n' "
                f"{shell_quote(invocation.session_id)} {shell_quote(invocation.action.name)} \"$started_at\" >> {log_path}"
            ),
            (
                "printf '[remote-dev-mcp] command=%s\\n' "
                f"{shell_quote(invocation.command_preview)} >> {log_path}"
            ),
        ]
        header_lines.extend(
            [
                f"cd {shell_quote(invocation.cwd)} || {{",
                (
                    "  printf '[remote-dev-mcp] error=%s\\n' "
                    f"{shell_quote('Working directory unavailable')} >> {log_path}"
                ),
                f"  rc={EXIT_CWD_UNAVAILABLE}",
                '  status_state="failed"',
                '  status_note="Working directory unavailable"',
                '  exit "${rc}"',
                "}",
            ]
        )
        for line in env_lines:
            header_lines.extend(
                [
                    f"{line} || {{",
                    (
                        "  printf '[remote-dev-mcp] error=%s\\n' "
                        f"{shell_quote('Invalid environment export')} >> {log_path}"
                    ),
                    f"  rc={EXIT_ENV_INVALID}",
                    '  status_state="failed"',
                    '  status_note="Invalid environment export"',
                    '  exit "${rc}"',
                    "}",
                ]
            )
        header_lines.extend(
            [
                "if command -v setsid >/dev/null 2>&1; then",
                f"  setsid {command_runner} >> {log_path} 2>&1 &",
                '  child_pid="$!"',
                '  kill_mode="pgid"',
                "else",
                f"  {command_runner} >> {log_path} 2>&1 &",
                '  child_pid="$!"',
                '  kill_mode="pid"',
                "fi",
                f"( sleep {invocation.action.timeout_sec}",
                '  if kill -0 "${child_pid}" 2>/dev/null; then',
                f"    : > {timeout_marker_path}",
                (
                    "    printf '[remote-dev-mcp] timeout=%s\\n' "
                    f"{shell_quote(str(invocation.action.timeout_sec))} >> {log_path}"
                ),
                '    if [ "${kill_mode}" = "pgid" ]; then',
                '      kill -TERM -- "-${child_pid}" 2>/dev/null || kill -TERM "${child_pid}" 2>/dev/null || true',
                "    else",
                '      kill -TERM "${child_pid}" 2>/dev/null || true',
                "    fi",
                "    sleep 5",
                '    if kill -0 "${child_pid}" 2>/dev/null; then',
                '      if [ "${kill_mode}" = "pgid" ]; then',
                '        kill -KILL -- "-${child_pid}" 2>/dev/null || kill -KILL "${child_pid}" 2>/dev/null || true',
                "      else",
                '        kill -KILL "${child_pid}" 2>/dev/null || true',
                "      fi",
                "    fi",
                "  fi",
                ") &",
                'watchdog_pid="$!"',
                'wait "${child_pid}" || rc=$?',
                'if [ "${rc}" -eq 0 ]; then',
                '  status_state="completed"',
                "else",
                '  status_state="failed"',
                "fi",
                f"if [ -f {timeout_marker_path} ]; then",
                f"  rc={EXIT_TIMEOUT}",
                '  status_state="failed"',
                f"  status_note={timeout_note}",
                "fi",
                'exit "${rc}"',
            ]
        )
        return "\n".join(header_lines)

    def _ensure_tmux_available(self, host_name: str) -> None:
        if host_name in self._tmux_checked:
            return
        host = self._get_host(host_name)
        executor = self._get_executor(host)
        result = self._run_argv(executor, host, [host.tmux_command, "-V"], timeout_sec=10)
        if result.exit_code != 0:
            message = (result.stderr or result.stdout).strip() or "tmux is unavailable"
            raise ServiceError(f"tmux check failed on host '{host_name}': {message}")
        self._tmux_checked.add(host_name)

    def _read_remote_status(self, executor: Any, host: HostConfig, status_path: str) -> dict[str, Any] | None:
        script = f"if [ -f {shell_quote(status_path)} ]; then cat {shell_quote(status_path)}; fi"
        result = self._run_script(executor, host, script, timeout_sec=10)
        payload = result.stdout.strip()
        if payload == "":
            return None
        try:
            data = json.loads(payload)
        except json.JSONDecodeError:
            return {"raw": payload}
        if not isinstance(data, dict):
            return {"raw": payload}
        return data

    def _tmux_has_session(self, executor: Any, host: HostConfig, session_name: str) -> bool:
        script = (
            f"{shell_quote(host.tmux_command)} has-session -t {shell_quote(session_name)} 2>/dev/null"
        )
        result = self._run_script(executor, host, script, timeout_sec=10)
        return result.exit_code == 0

    def _read_log_size(self, executor: Any, host: HostConfig, log_path: str) -> int:
        script = (
            f"if [ -f {shell_quote(log_path)} ]; then wc -c < {shell_quote(log_path)} | tr -d '[:space:]'; "
            "else printf '0'; fi"
        )
        result = self._run_script(executor, host, script, timeout_sec=10)
        payload = result.stdout.strip() or "0"
        try:
            return int(payload)
        except ValueError as exc:
            raise ServiceError(f"Failed to parse log size: {payload}") from exc

    def _get_executor(self, host: HostConfig) -> Any:
        if host.name not in self._executor_cache:
            try:
                self._executor_cache[host.name] = create_executor(host)
            except ExecutionError as exc:
                raise ServiceError(str(exc)) from exc
        return self._executor_cache[host.name]

    def _get_action(self, action_name: str) -> ActionConfig:
        action = self._config.actions.get(action_name)
        if action is None:
            raise ServiceError(f"Unknown action '{action_name}'")
        return action

    def _get_host(self, host_name: str | None) -> HostConfig:
        if not host_name:
            raise ServiceError("Host name is not configured")
        host = self._config.hosts.get(host_name)
        if host is None:
            raise ServiceError(f"Unknown host '{host_name}'")
        return host

    def _require_session(self, session_id: str) -> SessionRecord:
        record = self._store.get(session_id)
        if record is None:
            raise ServiceError(f"Unknown session '{session_id}'")
        return record

    def _delete_remote_state(self, record: SessionRecord) -> None:
        host = self._get_host(record.host_name)
        executor = self._get_executor(host)
        paths = [
            record.remote_log_path,
            record.remote_status_path,
            f"{record.remote_status_path}.tmp",
            f"{record.remote_status_path}.timeout",
            f"{record.remote_status_path}.stop",
        ]
        script = "rm -f " + " ".join(shell_quote(path) for path in paths)
        self._run_script(executor, host, script, timeout_sec=15)

    def _record_to_summary(self, record: SessionRecord) -> dict[str, Any]:
        action = self._config.actions.get(record.action_name)
        return {
            "session_id": record.session_id,
            "action": record.action_name,
            "host": record.host_name,
            "status": record.status,
            "created_at": record.created_at,
            "finished_at": record.finished_at,
            "exit_code": record.exit_code,
            "command_preview": record.command_preview,
            "params": record.params,
            "note": record.note,
            "ready_check": bool(action.ready_regex) if action is not None else False,
            "ready": record.ready if action is not None and action.ready_regex else None,
            "ready_at": record.ready_at,
        }

    def _log_matches_pattern(self, executor: Any, host: HostConfig, log_path: str, pattern: str) -> bool:
        script = (
            f"if [ -f {shell_quote(log_path)} ]; then "
            f"grep -E -q -- {shell_quote(pattern)} {shell_quote(log_path)}; else exit 1; fi"
        )
        result = self._run_script(executor, host, script, timeout_sec=10)
        if result.exit_code == 0:
            return True
        if result.exit_code == 1:
            return False
        message = (result.stderr or result.stdout).strip() or "ready_regex check failed"
        raise ServiceError(f"Host '{host.name}': {message}")

    def _diagnostic_action_programs(self, host_name: str) -> list[tuple[str, str]]:
        context = {
            **self._config.server.variables,
            **self._get_host(host_name).variables,
        }
        seen: set[str] = set()
        programs: list[tuple[str, str]] = []
        for action in sorted(self._config.actions.values(), key=lambda item: item.name):
            if action.host != host_name:
                continue
            try:
                program = render_template(action.program, context)
            except TemplateError:
                continue
            key = f"{action.name}:{program}"
            if key in seen:
                continue
            seen.add(key)
            programs.append((f"action_program:{action.name}", program))
        return programs

    def _diagnose_command_check(
        self,
        executor: Any,
        host: HostConfig,
        command: str,
        required: bool,
    ) -> dict[str, Any]:
        return self._diagnose_program_check(
            executor,
            host,
            name=f"command:{command}",
            program=command,
            required=required,
        )

    def _diagnose_program_check(
        self,
        executor: Any,
        host: HostConfig,
        name: str,
        program: str,
        required: bool,
    ) -> dict[str, Any]:
        if program.startswith("/"):
            script = (
                f"if [ -x {shell_quote(program)} ]; then printf '%s' {shell_quote(program)}; else exit 1; fi"
            )
        else:
            script = f"command -v {shell_quote(program)}"
        return self._diagnose_script_check(
            executor,
            host,
            name=name,
            script=script,
            required=required,
        )

    def _diagnose_argv_check(
        self,
        executor: Any,
        host: HostConfig,
        name: str,
        argv: list[str],
        required: bool,
    ) -> dict[str, Any]:
        try:
            result = executor.run_argv(argv, timeout_sec=10)
        except ExecutionError as exc:
            return {
                "name": name,
                "required": required,
                "ok": False,
                "detail": f"execution error: {exc}",
            }
        ok = result.exit_code == 0
        detail = (result.stdout or result.stderr).strip() or ("ok" if ok else f"exit {result.exit_code}")
        return {
            "name": name,
            "required": required,
            "ok": ok,
            "detail": detail,
        }

    def _diagnose_script_check(
        self,
        executor: Any,
        host: HostConfig,
        name: str,
        script: str,
        required: bool,
    ) -> dict[str, Any]:
        try:
            result = executor.run_script(script, timeout_sec=10)
        except ExecutionError as exc:
            return {
                "name": name,
                "required": required,
                "ok": False,
                "detail": f"execution error: {exc}",
            }
        ok = result.exit_code == 0
        detail = (result.stdout or result.stderr).strip() or ("ok" if ok else f"exit {result.exit_code}")
        return {
            "name": name,
            "required": required,
            "ok": ok,
            "detail": detail,
        }

    def _run_script(self, executor: Any, host: HostConfig | None, script: str, timeout_sec: int) -> Any:
        try:
            return executor.run_script(script, timeout_sec=timeout_sec)
        except ExecutionError as exc:
            prefix = f"Host '{host.name}': " if host is not None else ""
            raise ServiceError(f"{prefix}{exc}") from exc

    def _run_script_bytes(self, executor: Any, host: HostConfig, script: str, timeout_sec: int) -> Any:
        try:
            return executor.run_script_bytes(script, timeout_sec=timeout_sec)
        except ExecutionError as exc:
            raise ServiceError(f"Host '{host.name}': {exc}") from exc

    def _run_argv(self, executor: Any, host: HostConfig, argv: list[str], timeout_sec: int) -> Any:
        try:
            return executor.run_argv(argv, timeout_sec=timeout_sec)
        except ExecutionError as exc:
            raise ServiceError(f"Host '{host.name}': {exc}") from exc


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _parse_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _coerce_int(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return None
    return None


def _coerce_string(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return str(value)


def _normalize_statuses(statuses: list[str] | None) -> list[str] | None:
    if statuses is None:
        return None
    normalized = [str(status).strip() for status in statuses if str(status).strip()]
    if not normalized:
        return None
    invalid = [status for status in normalized if status not in TERMINAL_STATUSES and status != "running"]
    if invalid:
        joined = ", ".join(sorted(set(invalid)))
        raise ServiceError(f"Unknown session statuses: {joined}")
    return normalized
