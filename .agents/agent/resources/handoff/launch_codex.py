#!/usr/bin/env python3
"""Hand prepared work to an independent Codex session in a new terminal tab.

Run as `python launch_codex.py ...` on Windows (where `python3` is often the Microsoft Store alias stub
rather than a real interpreter) and `python3 launch_codex.py ...` elsewhere; the shebang and exec bit are
not relied on.
"""

import argparse
import importlib.util
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent


def _package_root():
    for root in HERE.parents:
        markers = (root / '.git', root / '.codex-plugin' / 'plugin.json', root / '.claude-plugin' / 'plugin.json')
        if any(marker.exists() for marker in markers):
            return root
    raise SystemExit(f'The package root was not found relative to {HERE}.')


def _load_agent_cli():
    root = _package_root()
    path = root / '.agents' / 'agent' / 'runtime' / 'agent_cli.py'
    if not path.is_file():
        raise SystemExit(f'The packaged agent_cli.py library was not found under {root}.')
    spec = importlib.util.spec_from_file_location('agent_cli', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def parse_args(argv, agent_cli):
    parser = argparse.ArgumentParser(description='Hand prepared work to an independent Codex session in a new terminal tab.')
    parser.add_argument('--working-directory', required=True)
    parser.add_argument('--prompt-path', required=True)
    parser.add_argument('--title', required=True)
    parser.add_argument('--lane', choices=('L1', 'L2', 'L3', 'L4', 'L5', 'L6', 'L7'), required=True)
    parser.add_argument('--minimum-version', default='0.154.0')
    parser.add_argument('--item', help='work item the child is linked to; one is created when omitted')
    parser.add_argument('--parent', help='parent session (host:id); defaults to the calling session')
    parser.add_argument('--item-field', action='append', default=[], type=agent_cli.item_field,
                        metavar='NAME=VALUE', help='a create field for the work item made when --item is omitted')
    args = parser.parse_args(argv)
    if not args.title.strip():
        raise agent_cli.LaunchError('--title must name the session; it becomes the tab title.')

    if args.lane == 'L7':
        raise agent_cli.LaunchError('--lane L7 is for in-session clerical work and cannot open a handoff.')

    return args


def resolve_model(args, agent_cli):
    model, effort = agent_cli.resolve_lane_model(agent_cli.Host.CODEX, lane=args.lane)
    return model, effort, f'lane {args.lane} -> '


def main(argv=None):
    agent_cli = _load_agent_cli()
    agent_cli.make_stdio_encoding_lossy()
    try:
        args = parse_args(argv, agent_cli)
        working_directory = agent_cli.resolve_tab_directory(args.working_directory)
        prompt_sentence, prompt_path = agent_cli.prompt_file_argument(args.prompt_path)
        adapter = agent_cli.host_launch(agent_cli.Host.CODEX)
        adapter.resolve_executable(minimum=args.minimum_version)
        model, effort, tier = resolve_model(args, agent_cli)
        parent = agent_cli.parent_session(args.parent)
        prompt_sentence += agent_cli.parent_rule(parent)
        item = agent_cli.resolve_item(args.item, args.title, working_directory, parent, args.item_field)
        shown = agent_cli.launch(agent_cli.Host.CODEX, working_directory, args.title,
                                 adapter.start_arguments(working_directory, args.title, prompt_sentence, model, effort),
                                 item=item, parent=parent)

        selection = f'{tier}{model}'
        if effort:
            selection += f' at {effort}'
        print(f"Launched Codex handoff tab '{args.title}' in {working_directory} on {selection} with prompt {prompt_path}")
        print(json.dumps({"worktree": str(working_directory), "prompt_path": str(prompt_path), "title": args.title,
                          "parent": parent, "launch_id": shown["launch"]["launch_id"]}))
        return 0
    except agent_cli.LaunchError as exc:
        return agent_cli.report_launch_failure(exc)


if __name__ == '__main__':
    sys.exit(main())
