#!/usr/bin/env python3
"""Open an interactive Claude Code CLI in a new terminal tab on an exact directory.

Run as `python open_claude.py ...` on Windows (where `python3` is often the Microsoft Store alias stub
rather than a real interpreter) and `python3 open_claude.py ...` elsewhere; the shebang and exec bit are
not relied on.
"""

import argparse
import importlib.util
from pathlib import Path
import sys

HERE = Path(__file__).resolve().parent


def _load_agent_cli():
    for root in HERE.parents:
        markers = (root / '.git', root / '.codex-plugin' / 'plugin.json', root / '.claude-plugin' / 'plugin.json')
        path = root / '.agents' / 'agent' / 'runtime' / 'agent_cli.py'
        if path.is_file() and any(marker.exists() for marker in markers):
            spec = importlib.util.spec_from_file_location('agent_cli', path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            return module
    raise SystemExit(f'The packaged agent_cli.py library was not found relative to {HERE}.')


def _load_recovery():
    for root in HERE.parents:
        markers = (root / '.git', root / '.codex-plugin' / 'plugin.json', root / '.claude-plugin' / 'plugin.json')
        path = root / '.agents' / 'agent' / 'runtime' / 'agent_recovery.py'
        if path.is_file() and any(marker.exists() for marker in markers):
            spec = importlib.util.spec_from_file_location('agent_recovery', path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            return module
    raise SystemExit(f'The packaged agent_recovery.py library was not found relative to {HERE}.')


def parse_args(argv, agent_cli):
    parser = argparse.ArgumentParser(description='Open a Claude Code CLI in a new terminal tab.')
    parser.add_argument('--working-directory', default='.')
    parser.add_argument('--resume')
    parser.add_argument('--continue', dest='continue_', action='store_true')
    parser.add_argument('--prompt')
    parser.add_argument('--prompt-path')
    parser.add_argument('--title', default='Claude')
    parser.add_argument('--model')
    parser.add_argument('--confirm-closed', action='store_true')
    parser.add_argument('--dangerously-skip-permissions', action='store_true')
    args = parser.parse_args(argv)

    if args.resume and args.continue_:
        raise agent_cli.LaunchError('Pass --resume for a specific session or --continue for the most recent one, not both.')

    if args.prompt and args.prompt_path:
        raise agent_cli.LaunchError('Pass --prompt for a short instruction or --prompt-path for a prepared file, not both.')

    # A prompt this long is no longer the short instruction --prompt exists for, and a command line is
    # the worst place to keep one: nothing on the receiving end can report a prompt that arrived damaged,
    # and once the tab is gone the text is gone with it. handoff-claude takes only --prompt-path for this
    # reason.
    if args.prompt and len(args.prompt) > 500:
        raise agent_cli.LaunchError(
            f'The inline --prompt is {len(args.prompt)} characters. Write it to a file and pass --prompt-path '
            'instead; --prompt is for a short instruction.'
        )

    return args


def build_arguments(args, agent_cli):
    arguments = []
    if args.resume:
        arguments += ['--resume', args.resume]
    if args.continue_:
        arguments.append('--continue')
    if args.dangerously_skip_permissions:
        arguments.append('--dangerously-skip-permissions')
    if args.model:
        arguments += ['--model', args.model]

    if args.prompt_path:
        sentence, _ = agent_cli.prompt_file_argument(args.prompt_path)
        arguments.append(sentence)
    elif args.prompt:
        arguments.append(args.prompt)

    return arguments


def main(argv=None):
    agent_cli = _load_agent_cli()
    agent_cli.make_stdio_encoding_lossy()
    try:
        args = parse_args(argv, agent_cli)

        # parse_args already rejected any argparse-level contradiction (--resume with --continue, a prompt
        # too long, etc.). What happens here is the first filesystem work, so the directory is checked
        # before any of it -- whichever of it is also wrong, the directory's error is never shadowed by a
        # later check that only looked irrelevant.
        working_directory = agent_cli.resolve_tab_directory(args.working_directory)

        if args.resume:
            recovery = _load_recovery()
            prompt = args.prompt if args.prompt_path else args.prompt or 'Continue the requested work.'
            roots = {'claude': agent_cli.claude_config_dir() / 'projects'}
            resumed = recovery.open_session('claude', args.resume, prompt, args.title, roots,
                                            confirm_closed=args.confirm_closed,
                                            prompt_path=args.prompt_path, model=args.model,
                                            dangerously_skip_permissions=args.dangerously_skip_permissions,
                                            cli=agent_cli)
        else:
            arguments = build_arguments(args, agent_cli)
            agent_cli.open_claude_tab(working_directory, args.title, arguments)

        actual_directory = resumed['cwd'] if args.resume else working_directory
        print(f"Launched claude tab '{args.title}' in {actual_directory}")
        return 0
    except (agent_cli.LaunchError, ValueError, RuntimeError) as exc:
        return agent_cli.report_launch_failure(exc)


if __name__ == '__main__':
    sys.exit(main())
