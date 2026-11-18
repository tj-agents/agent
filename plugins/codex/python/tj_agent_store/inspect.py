import argparse
import json
import sys
from pathlib import Path

from tj_agent_store import reader


def arguments(argv=None):
    parser = argparse.ArgumentParser(description="Inspect recorded Agent Store state.")
    parser.add_argument("--database", type=Path)
    selector = parser.add_mutually_exclusive_group()
    selector.add_argument("--execution-key")
    selector.add_argument("--repository")
    return parser.parse_args(argv)


def main(argv=None):
    args = arguments(argv)
    try:
        result = reader.ReadOnlyWorkItemStore(args.database).inspect(
            execution_key=args.execution_key,
            repository=args.repository,
        )
    except (OSError, RuntimeError, ValueError) as error:
        print(json.dumps({"error": str(error)}), file=sys.stderr)
        return 1
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
