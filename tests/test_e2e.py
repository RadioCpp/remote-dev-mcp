from __future__ import annotations

import os
import tempfile
import textwrap
import time
import unittest
from pathlib import Path

from remote_dev_mcp.config import load_config
from remote_dev_mcp.service import RemoteDevService, ServiceError
from remote_dev_mcp.session_store import SessionStore


FAKE_TMUX = """#!/usr/bin/env python3
import json
import os
import shlex
import signal
import subprocess
import sys
import time
from pathlib import Path

STATE_DIR = Path(os.environ["FAKE_TMUX_STATE_DIR"])
STATE_DIR.mkdir(parents=True, exist_ok=True)


def session_path(name: str) -> Path:
    return STATE_DIR / f"{name}.json"


def is_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def load_session(name: str) -> dict | None:
    path = session_path(name)
    if not path.exists():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    pid = int(data["pid"])
    if not is_alive(pid):
        path.unlink(missing_ok=True)
        return None
    return data


def main() -> int:
    argv = sys.argv[1:]
    if argv == ["-V"]:
        print("tmux 9.9")
        return 0
    if not argv:
        return 1
    cmd = argv[0]
    if cmd == "set-option":
        return 0
    if cmd == "attach":
        target = argv[argv.index("-t") + 1]
        print(f"attach {target}")
        return 0
    if cmd == "has-session":
        target = argv[argv.index("-t") + 1]
        return 0 if load_session(target) else 1
    if cmd == "kill-session":
        target = argv[argv.index("-t") + 1]
        data = load_session(target)
        if not data:
            return 0
        pid = int(data["pid"])
        try:
            os.killpg(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        time.sleep(0.1)
        if is_alive(pid):
            try:
                os.killpg(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        session_path(target).unlink(missing_ok=True)
        return 0
    if cmd == "new-session":
        target = argv[argv.index("-s") + 1]
        cwd = argv[argv.index("-c") + 1]
        command = argv[-1]
        popen_cwd = cwd if Path(cwd).exists() else None
        proc = subprocess.Popen(
            command,
            shell=True,
            cwd=popen_cwd,
            start_new_session=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
        )
        session_path(target).write_text(json.dumps({"pid": proc.pid}), encoding="utf-8")
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
"""


