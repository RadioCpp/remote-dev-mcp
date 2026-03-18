from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from remote_dev_mcp.config import ConfigError, load_config


GOOD_CONFIG = """
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
args = ["--build", "{repo_root}", "--target", "{target}"]
cwd = "{repo_root}"

[actions.build.params.target]
type = "enum"
values = ["core", "tests"]
"""


class ConfigTests(unittest.TestCase):
    def test_load_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_path = Path(tmp_dir) / "config.toml"
            config_path.write_text(GOOD_CONFIG, encoding="utf-8")
            config = load_config(config_path)
        self.assertEqual(config.server.default_host, "localbox")
        self.assertIn("build", config.actions)
        self.assertEqual(config.actions["build"].host, "localbox")
        self.assertEqual(config.actions["build"].params["target"].values, ("core", "tests"))

    def test_rejects_missing_host(self) -> None:
        broken = GOOD_CONFIG.replace('default_host = "localbox"', "")
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_path = Path(tmp_dir) / "config.toml"
            config_path.write_text(broken, encoding="utf-8")
            with self.assertRaises(ConfigError):
                load_config(config_path)

    def test_rejects_invalid_env_name(self) -> None:
        broken = GOOD_CONFIG + '\n[actions.build.env]\n1BAD = "x"\n'
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_path = Path(tmp_dir) / "config.toml"
            config_path.write_text(broken, encoding="utf-8")
            with self.assertRaises(ConfigError):
                load_config(config_path)

    def test_rejects_unbounded_path_param(self) -> None:
        broken = GOOD_CONFIG + """

[actions.build.params.output_path]
type = "path"
"""
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_path = Path(tmp_dir) / "config.toml"
            config_path.write_text(broken, encoding="utf-8")
            with self.assertRaises(ConfigError):
                load_config(config_path)

    def test_rejects_invalid_concurrency_policy(self) -> None:
        broken = GOOD_CONFIG.replace('cwd = "{repo_root}"', 'cwd = "{repo_root}"\nconcurrency_policy = "nope"', 1)
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_path = Path(tmp_dir) / "config.toml"
            config_path.write_text(broken, encoding="utf-8")
            with self.assertRaises(ConfigError):
                load_config(config_path)

    def test_loads_team_example(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        config = load_config(repo_root / "config.team.example.toml")
        self.assertIn("build_target", config.actions)
        self.assertIn("start_instance", config.actions)

    def test_check_config_cli(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        env = os.environ.copy()
        env["PYTHONPATH"] = str(repo_root / "src")
        result = subprocess.run(
            ["python3", "-m", "remote_dev_mcp", "--config", "config.team.example.toml", "--check-config"],
            cwd=repo_root,
            env=env,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertIn("Config OK:", result.stdout)
        self.assertIn("build_target", result.stdout)

    def test_cleanup_sessions_cli(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_path = Path(tmp_dir) / "config.toml"
            config_path.write_text(GOOD_CONFIG, encoding="utf-8")
            env = os.environ.copy()
            env["PYTHONPATH"] = str(repo_root / "src")
            result = subprocess.run(
                [
                    "python3",
                    "-m",
                    "remote_dev_mcp",
                    "--config",
                    str(config_path),
                    "--cleanup-sessions",
                    "--max-age-hours",
                    "0",
                ],
                cwd=repo_root,
                env=env,
                check=False,
                capture_output=True,
                text=True,
            )
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertIn("Deleted sessions:", result.stdout)

    def test_diagnose_host_cli(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as tmp_dir:
            fake_tmux = Path(tmp_dir) / "fake-tmux.sh"
            fake_tmux.write_text(
                "#!/bin/sh\nif [ \"$1\" = \"-V\" ]; then echo tmux 9.9; exit 0; fi\nexit 1\n",
                encoding="utf-8",
            )
            fake_tmux.chmod(0o755)
            config_path = Path(tmp_dir) / "diag.toml"
            config_path.write_text(
                f"""
[server]
default_host = "localbox"
state_dir = "{(Path(tmp_dir) / 'state').as_posix()}"

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
""".strip()
                + "\n",
                encoding="utf-8",
            )
            env = os.environ.copy()
            env["PYTHONPATH"] = str(repo_root / "src")
            result = subprocess.run(
                [
                    "python3",
                    "-m",
                    "remote_dev_mcp",
                    "--config",
                    str(config_path),
                    "--diagnose-host",
                    "localbox",
                ],
                cwd=repo_root,
                env=env,
                check=False,
                capture_output=True,
                text=True,
            )
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertIn("Overall: OK", result.stdout)
        self.assertIn("tmux", result.stdout)


if __name__ == "__main__":
    unittest.main()
