"""Command-line entry point for supported HDF5 structural recovery."""

from __future__ import annotations

import argparse
import json
import sys
import textwrap
from collections import Counter
from dataclasses import asdict
from pathlib import Path

from .diagnose import diagnose
from .format import FormatError
from .hints import HintsError, compare_hints, load_hints, require_no_conflicts
from .metadata import UnsupportedCase
from .readable_export import export_readable
from .recovery import RecoveryError, analyze, recover
from .survey import SurveyError, survey


MAX_DISPLAY_DATASETS = 5


def _dimensions(values: list[int] | None) -> str:
    if values is None:
        return "none"
    return " x ".join(map(str, values)) if values else "scalar"


def _display_path(value: str, limit: int = 120) -> str:
    # Escape control characters from untrusted filesystem and HDF5 names in
    # terminal output. JSON mode carries the exact strings for tooling.
    value = json.dumps(str(value), ensure_ascii=True)[1:-1]
    return value if len(value) <= limit else value[:limit - 3] + "..."


def _render_survey(report: dict) -> str:
    datasets = report["datasets"]
    statuses = Counter(item["support"]["status"] for item in datasets)
    lines = [
        "H5Reclaim dataset survey",
        f"Source: {_display_path(report['source']['path'], 240)}",
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
            reasons = "; ".join(_display_path(reason["detail"], 240) for reason in support["reasons"][:2])
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
            lines.append(f"  {_display_path(issue['code'])}: {_display_path(issue['detail'], 240)}")
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
        f"Source: {_display_path(summary['source']['path'], 240)}",
        f"Dataset: {_display_path(dataset['path'])}",
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
    if "operator_hints" in summary:
        lines.append("Operator hints match observed metadata; they do not verify historical measurements.")
    lines.append("Inspection writes no recovered file. It cannot establish historical authenticity.")
    return "\n".join(lines)


def _hint_report(hints, comparisons) -> dict:
    return {
        "trust_level": hints.trust_level,
        "comparisons": [asdict(item) for item in comparisons],
        "note": hints.note,
        "warning": "Matching hints do not prove the origin or historical value of measurements.",
    }


def _render_diagnose(report: dict) -> str:
    signature = report["format_signature"]
    where = (f"HDF5 signature at byte {signature['byte_offset']}"
             if signature["present"] else "No HDF5 signature at a documented location")
    lines = [
        "H5Reclaim diagnosis | no recovery attempted",
        f"Source: {_display_path(report['source']['path'], 240)}",
        f"Format: {where}",
        f"Condition: {report['condition']}",
    ]
    inventory = report["inventory"]
    if inventory is not None:
        counts = inventory["support_counts"]
        lines.append(
            f"Datasets: {inventory['dataset_count']} local | "
            f"{counts['candidate']} structural candidates | "
            f"{counts['unsupported']} unsupported | {counts['indeterminate']} indeterminate"
        )
    selection = report["selection"]
    if selection is not None:
        lines.append(
            f"Selected: {_display_path(selection['selected_path'])} | "
            f"{selection['support']['status']} | {selection.get('layout', 'unknown')}"
        )
    lines.extend([f"Next action: {report['next_action']}", report["detail"]])
    hints = report.get("operator_hints")
    if hints is not None:
        counts = Counter(item["status"] for item in hints["comparisons"])
        lines.append(
            "Operator hints: " + ", ".join(
                f"{counts[status]} {status}" for status in ("matches", "conflicts", "unobserved")
            ) + "; assertions are not proof of measurements"
        )
    lines.append("Questions for further investigation:")
    for item in report["questions"]:
        lines.append(f"  - {item['prompt']}")
    lines.append("For the complete report, rerun with --json.")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="h5reclaim")
    commands = parser.add_subparsers(dest="command", required=True)
    diagnose_cmd = commands.add_parser("diagnose", help="classify a file without reading measurements or attempting recovery")
    diagnose_cmd.add_argument("source", type=Path)
    diagnose_cmd.add_argument("--dataset", help="local dataset path to assess")
    diagnose_cmd.add_argument("--hints", type=Path, help="optional operator claims in bounded JSON, never proof of data")
    diagnose_cmd.add_argument("--json", action="store_true", help="print the complete machine-readable triage report")
    survey_cmd = commands.add_parser("survey", help="inventory local datasets without reading values")
    survey_cmd.add_argument("source", type=Path)
    survey_cmd.add_argument("--json", action="store_true", help="print the complete machine-readable report")
    inspect_cmd = commands.add_parser("inspect", help="inspect the supported dataset and B-tree")
    inspect_cmd.add_argument("source", type=Path)
    inspect_cmd.add_argument("--dataset", required=True)
    inspect_cmd.add_argument("--hints", type=Path, help="optional operator claims checked against observed metadata")
    inspect_cmd.add_argument("--json", action="store_true", help="print the complete machine-readable summary")
    recover_cmd = commands.add_parser("recover", help="export supported chunks to a separate file")
    recover_cmd.add_argument("source", type=Path)
    recover_cmd.add_argument("--dataset", help="selected dataset path (or provide it in --hints)")
    recover_cmd.add_argument("--hints", type=Path, help="optional operator claims checked before output")
    recover_cmd.add_argument("--output", required=True, type=Path)
    recover_cmd.add_argument("--report", required=True, type=Path)
    export_cmd = commands.add_parser(
        "export-readable", help="copy a fully native-readable numeric dataset without claiming structural repair"
    )
    export_cmd.add_argument("source", type=Path)
    export_cmd.add_argument("--dataset", help="selected dataset path (or provide it in --hints)")
    export_cmd.add_argument("--hints", type=Path, help="optional operator claims checked before output")
    export_cmd.add_argument("--output", required=True, type=Path)
    export_cmd.add_argument("--report", required=True, type=Path)
    args = parser.parse_args(argv)

    if args.command == "diagnose":
        try:
            hints = load_hints(args.hints) if args.hints else None
            selected_path = args.dataset or (hints.path if hints else None)
            report = diagnose(args.source, selected_path)
            if hints is not None:
                selection = report["selection"]
                observed = None
                if selection is not None:
                    observed = {
                        "path": selected_path,
                        "shape": selection.get("shape"),
                        "chunks": selection.get("chunks"),
                        "filters": [item["id"] for item in selection["filters"]]
                        if selection.get("filters") is not None else None,
                    }
                comparisons = compare_hints(
                    hints, observed_fields=observed, input_sha256=report["source"]["sha256"],
                )
                report["operator_hints"] = _hint_report(hints, comparisons)
                if any(item.status == "conflicts" for item in comparisons):
                    report["outcome"] = "limited"
                    report["next_action"] = "resolve_hint_conflicts"
                    report["detail"] = "Operator claims conflict with observed file evidence; no values were read or recovered."
            print(json.dumps(report, indent=2, sort_keys=True) if args.json else _render_diagnose(report))
            return 0 if report["outcome"] == "triaged" else 1
        except (HintsError, SurveyError, RecoveryError, OSError, ValueError, RuntimeError, KeyError, TypeError) as exc:
            if args.json:
                print(json.dumps({
                    "schema_version": 1, "outcome": "error", "recovery_attempted": False,
                    "source": {"path": str(args.source)},
                    "error": {"code": "diagnose_failed", "detail": str(exc)[:300]},
                }, sort_keys=True))
            else:
                print(f"h5reclaim: diagnosis failed: {_display_path(exc, 300)}", file=sys.stderr)
            return 2

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
                print(f"h5reclaim: survey failed: {_display_path(exc, 300)}", file=sys.stderr)
            return 2

    if args.command in ("inspect", "recover", "export-readable"):
        hints = None
        try:
            hints = load_hints(args.hints) if args.hints else None
        except (HintsError, OSError) as exc:
            if args.command == "inspect" and args.json:
                print(json.dumps({
                    "schema_version": 1, "outcome": "error",
                    "source": {"path": str(args.source)},
                    "dataset": {"path": args.dataset},
                    "error": {"code": "invalid_hints", "detail": str(exc)[:300]},
                }, sort_keys=True))
            else:
                print(f"h5reclaim: invalid hints: {_display_path(exc, 300)}", file=sys.stderr)
            return 2
        if args.command in ("recover", "export-readable") and not args.dataset:
            if hints is None:
                parser.error(f"{args.command} requires --dataset or --hints with a dataset path")
            args.dataset = hints.path
    try:
        if args.command == "inspect":
            analysis = analyze(args.source, args.dataset)
            report = analysis.report
            summary = {
                key: report[key]
                for key in ("outcome", "complete", "source", "dataset", "index", "counts")
            }
            summary["reconstructed_chunks"] = report["reconstructed_chunks"]
            summary["unresolved_links"] = report["unresolved_links"]
            if hints is not None:
                comparisons = compare_hints(
                    hints, observed_dataset=analysis.spec,
                    input_sha256=report["source"]["sha256_before"],
                )
                require_no_conflicts(comparisons)
                summary["operator_hints"] = _hint_report(hints, comparisons)
            print(json.dumps(summary, indent=2, sort_keys=True) if args.json else _render_inspect(summary))
        elif args.command == "recover":
            report = recover(args.source, args.dataset, args.output, args.report, hints=hints)
            print(
                f"{report['outcome']}: {report['counts']['recovered']} chunks exported; "
                f"{report['reconstructed_chunks']} via reconstructed index links"
            )
            print(f"output: {_display_path(args.output, 240)}\nreport: {_display_path(args.report, 240)}")
        else:
            report = export_readable(args.source, args.dataset, args.output, args.report, hints=hints)
            print(
                f"readable export: {report['dataset']['logical_bytes']} logical bytes "
                f"copied through native HDF5 in {report['blocks_verified']} blocks"
            )
            print("No damaged index was reconstructed; historical measurement values are not verified.")
            print(f"output: {_display_path(args.output, 240)}\nreport: {_display_path(args.report, 240)}")
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
            print(f"h5reclaim: {_display_path(exc, 300)}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
