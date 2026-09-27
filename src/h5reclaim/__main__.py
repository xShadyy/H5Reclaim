"""Command-line entry point for supported HDF5 structural recovery."""

from __future__ import annotations

import argparse
import json
import sys
import textwrap
from collections import Counter
from pathlib import Path

from .format import FormatError
from .metadata import UnsupportedCase
from .recovery import RecoveryError, analyze, recover
from .survey import SurveyError, survey


MAX_DISPLAY_DATASETS = 5


def _dimensions(values: list[int] | None) -> str:
    if values is None:
        return "none"
    return " x ".join(map(str, values)) if values else "scalar"


def _display_path(value: str, limit: int = 120) -> str:
    # Path names can be thousands of characters; the JSON mode keeps them intact.
    return value if len(value) <= limit else value[:limit - 3] + "..."


def _render_survey(report: dict) -> str:
    datasets = report["datasets"]
    statuses = Counter(item["support"]["status"] for item in datasets)
    lines = [
        "H5Reclaim dataset survey",
        f"Source: {report['source']['path']}",
        f"Inventory: {report['outcome']} | {report['dataset_count']} local datasets",
        "Support: " + " | ".join(
            f"{statuses[status]} {status}"
            for status in ("candidate", "unsupported", "indeterminate")
        ),
    ]
    skipped = report["skipped"]
    if any(skipped.values()):
        lines.append("Skipped: " + ", ".join(
            f"{count} {kind.replace('_', ' ')}"
            for kind, count in skipped.items() if count
        ))

    # Show candidates first; sample distinct unsupported structures before
    # repeating the same scalar metadata shape. JSON contains every entry.
    ordered = []
    for status in ("candidate", "indeterminate", "unsupported"):
        group = [item for item in datasets if item["support"]["status"] == status]
        distinct, repeated, seen = [], [], set()
        for item in group:
            dtype = str(item.get("dtype", "unknown"))
            if dtype.startswith(("|S", "|U")):
                dtype = dtype[:2]
            signature = (
                item.get("layout"), item.get("rank"), dtype,
                tuple(reason["code"] for reason in item["support"]["reasons"]),
            )
            (repeated if signature in seen else distinct).append(item)
            seen.add(signature)
        ordered.extend(distinct)
        ordered.extend(repeated)
    lines.append("")
    for item in ordered[:MAX_DISPLAY_DATASETS]:
        support = item["support"]
        lines.append(f"[{support['status'].upper()}] {_display_path(item['selected_path'])}")
        lines.append(
            f"  shape {_dimensions(item.get('shape'))} | "
            f"dtype {item.get('dtype', 'unknown')} | "
            f"{item.get('layout', 'unknown')} | "
            f"chunks {_dimensions(item.get('chunks'))}"
        )
        if support["reasons"]:
            reasons = "; ".join(reason["detail"] for reason in support["reasons"][:2])
            if len(support["reasons"]) > 2:
                reasons += f"; {len(support['reasons']) - 2} more reasons in --json"
            lines.append(textwrap.fill(
                reasons, width=96, initial_indent="  Reason: ",
                subsequent_indent="          ", break_long_words=True,
            ))
    hidden = len(ordered) - min(len(ordered), MAX_DISPLAY_DATASETS)
    if hidden:
        lines.append(f"... {hidden} more datasets. Use --json for every entry and reason.")
        frequent_reasons = Counter(
            reason["code"] for item in datasets for reason in item["support"]["reasons"]
        )
        if frequent_reasons:
            lines.append("Common reasons: " + ", ".join(
                f"{code} ({count})" for code, count in frequent_reasons.most_common(5)
            ))
    if not datasets:
        lines.append("No local hard-linked datasets found.")
    if report["issues"]:
        lines.append("")
        lines.append("Inventory issues:")
        for issue in report["issues"][:5]:
            lines.append(f"  {issue['code']}: {issue['detail']}")
        if len(report["issues"]) > 5:
            lines.append(f"  ... {len(report['issues']) - 5} more issues; see --json.")
    lines.extend([
        "",
        "Candidate means the metadata fits this release; recovery has not been tested by this survey.",
        "Use h5reclaim inspect SOURCE --dataset PATH to check a candidate's chunks.",
    ])
    return "\n".join(lines)


