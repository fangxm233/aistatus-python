"""CLI entry point: python -m aistatus.gateway"""

from __future__ import annotations

import argparse
import sys


def main():
    parser = argparse.ArgumentParser(
        prog="aistatus-gateway",
        description="Local transparent proxy for automatic AI API failover",
    )
    sub = parser.add_subparsers(dest="command")

    # --- start ---
    start_p = sub.add_parser("start", help="Start the gateway server")
    start_p.add_argument("-c", "--config", help="Config file path")
    start_p.add_argument("--host", default="127.0.0.1", help="Listen host (default: 127.0.0.1)")
    start_p.add_argument("-p", "--port", type=int, default=9880, help="Listen port (default: 9880)")
    start_p.add_argument(
        "--auto", action="store_true",
        help="Auto-discover providers from env vars (no config file needed)",
    )

    # --- init ---
    init_p = sub.add_parser("init", help="Generate example config file")
    init_p.add_argument("-o", "--output", help="Output path (default: ~/.aistatus/gateway.yaml)")

    args = parser.parse_args()

    if args.command == "init":
        _do_init(args)
    elif args.command == "start":
        _do_start(args)
    else:
        # No subcommand → default to start
        _do_start(args)


def _do_start(args):
    from . import start

    start(
        config_path=getattr(args, "config", None),
        host=getattr(args, "host", "127.0.0.1"),
        port=getattr(args, "port", 9880),
        auto=getattr(args, "auto", False),
    )


def _do_init(args):
    from pathlib import Path

    from .config import CONFIG_DIR, generate_config

    output = Path(args.output) if args.output else CONFIG_DIR / "gateway.yaml"
    output.parent.mkdir(parents=True, exist_ok=True)
    content = generate_config()
    output.write_text(content, encoding="utf-8")
    print(f"Config written to: {output}")
    print()
    print("Edit the file to configure your API keys, then run:")
    print("  python -m aistatus.gateway start")
    print()
    print("Or use auto-discovery (reads existing env vars):")
    print("  python -m aistatus.gateway start --auto")


if __name__ == "__main__":
    main()
