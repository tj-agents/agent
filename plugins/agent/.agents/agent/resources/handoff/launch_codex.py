#!/usr/bin/env python3
"""Hand prepared work to an independent Codex session in a new terminal tab."""

import argparse
import json
import sys

import agent_cli
from codex import launch as codex_launch
from launch_errors import LaunchError
from tj_agent.host import Host


def parse_args(argv, agent_cli):
    parser = argparse.ArgumentParser(description='Hand prepared work to an independent Codex session in a new terminal tab.')
    parser.add_argument('--working-directory', required=True)
    parser.add_argument('--prompt-path', required=True)
    parser.add_argument('--title', required=True)
    parser.add_argument('--lane', choices=('L1', 'L2', 'L3', 'L4', 'L5', 'L6', 'L7'), required=True)
    parser.add_argument('--minimum-version', default='0.169.0')
    parser.add_argument('--item', help='existing work-item URL or reference to link; omit to launch with no work item')
    parser.add_argument('--parent', help='explicit reporting parent (host:id)')
    args = parser.parse_args(argv)
    if not args.title.strip():
        raise LaunchError('--title must name the session; it becomes the tab title.')

    if args.lane == 'L7':
        raise LaunchError('--lane L7 is for in-session clerical work and cannot open a handoff.')

    return args


def resolve_model(args, agent_cli):
    model, effort = agent_cli.resolve_lane_model(Host.CODEX, lane=args.lane)
    return model, effort, f'lane {args.lane} -> '


def main(argv=None):
    agent_cli.make_stdio_encoding_lossy()
    try:
        args = parse_args(argv, agent_cli)
        working_directory = agent_cli.resolve_tab_directory(args.working_directory)
        prompt_sentence, prompt_path = agent_cli.prompt_file_argument(args.prompt_path)
        adapter = agent_cli.host_launch(Host.CODEX)
        codex_launch.resolve_executable(minimum=args.minimum_version, match_daemon=True)
        model, effort, tier = resolve_model(args, agent_cli)
        handoff = agent_cli.handoff.relationship(args.parent)
        prompt_sentence = handoff.prompt(prompt_sentence)
        item = agent_cli.resolve_item(args.item, working_directory)
        shown = agent_cli.launch(Host.CODEX, working_directory, args.title,
                                 adapter.start_arguments(working_directory, args.title, prompt_sentence, model, effort),
                                 item=item, parent=handoff.parent)

        selection = f'{tier}{model}'
        if effort:
            selection += f' at {effort}'
        print(f"Launched Codex handoff tab '{args.title}' in {working_directory} on {selection} with prompt {prompt_path}")
        print(json.dumps({"worktree": str(working_directory), "prompt_path": str(prompt_path), "title": args.title,
                          **handoff.receipt(), "launch_id": shown["launch"]["launch_id"]}))
        return 0
    except LaunchError as exc:
        return agent_cli.report_launch_failure(exc)


if __name__ == '__main__':
    sys.exit(main())
