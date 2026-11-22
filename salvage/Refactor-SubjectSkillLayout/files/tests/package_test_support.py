import shutil
from importlib.resources import files
from pathlib import Path


def bundle(root: Path) -> Path:
    for name in ("tj_agent", "tj_agent_store", "tj_cli"):
        shutil.copytree(
            Path(str(files(name))),
            root / "python" / name,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
        )
    return root
