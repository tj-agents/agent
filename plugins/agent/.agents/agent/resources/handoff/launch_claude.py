#!/usr/bin/env python3
"""Hand prepared work to an independent Claude Code session in a new terminal tab.

Run as `python launch_claude.py ...` on Windows (where `python3` is often the Microsoft Store alias stub
rather than a real interpreter) and `python3 launch_claude.py ...` elsewhere; the shebang and exec bit are
not relied on.
"""

import argparse
import importlib.util
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent


def is_haiku_model(model):
    return re.search(r'(^|[^a-z0-9])haiku($|[^a-z0-9])', model.lower()) is not None


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
    parser = argparse.ArgumentParser(description='Hand prepared work to an independent Claude Code session in a new terminal tab.')
    parser.add_argument('--working-directory', required=True)
    parser.add_argument('--prompt-path', required=True)
    parser.add_argument('--title', required=True)
    parser.add_argument('--model')
    parser.add_argument('--effort', choices=('low', 'medium', 'high', 'xhigh', 'max'))
    parser.add_argument('--lane', choices=('L1', 'L2', 'L3', 'L4', 'L5', 'L6', 'L7'))
    parser.add_argument('--frontier', action='store_true')
    parser.add_argument('--dangerously-skip-permissions', action='store_true')
    parser.add_argument('--item', help='work item the child is linked to')
    parser.add_argument('--parent', nargs='?', const='current',
                        help='parent session (host:id); bare --parent means the calling session')
    args = parser.parse_args(argv)
    if not args.title.strip():
        raise agent_cli.LaunchError('--title must name the session; it becomes the tab title.')

    # --frontier is the tier no lane resolves to and tolerates no competing selection beside it, so
    # passing it alongside --lane or --model is a contradiction to reject rather than an ambiguity to
    # rank.
    if args.frontier and (args.lane or args.model):
        raise agent_cli.LaunchError(
            '--frontier rejects --lane and --model beside it: the frontier tier is an explicit user '
            'request, not one selection among several.'
        )

    # --effort exists only for a model the user named outright: a --lane or the frontier tier already
    # carries its own effort for the model it resolves, so --effort beside either of them (or beside
    # neither) would be a caller's guess paired with a model it was never priced for, which is exactly
    # the defect this flag was removed and re-added to avoid.
    if args.effort and not args.model:
        raise agent_cli.LaunchError(
            '--effort is only accepted together with an explicit --model: a --lane or the frontier tier '
            'already carries its own effort for the model it resolves.'
        )

    if args.lane == 'L7':
        raise agent_cli.LaunchError('--lane L7 is for in-session clerical work and cannot open a handoff.')

    if args.model and is_haiku_model(args.model):
        raise agent_cli.LaunchError('--model cannot select the Haiku family for a handoff.')

    if not (args.frontier or args.lane or args.model):
        raise agent_cli.LaunchError(
            'handoff requires --lane L1-L6, --frontier, or an explicit non-Haiku --model; '
            'the CLI default is not verified for handoff.'
        )

    return args


def resolve_model(args, agent_cli):
    model = args.model
    effort = args.effort
    tier = ''
    if args.lane:
        route = agent_cli.resolve_lane('claude', lane=args.lane).get('handoff')
        if route:
            raise agent_cli.LaunchError(
                f"--lane {args.lane} routes to Codex {route['lane']}; use agent:handoff --host codex, "
                "or omit --lane for an explicit user-named model."
            )
    if args.frontier:
        model, effort = agent_cli.resolve_lane_model('claude', frontier=True)
        tier = 'frontier -> '
    elif not model and args.lane:
        model, effort = agent_cli.resolve_lane_model('claude', lane=args.lane)
        tier = f'lane {args.lane} -> '

    if model and model.strip().lower().split('[', 1)[0] in ('default', 'sonnet', 'opus', 'opusplan'):
        raise agent_cli.LaunchError('handoff requires a full model ID; configurable family aliases are not verified.')

    if model and is_haiku_model(model):
        raise agent_cli.LaunchError('resolved selection cannot use the Haiku family for a handoff.')

    return model, effort, tier




def main(argv=None):
    agent_cli = _load_agent_cli()
    agent_cli.make_stdio_encoding_lossy()
    try:
        args = parse_args(argv, agent_cli)
        working_directory = agent_cli.resolve_tab_directory(args.working_directory)
        prompt_sentence, prompt_path = agent_cli.prompt_file_argument(args.prompt_path)
        model, effort, tier = resolve_model(args, agent_cli)
        parent = agent_cli.parent_session(args.parent)
        prompt_sentence += agent_cli.parent_rule(parent)
        adapter = agent_cli.host_launch('claude')
        arguments = adapter.start_arguments(working_directory, args.title, prompt_sentence, model, effort)
        if args.dangerously_skip_permissions:
            arguments.insert(0, '--dangerously-skip-permissions')
        shown = agent_cli.launch('claude', working_directory, args.title, arguments, item=args.item, parent=parent)

        selection = f'{tier}{model}' if model else 'the CLI default model'
        if effort:
            selection += f' at {effort}'
        print(f"Launched claude handoff tab '{args.title}' in {working_directory} on {selection} with prompt {prompt_path}")
        print(json.dumps({"worktree": str(working_directory), "prompt_path": str(prompt_path), "title": args.title,
                          "parent": parent, "launch_id": shown["launch"]["launch_id"]}))
        return 0
    except agent_cli.LaunchError as exc:
        return agent_cli.report_launch_failure(exc)


if __name__ == '__main__':
    sys.exit(main())
