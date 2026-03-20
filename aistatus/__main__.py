from __future__ import annotations

import argparse
import sys

from .cli.usage import add_usage_subparser


def main() -> int:
    parser = argparse.ArgumentParser(prog="aistatus", description="aistatus SDK utilities")
    subparsers = parser.add_subparsers(dest="command")
    add_usage_subparser(subparsers)

    args = parser.parse_args()
    handler = getattr(args, "func", None)
    if handler is None:
        parser.print_help()
        return 0
    return int(handler(args) or 0)


if __name__ == "__main__":
    sys.exit(main())
