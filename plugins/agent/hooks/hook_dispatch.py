"""Run several hook scripts for one host event inside a single interpreter.

Repeated ``--hook <script> [arguments...]`` groups pass arguments to each script.
The host's sibling-hook merge is reproduced here, so one event costs one
interpreter launch instead of one per gate. Each gate still runs as its own ``__main__`` with its
own stdin, stdout and stderr. A leading ``--deadline <seconds>`` stays under the host timeout: a gate
still running then is reported, and every finished gate's verdict is kept.
"""

from __future__ import annotations

import io
import hashlib
import json
import os
from pathlib import Path
import runpy
import sys
import threading
import traceback


HOOKS = Path(__file__).resolve().parent
DECISIONS = ("deny", "defer", "ask", "allow")
BLOCKING_EXIT = 2


def exit_status(code) -> tuple[int, str]:
    if code is None:
        return 0, ""
    if isinstance(code, int):
        return code, ""
    return 1, f"{code}\n"


def captured(stream: io.TextIOWrapper) -> str:
    try:
        stream.flush()
        return stream.buffer.getvalue().decode("utf-8", errors="replace")
    except (ValueError, AttributeError):
        return ""


def run_gate(name: str, raw: bytes, arguments: list[str] | None = None) -> tuple[int, str, str]:
    script = HOOKS / name if not Path(name).is_absolute() else Path(name)
    stdin = io.TextIOWrapper(io.BytesIO(raw), encoding="utf-8")
    stdout = io.TextIOWrapper(io.BytesIO(), encoding="utf-8", write_through=True)
    stderr = io.TextIOWrapper(io.BytesIO(), encoding="utf-8", write_through=True)
    saved = sys.stdin, sys.stdout, sys.stderr, sys.argv, sys.path[:]
    sys.stdin, sys.stdout, sys.stderr, sys.argv = stdin, stdout, stderr, [str(script), *(arguments or [])]
    sys.path.insert(0, str(script.parent))
    code, message = 0, ""
    try:
        runpy.run_path(str(script), run_name="__main__")
    except SystemExit as exit:
        code, message = exit_status(exit.code)
    except BaseException:
        traceback.print_exc(file=stderr)
        code = 1
    finally:
        sys.stdin, sys.stdout, sys.stderr, sys.argv, sys.path[:] = saved
    return code, captured(stdout), captured(stderr) + message


