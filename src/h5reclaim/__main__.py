"""Command-line entry point for supported HDF5 structural recovery."""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
import textwrap
from collections import Counter
from dataclasses import asdict
from pathlib import Path

from .baseline import capture_baseline
from .diagnose import diagnose
from .dependency_routes import (
    DependencyError, inspect_dependencies, load_dependency_manifest, probe_status_copy,
    validate_dependency_manifest,
)
from .evidence import export_unassigned_fragments, load_evidence_report
from .format import FormatError
from .hints import HintsError, compare_hints, load_hints, require_no_conflicts
from .metadata import UnsupportedCase
from .readable_export import export_readable
from .recovery import VERSION, RecoveryError, analyze, recover
from .route_worker import run_route
from .survey import SurveyError, survey


MAX_DISPLAY_DATASETS = 5


def _dataset_path(value: str) -> str:
    if (not value.startswith("/") or value == "/" or "\x00" in value
            or any(part in ("", ".", "..") for part in value[1:].split("/"))):
        raise argparse.ArgumentTypeError("use an absolute HDF5 dataset path, such as /experiment/readings")
    return value


def _dimensions(values: list[int] | None) -> str:
    if values is None:
        return "none"
    return " x ".join(map(str, values)) if values else "scalar"


def _display_path(value: str, limit: int = 120) -> str:
    # Escape control characters from untrusted filesystem and HDF5 names in
    # terminal output. JSON mode carries the exact strings for tooling.
    value = json.dumps(str(value), ensure_ascii=True)[1:-1]
    return value if len(value) <= limit else value[:limit - 3] + "..."


def _coverage(report: dict) -> tuple[int, int, str]:
    """Keep element counts distinct from chunk counts in terminal summaries."""
    accepted = report.get("accepted_elements")
    if accepted is not None:
        return accepted, report.get("unknown_elements", 0), "elements"
    counts = report.get("counts", {})
    validity = report.get("validity") or {}
    unit = "elements" if (validity.get("granularity") == "element"
                           or str(validity.get("dataset", "")).endswith("/element_status")) else "chunks"
    return counts.get("recovered", 0), sum(value for name, value in counts.items()
                                          if name != "recovered"), unit


def _status_maps(report: dict) -> list[str]:
    """Use report paths, including rebased or collision-safe metadata groups."""
    validity = report.get("validity") or {}
    paths = [validity.get("dataset"), validity.get("chunk_status"), validity.get("element_status"),
             report.get("validity_map"), report.get("element_status")]
    if not any(paths) and report.get("counts") is not None:
        paths.append(report.get("metadata_group", "/_h5reclaim") + "/chunk_status")
    history = report.get("historical_integrity") or {}
    paths.append(history.get("status_dataset"))
    paths.append(history.get("prior_capture_match_status_dataset"))
    return list(dict.fromkeys(path for path in paths if path))


def _render_report(report: dict) -> str:
    """Summarize an existing evidence report without opening its source or output."""
    lines = [
        f"H5Reclaim evidence summary | {report['outcome']}",
        f"Source: {_display_path(report['source']['path'], 240)}",
        f"Output: {_display_path(report['output_path'], 240)}",
    ]
    if report.get("mode") == "whole_file_recovery":
        lines.append(f"Datasets: {report['datasets_exported']}/{report['datasets_discovered']} exported | "
                     f"{report['datasets_failed']} failed")
        records = sorted(report["datasets"], key=lambda item: item["report"]["outcome"] == "complete")
    else:
        records = [{"path": report["dataset"]["path"], "report": report}]
    for item in records[:MAX_DISPLAY_DATASETS]:
        selected = item["report"]
        accepted, unknown, unit = _coverage(selected)
        lines.append(f"[{selected['outcome'].upper()}] {_display_path(item['path'])} | "
                     f"{accepted} accepted {unit} | {unknown} unknown {unit}")
        lines.extend(f"  Status map: {_display_path(path, 240)}" for path in _status_maps(selected))
    if len(records) > MAX_DISPLAY_DATASETS:
        lines.append(f"... {len(records) - MAX_DISPLAY_DATASETS} more datasets in the JSON report.")
    failures = report.get("failures", [])
    for failure in failures[:MAX_DISPLAY_DATASETS]:
        lines.append(f"Unresolved: {_display_path(failure['path'])}: {_display_path(failure['reason'], 240)}")
    if len(failures) > MAX_DISPLAY_DATASETS:
        lines.append(f"... {len(failures) - MAX_DISPLAY_DATASETS} more failures in the JSON report.")
    context = report.get("scientific_context") or {}
    omitted = sum(len(item.get("attributes_omitted", [])) for section in ("groups", "datasets", "named_types")
                  for item in context.get(section, []))
    if omitted or context.get("issues") or context.get("scales_omitted"):
        lines.append(f"Context: {omitted} omitted attributes | {len(context.get('issues', []))} issues | "
                     f"{len(context.get('scales_omitted', []))} omitted scales; see scientific_context in the report.")
    inventory = report.get("inventory") or {}
    if inventory.get("issues") or inventory.get("skipped"):
        lines.append("Inventory is limited; see inventory.issues and inventory.skipped in the report.")
    if report["outcome"] != "complete":
        lines.append("Partial export: some values or scientific context remain unresolved. Check the status maps and report.")
    integrity = report.get("historical_integrity") or {}
    if integrity.get("capture_sha256"):
        lines.append(f"History: {integrity['matching_units']} units match the supplied prior capture; "
                     f"{integrity['unknown_units']} unknown.")
    else:
        lines.append("History: unverified; accepted values describe the available source, not proven prior measurements.")
    lines.append("This summary reads the evidence report; it does not revalidate output files.")
    return "\n".join(lines)


