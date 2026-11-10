# Python standards baseline

Existing violations at `7bc481c8deb92753dfcfd8d4d5fead9bec6573ab` are baselined by exact authored file and rule. New files receive all enabled checks. Remove an exception when repairing its source. The tool configuration owns enforcement; this document records the rollout evidence and rationale.

## Ruff

Reason: preserve existing implementations during the standards rollout. Each row records the existing count and a representative required repair. No wildcard path or whole-rule ignores were added.

| File | Rule | Existing count | Required repair |
| --- | --- | ---: | --- |
| `.agents/agent/runtime/latest_plugins.py` | `TID251` | 1 | `sys.path` is banned: Install the declared dependency; do not modify the import search path. |
| `.agents/agent/runtime/plugins/refresh.py` | `PLR6301` | 1 | Method `run` could be a function, class method, or static method |
| `.agents/agent/runtime/plugins/refresh.py` | `TID251` | 1 | `sys.path` is banned: Install the declared dependency; do not modify the import search path. |
| `.agents/agent/runtime/retired_state.py` | `PLR6301` | 2 | Method `listing` could be a function, class method, or static method |
| `.agents/agent/runtime/retired_state.py` | `TRY300` | 1 | Consider moving this statement to an `else` block |
| `.agents/agent/runtime/tj_recover.py` | `TID251` | 1 | `sys.path` is banned: Install the declared dependency; do not modify the import search path. |
| `.agents/agent/utility/plugins/scripts/config_conflicts.py` | `ANN401` | 4 | Dynamically typed expressions (typing.Any) are disallowed in `base` |
| `tests/fake_machine.py` | `ANN401` | 2 | Dynamically typed expressions (typing.Any) are disallowed in `machine_context` |
| `tests/test_boot_history.py` | `PT006` | 1 | Wrong type passed to first argument of `pytest.mark.parametrize`; expected `tuple` |
| `tests/test_conflicted_plugin_settings.py` | `PT011` | 1 | `pytest.raises(ValueError)` is too broad, set the `match` parameter or use a more specific exception |
| `tests/test_conflicted_plugin_settings.py` | `TID251` | 2 | `sys.path` is banned: Install the declared dependency; do not modify the import search path. |
| `tests/test_latest_plugins.py` | `ANN401` | 2 | Dynamically typed expressions (typing.Any) are disallowed in `hook` |
| `tests/test_local_codex_refresh.py` | `TID251` | 2 | `sys.path` is banned: Install the declared dependency; do not modify the import search path. |
| `tests/test_native_runtime.py` | `TID251` | 1 | `sys.path` is banned: Install the declared dependency; do not modify the import search path. |
| `tests/test_retired_state.py` | `ANN401` | 1 | Dynamically typed expressions (typing.Any) are disallowed in `**options` |
| `tests/test_retired_state.py` | `TID251` | 1 | `sys.path` is banned: Install the declared dependency; do not modify the import search path. |
| `tests/test_session_recovery.py` | `ANN401` | 7 | Dynamically typed expressions (typing.Any) are disallowed in `*arguments` |
| `tests/test_session_recovery.py` | `PLR6301` | 1 | Method `request` could be a function, class method, or static method |
| `tests/test_session_recovery.py` | `PT006` | 1 | Wrong type passed to first argument of `pytest.mark.parametrize`; expected `tuple` |
| `tests/test_tj_recover.py` | `ANN401` | 8 | Dynamically typed expressions (typing.Any) are disallowed in `**fields` |
| `tests/test_tj_recover.py` | `PLR6301` | 3 | Method `resume_arguments` could be a function, class method, or static method |

## Custom checker

Reason: preserve exact pre-existing custom-checker findings during the standards rollout. Each entry maps to one authored path and rule; repair the stated design issue and remove its exception. No generated or vendored path is included.

| File | Rule | Existing count | Required repair |
| --- | --- | ---: | --- |
| .agents/agent/resources/lanes/resolve.py | PY001 | 1 | Rewrite the nested comprehension as named loops or one generator. |
| .agents/agent/resources/lanes/resolve.py | PY016 | 1 | Inject process state or confine the read to an entry-point main. |
| .agents/agent/runtime/agent_cli.py | PY001 | 1 | Rewrite the nested comprehension as named loops or one generator. |
| .agents/agent/runtime/agent_cli.py | PY016 | 6 | Inject process state or confine the read to an entry-point main. |
| .agents/agent/runtime/history.py | PY001 | 1 | Rewrite the nested comprehension as named loops or one generator. |
| .agents/agent/runtime/install.py | PY001 | 1 | Rewrite the nested comprehension as named loops or one generator. |
| .agents/agent/runtime/install.py | PY016 | 2 | Inject process state or confine the read to an entry-point main. |
| .agents/agent/runtime/jobs.py | PY016 | 1 | Inject process state or confine the read to an entry-point main. |
| .agents/agent/runtime/latest_plugins.py | PY001 | 1 | Rewrite the nested comprehension as named loops or one generator. |
| .agents/agent/runtime/latest_plugins.py | PY016 | 1 | Inject process state or confine the read to an entry-point main. |
| .agents/agent/runtime/native_runtime.py | PY016 | 3 | Inject process state or confine the read to an entry-point main. |
| .agents/agent/runtime/required_plugins.py | PY001 | 1 | Rewrite the nested comprehension as named loops or one generator. |
| .agents/agent/runtime/retired_state.py | PY016 | 2 | Inject process state or confine the read to an entry-point main. |
| .agents/agent/runtime/tj_agent/host.py | PY016 | 1 | Inject process state or confine the read to an entry-point main. |
| .agents/agent/runtime/tj_start.py | PY016 | 1 | Inject process state or confine the read to an entry-point main. |
| .agents/agent/runtime/work_items_install.py | PY001 | 2 | Rewrite the nested comprehension as named loops or one generator. |
| .agents/agent/utility/plugins/scripts/machine_paths.py | PY001 | 2 | Rewrite the nested comprehension as named loops or one generator. |
| .agents/agent/utility/plugins/scripts/repo_config.py | PY001 | 3 | Rewrite the nested comprehension as named loops or one generator. |
| .agents/codex/utility/hooks/scripts/hook_control.py | PY016 | 1 | Inject process state or confine the read to an entry-point main. |
| .claude/runtime/permissions.py | PY001 | 1 | Rewrite the nested comprehension as named loops or one generator. |
| .claude/runtime/registry.py | PY001 | 1 | Rewrite the nested comprehension as named loops or one generator. |
| .codex/runtime/hook_trust.py | PY001 | 4 | Rewrite the nested comprehension as named loops or one generator. |
| .codex/runtime/hook_trust.py | PY016 | 3 | Inject process state or confine the read to an entry-point main. |
| .codex/runtime/installed.py | PY001 | 1 | Rewrite the nested comprehension as named loops or one generator. |
| .codex/runtime/launch.py | PY016 | 1 | Inject process state or confine the read to an entry-point main. |
| .codex/runtime/websocket.py | PY001 | 1 | Rewrite the nested comprehension as named loops or one generator. |
| tests/conftest.py | PY016 | 2 | Inject process state or confine the read to an entry-point main. |
| tests/fake_machine.py | PY016 | 2 | Inject process state or confine the read to an entry-point main. |
| tests/fixtures/owned_native.py | PY009 | 2 | Move import-time process I/O and environment reads into main or an explicit function. |
| tests/fixtures/owned_native.py | PY016 | 2 | Inject process state or confine the read to an entry-point main. |
| tests/fixtures/plugin_cli.py | PY001 | 1 | Rewrite the nested comprehension as named loops or one generator. |
| tests/fixtures/plugin_cli.py | PY009 | 1 | Move import-time process I/O and environment reads into main or an explicit function. |
| tests/fixtures/plugin_cli.py | PY016 | 4 | Inject process state or confine the read to an entry-point main. |
| tests/fixtures/plugin_console_probe.py | PY016 | 1 | Inject process state or confine the read to an entry-point main. |
| tests/fixtures/plugin_console_probe.py | PY017 | 2 | Inject a controllable clock. |
| tests/fixtures/runtime_import_probe.py | PY016 | 1 | Inject process state or confine the read to an entry-point main. |
| tests/git_isolation.py | PY016 | 2 | Inject process state or confine the read to an entry-point main. |
| tests/test_agent_cli.py | PY016 | 9 | Inject process state or confine the read to an entry-point main. |
| tests/test_claude_worktree_installations.py | PY016 | 1 | Inject process state or confine the read to an entry-point main. |
| tests/test_conflicted_plugin_settings.py | PY016 | 1 | Inject process state or confine the read to an entry-point main. |
| tests/test_handoff_launchers.py | PY016 | 3 | Inject process state or confine the read to an entry-point main. |
| tests/test_hook_trust.py | PY001 | 1 | Rewrite the nested comprehension as named loops or one generator. |
| tests/test_latest_plugins.py | PY017 | 1 | Inject a controllable clock. |
| tests/test_local_codex_refresh.py | PY016 | 1 | Inject process state or confine the read to an entry-point main. |
| tests/test_native_runtime.py | PY016 | 5 | Inject process state or confine the read to an entry-point main. |
| tests/test_plugin_modules.py | PY016 | 1 | Inject process state or confine the read to an entry-point main. |
| tests/test_plugin_setup.py | PY016 | 2 | Inject process state or confine the read to an entry-point main. |
| tests/test_refresh_plugins.py | PY016 | 2 | Inject process state or confine the read to an entry-point main. |
| tests/test_required_plugins.py | PY016 | 1 | Inject process state or confine the read to an entry-point main. |
| tests/test_selection_convergence.py | PY016 | 1 | Inject process state or confine the read to an entry-point main. |
| tests/test_session_hook_events.py | PY016 | 1 | Inject process state or confine the read to an entry-point main. |
| tests/test_windows_process_visibility.py | PY016 | 1 | Inject process state or confine the read to an entry-point main. |
| tests/test_windows_process_visibility.py | PY017 | 2 | Inject a controllable clock. |
| tests/test_work_items_install.py | PY001 | 3 | Rewrite the nested comprehension as named loops or one generator. |

