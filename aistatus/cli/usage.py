from __future__ import annotations

import argparse
from pathlib import Path

from ..usage import UsageTracker


def add_usage_subparser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser("usage", help="Show and export usage statistics")
    parser.add_argument("--period", choices=["week", "month"], default="month")
    parser.add_argument("--all", action="store_true", help="Aggregate all projects")
    parser.add_argument("--by", choices=["provider", "model"], help="Group output")
    parser.add_argument("--export", choices=["csv", "json"], help="Export records")
    parser.add_argument("-o", "--output", help="Export file path")
    parser.set_defaults(func=_handle_usage)


def _handle_usage(args: argparse.Namespace) -> int:
    tracker = UsageTracker()
    period = args.period
    all_projects = args.all

    if args.export:
        if not args.output:
            raise SystemExit("--output is required when using --export")
        output = Path(args.output)
        if args.export == "csv":
            tracker.export_csv(str(output), period=period, all_projects=all_projects)
        else:
            tracker.export_json(str(output), period=period, all_projects=all_projects)
        print(f"Exported usage to {output}")
        return 0

    summary = tracker.summary(period=period, all_projects=all_projects)
    print(_format_summary(summary))

    if args.by == "provider":
        print()
        print(_format_table(tracker.by_provider(period=period, all_projects=all_projects), "provider"))
    elif args.by == "model":
        print()
        print(_format_table(tracker.by_model(period=period, all_projects=all_projects), "model"))

    return 0


def _format_summary(summary: dict) -> str:
    lines = [
        f"Period: {summary['period']}{' (all projects)' if summary['all_projects'] else ''}",
        f"Requests: {summary['requests']}",
        f"Input tokens: {summary['input_tokens']}",
        f"Output tokens: {summary['output_tokens']}",
        f"Cost (USD): {summary['cost_usd']:.8f}",
        f"Avg latency (ms): {summary['avg_latency_ms']}",
        f"Fallback requests: {summary['fallback_requests']}",
    ]
    return "\n".join(lines)


def _format_table(rows: list[dict], group_key: str) -> str:
    if not rows:
        return "No usage records found."

    headers = [group_key, "requests", "input_tokens", "output_tokens", "cost_usd", "avg_latency_ms", "fallback_requests"]
    display_rows = []
    for row in rows:
        display_rows.append([
            str(row.get(group_key, "")),
            str(row.get("requests", 0)),
            str(row.get("input_tokens", 0)),
            str(row.get("output_tokens", 0)),
            f"{float(row.get('cost_usd', 0.0)):.8f}",
            str(row.get("avg_latency_ms", 0)),
            str(row.get("fallback_requests", 0)),
        ])

    widths = [len(header) for header in headers]
    for values in display_rows:
        for idx, value in enumerate(values):
            widths[idx] = max(widths[idx], len(value))

    def fmt_line(values: list[str]) -> str:
        return "  ".join(value.ljust(widths[idx]) for idx, value in enumerate(values))

    lines = [fmt_line(headers), fmt_line(["-" * width for width in widths])]
    lines.extend(fmt_line(values) for values in display_rows)
    return "\n".join(lines)
