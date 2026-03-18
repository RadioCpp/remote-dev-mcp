# AGENTS.md

This file explains how humans and coding agents should work in this repository.

## Project Intent

`remote-dev-mcp` is a config-driven MCP server for remote build / test / runtime
workflows where the agent must not receive unrestricted shell access.

The core product decision is not negotiable:

- keep the MCP tool surface small
- keep behavior in config
- keep enforcement on the server side
- do not add a generic remote shell tool

## Repository Map

- `src/remote_dev_mcp/config.py`
  Config parsing and validation
- `src/remote_dev_mcp/service.py`
  Main runtime logic, session lifecycle, concurrency, readiness, diagnostics
- `src/remote_dev_mcp/session_store.py`
  Local SQLite persistence
- `src/remote_dev_mcp/executor.py`
  Local / SSH execution layer
- `src/remote_dev_mcp/__main__.py`
  CLI entrypoint
- `config.example.toml`
  Compact example config
- `config.team.example.toml`
  Team rollout template
- `tests/test_e2e.py`
  Fake-`tmux` end-to-end tests
- `tests/test_ssh_integration.py`
  Optional real Docker-based SSH integration

## Non-Negotiable Design Constraints

- Do not add unrestricted command execution.
- Do not add a separate MCP tool per action.
- Keep new product behavior config-driven unless there is a strong reason not to.
- Preserve POSIX shell compatibility in the wrapper path.
- Preserve the current security model:
  - validated params
  - narrow working directories
  - explicit action definitions
  - no arbitrary shell strings from the model

## Preferred Extension Pattern

When adding a new capability, prefer this order:

1. Extend config schema.
2. Extend server-side validation.
3. Extend runtime logic in `service.py`.
4. Extend docs and example configs.
5. Add tests.

If you are tempted to add a new MCP tool, first ask whether the feature can fit into:

- action config
- `list_actions`
- `run_action`
- `get_session`
- `list_sessions`
- `read_session_output`
- `stop_session`

## Testing Expectations

Before considering a change done, run:

```bash
python3 -m compileall src tests
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

For changes that affect SSH / runtime lifecycle / `tmux`, also run:

```bash
REMOTE_DEV_MCP_RUN_DOCKER_TESTS=1 \
PYTHONPATH=src \
python3 -m unittest tests.test_ssh_integration -v
```

## Documentation Expectations

If you change behavior that users will notice, update all relevant docs:

- `README.md`
- `config.example.toml`
- `config.team.example.toml`
- `TODO.md` when roadmap items become done or obsolete

## Runtime Notes

- `ready_regex` uses remote `grep -E`.
- `concurrency_policy` currently applies to the same host, same action, same normalized params.
- `tmux` is the real persistence layer for long-running work.
- SQLite is only local metadata; do not treat it as the source of truth for live process state.
- `setsid` improves timeout and kill behavior but may be unavailable on some hosts.

## Common Footguns

- Do not widen `path` params unnecessarily.
- Do not make `program` templating too dynamic without strong guardrails.
- Do not assume macOS and Linux have identical process-tree semantics.
- Do not rely on local stored status without refreshing when lifecycle correctness matters.
- Do not forget to update session store schema migrations when session metadata changes.

## Commit Discipline

- Keep commits coherent.
- Include tests with behavior changes.
- Prefer real verification over “should work”.