def parse(stdout: str):
    text = stdout.strip()
    if not text:
        return None
    try:
        value = json.loads(text)
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def merge(event: str, results: list[tuple[str, int, str, str]]) -> tuple[dict, str]:
    decisions: dict[str, list[str]] = {}
    contexts: list[str] = []
    blocks: list[str] = []
    messages: list[str] = []
    stops: list[str] = []
    stopped = False
    suppress = False
    specific: dict = {}
    diagnostics: list[str] = []

    for name, code, stdout, stderr in results:
        data = parse(stdout)
        if stdout.strip() and data is None:
            if event == "SessionStart":
                contexts.append(stdout.strip())
            else:
                diagnostics.append(f"{name}: {stdout.strip()}")
                if code == 0:
                    code = 1
        if code == BLOCKING_EXIT:
            reason = stderr.strip() or f"{name} blocked this {event} event."
            if event == "PreToolUse":
                decisions.setdefault("deny", []).append(reason)
            else:
                blocks.append(reason)
        elif code:
            failure = (
                f"hook-dispatch: {Path(name).name} failed with exit status {code}. "
                f"Repair the gate and retry this {event} event."
            )
            if event == "PreToolUse":
                decisions.setdefault("deny", []).append(failure)
            elif event == "Stop":
                blocks.append(failure)
            else:
                stopped = True
                stops.append(failure)
            if stderr.strip():
                diagnostics.append(f"{name}: {stderr.strip()}")
            else:
                diagnostics.append(f"{name}: exited with status {code}")
        elif stderr.strip():
            diagnostics.append(f"{name}: {stderr.strip()}")
        if event == "SessionStart" and code not in (0, BLOCKING_EXIT):
            notice = f"hook-dispatch: {name} failed with exit status {code}."
            if stderr.strip():
                notice += "\n" + stderr.strip()
            messages.append(notice)
            contexts.append(notice)
        if data is None:
            continue

        output = data.get("hookSpecificOutput")
        if isinstance(output, dict):
            decision = output.get("permissionDecision")
            if decision in DECISIONS:
                reason = output.get("permissionDecisionReason")
                decisions.setdefault(decision, []).append(reason if isinstance(reason, str) else "")
            context = output.get("additionalContext")
            if isinstance(context, str) and context.strip():
                contexts.append(context.strip())
            for key, value in output.items():
                if key in ("hookEventName", "permissionDecision", "permissionDecisionReason",
                           "additionalContext"):
                    continue
                if key in specific and specific[key] != value:
                    diagnostics.append(f"{name}: conflicting {key} ignored; an earlier gate set it")
                specific.setdefault(key, value)

        decision = data.get("decision")
        if decision == "block":
            reason = data.get("reason")
            blocks.append(reason.strip() if isinstance(reason, str) and reason.strip()
                          else f"{name} blocked this {event} event.")

        message = data.get("systemMessage")
        if isinstance(message, str) and message.strip():
            messages.append(message.strip())
        if data.get("continue") is False:
            stopped = True
            reason = data.get("stopReason")
            if isinstance(reason, str) and reason.strip():
                stops.append(reason.strip())
        if data.get("suppressOutput") is True:
            suppress = True

    merged: dict = {}
    output: dict = {}
    for decision in DECISIONS:
        if decision in decisions:
            output["permissionDecision"] = decision
            reasons = [reason.strip() for reason in decisions[decision] if reason.strip()]
            if reasons:
                output["permissionDecisionReason"] = "\n\n".join(reasons)
            break
    if contexts:
        output["additionalContext"] = "\n\n".join(contexts)
    for key, value in specific.items():
        output.setdefault(key, value)
    if output:
        merged["hookSpecificOutput"] = {"hookEventName": event, **output}
    if blocks:
        merged["decision"] = "block"
        merged["reason"] = "\n\n".join(blocks)
    if messages:
        merged["systemMessage"] = "\n".join(messages)
    if stopped:
        merged["continue"] = False
        if stops:
            merged["stopReason"] = "\n".join(stops)
    if suppress:
        merged["suppressOutput"] = True
    return merged, "\n".join(diagnostics)


def overrun_notice(event: str, merged: dict, deadline: float, unfinished: list[str]) -> dict:
    stalled, skipped = unfinished[0], unfinished[1:]
    notice = (
        f"hook-dispatch: {Path(stalled).name} did not finish within {deadline:g}s, so its verdict "
        "is missing for this call"
    )
    if skipped:
        notice += "; these gates did not run: " + ", ".join(Path(name).name for name in skipped)
    notice += "."
    merged = dict(merged)
    message = merged.get("systemMessage")
    merged["systemMessage"] = f"{message}\n{notice}" if message else notice
    if event == "Stop":
        reason = merged.get("reason")
        merged["decision"] = "block"
        merged["reason"] = f"{reason}\n\n{notice}" if reason else notice
    elif event == "PreToolUse":
        specific = dict(merged.get("hookSpecificOutput") or {"hookEventName": event})
        if specific.get("permissionDecision") != "deny":
            specific["permissionDecision"] = "deny"
            specific["permissionDecisionReason"] = notice + " Retry when every required hook can finish."
        merged["hookSpecificOutput"] = specific
    else:
        merged["continue"] = False
        merged["stopReason"] = notice + " Stop ordinary work until required hooks recover."
    if event in ("SessionStart", "PreToolUse", "PostToolUse", "PostToolUseFailure", "UserPromptSubmit"):
        specific = dict(merged.get("hookSpecificOutput") or {"hookEventName": event})
        context = specific.get("additionalContext")
        specific["additionalContext"] = f"{context}\n\n{notice}" if context else notice
        merged["hookSpecificOutput"] = specific
    return merged


