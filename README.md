# remote-dev-mcp

`remote-dev-mcp` is a config-driven MCP server for remote C/C++ workflows where the
agent must be able to build, test, and run things on another machine without being
given unrestricted shell access.

This project is aimed at the common setup:

- development on a local laptop
- commits pushed from the laptop
- heavy build / runtime execution on a remote Linux box
- Copilot or another MCP client driving only approved actions

The server keeps the MCP surface intentionally small and pushes almost all behavior
into a local `TOML` config.

## What Problem It Solves

Typical remote dev flows usually fall into one of two bad extremes:

- too little automation, where every build/test/run is a manual SSH + `tmux` dance
- too much power, where the AI gets a raw shell and can do anything the remote user can do

`remote-dev-mcp` sits in the middle:

- the client can only call configured actions
- each action has fixed program / args / cwd / env
- parameters are validated before templating
- long-running work is persisted in `tmux`
- output is polled from logs, not from a fragile interactive terminal session

## Core Model

- The MCP server runs locally on your machine.
- Actions execute on a named host over `ssh`, or locally for tests.
- `tmux` on the target host owns the actual process lifecycle.
- Local metadata lives in SQLite.
- Remote output and final status live in files under `remote_state_dir`.

The exposed MCP tools are intentionally minimal:

- `list_actions`
- `run_action`
- `get_session`
- `list_sessions`
- `read_session_output`
- `stop_session`

There is no generic shell tool.

## Main Features

- Config-driven build / test / runtime actions
- `tmux`-backed sessions that survive client disconnects
- bounded parameter schemas with `enum`, `string`, `int`, `float`, `bool`, and guarded `path`
- enforced `timeout_sec`
- stable remote log polling
- `ready_regex` for long-running actions
- `concurrency_policy` for duplicate-run protection
- CLI diagnostics for configured hosts
- local cleanup of old sessions and optional remote state cleanup

## Typical Workflow

1. Define a small set of approved actions in `config.toml`.
2. Run `remote-dev-mcp --check-config` to validate the config.
3. Run `remote-dev-mcp --diagnose-host <host>` to verify the remote environment.
4. Start the server over `stdio` for Copilot / VS Code.
5. Let the client call:
   - `list_actions`
   - `run_action`
   - `get_session`
   - `read_session_output`
   - `stop_session`

For runtime actions, the client can keep polling until:

- `status == "running"` and `ready == true`, or
- the session reaches a terminal state

## Quick Start

Create a virtual environment and install the package:

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -e .
```

Start from the provided examples:

- [config.example.toml](/home/radiocpp/platform/opensource/remote-dev-mcp/config.example.toml)
- [config.team.example.toml](/home/radiocpp/platform/opensource/remote-dev-mcp/config.team.example.toml)

Validate a config:

```bash
remote-dev-mcp --config /path/to/config.toml --check-config
```

Diagnose a host before first use:

```bash
remote-dev-mcp --config /path/to/config.toml --diagnose-host devbox
```

Run over stdio:

```bash
remote-dev-mcp --config /path/to/config.toml
```

Run over Streamable HTTP:

```bash
remote-dev-mcp --config /path/to/config.toml --transport streamable-http
```

Clean old session records and optionally delete remote state:

```bash
remote-dev-mcp \
  --config /path/to/config.toml \
  --cleanup-sessions \
  --max-age-hours 168 \
  --remove-remote-state
```

## Configuration

All real behavior lives in `TOML`.

High-level structure:

```toml
[server]
name = "remote-dev-mcp"
default_host = "devbox"
state_dir = "~/.remote-dev-mcp"
session_prefix = "rdmcp"

[hosts.devbox]
transport = "ssh"
destination = "buildbox"
remote_state_dir = "/tmp/remote-dev-mcp"
allowed_cwd_roots = [
  "/work/project",
  "/work/project/build-debug",
  "/work/project/build-release",
]

[hosts.devbox.variables]
repo_root = "/work/project"
runtime_bin = "/work/project/out/debug/bin/app"

[actions.build_target]
description = "Build a configured CMake target"
host = "devbox"
program = "cmake"
args = ["--build", "{build_dir}", "--target", "{target}", "-j", "{jobs}"]
cwd = "{repo_root}"
timeout_sec = 1800

[actions.build_target.params.build_dir]
type = "enum"
values = ["/work/project/build-debug", "/work/project/build-release"]

[actions.build_target.params.target]
type = "enum"
values = ["core", "tests", "app"]