def _render_inspect(summary: dict) -> str:
    dataset, index, counts = summary["dataset"], summary["index"], summary["counts"]
    total = sum(counts.values())
    filter_names = {3: "Fletcher32", 1: "DEFLATE"}
    filters = ", ".join(filter_names.get(item, f"filter {item}")
                        for item in dataset["filters"]) or "none"
    lines = [
        "H5Reclaim inspection",
        f"Source: {summary['source']['path']}",
        f"Dataset: {dataset['path']}",
        f"Shape: {_dimensions(dataset['shape'])} | dtype {dataset['dtype']} | "
        f"chunks {_dimensions(dataset['chunks'])} | filters {filters}",
        f"Index: {index['type']} | root level {index['root_level']} | "
        f"{index['reachable_leaves']} reachable leaves | "
        f"{index['broken_links']} broken links",
        f"Result: {summary['outcome']} | {counts['recovered']}/{total} chunks accepted "
        f"({summary['reconstructed_chunks']} via reconstructed link)",
    ]
    remaining = [f"{count} {status.replace('_', ' ')}" for status, count in counts.items()
                 if status != "recovered" and count]
    if remaining:
        lines.append("Other chunk statuses: " + ", ".join(remaining))
    if summary["unresolved_links"]:
        lines.append(f"Unresolved links: {len(summary['unresolved_links'])}")
    lines.append("Inspection writes no recovered file. It cannot establish historical authenticity.")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="h5reclaim")
    commands = parser.add_subparsers(dest="command", required=True)
    survey_cmd = commands.add_parser("survey", help="inventory local datasets without reading values")
    survey_cmd.add_argument("source", type=Path)
    survey_cmd.add_argument("--json", action="store_true", help="print the complete machine-readable report")
    inspect_cmd = commands.add_parser("inspect", help="inspect the supported dataset and B-tree")
    inspect_cmd.add_argument("source", type=Path)
    inspect_cmd.add_argument("--dataset", required=True)
    inspect_cmd.add_argument("--json", action="store_true", help="print the complete machine-readable summary")
    recover_cmd = commands.add_parser("recover", help="export supported chunks to a separate file")
    recover_cmd.add_argument("source", type=Path)
    recover_cmd.add_argument("--dataset", required=True)
    recover_cmd.add_argument("--output", required=True, type=Path)
    recover_cmd.add_argument("--report", required=True, type=Path)
    args = parser.parse_args(argv)

    if args.command == "survey":
        try:
            report = survey(args.source)
            print(json.dumps(report, indent=2, sort_keys=True) if args.json else _render_survey(report))
            return 0 if report["outcome"] == "complete" else 1
        except (SurveyError, OSError, ValueError, RuntimeError, KeyError, TypeError) as exc:
            if args.json:
                print(json.dumps({
                    "schema_version": 1,
                    "outcome": "error",
                    "source": {"path": str(args.source)},
                    "error": {"code": "survey_failed", "detail": str(exc)[:300]},
                }, sort_keys=True))
            else:
                print(f"h5reclaim: survey failed: {exc}", file=sys.stderr)
            return 2

    try:
        if args.command == "inspect":
            report = analyze(args.source, args.dataset).report
            summary = {
                key: report[key]
                for key in ("outcome", "complete", "source", "dataset", "index", "counts")
            }
            summary["reconstructed_chunks"] = report["reconstructed_chunks"]
            summary["unresolved_links"] = report["unresolved_links"]
            print(json.dumps(summary, indent=2, sort_keys=True) if args.json else _render_inspect(summary))
        else:
            report = recover(args.source, args.dataset, args.output, args.report)
            print(
                f"{report['outcome']}: {report['counts']['recovered']} chunks exported; "
                f"{report['reconstructed_chunks']} via the broken-link path"
            )
            print(f"output: {args.output}\nreport: {args.report}")
        return 0
    except (
        FormatError, UnsupportedCase, RecoveryError, OSError, ValueError,
        RuntimeError, KeyError, TypeError,
    ) as exc:
        if args.command == "inspect" and args.json:
            print(json.dumps({
                "schema_version": 1,
                "outcome": "error",
                "source": {"path": str(args.source)},
                "dataset": {"path": args.dataset},
                "error": {"code": "inspect_failed", "detail": str(exc)[:300]},
            }, sort_keys=True))
        else:
            print(f"h5reclaim: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