class E2ETests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp_dir.name)
        self.project_root = self.base / "project"
        self.project_root.mkdir(parents=True)
        self.fake_tmux = self.base / "fake_tmux.py"
        self.fake_tmux.write_text(FAKE_TMUX, encoding="utf-8")
        self.fake_tmux.chmod(0o755)
        os.environ["FAKE_TMUX_STATE_DIR"] = str(self.base / "tmux-state")

        config_text = textwrap.dedent(
            f"""
            [server]
            default_host = "localbox"
            state_dir = "{(self.base / 'state').as_posix()}"

            [hosts.localbox]
            transport = "local"
            tmux_command = "{self.fake_tmux.as_posix()}"
            shell_path = "/bin/sh"
            remote_state_dir = "{(self.base / 'remote').as_posix()}"
            allowed_cwd_roots = ["{self.project_root.as_posix()}"]

            [hosts.localbox.variables]
            repo_root = "{self.project_root.as_posix()}"
            missing_dir = "{(self.project_root / 'missing').as_posix()}"
            log_root = "{(self.project_root / 'logs').as_posix()}"

            [actions.success]
            description = "Successful action"
            program = "/bin/sh"
            args = ["-lc", "printf 'hello\\\\n'"]
            cwd = "{{repo_root}}"
            timeout_sec = 5

            [actions.timeout]
            description = "Timeout action"
            program = "/bin/sh"
            args = ["-lc", "sleep 2; printf 'late\\\\n'"]
            cwd = "{{repo_root}}"
            timeout_sec = 1

            [actions.bad_cwd]
            description = "Bad cwd action"
            program = "/bin/sh"
            args = ["-lc", "printf 'should-not-run\\\\n'"]
            cwd = "{{missing_dir}}"
            timeout_sec = 5

            [actions.path_guard]
            description = "Path guard action"
            program = "/bin/sh"
            args = ["-lc", "printf '%s\\\\n' {{artifact_path}}"]
            cwd = "{{repo_root}}"
            timeout_sec = 5

            [actions.allowed_nonzero]
            description = "Non-zero allowed action"
            program = "/bin/sh"
            args = ["-lc", "printf 'allowed\\\\n'; exit 42"]
            cwd = "{{repo_root}}"
            timeout_sec = 5
            allowed_exit_codes = [0, 42]

            [actions.long_run]
            description = "Long running action"
            program = "/bin/sh"
            args = ["-lc", "printf 'running\\\\n'; sleep 30"]
            cwd = "{{repo_root}}"
            timeout_sec = 60

            [actions.short_run]
            description = "Short running action"
            program = "/bin/sh"
            args = ["-lc", "printf 'short\\\\n'; sleep 1"]
            cwd = "{{repo_root}}"
            timeout_sec = 10

            [actions.ready_instance]
            description = "Ready-marked long running action"
            program = "/bin/sh"
            args = ["-lc", "printf 'SERVICE_READY\\\\n'; sleep 30"]
            cwd = "{{repo_root}}"
            timeout_sec = 60
            ready_regex = "SERVICE_READY"

            [actions.singleton]
            description = "Single active action"
            program = "/bin/sh"
            args = ["-lc", "printf 'singleton\\\\n'; sleep 30"]
            cwd = "{{repo_root}}"
            timeout_sec = 60
            concurrency_policy = "deny_if_running"

            [actions.replaceable]
            description = "Replaceable action"
            program = "/bin/sh"
            args = ["-lc", "printf 'replaceable\\\\n'; sleep 30"]
            cwd = "{{repo_root}}"
            timeout_sec = 60
            concurrency_policy = "replace"

            [actions.path_guard.params.artifact_path]
            type = "path"
            roots = ["{(self.project_root / 'artifacts').as_posix()}"]
            """
        ).strip()
        self.config_path = self.base / "config.toml"
        self.config_path.write_text(config_text, encoding="utf-8")
        self.config = load_config(self.config_path)
        self.store = SessionStore(self.config.server.state_dir / "sessions.sqlite3")
        self.service = RemoteDevService(self.config, self.store)

    def tearDown(self) -> None:
        os.environ.pop("FAKE_TMUX_STATE_DIR", None)
        self.tmp_dir.cleanup()

    def test_success_action_reads_output(self) -> None:
        session = self.service.run_action("success")
        session = self._wait_for_terminal_state(session["session_id"])
        self.assertEqual(session["status"], "completed")
        self.assertEqual(session["exit_code"], 0)
        output = self.service.read_session_output(session["session_id"], tail_lines=20)
        self.assertIn("hello", output["content"])

    def test_timeout_is_enforced(self) -> None:
        session = self.service.run_action("timeout")
        session = self._wait_for_terminal_state(session["session_id"])
        self.assertEqual(session["status"], "failed")
        self.assertEqual(session["exit_code"], 124)
        self.assertIn("Timed out", session["note"])

    def test_allowed_nonzero_exit_code_is_completed(self) -> None:
        session = self.service.run_action("allowed_nonzero")
        session = self._wait_for_terminal_state(session["session_id"])
        self.assertEqual(session["status"], "completed")
        self.assertEqual(session["exit_code"], 42)
        self.assertNotEqual(session["note"], "Command failed")

    def test_ready_regex_marks_session_ready(self) -> None:
        session = self.service.run_action("ready_instance")
        try:
            ready = self._wait_for_ready(session["session_id"])
            self.assertEqual(ready["status"], "running")
            self.assertTrue(ready["ready_check"])
            self.assertTrue(ready["ready"])
            self.assertIsNotNone(ready["ready_at"])
        finally:
            self.service.stop_session(session["session_id"])

    def test_missing_cwd_fails_fast(self) -> None:
        session = self.service.run_action("bad_cwd")
        session = self._wait_for_terminal_state(session["session_id"])
        self.assertEqual(session["status"], "failed")
        self.assertEqual(session["exit_code"], 200)
        self.assertEqual(session["note"], "Working directory unavailable")
        output = self.service.read_session_output(session["session_id"], tail_lines=20)
        self.assertEqual(output["content"].count("should-not-run"), 1)

    def test_path_param_is_guarded(self) -> None:
        with self.assertRaises(ServiceError):
            self.service.run_action(
                "path_guard",
                {"artifact_path": "/tmp/not-allowed.txt"},
            )

    def test_read_output_rejects_zero_max_bytes(self) -> None:
        session = self.service.run_action("success")
        session = self._wait_for_terminal_state(session["session_id"])
        self.assertEqual(session["status"], "completed")
        with self.assertRaises(ServiceError):
            self.service.read_session_output(session["session_id"], max_bytes=0)

    def test_infra_errors_become_service_errors(self) -> None:
        broken = self.config_path.read_text(encoding="utf-8").replace(
            self.fake_tmux.as_posix(),
            (self.base / "missing-tmux").as_posix(),
        )
        broken_path = self.base / "broken.toml"
        broken_path.write_text(broken, encoding="utf-8")
        config = load_config(broken_path)
        service = RemoteDevService(config, self.store)
        with self.assertRaises(ServiceError):
            service.run_action("success")

    def test_stop_session_reports_stopped(self) -> None:
        session = self.service.run_action("long_run")
        self.assertEqual(session["status"], "running")
        stopped = self.service.stop_session(session["session_id"])
        self.assertEqual(stopped["status"], "stopped")
        self.assertEqual(stopped["note"], "Stopped by user request")

    def test_concurrency_policy_denies_duplicate_run(self) -> None:
        first = self.service.run_action("singleton")
        try:
            with self.assertRaises(ServiceError):
                self.service.run_action("singleton")
        finally:
            self.service.stop_session(first["session_id"])

    def test_concurrency_policy_replace_stops_previous_session(self) -> None:
        first = self.service.run_action("replaceable")
        second = self.service.run_action("replaceable")
        try:
            self.assertEqual(second["replaced_session_ids"], [first["session_id"]])
            old = self._wait_for_terminal_state(first["session_id"])
            self.assertEqual(old["status"], "stopped")
            self.assertEqual(second["status"], "running")
        finally:
            self.service.stop_session(second["session_id"])

    def test_malformed_status_does_not_override_running_session(self) -> None:
        session = self.service.run_action("long_run")
        try:
            record = self.store.get(session["session_id"])
            self.assertIsNotNone(record)
            assert record is not None
            Path(record.remote_status_path).write_text("{bad json", encoding="utf-8")
            refreshed = self.service.get_session(session["session_id"])
            self.assertEqual(refreshed["status"], "running")
            self.assertEqual(refreshed["note"], "Malformed status payload")
        finally:
            self.service.stop_session(session["session_id"])

    def test_list_sessions_and_filters(self) -> None:
        first = self.service.run_action("success")
        second = self.service.run_action("allowed_nonzero")
        self._wait_for_terminal_state(first["session_id"])
        self._wait_for_terminal_state(second["session_id"])
        sessions = self.service.list_sessions(limit=10)["sessions"]
        session_ids = [session["session_id"] for session in sessions]
        self.assertIn(first["session_id"], session_ids)
        self.assertIn(second["session_id"], session_ids)
        filtered = self.service.list_sessions(limit=10, action="allowed_nonzero")["sessions"]
        self.assertEqual(len(filtered), 1)
        self.assertEqual(filtered[0]["action"], "allowed_nonzero")

    def test_cleanup_sessions_removes_terminal_state(self) -> None:
        session = self.service.run_action("success")
        session = self._wait_for_terminal_state(session["session_id"])
        record = self.store.get(session["session_id"])
        self.assertIsNotNone(record)
        assert record is not None
        self.assertTrue(Path(record.remote_log_path).exists())
        result = self.service.cleanup_sessions(
            max_age_hours=0,
            remove_remote_state=True,
            max_delete=10,
        )
        self.assertEqual(result["deleted_count"], 1)
        self.assertEqual(self.store.get(session["session_id"]), None)
        self.assertFalse(Path(record.remote_log_path).exists())
        self.assertFalse(Path(record.remote_status_path).exists())

    def test_cleanup_refreshes_stale_running_session(self) -> None:
        session = self.service.run_action("short_run")
        self.assertEqual(session["status"], "running")
        time.sleep(2)
        record = self.store.get(session["session_id"])
        self.assertIsNotNone(record)
        assert record is not None
        self.assertEqual(record.status, "running")
        result = self.service.cleanup_sessions(
            max_age_hours=0,
            remove_remote_state=True,
            max_delete=10,
        )
        self.assertEqual(result["deleted_count"], 1)
        self.assertEqual(result["deleted_session_ids"], [session["session_id"]])
        self.assertEqual(self.store.get(session["session_id"]), None)

    def _wait_for_terminal_state(self, session_id: str) -> dict:
        deadline = time.time() + 5
        while time.time() < deadline:
            session = self.service.get_session(session_id)
            if session["status"] in {"completed", "failed", "stopped", "lost"}:
                return session
            time.sleep(0.1)
        return self.service.get_session(session_id)

    def _wait_for_ready(self, session_id: str) -> dict:
        deadline = time.time() + 5
        while time.time() < deadline:
            session = self.service.get_session(session_id)
            if session.get("ready") is True:
                return session
            if session["status"] in {"completed", "failed", "stopped", "lost"}:
                return session
            time.sleep(0.1)
        return self.service.get_session(session_id)


if __name__ == "__main__":
    unittest.main()
