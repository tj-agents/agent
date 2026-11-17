import json
import sys
from pathlib import Path


def main() -> None:
    script = Path(__file__)
    script.with_suffix(".actual.json").write_text(
        json.dumps({"arguments": sys.argv[1:], "cwd": str(Path.cwd())}),
        encoding="utf-8",
    )
    print(script.with_suffix(".request.json").read_text(encoding="utf-8"))


if __name__ == "__main__":
    main()
