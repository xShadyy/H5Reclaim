"""Command-line entry point for supported HDF5 structural recovery."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .format import FormatError
from .metadata import UnsupportedCase
from .recovery import RecoveryError, analyze, recover


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="h5reclaim")
    commands = parser.add_subparsers(dest="command", required=True)
    inspect_cmd = commands.add_parser("inspect", help="inspect the supported dataset and B-tree")
    inspect_cmd.add_argument("source", type=Path)
    inspect_cmd.add_argument("--dataset", required=True)
    recover_cmd = commands.add_parser("recover", help="export supported chunks to a separate file")
    recover_cmd.add_argument("source", type=Path)
    recover_cmd.add_argument("--dataset", required=True)
    recover_cmd.add_argument("--output", required=True, type=Path)
    recover_cmd.add_argument("--report", required=True, type=Path)
    args = parser.parse_args(argv)

    try:
        if args.command == "inspect":
            report = analyze(args.source, args.dataset).report
            summary = {
                key: report[key]
                for key in ("outcome", "complete", "source", "dataset", "index", "counts")
            }
            summary["reconstructed_chunks"] = report["reconstructed_chunks"]
            summary["unresolved_links"] = report["unresolved_links"]
            print(json.dumps(summary, indent=2, sort_keys=True))
        else:
            report = recover(args.source, args.dataset, args.output, args.report)
            print(
                f"{report['outcome']}: {report['counts']['recovered']} chunks exported; "
                f"{report['reconstructed_chunks']} via the broken-link path"
            )
            print(f"output: {args.output}\nreport: {args.report}")
        return 0
    except (FormatError, UnsupportedCase, RecoveryError, OSError, ValueError) as exc:
        print(f"h5reclaim: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
