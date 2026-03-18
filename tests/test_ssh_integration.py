from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import textwrap
import time
import unittest
from pathlib import Path

from remote_dev_mcp.config import load_config
from remote_dev_mcp.service import RemoteDevService
from remote_dev_mcp.session_store import SessionStore


@unittest.skipUnless(
    os.environ.get("REMOTE_DEV_MCP_RUN_DOCKER_TESTS") == "1",
    "docker integration test disabled",
)
class DockerSshIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if shutil.which("docker") is None:
            raise unittest.SkipTest("docker is not available")
        cls.repo_root = Path(__file__).resolve().parents[1]
        cls.image_tag = "remote-dev-mcp:ssh-integration"
        cls._run(
            [
                "docker",
                "build",
                "-t",
                cls.image_tag,
                "--build-arg",
                f"DEV_UID={os.getuid()}",
                "--build-arg",
                f"DEV_GID={os.getgid()}",
                "-f",
                str(cls.repo_root / "tests/docker/Dockerfile"),
                str(cls.repo_root / "tests/docker"),
            ],
            cwd=cls.repo_root,
            timeout=600,
        )

    def setUp(self) -> None:
        self.tmp_dir = tempfile.TemporaryDirectory(prefix=".docker-test-", dir=self.repo_root)
        self.base = Path(self.tmp_dir.name)
        self.project_root = self.base / "project"
        self.project_root.mkdir(parents=True)
        self._write_fixture_project(self.project_root)
        self.key_path = self.base / "id_ed25519"
        self._run(
            [
                "ssh-keygen",
                "-q",
                "-t",
                "ed25519",
                "-N",
                "",
                "-f",
                str(self.key_path),
            ],
            cwd=self.base,
            timeout=30,
        )
        public_key = self.key_path.with_suffix(".pub").read_text(encoding="utf-8").strip()
        self.container_id = self._run(
            [
                "docker",
                "run",
                "-d",
                "-P",
                "-e",
                f"AUTHORIZED_KEY={public_key}",
                "-v",
                f"{self.project_root}:/work/project",
                self.image_tag,
            ],
            cwd=self.base,
            timeout=120,
        ).stdout.strip()
        self.port = self._run(
            [
                "docker",
                "inspect",
                "-f",
                "{{(index (index .NetworkSettings.Ports \"22/tcp\") 0).HostPort}}",
                self.container_id,
            ],
            cwd=self.base,
            timeout=30,
        ).stdout.strip()
        self._wait_for_ssh()
        self.config_path = self._write_config()
        self.config = load_config(self.config_path)
        self.store = SessionStore(self.config.server.state_dir / "sessions.sqlite3")
        self.service = RemoteDevService(self.config, self.store)

    def tearDown(self) -> None:
        if getattr(self, "container_id", ""):
            subprocess.run(
                ["docker", "rm", "-f", self.container_id],
                check=False,
                capture_output=True,
                text=True,
            )
        self.tmp_dir.cleanup()

    def test_real_ssh_flow(self) -> None:
        configure = self.service.run_action(
            "configure",
            {
                "build_dir": "/work/project/build-debug",
                "generator": "Ninja",
                "build_type": "Debug",
            },
        )
        configure = self._wait_for_status(configure["session_id"], {"completed"})
        self.assertEqual(configure["exit_code"], 0)

        build = self.service.run_action(
            "build_target",
            {
                "build_dir": "/work/project/build-debug",
                "target": "your_app",
                "jobs": 2,
            },
        )
        build = self._wait_for_status(build["session_id"], {"completed"})
        self.assertEqual(build["exit_code"], 0)
        build_output = self.service.read_session_output(build["session_id"], tail_lines=50)
        self.assertIn("your_app", build_output["content"])

        test_run = self.service.run_action(
            "run_ctest_regex",
            {
                "build_dir": "/work/project/build-debug",
                "test_regex": "your_app_smoke",
            },
        )
        test_run = self._wait_for_status(test_run["session_id"], {"completed"})
        self.assertEqual(test_run["exit_code"], 0)
        test_output = self.service.read_session_output(test_run["session_id"], tail_lines=50)
        self.assertIn("100% tests passed", test_output["content"])

        runtime = self.service.run_action(
            "start_instance",
            {
                "instance_config": "/work/project/config/dev.toml",
                "log_file": "/work/project/logs/runtime.log",
            },
        )
        runtime = self._wait_for_status(runtime["session_id"], {"running"})
        runtime = self._wait_for_ready(runtime["session_id"])
        self.assertTrue(runtime["ready"])
        runtime_output = self.service.read_session_output(runtime["session_id"], tail_lines=50)
        self.assertIn("INSTANCE_STARTED", runtime_output["content"])

        stopped = self.service.stop_session(runtime["session_id"])
        self.assertIn(stopped["status"], {"stopped", "failed", "completed"})

    def _write_config(self) -> Path:
        config_path = self.base / "config.toml"
        config_path.write_text(
            textwrap.dedent(
                f"""
                [server]
                default_host = "devbox"
                state_dir = "{(self.base / 'state').as_posix()}"

                [hosts.devbox]
                transport = "ssh"
                destination = "dev@127.0.0.1"
                ssh_command = "ssh"
                ssh_args = [
                  "-p", "{self.port}",
                  "-i", "{self.key_path.as_posix()}",
                  "-o", "BatchMode=yes",
                  "-o", "StrictHostKeyChecking=no",
                  "-o", "UserKnownHostsFile=/dev/null",
                ]
                tmux_command = "tmux"
                shell_path = "/bin/sh"
                remote_state_dir = "/tmp/remote-dev-mcp"
                allowed_cwd_roots = [
                  "/work/project",
                  "/work/project/build-debug",
                  "/work/project/build-release",
                  "/work/project/config",
                  "/work/project/logs",
                  "/work/project/out",
                ]

                [hosts.devbox.variables]
                repo_root = "/work/project"
                runtime_bin = "/work/project/build-debug/your_app"

                [actions.configure]
                description = "Configure test project"
                program = "cmake"
                args = [
                  "-S", "{{repo_root}}",
                  "-B", "{{build_dir}}",
                  "-G", "{{generator}}",
                  "-DCMAKE_BUILD_TYPE={{build_type}}",
                ]
                cwd = "{{repo_root}}"
                timeout_sec = 300

                [actions.configure.params.build_dir]
                type = "enum"
                values = ["/work/project/build-debug", "/work/project/build-release"]

                [actions.configure.params.generator]
                type = "enum"
                values = ["Ninja", "Unix Makefiles"]
                default = "Ninja"

                [actions.configure.params.build_type]
                type = "enum"
                values = ["Debug", "Release"]
                default = "Debug"

                [actions.build_target]
                description = "Build target"
                program = "cmake"
                args = ["--build", "{{build_dir}}", "--target", "{{target}}", "-j", "{{jobs}}"]
                cwd = "{{repo_root}}"
                timeout_sec = 300

                [actions.build_target.params.build_dir]
                type = "enum"
                values = ["/work/project/build-debug", "/work/project/build-release"]

                [actions.build_target.params.target]
                type = "enum"
                values = ["your_app"]

                [actions.build_target.params.jobs]
                type = "int"
                default = 2
                min = 1
                max = 8

                [actions.run_ctest_regex]
                description = "Run test regex"
                program = "ctest"
                args = ["--test-dir", "{{build_dir}}", "-R", "{{test_regex}}", "--output-on-failure"]
                cwd = "{{repo_root}}"
                timeout_sec = 300

                [actions.run_ctest_regex.params.build_dir]
                type = "enum"
                values = ["/work/project/build-debug", "/work/project/build-release"]

                [actions.run_ctest_regex.params.test_regex]
                type = "string"
                pattern = "^[A-Za-z0-9_./:+*?-]+$"

                [actions.start_instance]
                description = "Start runtime"
                program = "{{runtime_bin}}"
                args = ["--config", "{{instance_config}}", "--log-file", "{{log_file}}", "--sleep-seconds", "120"]
                cwd = "{{repo_root}}"
                timeout_sec = 600
                ready_regex = "INSTANCE_STARTED"
                concurrency_policy = "replace"

                [actions.start_instance.params.instance_config]
                type = "path"
                roots = ["/work/project/config"]

                [actions.start_instance.params.log_file]
                type = "path"
                roots = ["/work/project/logs"]
                """
            ).strip()
            + "\n",
            encoding="utf-8",
        )
        return config_path

    def _write_fixture_project(self, project_root: Path) -> None:
        (project_root / "src").mkdir(parents=True)
        (project_root / "config").mkdir(parents=True)
        (project_root / "logs").mkdir(parents=True)
        (project_root / "build-debug").mkdir(parents=True)
        (project_root / "build-release").mkdir(parents=True)
        (project_root / "CMakeLists.txt").write_text(
            textwrap.dedent(
                """
                cmake_minimum_required(VERSION 3.16)
                project(remote_dev_fixture LANGUAGES CXX)

                enable_testing()
                add_executable(your_app src/main.cpp)
                target_compile_features(your_app PRIVATE cxx_std_17)
                add_test(NAME your_app_smoke COMMAND your_app --mode test)
                """
            ).strip()
            + "\n",
            encoding="utf-8",
        )
        (project_root / "src/main.cpp").write_text(
            textwrap.dedent(
                r"""
                #include <chrono>
                #include <fstream>
                #include <iostream>
                #include <string>
                #include <thread>

                int main(int argc, char** argv) {
                  std::string mode = "run";
                  std::string config_path;
                  std::string log_file;
                  int sleep_seconds = 0;

                  for (int i = 1; i < argc; ++i) {
                    std::string arg = argv[i];
                    if (arg == "--mode" && i + 1 < argc) {
                      mode = argv[++i];
                    } else if (arg == "--config" && i + 1 < argc) {
                      config_path = argv[++i];
                    } else if (arg == "--log-file" && i + 1 < argc) {
                      log_file = argv[++i];
                    } else if (arg == "--sleep-seconds" && i + 1 < argc) {
                      sleep_seconds = std::stoi(argv[++i]);
                    }
                  }

                  if (mode == "test") {
                    std::cout << "TEST_OK" << std::endl;
                    return 0;
                  }

                  if (config_path.empty() || log_file.empty()) {
                    std::cerr << "missing config or log path" << std::endl;
                    return 2;
                  }

                  std::ifstream cfg(config_path);
                  if (!cfg.good()) {
                    std::cerr << "config not readable" << std::endl;
                    return 3;
                  }

                  std::ofstream out(log_file, std::ios::app);
                  if (!out.good()) {
                    std::cerr << "log not writable" << std::endl;
                    return 4;
                  }

                  out << "instance_log_started" << std::endl;
                  out.flush();
                  std::cout << "INSTANCE_STARTED" << std::endl;
                  std::cout.flush();

                  if (sleep_seconds > 0) {
                    std::this_thread::sleep_for(std::chrono::seconds(sleep_seconds));
                  }
                  return 0;
                }
                """
            ).strip()
            + "\n",
            encoding="utf-8",
        )
        (project_root / "config/dev.toml").write_text("mode = 'dev'\n", encoding="utf-8")

    def _wait_for_ssh(self) -> None:
        deadline = time.time() + 30
        while time.time() < deadline:
            result = subprocess.run(
                [
                    "ssh",
                    "-p",
                    self.port,
                    "-i",
                    str(self.key_path),
                    "-o",
                    "BatchMode=yes",
                    "-o",
                    "StrictHostKeyChecking=no",
                    "-o",
                    "UserKnownHostsFile=/dev/null",
                    "dev@127.0.0.1",
                    "true",
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            if result.returncode == 0:
                return
            time.sleep(0.5)
        raise AssertionError("SSH did not become ready in time")

    def _wait_for_status(self, session_id: str, wanted: set[str]) -> dict:
        deadline = time.time() + 120
        last = None
        while time.time() < deadline:
            session = self.service.get_session(session_id)
            last = session
            if session["status"] in wanted:
                return session
            if session["status"] in {"failed", "completed", "stopped", "lost"} and session["status"] not in wanted:
                self.fail(f"Unexpected terminal state: {session}")
            time.sleep(0.5)
        self.fail(f"Timed out waiting for status {wanted}, last={last}")

    def _wait_for_ready(self, session_id: str) -> dict:
        deadline = time.time() + 120
        last = None
        while time.time() < deadline:
            session = self.service.get_session(session_id)
            last = session
            if session.get("ready") is True:
                return session
            if session["status"] in {"failed", "completed", "stopped", "lost"}:
                self.fail(f"Unexpected terminal state before ready: {session}")
            time.sleep(0.5)
        self.fail(f"Timed out waiting for ready state, last={last}")

    @staticmethod
    def _run(argv: list[str], cwd: Path, timeout: int) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            argv,
            check=False,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if result.returncode != 0:
            raise AssertionError(
                f"Command failed: {' '.join(argv)}\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
            )
        return result


if __name__ == "__main__":
    unittest.main()
