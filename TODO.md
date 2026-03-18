# TODO

This file tracks the next work after the current beta milestone.

## Current Baseline

- Config-driven MCP server with a narrow tool surface:
  - `list_actions`
  - `run_action`
  - `get_session`
  - `list_sessions`
  - `read_session_output`
  - `stop_session`
- Session lifecycle backed by `tmux`, local SQLite metadata, and remote log/status files.
- Verified test layers:
  - config/unit tests
  - fake `tmux` end-to-end tests
  - Docker-based real `ssh -> tmux -> cmake -> ctest -> runtime` integration test

## Release Blockers

- Run one real smoke test against the actual team remote box with a production-like `config.toml`.
- Write a short rollout guide for colleagues:
  - Python version
  - install command
  - required remote packages
  - example Copilot / VS Code MCP config
- Add a reproducible dependency workflow:
  - pinned dependency versions
  - lockfile or documented install path
- Add CI that runs:
  - unit/config tests on every push
  - fake `tmux` e2e on every push
  - Docker SSH integration on demand or in nightly CI

## P0 Reliability

- Add explicit health-check alternatives beyond `ready_regex`:
  - probe command
  - file/socket existence
  - HTTP health endpoint via a wrapper action
- Add stronger recovery for malformed or half-written remote status files with better diagnostics.
- Add batched remote cleanup for old session state so cleanup does fewer SSH round trips.
- Add remote state retention controls:
  - max session age
  - max log size
  - optional prune-on-start
- Add tests for interrupted SSH transport during:
  - `run_action`
  - `get_session`
  - `cleanup_sessions`

## P0 Security

- Add optional allowlists for rendered `program` paths, not only `cwd` and typed params.
- Add optional denylist for environment variable names even when syntactically valid.
- Add optional per-action confirmation metadata for destructive operations.
- Document and encourage strict SSH settings for team rollout:
  - host key checking
  - fixed identity file
  - dedicated SSH alias
- Add an option to assert the expected remote user or hostname before action execution.
- Keep the hard non-goal explicit: do not add a generic shell execution tool.

## P1 Operator UX

- Add `list_hosts` or `diagnose_all_hosts` admin command for multi-host setups.
- Add `--list-sessions` CLI wrapper for quick local inspection without an MCP client.
- Add JSON output mode for CLI admin operations.
- Add optional session labels or user tags for better filtering in larger teams.
- Add clearer notes in session output for common failure cases:
  - bad cwd
  - invalid env export
  - timeout
  - SSH transport failure

## P1 Config and Packaging

- Add a documented config schema reference with field-by-field examples.
- Add support for sharing config fragments:
  - base team config
  - per-user overlay
  - per-host overlay
- Add a packaged console entrypoint install path suitable for team rollout.
- Add release notes / changelog discipline before broader adoption.

## P2 Runtime Improvements

- Add optional "follow output" helper on the CLI side without expanding the MCP tool surface.
- Add optional archive/export of completed session logs before cleanup.
- Add optional remote metrics or audit events for:
  - action start
  - action stop
  - timeout
  - cleanup
- Improve process-tree termination fallback on hosts where `setsid` is unavailable.

## P2 Testing

- Add shell compatibility tests for more than one POSIX shell where practical.
- Add tests for very large log output and truncation behavior.
- Add tests for simultaneous sessions on the same host.
- Add tests for cleanup while sessions are still running.
- Add regression tests for allowed non-zero exit codes, explicit stop semantics, and session listing.

## Deliberate Non-Goals

- No unrestricted remote terminal tool.
- No free-form `bash -lc` supplied by the model.
- No explosion of MCP tools for every build/test command; keep domain actions in config.
