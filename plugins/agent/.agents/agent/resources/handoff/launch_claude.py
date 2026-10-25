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
    parser.add_argument('--title', default='Claude handoff')
    parser.add_argument('--model')
    parser.add_argument('--effort', choices=('low', 'medium', 'high', 'xhigh', 'max'))
    parser.add_argument('--lane', choices=('L1', 'L2', 'L3', 'L4', 'L5', 'L6', 'L7'))
    parser.add_argument('--frontier', action='store_true')
    parser.add_argument('--dangerously-skip-permissions', action='store_true')
    args = parser.parse_args(argv)

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


def build_arguments(args, model, effort, prompt_sentence):
    arguments = []
    if args.dangerously_skip_permissions:
        arguments.append('--dangerously-skip-permissions')
    if model:
        arguments += ['--model', model]
    if effort:
        arguments += ['--effort', effort]

    arguments.append(prompt_sentence)
    return arguments


def main(argv=None):
    agent_cli = _load_agent_cli()
    agent_cli.make_stdio_encoding_lossy()
    status_path = None
    try:
        args = parse_args(argv, agent_cli)

        # parse_args already rejected any argparse-level contradiction (a --frontier beside --lane or
        # --model, an --effort without an explicit --model). What happens here is the first filesystem or
        # lane-table work, so the directory is checked before any of it -- whichever of it is also wrong,
        # the directory's error is never shadowed by a later check that only looked irrelevant.
        working_directory = agent_cli.resolve_tab_directory(args.working_directory)

        prompt_sentence, prompt_path = agent_cli.prompt_file_argument(args.prompt_path)
        model, effort, tier = resolve_model(args, agent_cli)
        arguments = build_arguments(args, model, effort, prompt_sentence)

        status_path = agent_cli.handoff_status_path()
        agent_cli.open_claude_tab(working_directory, args.title, arguments, status_file=status_path, require_session_start=True)
        agent_cli.await_handoff_startup(status_path)

        selection = f'{tier}{model}' if model else 'the CLI default model'
        if effort:
            selection += f' at {effort}'
        print(f"Launched claude handoff tab '{args.title}' in {working_directory} on {selection} with prompt {prompt_path}")
        print(json.dumps({"worktree": str(working_directory), "prompt_path": str(prompt_path), "startup": "verified", "status_path": str(status_path)}))
        return 0
    except agent_cli.LaunchError as exc:
        if status_path is not None:
            print(f"Handoff startup receipt: {status_path}", file=sys.stderr)
        return agent_cli.report_launch_failure(exc)


if __name__ == '__main__':
    sys.exit(main())
