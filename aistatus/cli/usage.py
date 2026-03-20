from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from ..usage import UsageTracker


def add_usage_subparser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser("usage", help="Show and export usage statistics")
    parser.add_argument("--period", choices=["week", "month", "all"], default="month")
    parser.add_argument("--all", action="store_true", help="Aggregate all projects")
    parser.add_argument("--by", choices=["provider", "model"], help="Append grouped detail output")
    parser.add_argument("--format", choices=["human", "json"], default="human", help="Output format")
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
    by_provider = tracker.by_provider(period=period, all_projects=all_projects)
    by_model = tracker.by_model(period=period, all_projects=all_projects)

    if args.format == "json":
        payload = {
            "period": period,
            "project": None if all_projects else str(Path.cwd()),
            "summary": {
                "requests": summary["requests"],
                "input_tokens": summary["input_tokens"],
                "output_tokens": summary["output_tokens"],
                "cost_usd": summary["cost_usd"],
                "fallback_requests": summary["fallback_requests"],
                "avg_latency_ms": summary["avg_latency_ms"],
            },
            "by_provider": by_provider,
            "by_model": by_model,
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    print(_format_human_output(summary, by_provider, by_model, args.by))
    return 0


def _format_human_output(
    summary: dict[str, Any],
    by_provider: list[dict[str, Any]],
    by_model: list[dict[str, Any]],
    by: str | None,
) -> str:
    sections = [_format_summary(summary, by_provider)]
    if by == "provider":
        sections.append(_format_detail_table(by_provider, "provider", "By Provider"))
    elif by == "model":
        sections.append(_format_detail_table(by_model, "model", "By Model"))
    return "\n\n".join(section for section in sections if section)


def _format_summary(summary: dict[str, Any], by_provider: list[dict[str, Any]]) -> str:
    title = _format_title(summary["period"])
    table = _format_compact_table(by_provider, "provider", summary)
    fallback_requests = int(summary.get("fallback_requests", 0) or 0)
    requests = int(summary.get("requests", 0) or 0)
    fallback_rate = (fallback_requests / requests * 100) if requests else 0.0
    avg_latency = _format_number(summary.get("avg_latency_ms", 0))
    return "\n".join([
        title,
        "",
        table,
        "",
        f"Fallback requests: {fallback_requests} ({fallback_rate:.1f}%)",
        f"Avg latency: {avg_latency} ms",
    ])


def _format_detail_table(rows: list[dict[str, Any]], group_key: str, title: str) -> str:
    if not rows:
        return f"{title}\n\nNo usage records found."

    headers = [
        _group_header(group_key),
        "Input Tokens",
        "Output Tokens",
        "Calls",
        "Cost",
        "Avg Latency",
        "Fallback",
    ]
    display_rows = [
        [
            str(row.get(group_key, "")),
            _format_int(row.get("input_tokens", 0)),
            _format_int(row.get("output_tokens", 0)),
            _format_int(row.get("requests", 0)),
            _format_cost(row.get("cost_usd", 0.0)),
            f"{_format_number(row.get('avg_latency_ms', 0))} ms",
            _format_int(row.get("fallback_requests", 0)),
        ]
        for row in rows
    ]
    return "\n".join([
        title,
        "",
        _render_table(headers, display_rows, align_right={1, 2, 3, 4, 5, 6}),
    ])


def _format_compact_table(rows: list[dict[str, Any]], group_key: str, summary: dict[str, Any]) -> str:
    headers = [_group_header(group_key), "Input Tokens", "Output Tokens", "Calls", "Cost"]
    display_rows = [
        [
            str(row.get(group_key, "")),
            _format_int(row.get("input_tokens", 0)),
            _format_int(row.get("output_tokens", 0)),
            _format_int(row.get("requests", 0)),
            _format_cost(row.get("cost_usd", 0.0)),
        ]
        for row in rows
    ]
    display_rows.append([
        "Total",
        _format_int(summary.get("input_tokens", 0)),
        _format_int(summary.get("output_tokens", 0)),
        _format_int(summary.get("requests", 0)),
        _format_cost(summary.get("cost_usd", 0.0)),
    ])
    return _render_table(headers, display_rows, align_right={1, 2, 3, 4}, separator_before_last=True)


def _render_table(
    headers: list[str],
    rows: list[list[str]],
    *,
    align_right: set[int] | None = None,
    separator_before_last: bool = False,
) -> str:
    align_right = align_right or set()
    widths = [len(header) for header in headers]
    for row in rows:
        for idx, value in enumerate(row):
            widths[idx] = max(widths[idx], len(value))

    def format_row(values: list[str]) -> str:
        cells = []
        for idx, value in enumerate(values):
            cell = value.rjust(widths[idx]) if idx in align_right else value.ljust(widths[idx])
            cells.append(cell)
        return " | ".join(cells)

    separator = "-| -".join("-" * width for width in widths)
    lines = [format_row(headers), separator]
    for idx, row in enumerate(rows):
        if separator_before_last and idx == len(rows) - 1:
            lines.append(separator)
        lines.append(format_row(row))
    return "\n".join(lines)


def _format_title(period: str) -> str:
    now = datetime.now()
    if period == "week":
        start = now - timedelta(days=7)
        return f"AI Usage — This Week ({_format_date(start)} - {_format_date(now, include_year=True)})"
    if period == "month":
        start = now - timedelta(days=30)
        return f"AI Usage — This Month ({_format_date(start)} - {_format_date(now, include_year=True)})"
    return "AI Usage — All Time"


def _format_date(value: datetime, include_year: bool = False) -> str:
    if include_year:
        return f"{value.strftime('%b')} {value.day}, {value.year}"
    return f"{value.strftime('%b')} {value.day}"


def _group_header(group_key: str) -> str:
    return "Provider" if group_key == "provider" else "Model"


def _format_int(value: Any) -> str:
    return f"{int(value or 0):,}"


def _format_number(value: Any) -> str:
    number = float(value or 0)
    if number.is_integer():
        return f"{int(number):,}"
    return f"{number:,.2f}".rstrip("0").rstrip(".")


def _format_cost(value: Any) -> str:
    amount = float(value or 0.0)
    absolute = abs(amount)
    if absolute >= 0.01:
        precision = 2
    elif absolute >= 0.001:
        precision = 4
    else:
        precision = 6
    return f"${amount:.{precision}f}"