def main(argv: list[str]) -> int:
    arguments = argv[1:]
    deadline = None
    if len(arguments) >= 2 and arguments[0] == "--deadline":
        try:
            deadline = float(arguments[1])
        except ValueError:
            deadline = None
        arguments = arguments[2:]
    if not arguments:
        print("hook-dispatch: name the gate scripts to run.", file=sys.stderr)
        return 1
    stdout, stderr = sys.stdout, sys.stderr
    raw = sys.stdin.buffer.read()
    try:
        data = json.loads(raw.decode("utf-8-sig")) if raw.strip() else {}
    except (UnicodeError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    event = data.get("hook_event_name") or data.get("hookEventName") or ""

    def acknowledge(succeeded: bool, detail: str) -> None:
        if event != "SessionStart":
            return
        directory = os.environ.get("TJ_AGENTS_STARTUP_ACK_DIRECTORY")
        token = os.environ.get("TJ_AGENTS_STARTUP_ACK_TOKEN")
        required = os.environ.get("TJ_AGENTS_STARTUP_REQUIRED_HOOKS")
        if directory and token and required is not None and len(token) >= 32 and all(value.isalnum() or value in "-_" for value in token):
            path = Path(directory) / token
            try:
                identities = json.loads(required)
                if not isinstance(identities, list) or not all(isinstance(value, str) for value in identities):
                    return
                if identities:
                    harness = json.loads((Path(__file__).resolve().parents[1] / 'harness.json').read_text(encoding='utf-8'))
                    marketplace, _, name = harness['plugin'].partition('/')
                    identity = name + '@' + marketplace
                    if identity not in identities:
                        return
                    if not succeeded:
                        path.with_name(token + '.failed').write_text(
                            f"Required startup hook {identity} failed: {detail}", encoding='utf-8')
                        return
                    receipt = path.with_name(token + '.' + hashlib.sha256(identity.encode()).hexdigest())
                    receipt.write_text(token, encoding='utf-8')
                    if not all(path.with_name(token + '.' + hashlib.sha256(value.encode()).hexdigest()).is_file()
                               for value in identities):
                        return
                if not succeeded or path.with_name(token + '.failed').exists():
                    return
                if path.parent.is_dir() and path.parent.resolve() == Path(directory).resolve():
                    descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                    with os.fdopen(descriptor, "w", encoding="utf-8") as output:
                        output.write(token)
            except FileExistsError:
                pass
            except (OSError, ValueError, KeyError, TypeError):
                pass
    if arguments[0] != "--hook":
        stderr.write("hook-dispatch: use --hook <script> [arguments...] groups.\n")
        return 1
    gates: list[tuple[str, list[str]]] = []
    for argument in arguments:
        if argument == "--hook":
            gates.append(("", []))
        elif not gates[-1][0]:
            gates[-1] = (argument, [])
        else:
            gates[-1][1].append(argument)
    if any(not name for name, _ in gates):
        stderr.write("hook-dispatch: each --hook requires a script.\n")
        return 1
    names = [name for name, _ in gates]
    results: list[tuple[str, int, str, str]] = []
    lock = threading.Lock()

    def run_all() -> None:
        for name, script_arguments in gates:
            result = (name, *run_gate(name, raw, script_arguments))
            with lock:
                results.append(result)

    worker = threading.Thread(target=run_all, daemon=True)
    worker.start()
    worker.join(deadline)
    with lock:
        finished = list(results)
        overrun = len(finished) < len(names)
    merged, diagnostics = merge(event, finished)
    if overrun:
        merged = overrun_notice(event, merged, deadline, names[len(finished):])
    succeeded = not overrun and not any(code for _, code, _, _ in finished) \
        and merged.get("continue") is not False and merged.get("decision") != "block"
    acknowledge(succeeded, diagnostics or merged.get('stopReason') or str(merged))
    if diagnostics:
        stderr.write(diagnostics + "\n")
    if merged:
        stdout.write(json.dumps(merged, ensure_ascii=True) + "\n")
    if overrun:
        stdout.flush()
        stderr.flush()
        os._exit(0)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