## Expanded Python scope

These exact entries cover unchanged legacy diagnostics exposed when the root Python scope expanded. Mypy evidence covers the separate common, Claude runtime, and Codex runtime groups on win32 and linux. Repair each diagnostic and remove its matching baseline entry.

### Ruff

| File | Rule | Existing count | Required repair |
| --- | --- | ---: | --- |
| .agents/agent/resources/handoff/launch_claude.py | E501 | 32 | Repair the existing E501 diagnostic and remove this exact ignore. |
| .agents/agent/resources/handoff/launch_claude.py | TID251 | 1 | Repair the existing TID251 diagnostic and remove this exact ignore. |
| .agents/agent/resources/handoff/launch_claude.py | TRY300 | 1 | Repair the existing TRY300 diagnostic and remove this exact ignore. |
| .agents/agent/resources/handoff/launch_codex.py | E501 | 29 | Repair the existing E501 diagnostic and remove this exact ignore. |
| .agents/agent/resources/handoff/launch_codex.py | TID251 | 1 | Repair the existing TID251 diagnostic and remove this exact ignore. |
| .agents/agent/resources/handoff/launch_codex.py | TRY300 | 1 | Repair the existing TRY300 diagnostic and remove this exact ignore. |
| .agents/agent/resources/lanes/resolve.py | B904 | 2 | Repair the existing B904 diagnostic and remove this exact ignore. |
| .agents/agent/resources/lanes/resolve.py | C416 | 1 | Repair the existing C416 diagnostic and remove this exact ignore. |
| .agents/agent/resources/lanes/resolve.py | E501 | 9 | Repair the existing E501 diagnostic and remove this exact ignore. |
| .agents/agent/resources/lanes/resolve.py | I001 | 1 | Repair the existing I001 diagnostic and remove this exact ignore. |
| .agents/agent/resources/lanes/resolve.py | PTH100 | 1 | Repair the existing PTH100 diagnostic and remove this exact ignore. |
| .agents/agent/resources/lanes/resolve.py | PTH118 | 1 | Repair the existing PTH118 diagnostic and remove this exact ignore. |
| .agents/agent/resources/lanes/resolve.py | PTH120 | 1 | Repair the existing PTH120 diagnostic and remove this exact ignore. |
| .agents/agent/resources/lanes/resolve.py | PTH123 | 1 | Repair the existing PTH123 diagnostic and remove this exact ignore. |
| .agents/agent/resources/lanes/resolve.py | TID251 | 1 | Repair the existing TID251 diagnostic and remove this exact ignore. |
| .agents/agent/runtime/agent_cli.py | ANN401 | 1 | Repair the existing ANN401 diagnostic and remove this exact ignore. |
| .agents/agent/runtime/agent_cli.py | E402 | 6 | Repair the existing E402 diagnostic and remove this exact ignore. |
| .agents/agent/runtime/agent_cli.py | E501 | 69 | Repair the existing E501 diagnostic and remove this exact ignore. |
| .agents/agent/runtime/agent_cli.py | I001 | 2 | Repair the existing I001 diagnostic and remove this exact ignore. |
| .agents/agent/runtime/agent_cli.py | PTH100 | 1 | Repair the existing PTH100 diagnostic and remove this exact ignore. |
| .agents/agent/runtime/agent_cli.py | TID251 | 4 | Repair the existing TID251 diagnostic and remove this exact ignore. |
| .agents/agent/runtime/agent_store.py | E501 | 14 | Repair the existing E501 diagnostic and remove this exact ignore. |
| .agents/agent/runtime/agent_store.py | I001 | 1 | Repair the existing I001 diagnostic and remove this exact ignore. |
| .agents/agent/runtime/agent_store.py | PTH105 | 1 | Repair the existing PTH105 diagnostic and remove this exact ignore. |
| .agents/agent/runtime/history.py | C408 | 1 | Repair the existing C408 diagnostic and remove this exact ignore. |
| .agents/agent/runtime/history.py | E501 | 19 | Repair the existing E501 diagnostic and remove this exact ignore. |
| .agents/agent/runtime/history.py | I001 | 2 | Repair the existing I001 diagnostic and remove this exact ignore. |
| .agents/agent/runtime/history.py | PTH100 | 1 | Repair the existing PTH100 diagnostic and remove this exact ignore. |
| .agents/agent/runtime/history.py | TID251 | 1 | Repair the existing TID251 diagnostic and remove this exact ignore. |
| .agents/agent/runtime/history.py | UP017 | 3 | Repair the existing UP017 diagnostic and remove this exact ignore. |
| .agents/agent/runtime/host_permissions.py | E501 | 11 | Repair the existing E501 diagnostic and remove this exact ignore. |
| .agents/agent/runtime/host_permissions.py | I001 | 1 | Repair the existing I001 diagnostic and remove this exact ignore. |
| .agents/agent/runtime/host_permissions.py | RUF021 | 4 | Repair the existing RUF021 diagnostic and remove this exact ignore. |
| .agents/agent/runtime/host_permissions.py | RUF100 | 3 | Repair the existing RUF100 diagnostic and remove this exact ignore. |
| .agents/agent/runtime/host_permissions.py | TID251 | 2 | Repair the existing TID251 diagnostic and remove this exact ignore. |
| .agents/agent/runtime/host_plugins.py | E501 | 3 | Repair the existing E501 diagnostic and remove this exact ignore. |
| .agents/agent/runtime/host_sessions.py | E501 | 1 | Repair the existing E501 diagnostic and remove this exact ignore. |
| .agents/agent/runtime/install.py | B905 | 1 | Repair the existing B905 diagnostic and remove this exact ignore. |
| .agents/agent/runtime/install.py | E402 | 18 | Repair the existing E402 diagnostic and remove this exact ignore. |
| .agents/agent/runtime/install.py | E501 | 103 | Repair the existing E501 diagnostic and remove this exact ignore. |
| .agents/agent/runtime/install.py | I001 | 2 | Repair the existing I001 diagnostic and remove this exact ignore. |
| .agents/agent/runtime/install.py | PLR6301 | 3 | Repair the existing PLR6301 diagnostic and remove this exact ignore. |
| .agents/agent/runtime/install.py | PTH100 | 2 | Repair the existing PTH100 diagnostic and remove this exact ignore. |
| .agents/agent/runtime/install.py | PTH105 | 6 | Repair the existing PTH105 diagnostic and remove this exact ignore. |
| .agents/agent/runtime/install.py | PTH108 | 1 | Repair the existing PTH108 diagnostic and remove this exact ignore. |
| .agents/agent/runtime/install.py | PTH110 | 1 | Repair the existing PTH110 diagnostic and remove this exact ignore. |
| .agents/agent/runtime/install.py | RET502 | 1 | Repair the existing RET502 diagnostic and remove this exact ignore. |
| .agents/agent/runtime/install.py | RET503 | 1 | Repair the existing RET503 diagnostic and remove this exact ignore. |
| .agents/agent/runtime/install.py | RUF007 | 1 | Repair the existing RUF007 diagnostic and remove this exact ignore. |
| .agents/agent/runtime/install.py | SIM102 | 1 | Repair the existing SIM102 diagnostic and remove this exact ignore. |
| .agents/agent/runtime/install.py | SIM105 | 1 | Repair the existing SIM105 diagnostic and remove this exact ignore. |
| .agents/agent/runtime/install.py | TID251 | 4 | Repair the existing TID251 diagnostic and remove this exact ignore. |
| .agents/agent/runtime/install.py | UP035 | 1 | Repair the existing UP035 diagnostic and remove this exact ignore. |
| .agents/agent/runtime/install.py | UP036 | 1 | Repair the existing UP036 diagnostic and remove this exact ignore. |
| .agents/agent/runtime/launch_contract.py | E501 | 2 | Repair the existing E501 diagnostic and remove this exact ignore. |
| .agents/agent/runtime/launch_errors.py | N818 | 1 | Repair the existing N818 diagnostic and remove this exact ignore. |
| .agents/agent/runtime/lifecycle.py | E402 | 5 | Repair the existing E402 diagnostic and remove this exact ignore. |
| .agents/agent/runtime/lifecycle.py | E501 | 13 | Repair the existing E501 diagnostic and remove this exact ignore. |
| .agents/agent/runtime/lifecycle.py | I001 | 2 | Repair the existing I001 diagnostic and remove this exact ignore. |
| .agents/agent/runtime/lifecycle.py | TID251 | 1 | Repair the existing TID251 diagnostic and remove this exact ignore. |
| .agents/agent/runtime/package_root.py | E501 | 2 | Repair the existing E501 diagnostic and remove this exact ignore. |
| .agents/agent/runtime/package_root.py | I001 | 1 | Repair the existing I001 diagnostic and remove this exact ignore. |
| .agents/agent/runtime/repository_configuration.py | E501 | 8 | Repair the existing E501 diagnostic and remove this exact ignore. |
| .agents/agent/runtime/repository_configuration.py | I001 | 1 | Repair the existing I001 diagnostic and remove this exact ignore. |
| .agents/agent/runtime/repository_configuration.py | N818 | 1 | Repair the existing N818 diagnostic and remove this exact ignore. |
| .agents/agent/runtime/required_plugins.py | E501 | 63 | Repair the existing E501 diagnostic and remove this exact ignore. |
| .agents/agent/runtime/required_plugins.py | I001 | 1 | Repair the existing I001 diagnostic and remove this exact ignore. |
| .agents/agent/runtime/required_plugins.py | RUF059 | 2 | Repair the existing RUF059 diagnostic and remove this exact ignore. |
| .agents/agent/runtime/required_plugins.py | TRY300 | 1 | Repair the existing TRY300 diagnostic and remove this exact ignore. |
| .agents/agent/runtime/runtime_delivery.py | E501 | 21 | Repair the existing E501 diagnostic and remove this exact ignore. |
| .agents/agent/runtime/runtime_delivery.py | I001 | 1 | Repair the existing I001 diagnostic and remove this exact ignore. |
| .agents/agent/runtime/runtime_delivery.py | PTH100 | 1 | Repair the existing PTH100 diagnostic and remove this exact ignore. |
| .agents/agent/runtime/runtime_delivery.py | PTH105 | 1 | Repair the existing PTH105 diagnostic and remove this exact ignore. |
| .agents/agent/runtime/runtime_delivery.py | SIM105 | 1 | Repair the existing SIM105 diagnostic and remove this exact ignore. |
| .agents/agent/runtime/session_hook.py | E402 | 3 | Repair the existing E402 diagnostic and remove this exact ignore. |
| .agents/agent/runtime/session_hook.py | E501 | 11 | Repair the existing E501 diagnostic and remove this exact ignore. |
| .agents/agent/runtime/session_hook.py | I001 | 2 | Repair the existing I001 diagnostic and remove this exact ignore. |
| .agents/agent/runtime/session_hook.py | PTH109 | 1 | Repair the existing PTH109 diagnostic and remove this exact ignore. |
| .agents/agent/runtime/session_hook.py | TID251 | 1 | Repair the existing TID251 diagnostic and remove this exact ignore. |
| .agents/agent/runtime/tj_agent/content.py | E501 | 1 | Repair the existing E501 diagnostic and remove this exact ignore. |
| .agents/agent/runtime/tj_agent/host.py | E501 | 4 | Repair the existing E501 diagnostic and remove this exact ignore. |
| .agents/agent/runtime/tj_agent/host.py | I001 | 1 | Repair the existing I001 diagnostic and remove this exact ignore. |
| .agents/agent/runtime/tj_start.py | E501 | 14 | Repair the existing E501 diagnostic and remove this exact ignore. |
| .agents/agent/runtime/tj_start.py | I001 | 2 | Repair the existing I001 diagnostic and remove this exact ignore. |
| .agents/agent/runtime/tj_start.py | TID251 | 1 | Repair the existing TID251 diagnostic and remove this exact ignore. |
| .agents/agent/runtime/tj_status.py | E501 | 7 | Repair the existing E501 diagnostic and remove this exact ignore. |
| .agents/agent/runtime/tj_status.py | I001 | 1 | Repair the existing I001 diagnostic and remove this exact ignore. |
| .agents/agent/runtime/tj_status.py | TID251 | 1 | Repair the existing TID251 diagnostic and remove this exact ignore. |
| .agents/agent/utility/handoff/scripts/handoff.py | E501 | 4 | Repair the existing E501 diagnostic and remove this exact ignore. |
| .agents/agent/utility/handoff/scripts/handoff.py | I001 | 1 | Repair the existing I001 diagnostic and remove this exact ignore. |
| .agents/agent/utility/handoff/scripts/handoff.py | TID251 | 2 | Repair the existing TID251 diagnostic and remove this exact ignore. |
| .agents/agent/utility/refresh-plugins/scripts/refresh_plugins.py | E501 | 1 | Repair the existing E501 diagnostic and remove this exact ignore. |
| .agents/agent/utility/refresh-plugins/scripts/refresh_plugins.py | I001 | 1 | Repair the existing I001 diagnostic and remove this exact ignore. |
| .agents/codex/utility/hooks/scripts/hook_control.py | ANN401 | 5 | Repair the existing ANN401 diagnostic and remove this exact ignore. |
| .agents/codex/utility/hooks/scripts/hook_control.py | B904 | 2 | Repair the existing B904 diagnostic and remove this exact ignore. |
| .agents/codex/utility/hooks/scripts/hook_control.py | E501 | 51 | Repair the existing E501 diagnostic and remove this exact ignore. |
| .agents/codex/utility/hooks/scripts/hook_control.py | E712 | 1 | Repair the existing E712 diagnostic and remove this exact ignore. |
| .agents/codex/utility/hooks/scripts/hook_control.py | I001 | 1 | Repair the existing I001 diagnostic and remove this exact ignore. |
| .agents/codex/utility/hooks/scripts/hook_control.py | PTH105 | 1 | Repair the existing PTH105 diagnostic and remove this exact ignore. |
| .agents/codex/utility/hooks/scripts/hook_control.py | PTH123 | 1 | Repair the existing PTH123 diagnostic and remove this exact ignore. |
| .agents/codex/utility/hooks/scripts/hook_control.py | RUF059 | 1 | Repair the existing RUF059 diagnostic and remove this exact ignore. |
| .agents/codex/utility/hooks/scripts/hook_control.py | TRY300 | 1 | Repair the existing TRY300 diagnostic and remove this exact ignore. |
| .agents/codex/utility/hooks/scripts/hook_control.py | UP037 | 1 | Repair the existing UP037 diagnostic and remove this exact ignore. |
| .agents/codex/utility/memory/scripts/memory.py | E501 | 4 | Repair the existing E501 diagnostic and remove this exact ignore. |
| .agents/codex/utility/memory/scripts/memory.py | I001 | 1 | Repair the existing I001 diagnostic and remove this exact ignore. |
| .claude/runtime/installed.py | ANN401 | 3 | Repair the existing ANN401 diagnostic and remove this exact ignore. |
| .claude/runtime/installed.py | E501 | 10 | Repair the existing E501 diagnostic and remove this exact ignore. |
| .claude/runtime/installed.py | I001 | 1 | Repair the existing I001 diagnostic and remove this exact ignore. |
| .claude/runtime/installed.py | UP035 | 1 | Repair the existing UP035 diagnostic and remove this exact ignore. |
| .claude/runtime/launch.py | E501 | 7 | Repair the existing E501 diagnostic and remove this exact ignore. |
| .claude/runtime/launch.py | I001 | 1 | Repair the existing I001 diagnostic and remove this exact ignore. |
| .claude/runtime/launch.py | PTH113 | 1 | Repair the existing PTH113 diagnostic and remove this exact ignore. |
| .claude/runtime/plugins.py | E501 | 11 | Repair the existing E501 diagnostic and remove this exact ignore. |
| .claude/runtime/plugins.py | I001 | 1 | Repair the existing I001 diagnostic and remove this exact ignore. |
| .claude/runtime/plugins.py | SIM102 | 1 | Repair the existing SIM102 diagnostic and remove this exact ignore. |
| .claude/runtime/plugins.py | UP033 | 1 | Repair the existing UP033 diagnostic and remove this exact ignore. |
| .claude/runtime/registry.py | E501 | 4 | Repair the existing E501 diagnostic and remove this exact ignore. |
| .claude/runtime/registry.py | I001 | 1 | Repair the existing I001 diagnostic and remove this exact ignore. |
| .claude/runtime/registry.py | PTH105 | 1 | Repair the existing PTH105 diagnostic and remove this exact ignore. |
| .claude/runtime/registry.py | PTH108 | 1 | Repair the existing PTH108 diagnostic and remove this exact ignore. |
| .claude/runtime/registry.py | PTH110 | 1 | Repair the existing PTH110 diagnostic and remove this exact ignore. |
| .claude/runtime/sessions.py | C408 | 1 | Repair the existing C408 diagnostic and remove this exact ignore. |
| .claude/runtime/sessions.py | E501 | 3 | Repair the existing E501 diagnostic and remove this exact ignore. |
| .claude/runtime/sessions.py | I001 | 1 | Repair the existing I001 diagnostic and remove this exact ignore. |
| .codex/runtime/hook_trust.py | E501 | 59 | Repair the existing E501 diagnostic and remove this exact ignore. |
| .codex/runtime/hook_trust.py | I001 | 1 | Repair the existing I001 diagnostic and remove this exact ignore. |
| .codex/runtime/hook_trust.py | SIM114 | 1 | Repair the existing SIM114 diagnostic and remove this exact ignore. |
| .codex/runtime/hook_trust.py | TRY300 | 3 | Repair the existing TRY300 diagnostic and remove this exact ignore. |
| .codex/runtime/installed.py | ANN401 | 8 | Repair the existing ANN401 diagnostic and remove this exact ignore. |
| .codex/runtime/installed.py | E501 | 19 | Repair the existing E501 diagnostic and remove this exact ignore. |
| .codex/runtime/installed.py | UP035 | 1 | Repair the existing UP035 diagnostic and remove this exact ignore. |
| .codex/runtime/launch.py | E501 | 8 | Repair the existing E501 diagnostic and remove this exact ignore. |
| .codex/runtime/launch.py | I001 | 1 | Repair the existing I001 diagnostic and remove this exact ignore. |
| .codex/runtime/launch.py | PTH207 | 1 | Repair the existing PTH207 diagnostic and remove this exact ignore. |
| .codex/runtime/permissions.py | E501 | 1 | Repair the existing E501 diagnostic and remove this exact ignore. |
| .codex/runtime/plugins.py | B905 | 1 | Repair the existing B905 diagnostic and remove this exact ignore. |
| .codex/runtime/plugins.py | E501 | 11 | Repair the existing E501 diagnostic and remove this exact ignore. |
| .codex/runtime/plugins.py | I001 | 1 | Repair the existing I001 diagnostic and remove this exact ignore. |
| .codex/runtime/plugins.py | SIM102 | 1 | Repair the existing SIM102 diagnostic and remove this exact ignore. |
| .codex/runtime/sessions.py | C408 | 1 | Repair the existing C408 diagnostic and remove this exact ignore. |
| .codex/runtime/sessions.py | E501 | 2 | Repair the existing E501 diagnostic and remove this exact ignore. |
| .codex/runtime/sessions.py | F401 | 1 | Repair the existing F401 diagnostic and remove this exact ignore. |
| .codex/runtime/sessions.py | I001 | 1 | Repair the existing I001 diagnostic and remove this exact ignore. |
| .codex/runtime/websocket.py | E501 | 5 | Repair the existing E501 diagnostic and remove this exact ignore. |
| .codex/runtime/websocket.py | RUF021 | 1 | Repair the existing RUF021 diagnostic and remove this exact ignore. |
| tests/fixtures/owned_native.py | I001 | 1 | Repair the existing I001 diagnostic and remove this exact ignore. |
| tests/fixtures/plugin_cli.py | E501 | 5 | Repair the existing E501 diagnostic and remove this exact ignore. |
| tests/fixtures/plugin_cli.py | I001 | 1 | Repair the existing I001 diagnostic and remove this exact ignore. |
| tests/fixtures/plugin_console_probe.py | ANN401 | 1 | Repair the existing ANN401 diagnostic and remove this exact ignore. |
| tests/fixtures/plugin_console_probe.py | TID251 | 1 | Repair the existing TID251 diagnostic and remove this exact ignore. |
| tests/fixtures/runtime_import_probe.py | TID251 | 1 | Repair the existing TID251 diagnostic and remove this exact ignore. |
| tests/git_isolation.py | E501 | 3 | Repair the existing E501 diagnostic and remove this exact ignore. |
| tests/test_adopt_plugins.py | E501 | 16 | Repair the existing E501 diagnostic and remove this exact ignore. |
| tests/test_adopt_plugins.py | I001 | 1 | Repair the existing I001 diagnostic and remove this exact ignore. |
| tests/test_adopt_plugins.py | PT009 | 30 | Repair the existing PT009 diagnostic and remove this exact ignore. |
| tests/test_adopt_plugins.py | TID251 | 3 | Repair the existing TID251 diagnostic and remove this exact ignore. |
| tests/test_agent_cli.py | E501 | 108 | Repair the existing E501 diagnostic and remove this exact ignore. |
| tests/test_agent_cli.py | F401 | 3 | Repair the existing F401 diagnostic and remove this exact ignore. |
| tests/test_agent_cli.py | I001 | 1 | Repair the existing I001 diagnostic and remove this exact ignore. |
| tests/test_agent_cli.py | PLR6301 | 6 | Repair the existing PLR6301 diagnostic and remove this exact ignore. |
| tests/test_agent_cli.py | PT009 | 78 | Repair the existing PT009 diagnostic and remove this exact ignore. |
| tests/test_agent_cli.py | PTH100 | 2 | Repair the existing PTH100 diagnostic and remove this exact ignore. |
| tests/test_agent_cli.py | PTH109 | 1 | Repair the existing PTH109 diagnostic and remove this exact ignore. |
| tests/test_agent_cli.py | PTH118 | 1 | Repair the existing PTH118 diagnostic and remove this exact ignore. |
| tests/test_agent_cli.py | RUF059 | 1 | Repair the existing RUF059 diagnostic and remove this exact ignore. |
| tests/test_agent_cli.py | TID251 | 18 | Repair the existing TID251 diagnostic and remove this exact ignore. |
| tests/test_claude_worktree_installations.py | E501 | 17 | Repair the existing E501 diagnostic and remove this exact ignore. |
| tests/test_claude_worktree_installations.py | I001 | 2 | Repair the existing I001 diagnostic and remove this exact ignore. |
| tests/test_claude_worktree_installations.py | N812 | 1 | Repair the existing N812 diagnostic and remove this exact ignore. |
| tests/test_claude_worktree_installations.py | PLR6301 | 1 | Repair the existing PLR6301 diagnostic and remove this exact ignore. |
| tests/test_claude_worktree_installations.py | PT009 | 4 | Repair the existing PT009 diagnostic and remove this exact ignore. |
| tests/test_claude_worktree_installations.py | TID251 | 2 | Repair the existing TID251 diagnostic and remove this exact ignore. |
| tests/test_codex_config_writers.py | E501 | 2 | Repair the existing E501 diagnostic and remove this exact ignore. |
| tests/test_codex_config_writers.py | I001 | 2 | Repair the existing I001 diagnostic and remove this exact ignore. |
| tests/test_codex_config_writers.py | PT009 | 4 | Repair the existing PT009 diagnostic and remove this exact ignore. |
| tests/test_codex_config_writers.py | TID251 | 2 | Repair the existing TID251 diagnostic and remove this exact ignore. |
| tests/test_codex_config_writers.py | UP036 | 1 | Repair the existing UP036 diagnostic and remove this exact ignore. |
| tests/test_credential_deny_rules.py | E501 | 3 | Repair the existing E501 diagnostic and remove this exact ignore. |
| tests/test_credential_deny_rules.py | I001 | 1 | Repair the existing I001 diagnostic and remove this exact ignore. |
| tests/test_credential_deny_rules.py | PT009 | 7 | Repair the existing PT009 diagnostic and remove this exact ignore. |
| tests/test_credential_deny_rules.py | TID251 | 2 | Repair the existing TID251 diagnostic and remove this exact ignore. |
| tests/test_credential_deny_rules.py | UP036 | 1 | Repair the existing UP036 diagnostic and remove this exact ignore. |
| tests/test_handoff_launchers.py | E501 | 21 | Repair the existing E501 diagnostic and remove this exact ignore. |
| tests/test_handoff_launchers.py | I001 | 1 | Repair the existing I001 diagnostic and remove this exact ignore. |
| tests/test_handoff_launchers.py | PT009 | 26 | Repair the existing PT009 diagnostic and remove this exact ignore. |
| tests/test_handoff_launchers.py | RUF059 | 1 | Repair the existing RUF059 diagnostic and remove this exact ignore. |
| tests/test_handoff_launchers.py | TID251 | 2 | Repair the existing TID251 diagnostic and remove this exact ignore. |
| tests/test_harness_permissions_sync.py | E501 | 40 | Repair the existing E501 diagnostic and remove this exact ignore. |
| tests/test_harness_permissions_sync.py | I001 | 1 | Repair the existing I001 diagnostic and remove this exact ignore. |
| tests/test_harness_permissions_sync.py | PT009 | 39 | Repair the existing PT009 diagnostic and remove this exact ignore. |
| tests/test_harness_permissions_sync.py | SIM117 | 4 | Repair the existing SIM117 diagnostic and remove this exact ignore. |
| tests/test_harness_permissions_sync.py | TID251 | 3 | Repair the existing TID251 diagnostic and remove this exact ignore. |
| tests/test_history.py | C408 | 49 | Repair the existing C408 diagnostic and remove this exact ignore. |
| tests/test_history.py | E501 | 50 | Repair the existing E501 diagnostic and remove this exact ignore. |
| tests/test_history.py | I001 | 1 | Repair the existing I001 diagnostic and remove this exact ignore. |
| tests/test_history.py | PT009 | 37 | Repair the existing PT009 diagnostic and remove this exact ignore. |
| tests/test_history.py | PTH201 | 1 | Repair the existing PTH201 diagnostic and remove this exact ignore. |
| tests/test_history.py | TID251 | 2 | Repair the existing TID251 diagnostic and remove this exact ignore. |
| tests/test_hook_control.py | BLE001 | 2 | Repair the existing BLE001 diagnostic and remove this exact ignore. |
| tests/test_hook_control.py | E501 | 16 | Repair the existing E501 diagnostic and remove this exact ignore. |
| tests/test_hook_control.py | I001 | 1 | Repair the existing I001 diagnostic and remove this exact ignore. |
| tests/test_hook_control.py | PT009 | 64 | Repair the existing PT009 diagnostic and remove this exact ignore. |
| tests/test_hook_control.py | SIM117 | 2 | Repair the existing SIM117 diagnostic and remove this exact ignore. |
| tests/test_hook_control.py | TID251 | 2 | Repair the existing TID251 diagnostic and remove this exact ignore. |
| tests/test_hook_trust.py | E402 | 1 | Repair the existing E402 diagnostic and remove this exact ignore. |
| tests/test_hook_trust.py | E501 | 69 | Repair the existing E501 diagnostic and remove this exact ignore. |
| tests/test_hook_trust.py | I001 | 1 | Repair the existing I001 diagnostic and remove this exact ignore. |
| tests/test_hook_trust.py | N812 | 1 | Repair the existing N812 diagnostic and remove this exact ignore. |
| tests/test_hook_trust.py | PT009 | 28 | Repair the existing PT009 diagnostic and remove this exact ignore. |
| tests/test_hook_trust.py | RET503 | 1 | Repair the existing RET503 diagnostic and remove this exact ignore. |
| tests/test_hook_trust.py | SIM117 | 1 | Repair the existing SIM117 diagnostic and remove this exact ignore. |
| tests/test_hook_trust.py | TID251 | 2 | Repair the existing TID251 diagnostic and remove this exact ignore. |
| tests/test_host_launch.py | TID251 | 1 | Repair the existing TID251 diagnostic and remove this exact ignore. |
| tests/test_install.py | E501 | 124 | Repair the existing E501 diagnostic and remove this exact ignore. |
| tests/test_install.py | I001 | 2 | Repair the existing I001 diagnostic and remove this exact ignore. |
| tests/test_install.py | PLR6301 | 3 | Repair the existing PLR6301 diagnostic and remove this exact ignore. |
| tests/test_install.py | PT009 | 129 | Repair the existing PT009 diagnostic and remove this exact ignore. |
| tests/test_install.py | SIM117 | 4 | Repair the existing SIM117 diagnostic and remove this exact ignore. |
| tests/test_install.py | TID251 | 2 | Repair the existing TID251 diagnostic and remove this exact ignore. |
| tests/test_install.py | UP036 | 1 | Repair the existing UP036 diagnostic and remove this exact ignore. |
| tests/test_lifecycle_commands.py | E501 | 15 | Repair the existing E501 diagnostic and remove this exact ignore. |
| tests/test_lifecycle_commands.py | I001 | 1 | Repair the existing I001 diagnostic and remove this exact ignore. |
| tests/test_lifecycle_commands.py | PLR6301 | 1 | Repair the existing PLR6301 diagnostic and remove this exact ignore. |
| tests/test_lifecycle_commands.py | PT009 | 8 | Repair the existing PT009 diagnostic and remove this exact ignore. |
| tests/test_lifecycle_commands.py | TID251 | 2 | Repair the existing TID251 diagnostic and remove this exact ignore. |
| tests/test_loader_roots.py | F401 | 1 | Repair the existing F401 diagnostic and remove this exact ignore. |
| tests/test_loader_roots.py | TID251 | 1 | Repair the existing TID251 diagnostic and remove this exact ignore. |
| tests/test_package_root.py | E501 | 3 | Repair the existing E501 diagnostic and remove this exact ignore. |
| tests/test_package_root.py | I001 | 1 | Repair the existing I001 diagnostic and remove this exact ignore. |
| tests/test_package_root.py | PT009 | 3 | Repair the existing PT009 diagnostic and remove this exact ignore. |
| tests/test_package_root.py | TID251 | 2 | Repair the existing TID251 diagnostic and remove this exact ignore. |
| tests/test_plugin_modules.py | E501 | 1 | Repair the existing E501 diagnostic and remove this exact ignore. |
| tests/test_plugin_modules.py | I001 | 1 | Repair the existing I001 diagnostic and remove this exact ignore. |
| tests/test_plugin_modules.py | PT009 | 1 | Repair the existing PT009 diagnostic and remove this exact ignore. |
| tests/test_plugin_modules.py | TID251 | 1 | Repair the existing TID251 diagnostic and remove this exact ignore. |
| tests/test_plugin_scripts_entry.py | E501 | 3 | Repair the existing E501 diagnostic and remove this exact ignore. |
| tests/test_plugin_scripts_entry.py | PT009 | 1 | Repair the existing PT009 diagnostic and remove this exact ignore. |
| tests/test_plugin_scripts_entry.py | TID251 | 1 | Repair the existing TID251 diagnostic and remove this exact ignore. |
| tests/test_plugin_setup.py | E402 | 1 | Repair the existing E402 diagnostic and remove this exact ignore. |
| tests/test_plugin_setup.py | E501 | 38 | Repair the existing E501 diagnostic and remove this exact ignore. |
| tests/test_plugin_setup.py | I001 | 2 | Repair the existing I001 diagnostic and remove this exact ignore. |
| tests/test_plugin_setup.py | PT009 | 20 | Repair the existing PT009 diagnostic and remove this exact ignore. |
| tests/test_plugin_setup.py | TID251 | 2 | Repair the existing TID251 diagnostic and remove this exact ignore. |
| tests/test_refresh_plugins.py | ANN401 | 3 | Repair the existing ANN401 diagnostic and remove this exact ignore. |
| tests/test_refresh_plugins.py | E501 | 9 | Repair the existing E501 diagnostic and remove this exact ignore. |
| tests/test_refresh_plugins.py | I001 | 1 | Repair the existing I001 diagnostic and remove this exact ignore. |
| tests/test_refresh_plugins.py | PT009 | 19 | Repair the existing PT009 diagnostic and remove this exact ignore. |
| tests/test_refresh_plugins.py | TID251 | 3 | Repair the existing TID251 diagnostic and remove this exact ignore. |
| tests/test_required_plugins.py | E501 | 17 | Repair the existing E501 diagnostic and remove this exact ignore. |
| tests/test_required_plugins.py | I001 | 2 | Repair the existing I001 diagnostic and remove this exact ignore. |
| tests/test_required_plugins.py | N812 | 1 | Repair the existing N812 diagnostic and remove this exact ignore. |
| tests/test_required_plugins.py | PT009 | 10 | Repair the existing PT009 diagnostic and remove this exact ignore. |
| tests/test_required_plugins.py | PT011 | 1 | Repair the existing PT011 diagnostic and remove this exact ignore. |
| tests/test_required_plugins.py | SIM117 | 1 | Repair the existing SIM117 diagnostic and remove this exact ignore. |
| tests/test_required_plugins.py | TID251 | 2 | Repair the existing TID251 diagnostic and remove this exact ignore. |
| tests/test_runtime_delivery.py | E402 | 1 | Repair the existing E402 diagnostic and remove this exact ignore. |
| tests/test_runtime_delivery.py | E501 | 10 | Repair the existing E501 diagnostic and remove this exact ignore. |
| tests/test_runtime_delivery.py | I001 | 2 | Repair the existing I001 diagnostic and remove this exact ignore. |
| tests/test_runtime_delivery.py | PT009 | 22 | Repair the existing PT009 diagnostic and remove this exact ignore. |
| tests/test_runtime_delivery.py | PTH211 | 2 | Repair the existing PTH211 diagnostic and remove this exact ignore. |
| tests/test_runtime_delivery.py | TID251 | 3 | Repair the existing TID251 diagnostic and remove this exact ignore. |
| tests/test_selection_convergence.py | E402 | 3 | Repair the existing E402 diagnostic and remove this exact ignore. |
| tests/test_selection_convergence.py | E501 | 12 | Repair the existing E501 diagnostic and remove this exact ignore. |
| tests/test_selection_convergence.py | I001 | 1 | Repair the existing I001 diagnostic and remove this exact ignore. |
| tests/test_selection_convergence.py | PT009 | 12 | Repair the existing PT009 diagnostic and remove this exact ignore. |
| tests/test_selection_convergence.py | RET501 | 1 | Repair the existing RET501 diagnostic and remove this exact ignore. |
| tests/test_selection_convergence.py | TID251 | 2 | Repair the existing TID251 diagnostic and remove this exact ignore. |
| tests/test_session_hook_events.py | E501 | 20 | Repair the existing E501 diagnostic and remove this exact ignore. |
| tests/test_session_hook_events.py | I001 | 1 | Repair the existing I001 diagnostic and remove this exact ignore. |
| tests/test_session_hook_events.py | PLR6301 | 2 | Repair the existing PLR6301 diagnostic and remove this exact ignore. |
| tests/test_session_hook_events.py | PT009 | 7 | Repair the existing PT009 diagnostic and remove this exact ignore. |
| tests/test_session_hook_events.py | TID251 | 3 | Repair the existing TID251 diagnostic and remove this exact ignore. |
| tests/test_session_hooks.py | E501 | 4 | Repair the existing E501 diagnostic and remove this exact ignore. |
| tests/test_session_hooks.py | I001 | 1 | Repair the existing I001 diagnostic and remove this exact ignore. |
| tests/test_session_hooks.py | PT009 | 10 | Repair the existing PT009 diagnostic and remove this exact ignore. |
| tests/test_session_hooks.py | SIM300 | 1 | Repair the existing SIM300 diagnostic and remove this exact ignore. |
| tests/test_session_hooks.py | TID251 | 1 | Repair the existing TID251 diagnostic and remove this exact ignore. |
| tests/test_verify_machine.py | E501 | 28 | Repair the existing E501 diagnostic and remove this exact ignore. |
| tests/test_verify_machine.py | I001 | 1 | Repair the existing I001 diagnostic and remove this exact ignore. |
| tests/test_verify_machine.py | PT009 | 30 | Repair the existing PT009 diagnostic and remove this exact ignore. |
| tests/test_verify_machine.py | TID251 | 2 | Repair the existing TID251 diagnostic and remove this exact ignore. |
| tests/test_websocket.py | E501 | 9 | Repair the existing E501 diagnostic and remove this exact ignore. |
| tests/test_websocket.py | I001 | 1 | Repair the existing I001 diagnostic and remove this exact ignore. |
| tests/test_websocket.py | N812 | 1 | Repair the existing N812 diagnostic and remove this exact ignore. |
| tests/test_websocket.py | PT009 | 7 | Repair the existing PT009 diagnostic and remove this exact ignore. |
| tests/test_websocket.py | TID251 | 2 | Repair the existing TID251 diagnostic and remove this exact ignore. |
| tests/test_work_item.py | TID251 | 1 | Repair the existing TID251 diagnostic and remove this exact ignore. |

