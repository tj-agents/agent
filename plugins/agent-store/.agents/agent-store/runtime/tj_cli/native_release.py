"""Qualify an immutable native release against the latest required push runs."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

_WORKFLOWS = (".github/workflows/ci.yml", ".github/workflows/rust-ci.yml")


def _repository_matches(run: dict[str, object], repository: str) -> bool:
    head_repository = run.get("head_repository")
    return (
        isinstance(head_repository, dict)
        and head_repository.get("full_name") == repository
    )


def _run_order(run: dict[str, object]) -> tuple[int, int]:
    identifier, attempt = run.get("id"), run.get("run_attempt", 1)
    if not isinstance(identifier, int) or not isinstance(attempt, int):
        raise ValueError("workflow runs must have numeric ids and attempts")
    return identifier, attempt


def qualified_revision(
    event: dict[str, object],
    repository: str,
    default_branch: str,
    current_sha: str,
    runs: list[dict[str, object]],
) -> str | None:
    """Return the current SHA only when its latest required push runs succeeded."""
    run = event.get("workflow_run")
    if not isinstance(run, dict):
        return None
    head_repository = run.get("head_repository")
    if (
        not isinstance(head_repository, dict)
        or head_repository.get("full_name") != repository
        or run.get("event") != "push"
        or run.get("status") != "completed"
        or run.get("conclusion") != "success"
        or run.get("head_branch") != default_branch
        or run.get("head_sha") != current_sha
    ):
        return None
    for workflow in _WORKFLOWS:
        candidates = [
            item
            for item in runs
            if item.get("path") == workflow
            and item.get("event") == "push"
            and item.get("head_branch") == default_branch
            and item.get("head_sha") == current_sha
            and _repository_matches(item, repository)
        ]
        if not candidates:
            return None
        latest = max(candidates, key=_run_order)
        if latest.get("status") != "completed" or latest.get("conclusion") != "success":
            return None
    return current_sha


def _api(endpoint: str) -> object:
    result = subprocess.run(
        ["gh", "api", endpoint], check=True, capture_output=True, text=True
    )
    return json.loads(result.stdout)


def main() -> None:
    """Write the qualified SHA to the Actions output, otherwise leave it absent."""
    repository = os.environ["GITHUB_REPOSITORY"]
    event = json.loads(
        Path(os.environ["GITHUB_EVENT_PATH"]).read_text(encoding="utf-8")
    )
    metadata = _api(f"repos/{repository}")
    if not isinstance(metadata, dict):
        raise ValueError("repository metadata must be an object")
    branch = str(metadata["default_branch"])
    head = _api(f"repos/{repository}/commits/{branch}")
    if not isinstance(head, dict):
        raise ValueError("default branch head must be an object")
    sha = str(head["sha"])
    runs = _api(
        f"repos/{repository}/actions/runs?event=push&head_sha={sha}&per_page=100"
    )
    if not isinstance(runs, dict) or not isinstance(runs.get("workflow_runs"), list):
        raise ValueError("workflow run response must contain runs")
    qualified = qualified_revision(
        event, repository, branch, sha, runs["workflow_runs"]
    )
    if qualified:
        with Path(os.environ["GITHUB_OUTPUT"]).open("a", encoding="utf-8") as output:
            output.write(f"sha={qualified}\n")
    else:
        print("native release is superseded or lacks successful current push checks")


if __name__ == "__main__":
    main()
