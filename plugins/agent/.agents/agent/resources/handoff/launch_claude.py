#!/usr/bin/env python3
"""Hand prepared work to an independent Claude Code session in a new terminal tab."""

import argparse
import json
import sys

import agent_cli
from launch_errors import LaunchError
from tj_agent.host import Host


def parse_args(argv, agent_cli):
    parser = argparse.ArgumentParser(description='Hand prepared work to an independent Claude Code session in a new terminal tab.')
    parser.add_argument('--working-directory', required=True)
    parser.add_argument('--prompt-path', required=True)
    parser.add_argument('--title', required=True)
    parser.add_argument('--lane', choices=('L1', 'L2', 'L3', 'L4', 'L5', 'L6', 'L7'), required=True)
    parser.add_argument('--dangerously-skip-permissions', action='store_true')
    parser.add_argument('--item', help='existing work-item URL or reference to link; omit to launch with no work item')
    parser.add_argument('--parent', help='explicit reporting parent (host:id)')
    args = parser.parse_args(argv)
    if not args.title.strip():
        raise LaunchError('--title must name the session; it becomes the tab title.')

    if args.lane == 'L7':
        raise LaunchError('--lane L7 is for in-session clerical work and cannot open a handoff.')

    return args


def resolve_model(args, agent_cli):
    entry = agent_cli.resolve_lane(Host.CLAUDE, lane=args.lane)
    route = entry.get('handoff')
    if route:
        raise LaunchError(
            f"--lane {args.lane} routes to Codex {route['lane']}; use agent:handoff --host codex."
        )
    return entry['model'], entry.get('effort'), f'lane {args.lane} -> '




def main(argv=None):
    agent_cli.make_stdio_encoding_lossy()
    try:
        args = parse_args(argv, agent_cli)
        working_directory = agent_cli.resolve_tab_directory(args.working_directory)
        prompt_sentence, prompt_path = agent_cli.prompt_file_argument(args.prompt_path)
        model, effort, tier = resolve_model(args, agent_cli)
        handoff = agent_cli.handoff.relationship(args.parent)
        prompt_sentence = handoff.prompt(prompt_sentence)
        item = agent_cli.resolve_item(args.item, working_directory)
        adapter = agent_cli.host_launch(Host.CLAUDE)
        arguments = adapter.start_arguments(working_directory, args.title, prompt_sentence, model, effort)
        if args.dangerously_skip_permissions:
            arguments.insert(0, '--dangerously-skip-permissions')
        shown = agent_cli.launch(Host.CLAUDE, working_directory, args.title, arguments, item=item, parent=handoff.parent)

        selection = f'{tier}{model}'
        if effort:
            selection += f' at {effort}'
        print(f"Launched claude handoff tab '{args.title}' in {working_directory} on {selection} with prompt {prompt_path}")
        print(json.dumps({"worktree": str(working_directory), "prompt_path": str(prompt_path), "title": args.title,
                          **handoff.receipt(), "launch_id": shown["launch"]["launch_id"]}))
        return 0
    except LaunchError as exc:
        return agent_cli.report_launch_failure(exc)


if __name__ == '__main__':
    sys.exit(main())