### Ruff format

| File | Existing count | Required repair |
| --- | ---: | --- |
| .agents/agent/resources/handoff/launch_claude.py | 1 | Format this existing file and remove this exact exclusion. |
| .agents/agent/resources/handoff/launch_codex.py | 1 | Format this existing file and remove this exact exclusion. |
| .agents/agent/resources/lanes/resolve.py | 1 | Format this existing file and remove this exact exclusion. |
| .agents/agent/runtime/agent_cli.py | 1 | Format this existing file and remove this exact exclusion. |
| .agents/agent/runtime/agent_store.py | 1 | Format this existing file and remove this exact exclusion. |
| .agents/agent/runtime/history.py | 1 | Format this existing file and remove this exact exclusion. |
| .agents/agent/runtime/host_permissions.py | 1 | Format this existing file and remove this exact exclusion. |
| .agents/agent/runtime/host_plugins.py | 1 | Format this existing file and remove this exact exclusion. |
| .agents/agent/runtime/host_sessions.py | 1 | Format this existing file and remove this exact exclusion. |
| .agents/agent/runtime/install.py | 1 | Format this existing file and remove this exact exclusion. |
| .agents/agent/runtime/launch_contract.py | 1 | Format this existing file and remove this exact exclusion. |
| .agents/agent/runtime/lifecycle.py | 1 | Format this existing file and remove this exact exclusion. |
| .agents/agent/runtime/package_root.py | 1 | Format this existing file and remove this exact exclusion. |
| .agents/agent/runtime/repository_configuration.py | 1 | Format this existing file and remove this exact exclusion. |
| .agents/agent/runtime/required_plugins.py | 1 | Format this existing file and remove this exact exclusion. |
| .agents/agent/runtime/runtime_delivery.py | 1 | Format this existing file and remove this exact exclusion. |
| .agents/agent/runtime/session_hook.py | 1 | Format this existing file and remove this exact exclusion. |
| .agents/agent/runtime/tj_agent/content.py | 1 | Format this existing file and remove this exact exclusion. |
| .agents/agent/runtime/tj_agent/host.py | 1 | Format this existing file and remove this exact exclusion. |
| .agents/agent/runtime/tj_start.py | 1 | Format this existing file and remove this exact exclusion. |
| .agents/agent/runtime/tj_status.py | 1 | Format this existing file and remove this exact exclusion. |
| .agents/agent/runtime/transcripts.py | 1 | Format this existing file and remove this exact exclusion. |
| .agents/agent/utility/handoff/scripts/handoff.py | 1 | Format this existing file and remove this exact exclusion. |
| .agents/agent/utility/refresh-plugins/scripts/refresh_plugins.py | 1 | Format this existing file and remove this exact exclusion. |
| .agents/codex/utility/hooks/scripts/hook_control.py | 1 | Format this existing file and remove this exact exclusion. |
| .agents/codex/utility/memory/scripts/memory.py | 1 | Format this existing file and remove this exact exclusion. |
| .claude/runtime/installed.py | 1 | Format this existing file and remove this exact exclusion. |
| .claude/runtime/launch.py | 1 | Format this existing file and remove this exact exclusion. |
| .claude/runtime/permissions.py | 1 | Format this existing file and remove this exact exclusion. |
| .claude/runtime/plugins.py | 1 | Format this existing file and remove this exact exclusion. |
| .claude/runtime/registry.py | 1 | Format this existing file and remove this exact exclusion. |
| .claude/runtime/sessions.py | 1 | Format this existing file and remove this exact exclusion. |
| .codex/runtime/hook_trust.py | 1 | Format this existing file and remove this exact exclusion. |
| .codex/runtime/installed.py | 1 | Format this existing file and remove this exact exclusion. |
| .codex/runtime/launch.py | 1 | Format this existing file and remove this exact exclusion. |
| .codex/runtime/plugins.py | 1 | Format this existing file and remove this exact exclusion. |
| .codex/runtime/sessions.py | 1 | Format this existing file and remove this exact exclusion. |
| .codex/runtime/websocket.py | 1 | Format this existing file and remove this exact exclusion. |
| tests/fixtures/plugin_cli.py | 1 | Format this existing file and remove this exact exclusion. |
| tests/git_isolation.py | 1 | Format this existing file and remove this exact exclusion. |
| tests/test_adopt_plugins.py | 1 | Format this existing file and remove this exact exclusion. |
| tests/test_agent_cli.py | 1 | Format this existing file and remove this exact exclusion. |
| tests/test_claude_worktree_installations.py | 1 | Format this existing file and remove this exact exclusion. |
| tests/test_codex_config_writers.py | 1 | Format this existing file and remove this exact exclusion. |
| tests/test_credential_deny_rules.py | 1 | Format this existing file and remove this exact exclusion. |
| tests/test_handoff_launchers.py | 1 | Format this existing file and remove this exact exclusion. |
| tests/test_harness_permissions_sync.py | 1 | Format this existing file and remove this exact exclusion. |
| tests/test_history.py | 1 | Format this existing file and remove this exact exclusion. |
| tests/test_hook_control.py | 1 | Format this existing file and remove this exact exclusion. |
| tests/test_hook_trust.py | 1 | Format this existing file and remove this exact exclusion. |
| tests/test_install.py | 1 | Format this existing file and remove this exact exclusion. |
| tests/test_lifecycle_commands.py | 1 | Format this existing file and remove this exact exclusion. |
| tests/test_package_root.py | 1 | Format this existing file and remove this exact exclusion. |
| tests/test_plugin_modules.py | 1 | Format this existing file and remove this exact exclusion. |
| tests/test_plugin_scripts_entry.py | 1 | Format this existing file and remove this exact exclusion. |
| tests/test_plugin_setup.py | 1 | Format this existing file and remove this exact exclusion. |
| tests/test_refresh_plugins.py | 1 | Format this existing file and remove this exact exclusion. |
| tests/test_required_plugins.py | 1 | Format this existing file and remove this exact exclusion. |
| tests/test_runtime_delivery.py | 1 | Format this existing file and remove this exact exclusion. |
| tests/test_selection_convergence.py | 1 | Format this existing file and remove this exact exclusion. |
| tests/test_session_hook_events.py | 1 | Format this existing file and remove this exact exclusion. |
| tests/test_session_hooks.py | 1 | Format this existing file and remove this exact exclusion. |
| tests/test_verify_machine.py | 1 | Format this existing file and remove this exact exclusion. |
| tests/test_websocket.py | 1 | Format this existing file and remove this exact exclusion. |