def _render_survey(report: dict) -> str:
    datasets = report["datasets"]
    statuses = Counter(item["support"]["status"] for item in datasets)
    lines = [
        "H5Reclaim dataset survey",
        f"Source: {_display_path(report['source']['path'], 240)}",
        f"Inventory: {report['outcome']} | {report['dataset_count']} local datasets",
        "Structural support: " + " | ".join(
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
        "These support labels assess structural parsers only; rescue also uses native-readable and streaming routes.",
        "Candidate means the metadata fits a structural route; recovery has not been tested by this survey.",
        "Use h5reclaim inspect SOURCE --dataset PATH to check a candidate's chunks.",
        "Use h5reclaim rescue SOURCE to try automatic recovery, including datasets labeled unsupported here.",
    ])
    return "\n".join(lines)


def _render_inspect(summary: dict) -> str:
    dataset, index, counts = summary["dataset"], summary["index"], summary["counts"]
    total = sum(counts.values())
    filter_names = {3: "Fletcher32", 2: "shuffle", 1: "DEFLATE", 32000: "LZF"}
    filters = ", ".join(filter_names.get(item, f"filter {item}")
                        for item in dataset["filters"]) or "none"
    if index.get("root_level") is None:
        index_text = (
            f"Index: {index['type']} | layout version {index.get('layout_version', 'unknown')} | "
            "intact anchored structure"
        )
    else:
        index_text = (
            f"Index: {index['type']} | root level {index['root_level']} | "
            f"{index['reachable_leaves']} reachable leaves | {index['broken_links']} broken links"
        )
    lines = [
        "H5Reclaim inspection",
        f"Source: {_display_path(summary['source']['path'], 240)}",
        f"Dataset: {_display_path(dataset['path'])}",
        f"Shape: {_dimensions(dataset['shape'])} | dtype {dataset['dtype']} | "
        f"chunks {_dimensions(dataset['chunks'])} | filters {filters}",
        index_text,
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
    if "metadata_resolution" in summary:
        lines.append(
            "Metadata path: " + summary["metadata_resolution"]["route"]
            + " (rooted raw fallback; see report for limits)"
        )
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
            f"{counts['unsupported']} structurally unsupported | {counts['indeterminate']} indeterminate"
        )
    selection = report["selection"]
    if selection is not None:
        lines.append(
            f"Selected: {_display_path(selection['selected_path'])} | "
            f"structural parser {selection['support']['status']} | {selection.get('layout', 'unknown')}"
        )
    status = report.get("file_status")
    if status and status["outcome"] == "observed":
        lines.append(
            f"Superblock: version {status['superblock_version']} | "
            f"status {status['status_flags']['interpretation']} | "
            f"EOA {status['end_of_address']['relation']} (raw, checksum unvalidated)"
        )
    dependencies = report.get("dependencies")
    if dependencies and dependencies["dependencies"]:
        lines.append(
            f"Dependencies: {len(dependencies['dependencies'])} declared "
            f"({dependencies['outcome']}); referenced files have not been opened"
        )
    validation = report.get("dependency_validation")
    if validation is not None:
        counts = Counter(item["status"] for item in validation["references"])
        lines.append("Related files: " + ", ".join(
            f"{count} {status.replace('_', ' ')}" for status, count in sorted(counts.items())
        ))
        lines.append("File hash matches do not verify historical measurements or VDS coverage.")
    lines.extend([f"Next action: {report['next_action']}", report["detail"]])
    if inventory is not None:
        lines.append("Support labels assess structural parsers only; rescue also tries native-readable and streaming routes.")
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
    parser = argparse.ArgumentParser(
        prog="h5reclaim", formatter_class=argparse.RawDescriptionHelpFormatter,
        description=(
            "Recover HDF5 data into a new file with a JSON evidence report.\n\n"
            "Start here:\n"
            "  h5reclaim rescue damaged.h5\n"
            "  h5reclaim diagnose damaged.h5\n"
            "  h5reclaim report damaged.recovered.report.json\n\n"
            "Rescue discovers all datasets by default and never overwrites the source.\n"
            "Use rescue --help for destinations, dataset selection and advanced options."
        ),
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    commands = parser.add_subparsers(dest="command", required=True, metavar="COMMAND", title="commands")
    report_cmd = commands.add_parser("report", help="summarize an existing evidence JSON report and its status-map paths")
    report_cmd.add_argument("report", type=Path, help="evidence JSON produced by rescue, recover or export-readable")
    manifest_cmd = commands.add_parser("related-manifest", help="find and hash declared companion files in a supplied directory")
    manifest_cmd.add_argument("source", type=Path)
    manifest_cmd.add_argument("--directory", required=True, type=Path)
    manifest_cmd.add_argument("--output", required=True, type=Path)
    manifest_cmd.add_argument("--streaming-budget", type=Path)
    discover_cmd = commands.add_parser("discover", help="find surviving legacy and modern dataset headers despite damaged group links")
    discover_cmd.add_argument("source", type=Path)
    discover_cmd.add_argument("--json", action="store_true", help="print complete object-address discovery metadata")
    discover_cmd.add_argument("--streaming-budget", type=Path, help="JSON source, scan and time budgets")
    diagnose_cmd = commands.add_parser(
        "diagnose", help="check file condition and structural support without attempting recovery",
        description="Classify file condition without reading measurements. Support labels cover structural parsers; rescue also uses other routes.",
    )
    diagnose_cmd.add_argument("source", type=Path)
    diagnose_cmd.add_argument("--dataset", help="local dataset path to assess")
    diagnose_cmd.add_argument("--hints", type=Path, help="optional operator claims in bounded JSON, never proof of data")
    diagnose_cmd.add_argument(
        "--related-files", type=Path,
        help="bounded JSON mapping exact declared names to explicit absolute paths and SHA-256 hashes",
    )
    diagnose_cmd.add_argument("--json", action="store_true", help="print the complete machine-readable triage report")
    status_cmd = commands.add_parser(
        "probe-status", help="try h5clear --status only on a disposable copy of a version-3 write-flagged file"
    )
    status_cmd.add_argument("source", type=Path)
    status_cmd.add_argument("--json", action="store_true", help="print the complete machine-readable probe report")
    fragments_cmd = commands.add_parser(
        "export-fragments", help="export anchored but unresolved raw extents from a report as coordinate-free bytes"
    )
    fragments_cmd.add_argument("report", type=Path, help="recovery report containing the evidence ledger")
    fragments_cmd.add_argument("source", type=Path, help="explicit damaged source; its SHA-256 must match the ledger")
    fragments_cmd.add_argument("--output", required=True, type=Path, help="new raw-fragment ZIP destination")
    survey_cmd = commands.add_parser(
        "survey", help="inventory datasets and structural-parser support without reading values",
        description="List local datasets without reading values. Unsupported means outside the structural parsers; rescue may still export native-readable values.",
    )
    survey_cmd.add_argument("source", type=Path)
    survey_cmd.add_argument("--json", action="store_true", help="print the complete machine-readable report")
    inspect_cmd = commands.add_parser("inspect", help="inspect a supported dataset and chunk index")
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
        "export-readable", help="copy bounded currently readable local values with explicit sparse validity"
    )
    export_cmd.add_argument("source", type=Path)
    export_cmd.add_argument("--dataset", help="selected dataset path (or provide it in --hints)")
    export_cmd.add_argument("--hints", type=Path, help="optional operator claims checked before output")
    export_cmd.add_argument("--output", required=True, type=Path)
    export_cmd.add_argument("--report", required=True, type=Path)
    baseline_cmd = commands.add_parser(
        "capture-baseline",
        help="record decoded chunk hashes from a complete intact acquisition for later independent comparison",
    )
    baseline_cmd.add_argument("source", type=Path)
    baseline_cmd.add_argument("--dataset", required=True)
    baseline_cmd.add_argument("--output", required=True, type=Path, help="new baseline JSON destination, stored separately")
    element_cmd = commands.add_parser(
        "capture-element-baseline",
        help="record prospective per-element hashes for a complete rooted compact/contiguous numeric dataset",
    )
    element_cmd.add_argument("source", type=Path)
    element_cmd.add_argument("--dataset", required=True)
    element_cmd.add_argument("--output", required=True, type=Path, help="new element baseline ZIP destination")
    parity_cmd = commands.add_parser(
        "capture-parity", help="record prospective XOR parity after a complete prior chunk-hash baseline",
    )
    parity_cmd.add_argument("source", type=Path)
    parity_cmd.add_argument("--dataset", required=True)
    parity_cmd.add_argument("--baseline", required=True, type=Path)
    parity_cmd.add_argument("--output", required=True, type=Path, help="new parity ZIP destination, stored independently")
    parity_cmd.add_argument("--stripe-width", type=int, default=4, help="1 to 16 chunks per stripe (default: 4)")
    erasure_cmd = commands.add_parser(
        "capture-erasure", help="capture two to four independent parity shards per stripe before damage"
    )
    erasure_cmd.add_argument("source", type=Path)
    erasure_cmd.add_argument("--dataset", required=True)
    erasure_cmd.add_argument("--baseline", required=True, type=Path, help="prior complete coordinate-hash baseline")
    erasure_cmd.add_argument("--output", required=True, type=Path, help="new independently retained erasure ZIP")
    erasure_cmd.add_argument("--stripe-width", type=int, default=4, help="2 through 16 data chunks per stripe")
    erasure_cmd.add_argument("--parity-shards", type=int, default=2, help="2 through 4 parity shards per stripe")
    capsule_cmd = commands.add_parser(
        "capture-capsule", help="capture a prospective independent schema, physical map, and block hashes"
    )
    capsule_cmd.add_argument("source", type=Path)
    capsule_cmd.add_argument("--dataset", required=True)
    capsule_cmd.add_argument("--output", required=True, type=Path, help="new independently retained recovery capsule")
    protect_cmd = commands.add_parser(
        "protect", help="capture one prospective ZIP with a schema capsule and optional erasure shards"
    )
    protect_cmd.add_argument("source", type=Path, help="intact acquisition to protect before an incident")
    protect_cmd.add_argument("--dataset", required=True)
    protect_cmd.add_argument("--output", required=True, type=Path, help="new independent protection ZIP")
    protect_cmd.add_argument("--capsule-only", action="store_true",
                             help="omit the complete numeric baseline and erasure sidecar")
    protect_cmd.add_argument("--stripe-width", type=int, default=4)
    protect_cmd.add_argument("--parity-shards", type=int, default=2)
    verify_cmd = commands.add_parser("verify-protection", help="verify a prior protection ZIP and its separately retained manifest digest")
    verify_cmd.add_argument("bundle", type=Path)
    verify_cmd.add_argument("--manifest-sha256", required=True)
    verify_cmd.add_argument("--source", type=Path, help="also compare the current intact acquisition to its capture hash")
    drill_cmd = commands.add_parser("drill-protection", help="test capsule and retained parity on disposable damaged copies of an intact acquisition")
    drill_cmd.add_argument("bundle", type=Path)
    drill_cmd.add_argument("source", type=Path)
    drill_cmd.add_argument("--manifest-sha256", required=True)
    rescue_cmd = commands.add_parser(
        "rescue", help="recover one dataset or discover and recover the whole file",
        description="Discover and recover all datasets, or select one with --dataset. The source stays unchanged.",
        epilog="Exit status: 0 means an export was created; --fail-on-partial returns 1 for a partial export; errors return 2.",
    )
    rescue_cmd.add_argument("source", type=Path, help="damaged HDF5 file, or member zero for a Family bundle")
    selection = rescue_cmd.add_mutually_exclusive_group()
    selection.add_argument("--dataset", type=_dataset_path, help="absolute selected HDF5 dataset path")
    selection.add_argument("--all", action="store_true", help="recover all discovered local datasets (the default without --dataset)")
    rescue_cmd.add_argument("--output", type=Path,
                            help="new HDF5 destination (default: SOURCE stem + .recovered.h5, beside the source)")
    rescue_cmd.add_argument("--report", type=Path,
                            help="new JSON evidence destination (default: OUTPUT stem + .report.json)")
    rescue_cmd.add_argument("--fail-on-partial", action="store_true",
                            help="return exit status 1 when the reported export outcome is partial")
    rescue_cmd.add_argument("--strict-history", action="store_true",
                            help="publish only when a separately pinned prior capture verifies every accepted unit")
    rescue_cmd.add_argument("--no-context-audit", action="store_true",
                            help="skip the bounded audit of omitted attributes, units, scales, groups, and links")
    rescue_cmd.add_argument("--streaming-budget", type=Path,
                            help="JSON object configuring source, copy, output, chunk and time budgets for streaming")
    rescue_cmd.add_argument("--resume-dir", type=Path,
                            help="reuse completed datasets and native chunks or contiguous blocks after interruption")
    rescue_cmd.add_argument("--hints", type=Path,
                            help="expected dataset shape/type JSON, checked against observed metadata")
    choices = rescue_cmd.add_mutually_exclusive_group()
    choices.add_argument("--object-address", type=lambda value: int(value, 0),
                         help="recover a checked detached legacy or modern dataset header at this address")
    choices.add_argument("--related-files", type=Path, help="pinned manifest for external raw or virtual datasets")
    choices.add_argument("--related-dir", type=Path, help="find and hash declared companion files inside this directory")
    choices.add_argument("--family-members", type=Path, help="pinned HDF5 Family member manifest")
    choices.add_argument("--split-members", type=Path, help="pinned HDF5 Split metadata and raw member manifest")
    choices.add_argument("--replicas", type=Path, help="pinned replica manifest and prospective baseline")
    choices.add_argument("--parity", type=Path, help="pinned baseline plus prospective parity sidecar manifest")
    choices.add_argument("--erasure", type=Path, help="pinned baseline plus multiple-erasure sidecar manifest")
    choices.add_argument("--capsule", type=Path, help="prospective pinned schema and physical map capsule")
    choices.add_argument("--protection-bundle", type=Path,
                         help="prospective pinned capsule/parity bundle retained before damage")
    choices.add_argument("--element-baseline", type=Path, help="prior compact/contiguous element-hash ZIP")
    choices.add_argument("--chunk-baseline", type=Path, help="prior coordinate chunk-hash JSON for integrity-gated export")
    rescue_cmd.add_argument("--element-baseline-sha256", help="independently retained SHA-256 of the element baseline ZIP")
    rescue_cmd.add_argument("--chunk-baseline-sha256", help="independently retained SHA-256 of the chunk baseline JSON")
    rescue_cmd.add_argument("--capsule-sha256", help="independently retained SHA-256 of the recovery capsule")
    rescue_cmd.add_argument("--protection-manifest-sha256",
                            help="independently retained SHA-256 of the protection ZIP's manifest")
    rescue_cmd.add_argument("--protection-method", choices=("capsule", "erasure"), default="capsule",
                            help="use physical-map capsule or parity shards from a protection bundle")
    choices.add_argument("--status-trial", action="store_true",
                         help="trial a validated v3 write flag on a disposable copy before native-readable export")
    choices.add_argument("--metadata-trial", choices=("root", "layout", "dimension"),
                         help="trial one checksum-constrained modern root, layout pointer, or chunk dimension byte on a copy")
    choices.add_argument("--truncated-chunks", action="store_true",
                         help="retain complete rooted chunks before a physical tail truncation")
    choices.add_argument("--large-readable", action="store_true",
                         help="stream currently native-readable multidimensional fixed records")
    choices.add_argument("--large-structural", action="store_true",
                         help="stream one checked damaged fixed-array pointer in a large sparse numeric dataset")
    arguments = argv if argv is not None else sys.argv[1:]
    if not arguments:
        parser.print_help()
        return 0
    args = parser.parse_args(arguments)

    if args.command == "report":
        try:
            if args.report.stat().st_size > 256 * 1024 * 1024:
                raise RecoveryError("evidence report exceeds the 256 MiB summary limit")
            report = json.loads(args.report.read_text(encoding="utf-8"))
            if (not isinstance(report, dict) or report.get("tool") != "h5reclaim"
                    or report.get("schema_version") != 1 or report.get("outcome") not in ("complete", "partial")):
                raise RecoveryError("expected a H5Reclaim recovery evidence report with schema_version 1")
            print(_render_report(report))
            return 0
        except (RecoveryError, OSError, ValueError, RuntimeError, KeyError, TypeError, AttributeError) as exc:
            print(f"h5reclaim: report summary failed: {_display_path(exc, 300)}", file=sys.stderr)
            return 2

    if args.command == "related-manifest":
        try:
            from .large_streaming import LargeBudget
            from .related_manifest import build_related_manifest
            options = json.loads(args.streaming_budget.read_text(encoding="utf-8")) if args.streaming_budget else {}
            result = build_related_manifest(args.source, args.directory, args.output, budget=LargeBudget(**options))
            print(f"Pinned {len(result['manifest']['files'])} companion files: {_display_path(args.output, 240)}")
            for item in result['unresolved']:
                print(f"Unresolved: {_display_path(item['declared_name'])}: {item['reason']}")
            return 0
        except (FormatError, UnsupportedCase, RecoveryError, OSError, ValueError, RuntimeError, TypeError) as exc:
            print(f"h5reclaim: manifest creation failed: {_display_path(exc, 300)}", file=sys.stderr)
            return 2

    if args.command == "capture-baseline":
        try:
            result = capture_baseline(args.source, args.dataset, args.output)
            print("H5Reclaim acquisition baseline")
            print(f"Dataset: {_display_path(result['dataset_path'])} | {len(result['chunk_hashes'])} allocated chunks hashed")
            print(f"Baseline: {_display_path(args.output, 240)}")
            print("Keep this record independently. Its hashes describe the observed capture, not earlier historical truth.")
            return 0
        except (FormatError, UnsupportedCase, RecoveryError, OSError, ValueError, RuntimeError) as exc:
            print(f"h5reclaim: baseline capture failed: {_display_path(exc, 300)}", file=sys.stderr)
            return 2

    if args.command == "capture-element-baseline":
        try:
            from .payload_integrity import capture_element_baseline
            result = capture_element_baseline(args.source, args.dataset, args.output)
            print("H5Reclaim prospective element baseline")
            print(f"Dataset: {_display_path(args.dataset)} | {result['dataset']['elements']} element hashes")
            print(f"Baseline: {_display_path(args.output, 240)}")
            print(f"SHA-256 to retain separately: {result['archive_sha256']}")
            print("Capture while the file is intact; a later mismatch will be unknown, not reconstructed from a hash.")
            return 0
        except (FormatError, UnsupportedCase, RecoveryError, OSError, ValueError, RuntimeError) as exc:
            print(f"h5reclaim: element baseline capture failed: {_display_path(exc, 300)}", file=sys.stderr)
            return 2

    if args.command == "capture-parity":
        try:
            from .parity_sidecar import capture_parity_sidecar
            result = capture_parity_sidecar(args.source, args.dataset, args.baseline,
                                            args.output, stripe_width=args.stripe_width)
            print("H5Reclaim prospective parity capture")
            print(f"Dataset: {_display_path(args.dataset)} | {len(result['stripes'])} stripes")
            print(f"Sidecar: {_display_path(args.output, 240)}")
            print("Keep the baseline and sidecar separately; one damaged chunk per stripe can be reconstructed when other evidence survives.")
            return 0
        except (FormatError, UnsupportedCase, RecoveryError, OSError, ValueError, RuntimeError, KeyError) as exc:
            print(f"h5reclaim: parity capture failed: {_display_path(exc, 300)}", file=sys.stderr)
            return 2

    if args.command == "capture-erasure":
        try:
            from .erasure_sidecar import capture_erasure_sidecar
            result = capture_erasure_sidecar(
                args.source, args.dataset, args.baseline, args.output,
                stripe_width=args.stripe_width, parity_shards=args.parity_shards,
            )
            print("H5Reclaim prospective multiple-erasure capture")
            print(f"Dataset: {_display_path(args.dataset)} | {len(result['stripes'])} stripes "
                  f"| {result['parity_shards']} parity shards per stripe")
            print(f"Sidecar: {_display_path(args.output, 240)}")
            print(f"SHA-256 to retain separately: {result['archive_sha256']}")
            print("Keep the baseline, sidecar, and their hashes before an incident.")
            return 0
        except (FormatError, UnsupportedCase, RecoveryError, OSError, ValueError, RuntimeError, KeyError) as exc:
            print(f"h5reclaim: erasure capture failed: {_display_path(exc, 300)}", file=sys.stderr)
            return 2

    if args.command == "capture-capsule":
        try:
            from .recovery_capsule import capture_recovery_capsule
            result = capture_recovery_capsule(args.source, args.dataset, args.output)
            print("H5Reclaim prospective recovery capsule")
            print(f"Dataset: {_display_path(args.dataset)}")
            print(f"Capsule: {_display_path(args.output, 240)}")
            print(f"SHA-256 to retain separately: {result['archive_sha256']}")
            print("Keep this capsule and its hash independently before damage.")
            return 0
        except (FormatError, UnsupportedCase, RecoveryError, OSError, ValueError, RuntimeError, KeyError) as exc:
            print(f"h5reclaim: capsule capture failed: {_display_path(exc, 300)}", file=sys.stderr)
            return 2

    if args.command == "protect":
        try:
            from .protection_bundle import capture_protection_bundle
            result = capture_protection_bundle(
                args.source, args.dataset, args.output,
                include_erasure=not args.capsule_only,
                stripe_width=args.stripe_width, parity_shards=args.parity_shards,
            )
            print("H5Reclaim prospective protection")
            print(f"Dataset: {_display_path(result['dataset_path'])}")
            print(f"Bundle: {_display_path(args.output, 240)}")
            print("Components: " + ", ".join(sorted(result["components"])))
            print(f"Retain this manifest SHA-256 separately before damage: {result['manifest_sha256']}")
            print("The capture records current bytes and a local clock, not authenticated capture time or scientific correctness.")
            return 0
        except (FormatError, UnsupportedCase, RecoveryError, OSError, ValueError, RuntimeError, KeyError) as exc:
            print(f"h5reclaim: protection failed: {_display_path(exc, 300)}", file=sys.stderr)
            return 2

    if args.command in ("verify-protection", "drill-protection"):
        try:
            from .protection_bundle import drill_protection_bundle, verify_protection_bundle
            if args.command == "verify-protection":
                result = verify_protection_bundle(args.bundle, args.manifest_sha256, source=args.source)
                print("H5Reclaim protection verification | passed")
                print(f"Dataset: {_display_path(result['dataset_path'])}")
                print("Components: " + ", ".join(sorted(result["components"])))
                print(f"Bundle SHA-256: {result['bundle_sha256']}")
            else:
                result = drill_protection_bundle(args.bundle, args.manifest_sha256, args.source)
                print("H5Reclaim disposable restore drill | passed")
                print(f"Allocated chunks restored exactly: {result['restored_allocated_chunks']}")
                print(f"Parity losses rebuilt exactly: {result['erasure_restored_chunks']}")
                print("The original source and protection bundle were not modified.")
            return 0
        except (FormatError, UnsupportedCase, RecoveryError, OSError, ValueError, RuntimeError, KeyError) as exc:
            print(f"h5reclaim: protection verification failed: {_display_path(exc, 300)}", file=sys.stderr)
            return 2

    if args.command == "discover":
        try:
            from .object_discovery import discover
            from .large_streaming import LargeBudget
            options = {}
            if args.streaming_budget:
                if args.streaming_budget.stat().st_size > 65536:
                    raise RecoveryError("discovery budget JSON exceeds 64 KiB")
                options = json.loads(args.streaming_budget.read_text(encoding="utf-8"))
            report = discover(args.source, budget=LargeBudget(**options))
            if args.json:
                print(json.dumps(report, indent=2, sort_keys=True))
            else:
                print(f"H5Reclaim object discovery | {len(report['datasets'])} checked dataset headers")
                for entry in report["datasets"]:
                    print(f"Object {entry['address']:#x} | shape {_dimensions(entry['shape'])} | dtype {entry['dtype']}")
                print("Original paths remain unknown. Use rescue --object-address ADDRESS --dataset OUTPUT_PATH to export a checked object.")
            return 0
        except (FormatError, UnsupportedCase, RecoveryError, OSError, ValueError, RuntimeError, TypeError) as exc:
            print(f"h5reclaim: discovery failed: {_display_path(exc, 300)}", file=sys.stderr)
            return 2

    if args.command == "rescue":
        try:
            if args.output is None:
                args.output = args.source.with_name(args.source.stem + ".recovered.h5")
            if args.report is None:
                args.report = args.output.with_name(args.output.stem + ".report.json")
            from .recovery import _validate_paths
            _validate_paths(args.source, args.output, args.report)
            source = str(args.source.absolute())
            streaming_budget = None
            if args.streaming_budget is not None:
                from .large_streaming import LargeBudget
                if args.streaming_budget.stat().st_size > 65536:
                    raise RecoveryError("streaming budget JSON exceeds 64 KiB")
                streaming_budget = json.loads(args.streaming_budget.read_text(encoding="utf-8"))
                if not isinstance(streaming_budget, dict):
                    raise RecoveryError("streaming budget must be a JSON object")
                LargeBudget(**streaming_budget)
            if args.related_dir is not None:
                from .related_manifest import build_related_manifest
                from .large_streaming import LargeBudget
                with tempfile.TemporaryDirectory(prefix=".h5reclaim-related-", dir=args.report.parent) as directory:
                    manifest_path = Path(directory) / "related-files.json"
                    result = build_related_manifest(args.source, args.related_dir, manifest_path,
                                                    budget=LargeBudget(**(streaming_budget or {})))
                    for item in result["unresolved"]:
                        print(f"Unresolved companion: {_display_path(item['declared_name'])}: {item['reason']}")
                    forwarded, skip = [], False
                    for argument in (argv if argv is not None else sys.argv[1:]):
                        if skip:
                            skip = False
                        elif argument == "--related-dir":
                            skip = True
                        elif not argument.startswith("--related-dir="):
                            forwarded.append(argument)
                    if result["manifest"]["files"]:
                        forwarded.extend(["--related-files", str(manifest_path)])
                    return main(forwarded)
            if args.dataset is None:
                explicit = (args.object_address is not None, args.replicas,
                            args.parity, args.erasure, args.capsule, args.protection_bundle,
                            args.element_baseline, args.chunk_baseline, args.metadata_trial,
                            args.status_trial, args.truncated_chunks, args.large_readable, args.large_structural,
                            args.element_baseline_sha256, args.chunk_baseline_sha256, args.capsule_sha256,
                            args.protection_manifest_sha256)
                if any(explicit):
                    raise RecoveryError("an explicit recovery route or prior capture requires --dataset")
                if args.hints is not None:
                    raise RecoveryError("--hints requires --dataset")
                if args.strict_history:
                    raise UnsupportedCase("strict historical integrity requires a separately retained prior capture and --dataset")
                from .whole_file import rescue_all
                report = rescue_all(args.source, args.output, args.report, streaming_budget=streaming_budget,
                                    related_files=args.related_files, resume_dir=args.resume_dir,
                                    _bundle_kind="family" if args.family_members else "split" if args.split_members else None,
                                    _bundle_manifest=args.family_members or args.split_members)
                print(f"H5Reclaim whole-file rescue | {report['outcome']} | "
                      f"{report['datasets_exported']}/{report['datasets_discovered']} datasets exported")
                for failure in report["failures"]:
                    print(f"Unresolved: {_display_path(failure['path'])}: {_display_path(failure['reason'], 240)}")
                print(f"Output: {_display_path(args.output, 240)}")
                print(f"Evidence report: {_display_path(args.report, 240)}")
                print(f"Check each dataset's status map under {report['metadata_group']}/datasets before using its values.")
                if report["outcome"] != "complete":
                    print("Partial export: some data or scientific context remain unresolved. "
                          "Use h5reclaim report with the evidence path above for a summary.")
                return 1 if args.fail_on_partial and report["outcome"] != "complete" else 0
            def run_rescue(route: str, output: Path, report_path: Path, **kwargs: str) -> dict:
                if streaming_budget is not None:
                    kwargs["streaming_budget"] = json.dumps(streaming_budget)
                if args.hints is not None:
                    kwargs["hints"] = str(args.hints.absolute())
                if args.resume_dir is not None:
                    if route not in ("native_family", "native_split"):
                        raise RecoveryError("selection checkpoints require automatic dataset recovery or Family/Split export")
                    kwargs["resume_dir"] = str(args.resume_dir.absolute())
                return run_route(
                    route, output, report_path, strict_history=args.strict_history,
                    annotate_history=True, audit_science_context=not args.no_context_audit,
                    **kwargs,
                )
            if (args.element_baseline is None) != (args.element_baseline_sha256 is None):
                raise RecoveryError("--element-baseline and --element-baseline-sha256 must be supplied together")
            if (args.chunk_baseline is None) != (args.chunk_baseline_sha256 is None):
                raise RecoveryError("--chunk-baseline and --chunk-baseline-sha256 must be supplied together")
            if (args.capsule is None) != (args.capsule_sha256 is None):
                raise RecoveryError("--capsule and --capsule-sha256 must be supplied together")
            if (args.protection_bundle is None) != (args.protection_manifest_sha256 is None):
                raise RecoveryError("--protection-bundle and --protection-manifest-sha256 must be supplied together")
            if args.protection_bundle is None and args.protection_method != "capsule":
                raise RecoveryError("--protection-method requires --protection-bundle")
            if args.strict_history and all(item is None for item in (
                args.replicas, args.parity, args.erasure, args.capsule,
                args.protection_bundle, args.element_baseline, args.chunk_baseline,
            )):
                raise RecoveryError(
                    "--strict-history requires a separately pinned prior capture: "
                    "baseline, capsule, protection bundle, replica, or parity route"
                )
            if args.element_baseline_sha256 is not None and args.element_baseline is None:
                raise RecoveryError("an element baseline digest cannot be supplied with another route")
            if args.chunk_baseline_sha256 is not None and args.chunk_baseline is None:
                raise RecoveryError("a chunk baseline digest cannot be supplied with another route")
            if args.capsule_sha256 is not None and args.capsule is None:
                raise RecoveryError("a capsule digest cannot be supplied with another route")
            if args.replicas is not None:
                report = run_rescue("replicas", args.output, args.report, source=source,
                                   dataset=args.dataset, manifest=str(args.replicas.absolute()))
            elif args.parity is not None:
                report = run_rescue("parity", args.output, args.report, source=source,
                                   dataset=args.dataset, manifest=str(args.parity.absolute()))
            elif args.erasure is not None:
                report = run_rescue("erasure", args.output, args.report, source=source,
                                   dataset=args.dataset, manifest=str(args.erasure.absolute()))
            elif args.capsule is not None:
                report = run_rescue("capsule", args.output, args.report, source=source,
                                   dataset=args.dataset, capsule=str(args.capsule.absolute()),
                                   capsule_sha256=args.capsule_sha256)
            elif args.protection_bundle is not None:
                report = run_rescue(
                    "protection", args.output, args.report, source=source,
                    dataset=args.dataset, bundle=str(args.protection_bundle.absolute()),
                    manifest_sha256=args.protection_manifest_sha256,
                    method=args.protection_method,
                )
            elif args.element_baseline is not None:
                report = run_rescue("element_baseline", args.output, args.report,
                                   source=source, dataset=args.dataset,
                                   baseline=str(args.element_baseline.absolute()),
                                   baseline_sha256=args.element_baseline_sha256)
            elif args.chunk_baseline is not None:
                report = run_rescue("chunk_baseline", args.output, args.report,
                                   source=source, dataset=args.dataset,
                                   baseline=str(args.chunk_baseline.absolute()),
                                   baseline_sha256=args.chunk_baseline_sha256)
            elif args.status_trial:
                report = run_rescue("status", args.output, args.report, source=source,
                                   dataset=args.dataset)
            elif args.metadata_trial is not None:
                report = run_rescue("metadata_trial", args.output, args.report, source=source,
                                   dataset=args.dataset, kind=args.metadata_trial)
            elif args.truncated_chunks:
                report = run_rescue("truncated_chunks", args.output, args.report, source=source,
                                   dataset=args.dataset)
            elif args.large_readable:
                report = run_rescue("large_readable", args.output, args.report, source=source,
                                   dataset=args.dataset)
            elif args.large_structural:
                report = run_rescue("large_structural", args.output, args.report, source=source,
                                   dataset=args.dataset)
            elif args.family_members is not None:
                from .family_bundle import _manifest
                _, members = _manifest(args.family_members)
                if args.source.resolve(strict=True) != Path(members[0]["path"]).resolve(strict=True):
                    raise RecoveryError("Family source argument must be manifest member zero")
                report = run_rescue("native_family", args.output, args.report,
                                   dataset=args.dataset, manifest=str(args.family_members.absolute()))
            elif args.split_members is not None:
                from .split_bundle import _load_manifest
                metadata, _, _, _ = _load_manifest(args.split_members)
                if args.source.resolve(strict=True) != metadata.resolve(strict=True):
                    raise RecoveryError("Split source argument must be the metadata member")
                report = run_rescue("native_split", args.output, args.report,
                                   dataset=args.dataset, manifest=str(args.split_members.absolute()))
            elif args.object_address is not None:
                report = run_rescue("object", args.output, args.report, source=source,
                    dataset=args.dataset, object_address=hex(args.object_address),
                    **({"hints": str(args.hints.absolute())} if args.hints else {}))
            elif args.related_files is not None:
                inventory = inspect_dependencies(args.source, args.dataset)
                kinds = {item["kind"] for item in inventory["dependencies"]}
                if inventory["outcome"] not in ("complete", "external_link") or len(kinds) != 1:
                    raise UnsupportedCase("selected dependency metadata is incomplete or mixes unsupported routes")
                if kinds == {"external_raw_storage"}:
                    route = "external_raw"
                elif kinds == {"virtual_source"}:
                    route = "vds"
                elif kinds == {"external_link"} and inventory["outcome"] == "external_link":
                    route = "external_link"
                else:
                    raise UnsupportedCase("selected dependency kind has no safe value-export route")
                report = run_rescue(route, args.output, args.report, source=source,
                                   dataset=args.dataset, manifest=str(args.related_files.absolute()))
            else:
                from .rescue import auto_rescue
                report = auto_rescue(args.source, args.dataset, args.output, args.report,
                                     strict_history=args.strict_history,
                                     audit_science_context=not args.no_context_audit,
                                     streaming_budget=streaming_budget, hints=args.hints, resume_dir=args.resume_dir)
            accepted, unknown, unit = _coverage(report)
            print(f"H5Reclaim rescue | {report['outcome']} | {accepted} accepted {unit} | {unknown} unknown {unit}")
            if report.get("mode") == "readable_export":
                print("Route: native-readable copy of currently accessible values; no damaged index was reconstructed.")
            elif report.get("mode") == "status_trial_readable_export":
                print("Route: status-only trial on a disposable copy, then native-readable export; historical values are unverified.")
            elif report.get("mode") == "metadata_trial_readable_export":
                print("Route: single metadata pointer correction on a disposable copy, then readable export; historical values are unverified.")
            elif report.get("mode") == "large_native_readable_export":
                print("Route: streamed copy of currently native-readable values; no damaged index was reconstructed.")
            elif report.get("mode") == "variable_native_readable_export":
                print("Route: native-readable variable elements, individually checked against the derived output.")
            print(f"Output: {_display_path(args.output, 240)}")
            print(f"Evidence report: {_display_path(args.report, 240)}")
            for path in _status_maps(report):
                print(f"Status map: {_display_path(path, 240)}")
            integrity = report.get("historical_integrity")
            if integrity and integrity.get("capture_sha256"):
                print(f"History: {integrity['matching_units']} units match an operator-supplied prior capture; "
                      f"{integrity['unknown_units']} unknown. Capture timing is not authenticated.")
            else:
                print("History: unverified; current readable values do not establish prior measurements.")
            print("Check the validity and historical-status maps before using output values.")
            return 1 if args.fail_on_partial and report["outcome"] != "complete" else 0
        except (FormatError, DependencyError, UnsupportedCase, RecoveryError, OSError,
                ValueError, RuntimeError, KeyError, TypeError, UnicodeError) as exc:
            print(f"h5reclaim: rescue failed: {_display_path(exc, 300)}", file=sys.stderr)
            return 2

    if args.command == "export-fragments":
        try:
            ledger = load_evidence_report(args.report)
            if len(ledger.sources) != 1:
                raise ValueError("this command requires exactly one recorded source")
            count = sum(decision.status != "accepted" for decision in ledger.decisions)
            if count == 0:
                raise ValueError("the report has no bounded unresolved raw extents to export")
            export_unassigned_fragments(
                ledger, {ledger.sources[0].source_id: args.source}, args.output,
            )
            print(f"Exported {count} unresolved raw fragment(s) without dataset coordinates.")
            print(f"ZIP: {_display_path(args.output, 240)}")
            print("Fragment hashes and source identity checked; no measurement was restored.")
            return 0
        except (OSError, ValueError, RuntimeError, KeyError, TypeError) as exc:
            print(f"h5reclaim: fragment export failed: {_display_path(exc, 300)}", file=sys.stderr)
            return 2

    if args.command == "probe-status":
        try:
            result = probe_status_copy(args.source)
            if args.json:
                print(json.dumps(result, indent=2, sort_keys=True))
            else:
                print("H5Reclaim disposable status probe")
                print(f"Source: {_display_path(args.source, 240)}")
                print(f"Result: {result['outcome']} | {_display_path(result['detail'], 240)}")
                print("Original file unchanged. No measurements verified or restored.")
            return 0 if result["outcome"] == "copy_metadata_opened" else 1
        except (RecoveryError, UnsupportedCase, OSError, ValueError, RuntimeError) as exc:
            if args.json:
                print(json.dumps({"outcome": "error", "error": str(exc)[:300]}, sort_keys=True))
            else:
                print(f"h5reclaim: status probe failed: {_display_path(exc, 300)}", file=sys.stderr)
            return 2

    if args.command == "diagnose":
        try:
            hints = load_hints(args.hints) if args.hints else None
            selected_path = args.dataset or (hints.path if hints else None)
            report = diagnose(args.source, selected_path)
            if args.related_files is not None:
                report["dependency_validation"] = validate_dependency_manifest(
                    report["dependencies"] or {}, load_dependency_manifest(args.related_files),
                )
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
        except (DependencyError, HintsError, SurveyError, RecoveryError, OSError, ValueError, RuntimeError, KeyError, TypeError) as exc:
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
            if "metadata_resolution" in report:
                summary["metadata_resolution"] = report["metadata_resolution"]
            if hints is not None:
                comparisons = compare_hints(
                    hints, observed_dataset=analysis.spec,
                    input_sha256=report["source"]["sha256_before"],
                )
                require_no_conflicts(comparisons)
                summary["operator_hints"] = _hint_report(hints, comparisons)
            print(json.dumps(summary, indent=2, sort_keys=True) if args.json else _render_inspect(summary))
        elif args.command == "recover":
            report = run_route(
                "structural", args.output, args.report,
                source=str(args.source.absolute()), dataset=args.dataset,
                hints=str(args.hints.absolute()) if args.hints else "",
            )
            counts = report["counts"]
            total = sum(counts.values())
            print(f"H5Reclaim recovery | {report['outcome']}")
            print(f"Dataset: {_display_path(report['dataset']['path'])} | "
                  f"{report['index']['type']}")
            print(f"Accepted: {counts['recovered']}/{total} chunks | "
                  f"{report['reconstructed_chunks']} from a reconstructed leaf link")
            unknown = [f"{value} {name.replace('_', ' ')}" for name, value in counts.items()
                       if name != "recovered" and value]
            if unknown:
                print("Unaccepted: " + ", ".join(unknown))
                print("Check /_h5reclaim/chunk_status before using output fill values.")
            if "metadata_resolution" in report:
                print("Metadata: " + report["metadata_resolution"]["route"])
            print("Accepted coordinates do not prove historical measurement integrity.")
            print(f"Output: {_display_path(args.output, 240)}\n"
                  f"Evidence report: {_display_path(args.report, 240)}")
        else:
            report = export_readable(args.source, args.dataset, args.output, args.report, hints=hints)
            print(
                f"readable export: {report['outcome']} | "
                f"{report.get('accepted_elements', 0)} accepted elements, "
                f"{report.get('unknown_elements', 0)} unknown elements | "
                f"{report['blocks_verified']} blocks checked"
            )
            if report.get("unknown_elements"):
                print("Check /_h5reclaim/validity before using any output fill values.")
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