[actions.start_instance]
description = "Start a long-running runtime instance"
host = "devbox"
program = "{runtime_bin}"
args = ["--config", "{instance_config}", "--log-file", "{log_file}"]
cwd = "{repo_root}"
timeout_sec = 86400
ready_regex = "INSTANCE_STARTED"
concurrency_policy = "replace"
```

### Parameter Types

- `string`
- `int`
- `float`
- `bool`
- `enum`
- `path`

Useful constraints:

- `default`
- `required`
- `pattern`
- `min`
- `max`
- `values`
- `allow_empty`
- `must_be_relative`
- `roots`

For `path` parameters, you must set one of:

- `must_be_relative = true`
- `roots = ["/allowed/root", ...]`

Unbounded absolute path params are rejected at config load time.

### Action Fields Worth Knowing

`ready_regex`

- optional
- uses remote `grep -E` against the session log
- once matched, the session is marked ready and `ready_at` is stored
- useful for runtime processes that start quickly but become usable only after a known log line

`concurrency_policy`

- `allow`
- `deny_if_running`
- `replace`

Concurrency is evaluated per:

- host
- action name
- exact normalized parameter set

That means two runs of the same action with different params are treated as different sessions.

## CLI Admin Commands

`remote-dev-mcp --check-config`

- validates the config without starting the MCP server

`remote-dev-mcp --diagnose-host <host>`

- checks connectivity
- checks `shell_path`
- checks `tmux`
- checks the required shell utilities used by the wrapper
- checks renderable action programs for that host

`remote-dev-mcp --cleanup-sessions`

- prunes old local session metadata
- can also delete remote log / status files

## MCP Tool Behavior

### `list_actions`

Returns discoverable actions with:

- parameter schema
- host
- tags
- timeout
- `ready_regex`
- `concurrency_policy`

### `run_action`

- validates params
- renders templates
- applies concurrency policy
- starts a `tmux` session
- records local metadata
- returns session metadata

If `concurrency_policy = "replace"`, the response also includes `replaced_session_ids`.

### `get_session`

Returns normalized lifecycle state:

- `running`
- `completed`
- `failed`
- `stopped`
- `lost`

For actions with `ready_regex`, it also returns:

- `ready_check`
- `ready`
- `ready_at`

### `list_sessions`

Lists known sessions from local SQLite state.

### `read_session_output`

Reads remote output either:

- from a byte `offset`
- or by `tail_lines`

### `stop_session`

Stops the backing `tmux` session and records a stable `stopped` state when possible.

## VS Code / Copilot Example

Local stdio server:

```json
{
  "servers": {
    "remote-dev": {
      "type": "local",
      "command": "/absolute/path/to/.venv/bin/remote-dev-mcp",
      "args": ["--config", "/absolute/path/to/config.toml"],
      "tools": [
        "list_actions",
        "run_action",
        "get_session",
        "list_sessions",
        "read_session_output",
        "stop_session"
      ]
    }
  }
}
```

## Testing

Run the main suite:

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

Run the optional real SSH integration with Docker:

```bash
REMOTE_DEV_MCP_RUN_DOCKER_TESTS=1 \
PYTHONPATH=src \
python3 -m unittest tests.test_ssh_integration -v
```

## Security Model

This server reduces risk, but it is not a sandbox.

- The local user running the server still has the power of that local user.
- The remote account should be restricted and should not have `sudo`.
- `config.toml` must be reviewed like code.
- Keep `allowed_cwd_roots` narrow.
- Prefer `enum` params for build dirs, targets, tests, and instance names.
- Only use `path` params when you actually need them.
- Do not reintroduce a generic shell tool.

## macOS and Linux Notes

The common setup of:

- macOS laptop
- local MCP server on the laptop
- remote Linux build/runtime box over `ssh`

is supported well.

Notes:

- `tmux` is required on every host that actually runs actions
- `setsid` improves timeout/stop behavior but is not strictly required
- the wrapper assumes POSIX shell behavior
- `ready_regex` depends on remote `grep -E`

If you run actions locally on macOS instead of on a Linux box:

- make sure `tmux` is installed
- expect process-tree termination to be weaker if `setsid` is missing

## Current State

This repository is already in usable internal-beta shape:

- config validation exists
- fake `tmux` end-to-end tests exist
- real Docker-based `ssh -> tmux -> cmake -> ctest -> runtime` integration exists

The remaining roadmap is tracked in [TODO.md](/home/radiocpp/platform/opensource/remote-dev-mcp/TODO.md).