### Mypy

| Module | Rule | Existing count | Required repair |
| --- | --- | ---: | --- |
| adopt_plugins | assignment | 2 | Repair the existing assignment diagnostic and remove this exact override. |
| adopt_plugins | type-arg | 8 | Repair the existing type-arg diagnostic and remove this exact override. |
| adopt_plugins | var-annotated | 3 | Repair the existing var-annotated diagnostic and remove this exact override. |
| agent_cli | arg-type | 2 | Repair the existing arg-type diagnostic and remove this exact override. |
| agent_cli | no-untyped-call | 10 | Repair the existing no-untyped-call diagnostic and remove this exact override. |
| agent_cli | no-untyped-def | 20 | Repair the existing no-untyped-def diagnostic and remove this exact override. |
| agent_cli | union-attr | 2 | Repair the existing union-attr diagnostic and remove this exact override. |
| agent_store | no-any-return | 3 | Repair the existing no-any-return diagnostic and remove this exact override. |
| agent_store | no-untyped-def | 1 | Repair the existing no-untyped-def diagnostic and remove this exact override. |
| agent_store | type-arg | 1 | Repair the existing type-arg diagnostic and remove this exact override. |
| fake_machine | attr-defined | 2 | Repair the existing attr-defined diagnostic and remove this exact override. |
| git_isolation | no-untyped-call | 2 | Repair the existing no-untyped-call diagnostic and remove this exact override. |
| git_isolation | no-untyped-def | 3 | Repair the existing no-untyped-def diagnostic and remove this exact override. |
| handoff | arg-type | 1 | Repair the existing arg-type diagnostic and remove this exact override. |
| handoff | no-untyped-call | 6 | Repair the existing no-untyped-call diagnostic and remove this exact override. |
| handoff | no-untyped-def | 5 | Repair the existing no-untyped-def diagnostic and remove this exact override. |
| handoff | union-attr | 1 | Repair the existing union-attr diagnostic and remove this exact override. |
| harness_permissions | type-arg | 7 | Repair the existing type-arg diagnostic and remove this exact override. |
| harness_permissions | union-attr | 1 | Repair the existing union-attr diagnostic and remove this exact override. |
| harness_permissions_sync | arg-type | 1 | Repair the existing arg-type diagnostic and remove this exact override. |
| harness_permissions_sync | attr-defined | 1 | Repair the existing attr-defined diagnostic and remove this exact override. |
| harness_permissions_sync | index | 1 | Repair the existing index diagnostic and remove this exact override. |
| harness_permissions_sync | return-value | 1 | Repair the existing return-value diagnostic and remove this exact override. |
| harness_permissions_sync | type-arg | 18 | Repair the existing type-arg diagnostic and remove this exact override. |
| history | arg-type | 1 | Repair the existing arg-type diagnostic and remove this exact override. |
| history | no-untyped-call | 12 | Repair the existing no-untyped-call diagnostic and remove this exact override. |
| history | no-untyped-def | 10 | Repair the existing no-untyped-def diagnostic and remove this exact override. |
| hook_control | attr-defined | 4 | Repair the existing attr-defined diagnostic and remove this exact override. |
| hook_control | no-any-return | 2 | Repair the existing no-any-return diagnostic and remove this exact override. |
| host_permissions | arg-type | 1 | Repair the existing arg-type diagnostic and remove this exact override. |
| host_permissions | no-any-return | 1 | Repair the existing no-any-return diagnostic and remove this exact override. |
| host_permissions | no-untyped-def | 1 | Repair the existing no-untyped-def diagnostic and remove this exact override. |
| host_permissions | union-attr | 1 | Repair the existing union-attr diagnostic and remove this exact override. |
| host_permissions | unreachable | 1 | Repair the existing unreachable diagnostic and remove this exact override. |
| host_plugins | type-arg | 2 | Repair the existing type-arg diagnostic and remove this exact override. |
| host_sessions | type-arg | 2 | Repair the existing type-arg diagnostic and remove this exact override. |
| install | arg-type | 2 | Repair the existing arg-type diagnostic and remove this exact override. |
| install | attr-defined | 15 | Repair the existing attr-defined diagnostic and remove this exact override. |
| install | no-any-return | 4 | Repair the existing no-any-return diagnostic and remove this exact override. |
| install | no-untyped-def | 1 | Repair the existing no-untyped-def diagnostic and remove this exact override. |
| install | type-arg | 14 | Repair the existing type-arg diagnostic and remove this exact override. |
| install | union-attr | 2 | Repair the existing union-attr diagnostic and remove this exact override. |
| install | var-annotated | 2 | Repair the existing var-annotated diagnostic and remove this exact override. |
| latest_plugins | arg-type | 1 | Repair the existing arg-type diagnostic and remove this exact override. |
| launch_claude | arg-type | 1 | Repair the existing arg-type diagnostic and remove this exact override. |
| launch_claude | no-untyped-call | 7 | Repair the existing no-untyped-call diagnostic and remove this exact override. |
| launch_claude | no-untyped-def | 6 | Repair the existing no-untyped-def diagnostic and remove this exact override. |
| launch_claude | union-attr | 1 | Repair the existing union-attr diagnostic and remove this exact override. |
| launch_codex | arg-type | 1 | Repair the existing arg-type diagnostic and remove this exact override. |
| launch_codex | no-untyped-call | 5 | Repair the existing no-untyped-call diagnostic and remove this exact override. |
| launch_codex | no-untyped-def | 5 | Repair the existing no-untyped-def diagnostic and remove this exact override. |
| launch_codex | union-attr | 1 | Repair the existing union-attr diagnostic and remove this exact override. |
| lifecycle | call-arg | 2 | Repair the existing call-arg diagnostic and remove this exact override. |
| lifecycle | no-any-return | 2 | Repair the existing no-any-return diagnostic and remove this exact override. |
| lifecycle | no-untyped-def | 1 | Repair the existing no-untyped-def diagnostic and remove this exact override. |
| lifecycle | type-arg | 4 | Repair the existing type-arg diagnostic and remove this exact override. |
| lifecycle | union-attr | 1 | Repair the existing union-attr diagnostic and remove this exact override. |
| lock_io | no-any-return | 1 | Repair the existing no-any-return diagnostic and remove this exact override. |
| lock_io | no-untyped-def | 1 | Repair the existing no-untyped-def diagnostic and remove this exact override. |
| memory | type-arg | 1 | Repair the existing type-arg diagnostic and remove this exact override. |
| plugin_selection | arg-type | 1 | Repair the existing arg-type diagnostic and remove this exact override. |
| plugin_selection | index | 1 | Repair the existing index diagnostic and remove this exact override. |
| plugin_selection | no-untyped-call | 1 | Repair the existing no-untyped-call diagnostic and remove this exact override. |
| plugin_selection | no-untyped-def | 1 | Repair the existing no-untyped-def diagnostic and remove this exact override. |
| plugin_selection | return-value | 1 | Repair the existing return-value diagnostic and remove this exact override. |
| plugin_selection | type-arg | 4 | Repair the existing type-arg diagnostic and remove this exact override. |
| plugin_selection | union-attr | 2 | Repair the existing union-attr diagnostic and remove this exact override. |
| plugins.refresh | arg-type | 1 | Repair the existing arg-type diagnostic and remove this exact override. |
| refresh_plugins | no-untyped-call | 1 | Repair the existing no-untyped-call diagnostic and remove this exact override. |
| refresh_plugins | no-untyped-def | 1 | Repair the existing no-untyped-def diagnostic and remove this exact override. |
| repo_config | assignment | 4 | Repair the existing assignment diagnostic and remove this exact override. |
| repo_config | no-untyped-call | 1 | Repair the existing no-untyped-call diagnostic and remove this exact override. |
| repo_config | no-untyped-def | 5 | Repair the existing no-untyped-def diagnostic and remove this exact override. |
| repo_config | type-arg | 10 | Repair the existing type-arg diagnostic and remove this exact override. |
| repo_config | var-annotated | 1 | Repair the existing var-annotated diagnostic and remove this exact override. |
| repository_configuration | no-untyped-call | 7 | Repair the existing no-untyped-call diagnostic and remove this exact override. |
| repository_configuration | no-untyped-def | 10 | Repair the existing no-untyped-def diagnostic and remove this exact override. |
| required_plugins | arg-type | 1 | Repair the existing arg-type diagnostic and remove this exact override. |
| required_plugins | no-untyped-call | 1 | Repair the existing no-untyped-call diagnostic and remove this exact override. |
| required_plugins | no-untyped-def | 4 | Repair the existing no-untyped-def diagnostic and remove this exact override. |
| required_plugins | type-arg | 6 | Repair the existing type-arg diagnostic and remove this exact override. |
| required_plugins | var-annotated | 1 | Repair the existing var-annotated diagnostic and remove this exact override. |
| resolve | arg-type | 1 | Repair the existing arg-type diagnostic and remove this exact override. |
| resolve | no-untyped-call | 6 | Repair the existing no-untyped-call diagnostic and remove this exact override. |
| resolve | no-untyped-def | 5 | Repair the existing no-untyped-def diagnostic and remove this exact override. |
| runtime.hook_trust | no-untyped-call | 46 | Repair the existing no-untyped-call diagnostic and remove this exact override. |
| runtime.hook_trust | no-untyped-def | 27 | Repair the existing no-untyped-def diagnostic and remove this exact override. |
| runtime.hook_trust | union-attr | 6 | Repair the existing union-attr diagnostic and remove this exact override. |
| runtime.hook_trust | var-annotated | 2 | Repair the existing var-annotated diagnostic and remove this exact override. |
| runtime.installed | no-any-return | 1 | Repair the existing no-any-return diagnostic and remove this exact override. |
| runtime.installed | type-arg | 1 | Repair the existing type-arg diagnostic and remove this exact override. |
| runtime.installed | var-annotated | 1 | Repair the existing var-annotated diagnostic and remove this exact override. |
| runtime.launch | no-any-return | 1 | Repair the existing no-any-return diagnostic and remove this exact override. |
| runtime.launch | no-untyped-call | 8 | Repair the existing no-untyped-call diagnostic and remove this exact override. |
| runtime.launch | no-untyped-def | 16 | Repair the existing no-untyped-def diagnostic and remove this exact override. |
| runtime.permissions | type-arg | 1 | Repair the existing type-arg diagnostic and remove this exact override. |
| runtime.plugins | no-any-return | 2 | Repair the existing no-any-return diagnostic and remove this exact override. |
| runtime.plugins | operator | 1 | Repair the existing operator diagnostic and remove this exact override. |
| runtime.plugins | return-value | 1 | Repair the existing return-value diagnostic and remove this exact override. |
| runtime.plugins | type-arg | 4 | Repair the existing type-arg diagnostic and remove this exact override. |
| runtime.registry | type-arg | 1 | Repair the existing type-arg diagnostic and remove this exact override. |
| runtime.sessions | assignment | 2 | Repair the existing assignment diagnostic and remove this exact override. |
| runtime.sessions | no-untyped-call | 8 | Repair the existing no-untyped-call diagnostic and remove this exact override. |
| runtime.sessions | no-untyped-def | 2 | Repair the existing no-untyped-def diagnostic and remove this exact override. |
| runtime.sessions | type-arg | 5 | Repair the existing type-arg diagnostic and remove this exact override. |
| runtime.sessions | union-attr | 4 | Repair the existing union-attr diagnostic and remove this exact override. |
| runtime.sessions | var-annotated | 2 | Repair the existing var-annotated diagnostic and remove this exact override. |
| runtime.websocket | no-untyped-call | 5 | Repair the existing no-untyped-call diagnostic and remove this exact override. |
| runtime.websocket | no-untyped-def | 5 | Repair the existing no-untyped-def diagnostic and remove this exact override. |
| runtime_delivery | arg-type | 1 | Repair the existing arg-type diagnostic and remove this exact override. |
| runtime_delivery | attr-defined | 2 | Repair the existing attr-defined diagnostic and remove this exact override. |
| runtime_delivery | no-untyped-def | 3 | Repair the existing no-untyped-def diagnostic and remove this exact override. |
| runtime_delivery | type-arg | 9 | Repair the existing type-arg diagnostic and remove this exact override. |
| session_hook | arg-type | 1 | Repair the existing arg-type diagnostic and remove this exact override. |
| session_hook | no-any-return | 1 | Repair the existing no-any-return diagnostic and remove this exact override. |
| session_hook | no-untyped-def | 2 | Repair the existing no-untyped-def diagnostic and remove this exact override. |
| session_hook | type-arg | 3 | Repair the existing type-arg diagnostic and remove this exact override. |
| test_adopt_plugins | arg-type | 1 | Repair the existing arg-type diagnostic and remove this exact override. |
| test_adopt_plugins | assignment | 1 | Repair the existing assignment diagnostic and remove this exact override. |
| test_adopt_plugins | no-untyped-call | 17 | Repair the existing no-untyped-call diagnostic and remove this exact override. |
| test_adopt_plugins | no-untyped-def | 16 | Repair the existing no-untyped-def diagnostic and remove this exact override. |
| test_adopt_plugins | union-attr | 1 | Repair the existing union-attr diagnostic and remove this exact override. |
| test_agent_cli | attr-defined | 3 | Repair the existing attr-defined diagnostic and remove this exact override. |
| test_agent_cli | func-returns-value | 1 | Repair the existing func-returns-value diagnostic and remove this exact override. |
| test_agent_cli | no-untyped-call | 53 | Repair the existing no-untyped-call diagnostic and remove this exact override. |
| test_agent_cli | no-untyped-def | 91 | Repair the existing no-untyped-def diagnostic and remove this exact override. |
| test_agent_cli | var-annotated | 1 | Repair the existing var-annotated diagnostic and remove this exact override. |
| test_claude_worktree_installations | attr-defined | 26 | Repair the existing attr-defined diagnostic and remove this exact override. |
| test_claude_worktree_installations | no-untyped-call | 6 | Repair the existing no-untyped-call diagnostic and remove this exact override. |
| test_claude_worktree_installations | no-untyped-def | 7 | Repair the existing no-untyped-def diagnostic and remove this exact override. |
| test_codex_config_writers | no-untyped-call | 1 | Repair the existing no-untyped-call diagnostic and remove this exact override. |
| test_codex_config_writers | no-untyped-def | 4 | Repair the existing no-untyped-def diagnostic and remove this exact override. |
| test_credential_deny_rules | no-untyped-def | 3 | Repair the existing no-untyped-def diagnostic and remove this exact override. |
| test_handoff_launchers | arg-type | 1 | Repair the existing arg-type diagnostic and remove this exact override. |
| test_handoff_launchers | no-untyped-call | 14 | Repair the existing no-untyped-call diagnostic and remove this exact override. |
| test_handoff_launchers | no-untyped-def | 16 | Repair the existing no-untyped-def diagnostic and remove this exact override. |
| test_handoff_launchers | union-attr | 1 | Repair the existing union-attr diagnostic and remove this exact override. |
| test_harness_permissions_sync | arg-type | 1 | Repair the existing arg-type diagnostic and remove this exact override. |
| test_harness_permissions_sync | no-untyped-call | 71 | Repair the existing no-untyped-call diagnostic and remove this exact override. |
| test_harness_permissions_sync | no-untyped-def | 33 | Repair the existing no-untyped-def diagnostic and remove this exact override. |
| test_harness_permissions_sync | type-arg | 1 | Repair the existing type-arg diagnostic and remove this exact override. |
| test_harness_permissions_sync | union-attr | 1 | Repair the existing union-attr diagnostic and remove this exact override. |
| test_history | arg-type | 1 | Repair the existing arg-type diagnostic and remove this exact override. |
| test_history | no-untyped-call | 21 | Repair the existing no-untyped-call diagnostic and remove this exact override. |
| test_history | no-untyped-def | 21 | Repair the existing no-untyped-def diagnostic and remove this exact override. |
| test_history | union-attr | 1 | Repair the existing union-attr diagnostic and remove this exact override. |
| test_hook_control | arg-type | 1 | Repair the existing arg-type diagnostic and remove this exact override. |
| test_hook_control | attr-defined | 1 | Repair the existing attr-defined diagnostic and remove this exact override. |
| test_hook_control | no-untyped-call | 24 | Repair the existing no-untyped-call diagnostic and remove this exact override. |
| test_hook_control | no-untyped-def | 34 | Repair the existing no-untyped-def diagnostic and remove this exact override. |
| test_hook_control | union-attr | 2 | Repair the existing union-attr diagnostic and remove this exact override. |
| test_hook_trust | dict-item | 5 | Repair the existing dict-item diagnostic and remove this exact override. |
| test_hook_trust | index | 2 | Repair the existing index diagnostic and remove this exact override. |
| test_hook_trust | no-untyped-call | 23 | Repair the existing no-untyped-call diagnostic and remove this exact override. |
| test_hook_trust | no-untyped-def | 34 | Repair the existing no-untyped-def diagnostic and remove this exact override. |
| test_hook_trust | operator | 1 | Repair the existing operator diagnostic and remove this exact override. |
| test_hook_trust | var-annotated | 1 | Repair the existing var-annotated diagnostic and remove this exact override. |
| test_install | arg-type | 4 | Repair the existing arg-type diagnostic and remove this exact override. |
| test_install | assignment | 1 | Repair the existing assignment diagnostic and remove this exact override. |
| test_install | attr-defined | 24 | Repair the existing attr-defined diagnostic and remove this exact override. |
| test_install | list-item | 1 | Repair the existing list-item diagnostic and remove this exact override. |
| test_install | no-untyped-call | 92 | Repair the existing no-untyped-call diagnostic and remove this exact override. |
| test_install | no-untyped-def | 79 | Repair the existing no-untyped-def diagnostic and remove this exact override. |
| test_install | var-annotated | 1 | Repair the existing var-annotated diagnostic and remove this exact override. |
| test_install_artefacts | attr-defined | 1 | Repair the existing attr-defined diagnostic and remove this exact override. |
| test_latest_plugins | attr-defined | 2 | Repair the existing attr-defined diagnostic and remove this exact override. |
| test_lifecycle_commands | no-untyped-call | 7 | Repair the existing no-untyped-call diagnostic and remove this exact override. |
| test_lifecycle_commands | no-untyped-def | 14 | Repair the existing no-untyped-def diagnostic and remove this exact override. |
| test_loader_roots | arg-type | 1 | Repair the existing arg-type diagnostic and remove this exact override. |
| test_loader_roots | no-untyped-call | 1 | Repair the existing no-untyped-call diagnostic and remove this exact override. |
| test_loader_roots | no-untyped-def | 2 | Repair the existing no-untyped-def diagnostic and remove this exact override. |
| test_loader_roots | union-attr | 1 | Repair the existing union-attr diagnostic and remove this exact override. |
| test_package_root | arg-type | 1 | Repair the existing arg-type diagnostic and remove this exact override. |
| test_package_root | no-untyped-def | 2 | Repair the existing no-untyped-def diagnostic and remove this exact override. |
| test_package_root | union-attr | 1 | Repair the existing union-attr diagnostic and remove this exact override. |
| test_plugin_modules | no-untyped-def | 1 | Repair the existing no-untyped-def diagnostic and remove this exact override. |
| test_plugin_scripts_entry | no-untyped-def | 1 | Repair the existing no-untyped-def diagnostic and remove this exact override. |
| test_plugin_setup | arg-type | 13 | Repair the existing arg-type diagnostic and remove this exact override. |
| test_plugin_setup | dict-item | 13 | Repair the existing dict-item diagnostic and remove this exact override. |
| test_plugin_setup | no-untyped-call | 6 | Repair the existing no-untyped-call diagnostic and remove this exact override. |
| test_plugin_setup | no-untyped-def | 13 | Repair the existing no-untyped-def diagnostic and remove this exact override. |
| test_refresh_plugins | arg-type | 1 | Repair the existing arg-type diagnostic and remove this exact override. |
| test_refresh_plugins | func-returns-value | 3 | Repair the existing func-returns-value diagnostic and remove this exact override. |
| test_refresh_plugins | no-untyped-call | 6 | Repair the existing no-untyped-call diagnostic and remove this exact override. |
| test_refresh_plugins | no-untyped-def | 16 | Repair the existing no-untyped-def diagnostic and remove this exact override. |
| test_refresh_plugins | union-attr | 1 | Repair the existing union-attr diagnostic and remove this exact override. |
| test_required_plugins | arg-type | 1 | Repair the existing arg-type diagnostic and remove this exact override. |
| test_required_plugins | attr-defined | 5 | Repair the existing attr-defined diagnostic and remove this exact override. |
| test_required_plugins | no-untyped-call | 5 | Repair the existing no-untyped-call diagnostic and remove this exact override. |
| test_required_plugins | no-untyped-def | 12 | Repair the existing no-untyped-def diagnostic and remove this exact override. |
| test_runtime_delivery | arg-type | 1 | Repair the existing arg-type diagnostic and remove this exact override. |
| test_runtime_delivery | no-untyped-call | 10 | Repair the existing no-untyped-call diagnostic and remove this exact override. |
| test_runtime_delivery | no-untyped-def | 9 | Repair the existing no-untyped-def diagnostic and remove this exact override. |
| test_runtime_delivery | union-attr | 1 | Repair the existing union-attr diagnostic and remove this exact override. |
| test_selection_convergence | no-untyped-call | 1 | Repair the existing no-untyped-call diagnostic and remove this exact override. |
| test_selection_convergence | no-untyped-def | 4 | Repair the existing no-untyped-def diagnostic and remove this exact override. |
| test_session_hook_events | no-untyped-call | 10 | Repair the existing no-untyped-call diagnostic and remove this exact override. |
| test_session_hook_events | no-untyped-def | 16 | Repair the existing no-untyped-def diagnostic and remove this exact override. |
| test_session_hooks | no-untyped-call | 6 | Repair the existing no-untyped-call diagnostic and remove this exact override. |
| test_session_hooks | no-untyped-def | 7 | Repair the existing no-untyped-def diagnostic and remove this exact override. |
| test_tj_recover | arg-type | 1 | Repair the existing arg-type diagnostic and remove this exact override. |
| test_tj_recover | attr-defined | 3 | Repair the existing attr-defined diagnostic and remove this exact override. |
| test_tj_recover | misc | 2 | Repair the existing misc diagnostic and remove this exact override. |
| test_tj_recover | no-untyped-call | 2 | Repair the existing no-untyped-call diagnostic and remove this exact override. |
| test_verify_machine | arg-type | 1 | Repair the existing arg-type diagnostic and remove this exact override. |
| test_verify_machine | no-untyped-def | 14 | Repair the existing no-untyped-def diagnostic and remove this exact override. |
| test_verify_machine | union-attr | 1 | Repair the existing union-attr diagnostic and remove this exact override. |
| test_websocket | no-untyped-call | 4 | Repair the existing no-untyped-call diagnostic and remove this exact override. |
| test_websocket | no-untyped-def | 6 | Repair the existing no-untyped-def diagnostic and remove this exact override. |
| test_work_items_install | attr-defined | 1 | Repair the existing attr-defined diagnostic and remove this exact override. |
| tj_agent.host | no-any-return | 1 | Repair the existing no-any-return diagnostic and remove this exact override. |
| tj_agent.plugins.sources | no-any-return | 2 | Repair the existing no-any-return diagnostic and remove this exact override. |
| tj_agent.plugins.sources | type-arg | 2 | Repair the existing type-arg diagnostic and remove this exact override. |
| tj_agent.plugins.sources | union-attr | 1 | Repair the existing union-attr diagnostic and remove this exact override. |
| tj_recover | attr-defined | 7 | Repair the existing attr-defined diagnostic and remove this exact override. |
| tj_recover | call-arg | 3 | Repair the existing call-arg diagnostic and remove this exact override. |
| tj_recover | no-untyped-call | 1 | Repair the existing no-untyped-call diagnostic and remove this exact override. |
| tj_start | attr-defined | 1 | Repair the existing attr-defined diagnostic and remove this exact override. |
| tj_start | no-any-return | 2 | Repair the existing no-any-return diagnostic and remove this exact override. |
| tj_start | no-untyped-call | 3 | Repair the existing no-untyped-call diagnostic and remove this exact override. |
| tj_start | no-untyped-def | 2 | Repair the existing no-untyped-def diagnostic and remove this exact override. |
| tj_start | type-arg | 3 | Repair the existing type-arg diagnostic and remove this exact override. |
| tj_status | no-untyped-def | 1 | Repair the existing no-untyped-def diagnostic and remove this exact override. |
| tj_status | type-arg | 2 | Repair the existing type-arg diagnostic and remove this exact override. |
| transcripts | no-untyped-call | 2 | Repair the existing no-untyped-call diagnostic and remove this exact override. |
| transcripts | no-untyped-def | 3 | Repair the existing no-untyped-def diagnostic and remove this exact override. |
| update_marketplace_plugins | no-any-return | 1 | Repair the existing no-any-return diagnostic and remove this exact override. |
| verify_machine | assignment | 1 | Repair the existing assignment diagnostic and remove this exact override. |
| verify_machine | no-untyped-def | 8 | Repair the existing no-untyped-def diagnostic and remove this exact override. |
| verify_machine | type-arg | 1 | Repair the existing type-arg diagnostic and remove this exact override. |
| verify_machine | unreachable | 1 | Repair the existing unreachable diagnostic and remove this exact override. |
