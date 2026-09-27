"""Metadata-only inventory and explicit support classification."""

from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import h5py
import numpy as np

from h5reclaim.__main__ import main
from h5reclaim import survey as survey_module


class SurveyTests(unittest.TestCase):
    def test_bundled_gwosc_candidate_has_no_payload_reads(self) -> None:
        source = Path(__file__).resolve().parents[1] / "corpus/files/H-H1_GWOSC_16KHZ_R1-1126259447-32.hdf5"
        original = survey_module.sha256_file(source)
        with patch.object(h5py.Dataset, "__getitem__", side_effect=AssertionError("payload read")):
            report = survey_module.survey(source)
        strain = next(item for item in report["datasets"] if item["selected_path"] == "/strain/Strain")
        self.assertEqual(strain["shape"], [524288])
        self.assertEqual(strain["chunks"], [4096])
        self.assertEqual(strain["support"]["status"], "candidate")
        self.assertEqual(strain["index"]["root_level"], 1)
        self.assertEqual(survey_module.sha256_file(source), original)

    def test_candidate_uses_no_dataset_reads_and_keeps_source_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "input.h5"
            with h5py.File(source, "x", libver=("earliest", "v108")) as handle:
                handle.create_dataset(
                    "measurements", data=np.arange(144, dtype="<u4").reshape(12, 12),
                    chunks=(1, 1),
                )
            original = source.read_bytes()
            with patch.object(h5py.Dataset, "__getitem__", side_effect=AssertionError("payload read")):
                report = survey_module.survey(source)
            self.assertEqual(source.read_bytes(), original)
            self.assertEqual(report["outcome"], "complete")
            self.assertEqual(report["dataset_count"], 1)
            item = report["datasets"][0]
            self.assertEqual(item["selected_path"], "/measurements")
            self.assertEqual(item["shape"], [12, 12])
            self.assertEqual(item["chunks"], [1, 1])
            self.assertEqual(item["rank"], 2)
            self.assertEqual(item["layout"], "chunked")
            self.assertEqual(item["support"]["status"], "candidate")
            self.assertEqual(item["index"]["type"], "v1_raw_data_btree")

    def test_external_and_soft_links_are_skipped_without_opening_target(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "input.h5"
            with h5py.File(source, "x", libver=("earliest", "v108")) as handle:
                dataset = handle.create_dataset(
                    "measurements", data=np.arange(144, dtype="<u4").reshape(12, 12),
                    chunks=(1, 1),
                )
                handle["alias"] = dataset
                handle["outside"] = h5py.ExternalLink("nonexistent.h5", "/private")
                handle["soft"] = h5py.SoftLink("/measurements")
                handle["loop"] = handle["/"]
            report = survey_module.survey(source)
            self.assertEqual(report["dataset_count"], 1)
            self.assertEqual(report["skipped"]["external_links"], 1)
            self.assertEqual(report["skipped"]["soft_links"], 1)
            self.assertEqual(report["visited_groups"], 1)
            self.assertEqual(
                {report["datasets"][0]["selected_path"], *report["datasets"][0]["aliases"]},
                {"/alias", "/measurements"},
            )

    def test_metadata_variants_have_machine_readable_reasons(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "mixed.h5"
            with h5py.File(source, "x", libver=("earliest", "v108")) as handle:
                handle.create_dataset("filtered", data=np.arange(16, dtype="<u4").reshape(4, 4),
                                      chunks=(2, 2), compression="gzip")
                handle.create_dataset("contiguous", data=np.arange(6, dtype="<f4"))
            report = survey_module.survey(source)
            self.assertEqual(report["outcome"], "complete")
            self.assertEqual(report["dataset_count"], 2)
            items = {entry["selected_path"]: entry for entry in report["datasets"]}
            self.assertEqual(items["/filtered"]["filters"][0]["name"], "deflate")
            self.assertEqual(items["/contiguous"]["layout"], "contiguous")
            self.assertEqual(items["/contiguous"]["rank"], 1)
            self.assertEqual(items["/filtered"]["support"]["status"], "candidate")
            self.assertEqual(items["/filtered"]["index"]["type"], "v1_raw_data_btree")
            self.assertEqual(items["/contiguous"]["support"]["status"], "unsupported")
            self.assertIn("layout", {reason["code"] for reason in items["/contiguous"]["support"]["reasons"]})

    def test_multiple_local_datasets_are_evaluated_independently(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "multi.h5"
            with h5py.File(source, "x", libver=("earliest", "v108")) as handle:
                values = np.arange(144, dtype="<u4").reshape(12, 12)
                handle.create_dataset("measurements", data=values, chunks=(1, 1))
                handle.create_dataset("nested/other", data=values + 1, chunks=(1, 1))
            report = survey_module.survey(source)
            items = {item["selected_path"]: item for item in report["datasets"]}
            self.assertEqual(report["dataset_count"], 2)
            self.assertEqual(set(items), {"/measurements", "/nested/other"})
            self.assertEqual({item["support"]["status"] for item in items.values()}, {"candidate"})

    def test_noncanonical_uint32_precision_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "narrow.h5"
            with h5py.File(source, "x", libver=("earliest", "v108")) as handle:
                datatype = h5py.h5t.STD_U32LE.copy()
                datatype.set_precision(24)
                space = h5py.h5s.create_simple((12, 12))
                plist = h5py.h5p.create(h5py.h5p.DATASET_CREATE)
                plist.set_chunk((1, 1))
                h5py.h5d.create(handle.id, b"narrow", datatype, space, dcpl=plist)
            report = survey_module.survey(source)
            item = report["datasets"][0]
            self.assertEqual(item["support"]["status"], "unsupported")
            self.assertIn("datatype", {reason["code"] for reason in item["support"]["reasons"]})

    def test_traversal_limit_is_explicit_and_prevents_false_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "input.h5"
            with h5py.File(source, "x", libver=("earliest", "v108")) as handle:
                handle.create_dataset("a", data=np.arange(144, dtype="<u4").reshape(12, 12),
                                      chunks=(1, 1))
                handle.create_group("b")
            with patch.object(survey_module, "MAX_LINKS", 1):
                report = survey_module.survey(source)
            self.assertEqual(report["outcome"], "partial")
            self.assertIn("link_limit", {issue["code"] for issue in report["issues"]})
            self.assertEqual(report["datasets"][0]["support"]["status"], "indeterminate")

    def test_cli_returns_json_for_survey_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "invalid.h5"
            source.write_bytes(b"not an HDF5 file")
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                code = main(["survey", str(source), "--json"])
            self.assertEqual(code, 2)
            report = json.loads(stdout.getvalue())
            self.assertEqual(report["outcome"], "error")
            self.assertEqual(report["error"]["code"], "survey_failed")

    def test_cli_human_survey_prioritizes_candidate_and_bounds_long_inventory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "input.h5"
            with h5py.File(source, "x", libver=("earliest", "v108")) as handle:
                for index in range(13):
                    handle.create_dataset(f"a{index:02}", data=[index])
                handle.create_dataset(
                    "zz_candidate", data=np.arange(144, dtype="<u4").reshape(12, 12),
                    chunks=(1, 1),
                )
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                code = main(["survey", str(source)])
            self.assertEqual(code, 0)
            display = stdout.getvalue()
            self.assertIn("14 local datasets", display)
            self.assertIn("1 candidate | 13 unsupported", display)
            self.assertIn("[CANDIDATE] /zz_candidate", display)
            self.assertIn("[UNSUPPORTED] /a00", display)
            self.assertIn("Reason: requires", display)
            self.assertIn("9 more datasets. Use --json", display)
            self.assertNotIn("[UNSUPPORTED] /a04", display)
            self.assertEqual(display.count("[UNSUPPORTED]"), 4)

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                code = main(["survey", str(source), "--json"])
            self.assertEqual(code, 0)
            report = json.loads(stdout.getvalue())
            self.assertEqual(report["dataset_count"], 14)
            self.assertEqual(len(report["datasets"]), 14)
            self.assertEqual(report["schema_version"], 1)

    def test_cli_partial_survey_reports_issue_and_exit_code(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "input.h5"
            with h5py.File(source, "x", libver=("earliest", "v108")) as handle:
                handle.create_dataset("a", data=np.arange(144, dtype="<u4").reshape(12, 12),
                                      chunks=(1, 1))
                handle.create_group("b")
            stdout = io.StringIO()
            with patch.object(survey_module, "MAX_LINKS", 1), contextlib.redirect_stdout(stdout):
                code = main(["survey", str(source)])
            self.assertEqual(code, 1)
            self.assertIn("Inventory: partial", stdout.getvalue())
            self.assertIn("link_limit", stdout.getvalue())
            self.assertIn("[INDETERMINATE]", stdout.getvalue())

    def test_cli_inspect_human_and_json_are_both_available(self) -> None:
        source = Path(__file__).resolve().parents[1] / "corpus/files/H-H1_GWOSC_16KHZ_R1-1126259447-32.hdf5"
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = main(["inspect", str(source), "--dataset", "/strain/Strain"])
        self.assertEqual(code, 0)
        display = stdout.getvalue()
        self.assertIn("H5Reclaim inspection", display)
        self.assertIn("128/128 chunks accepted", display)
        self.assertIn("Fletcher32, DEFLATE", display)
        self.assertIn("Inspection writes no recovered file", display)

        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = main(["inspect", str(source), "--dataset", "/strain/Strain", "--json"])
        self.assertEqual(code, 0)
        report = json.loads(stdout.getvalue())
        self.assertEqual(report["dataset"]["path"], "/strain/Strain")
        self.assertEqual(report["counts"]["recovered"], 128)
        self.assertEqual(report["reconstructed_chunks"], 0)
        self.assertEqual(report["source"]["sha256_before"], report["source"]["sha256_after"])

    def test_cli_human_errors_go_to_stderr_and_json_errors_are_structured(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "invalid.h5"
            source.write_bytes(b"not an HDF5 file")
            for command in ("survey", "inspect"):
                args = [command, str(source)]
                if command == "inspect":
                    args += ["--dataset", "/measurements"]
                stdout, stderr = io.StringIO(), io.StringIO()
                with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                    code = main(args)
                self.assertEqual(code, 2)
                self.assertEqual(stdout.getvalue(), "")
                self.assertIn("h5reclaim:", stderr.getvalue())

                stdout, stderr = io.StringIO(), io.StringIO()
                with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                    code = main([*args, "--json"])
                self.assertEqual(code, 2)
                self.assertEqual(stderr.getvalue(), "")
                result = json.loads(stdout.getvalue())
                self.assertEqual(result["error"]["code"], f"{command}_failed")


if __name__ == "__main__":
    unittest.main()
