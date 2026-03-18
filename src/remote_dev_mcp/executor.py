from __future__ import annotations

import subprocess
from dataclasses import dataclass
from typing import Protocol

from .config import HostConfig
from .shell import shell_join


class ExecutionError(RuntimeError):
    """Raised when command execution fails before the remote command itself runs."""


@dataclass(frozen=True)
class CommandResult:
    exit_code: int
    stdout: str
    stderr: str


@dataclass(frozen=True)
class BinaryCommandResult:
    exit_code: int
    stdout: bytes
    stderr: bytes


class Executor(Protocol):
    def run_script(self, script: str, timeout_sec: int | None = None) -> CommandResult:
        raise NotImplementedError

    def run_script_bytes(self, script: str, timeout_sec: int | None = None) -> BinaryCommandResult:
        raise NotImplementedError

    def run_argv(self, argv: list[str], timeout_sec: int | None = None) -> CommandResult:
        raise NotImplementedError

    def attach_command(self, tmux_command: str, session_name: str) -> str:
        raise NotImplementedError

    @property
    def label(self) -> str:
        raise NotImplementedError


class LocalExecutor:
    def __init__(self, host: HostConfig):
        self._host = host

    @property
    def label(self) -> str:
        return "local"

    def run_script(self, script: str, timeout_sec: int | None = None) -> CommandResult:
        result = self._run([self._host.shell_path, "-lc", script], timeout_sec=timeout_sec, text=True)
        return CommandResult(exit_code=result.returncode, stdout=result.stdout, stderr=result.stderr)

    def run_script_bytes(self, script: str, timeout_sec: int | None = None) -> BinaryCommandResult:
        result = self._run([self._host.shell_path, "-lc", script], timeout_sec=timeout_sec, text=False)
        return BinaryCommandResult(exit_code=result.returncode, stdout=result.stdout, stderr=result.stderr)

    def run_argv(self, argv: list[str], timeout_sec: int | None = None) -> CommandResult:
        result = self._run(argv, timeout_sec=timeout_sec, text=True)
        return CommandResult(exit_code=result.returncode, stdout=result.stdout, stderr=result.stderr)

    def attach_command(self, tmux_command: str, session_name: str) -> str:
        return shell_join([tmux_command, "attach", "-t", session_name])

    def _run(self, argv: list[str], timeout_sec: int | None, text: bool) -> subprocess.CompletedProcess:
        try:
            return subprocess.run(
                argv,
                check=False,
                capture_output=True,
                text=text,
                timeout=timeout_sec,
            )
        except FileNotFoundError as exc:
            raise ExecutionError(f"Local command not found: {argv[0]}") from exc
        except subprocess.TimeoutExpired as exc:
            raise ExecutionError(f"Local command timed out after {timeout_sec}s: {argv[0]}") from exc


class SshExecutor:
    def __init__(self, host: HostConfig):
        self._host = host
        if not host.destination:
            raise ExecutionError(f"SSH host '{host.name}' is missing destination")

    @property
    def label(self) -> str:
        return f"ssh:{self._host.destination}"

    def run_script(self, script: str, timeout_sec: int | None = None) -> CommandResult:
        result = self._run(self._ssh_argv(script), timeout_sec=timeout_sec, text=True)
        return CommandResult(exit_code=result.returncode, stdout=result.stdout, stderr=result.stderr)

    def run_script_bytes(self, script: str, timeout_sec: int | None = None) -> BinaryCommandResult:
        result = self._run(self._ssh_argv(script), timeout_sec=timeout_sec, text=False)
        return BinaryCommandResult(exit_code=result.returncode, stdout=result.stdout, stderr=result.stderr)

    def run_argv(self, argv: list[str], timeout_sec: int | None = None) -> CommandResult:
        return self.run_script(shell_join(argv), timeout_sec=timeout_sec)

    def attach_command(self, tmux_command: str, session_name: str) -> str:
        return shell_join(
            [self._host.ssh_command, *self._host.ssh_args, self._host.destination, tmux_command, "attach", "-t", session_name]
        )

    def _ssh_argv(self, script: str) -> list[str]:
        return [self._host.ssh_command, *self._host.ssh_args, self._host.destination, script]

    def _run(self, argv: list[str], timeout_sec: int | None, text: bool) -> subprocess.CompletedProcess:
        try:
            return subprocess.run(
                argv,
                check=False,
                capture_output=True,
                text=text,
                timeout=timeout_sec,
            )
        except FileNotFoundError as exc:
            raise ExecutionError(f"SSH command not found: {self._host.ssh_command}") from exc
        except subprocess.TimeoutExpired as exc:
            raise ExecutionError(f"SSH command timed out after {timeout_sec}s: {self._host.destination}") from exc


def create_executor(host: HostConfig) -> Executor:
    if host.transport == "local":
        return LocalExecutor(host)
    if host.transport == "ssh":
        return SshExecutor(host)
    raise ExecutionError(f"Unsupported transport: {host.transport}")

