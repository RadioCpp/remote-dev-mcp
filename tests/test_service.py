from __future__ import annotations

import tempfile
import textwrap
import unittest
from pathlib import Path

from remote_dev_mcp.config import load_config
from remote_dev_mcp.service import RemoteDevService, ServiceError
from remote_dev_mcp.session_store import SessionStore


CONFIG = """
[server]
default_host = "localbox"
state_dir = "./.state"

[hosts.localbox]
transport = "local"
allowed_cwd_roots = ["/tmp/project"]
remote_state_dir = "/tmp/rdmcp"

[hosts.localbox.variables]
repo_root = "/tmp/project"

[actions.build]
description = "Build something"
program = "cmake"
args = ["--build", "{repo_root}", "--target", "{target}", "-j", "{jobs}"]
cwd = "{repo_root}"

[actions.build.params.target]
type = "enum"
values = ["core", "tests"]

[actions.build.params.jobs]
type = "int"
default = 4
min = 1
max = 16
"""


class ServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp_dir = tempfile.TemporaryDirectory()
        config_path = Path(self._tmp_dir.name) / "config.toml"
        config_path.write_text(CONFIG, encoding="utf-8")
        self.config = load_config(config_path)
        self.store = SessionStore(self.config.server.state_dir / "sessions.sqlite3")
        self.service = RemoteDevService(self.config, self.store)

    def tearDown(self) -> None:
        self._tmp_dir.cleanup()

    def test_list_actions(self) -> None:
        actions = self.service.list_actions()["actions"]
        self.assertEqual(actions[0]["name"], "build")
        self.assertIn("jobs", actions[0]["params"])
        self.assertEqual(actions[0]["concurrency_policy"], "allow")
        self.assertEqual(actions[0]["ready_regex"], None)

    def test_rejects_unknown_params(self) -> None:
        with self.assertRaises(ServiceError):
            self.service.run_action("build", {"nope": "x"})

    def test_rejects_enum_violation(self) -> None:
        with self.assertRaises(ServiceError):
            self.service.run_action("build", {"target": "unknown"})

    def test_rejects_outside_allowed_cwd(self) -> None:
        config_text = CONFIG.replace('repo_root = "/tmp/project"', 'repo_root = "/etc"')
        config_path = Path(self._tmp_dir.name) / "bad-config.toml"
        config_path.write_text(config_text, encoding="utf-8")
        config = load_config(config_path)
        service = RemoteDevService(config, self.store)
        with self.assertRaises(ServiceError):
            service.run_action("build", {"target": "core"})

    def test_diagnose_host_reports_checks(self) -> None:
        fake_tmux = Path(self._tmp_dir.name) / "fake-tmux.sh"
        fake_tmux.write_text("#!/bin/sh\nif [ \"$1\" = \"-V\" ]; then echo tmux 9.9; exit 0; fi\nexit 1\n", encoding="utf-8")
        fake_tmux.chmod(0o755)
        config_text = textwrap.dedent(
            f"""
            [server]
            default_host = "localbox"
            state_dir = "{(Path(self._tmp_dir.name) / 'diag-state').as_posix()}"

            [hosts.localbox]
            transport = "local"
            tmux_command = "{fake_tmux.as_posix()}"
            shell_path = "/bin/sh"
            allowed_cwd_roots = ["/tmp/project"]
            remote_state_dir = "/tmp/rdmcp"

            [hosts.localbox.variables]
            repo_root = "/tmp/project"

            [actions.runtime]
            description = "Runtime"
            program = "python3"
            args = ["--version"]
            cwd = "{{repo_root}}"
            ready_regex = "Python"
            concurrency_policy = "replace"
            """
        ).strip()
        config_path = Path(self._tmp_dir.name) / "diag-config.toml"
        config_path.write_text(config_text, encoding="utf-8")
        config = load_config(config_path)
        service = RemoteDevService(config, self.store)
        result = service.diagnose_host("localbox")
        self.assertTrue(result["ok"])
        names = {check["name"] for check in result["checks"]}
        self.assertIn("tmux", names)
        self.assertIn("command:grep", names)
        self.assertIn("action_program:runtime", names)


if __name__ == "__main__":
    unittest.main()
