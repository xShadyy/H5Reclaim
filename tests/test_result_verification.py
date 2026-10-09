"""Artifact consistency gate: actual whole-file export and deliberate tampering."""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path

import h5py
import numpy as np

from h5reclaim.__main__ import main
from h5reclaim.native_stream import export_native_stream
from h5reclaim.readable_export import export_readable
from h5reclaim.result_verification import ResultMismatch, ResultUnsupported, verify_result
from h5reclaim.whole_file import rescue_all


class ResultVerificationTests(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.source = self.root / "source.h5"
        self.output = self.root / "output.h5"
        self.report = self.root / "report.json"

    def _selected_partial(self) -> None:
        self.source.write_bytes(b"damaged original")
        digest = hashlib.sha256(self.source.read_bytes()).hexdigest()
        report = {"tool": "h5reclaim", "schema_version": 1, "outcome": "partial",
                  "source": {"sha256_before": digest, "sha256_after": digest},
                  "dataset": {"path": "/values", "shape": [6], "chunks": [2]},
                  "metadata_group": "/_h5reclaim", "accepted_elements": 4,
                  "unknown_elements": 2,
                  "validity": {"dataset": "/_h5reclaim/chunk_status", "granularity": "chunk",
                               "codes": {"recovered": 1, "allocation_unknown": 2}},
                  "counts": {"recovered": 2, "allocation_unknown": 1}}
        serialized = json.dumps(report, indent=2, sort_keys=True) + "\n"
        self.report.write_text(serialized, encoding="utf-8")
        with h5py.File(self.output, "w") as file:
            file.create_dataset("values", data=np.arange(6, dtype="i4"), chunks=(2,))
            meta = file.create_group("_h5reclaim")
            meta.attrs["source_sha256"] = digest
            status = meta.create_dataset("chunk_status", data=np.array([1, 2, 1], dtype="u1"))
            status.attrs["codes_json"] = json.dumps({"recovered": 1, "allocation_unknown": 2})
            meta.create_dataset("report_json", data=serialized, dtype=h5py.string_dtype("utf-8"))

    def test_chunked_whole_file_checks_each_embedded_report_and_map(self) -> None:
        with h5py.File(self.source, "w") as file:
            file.create_dataset("a", data=np.arange(8), chunks=(4,))
            file.create_dataset("b", data=np.arange(6).reshape(2, 3), chunks=(1, 3))
        rescue_all(self.source, self.output, self.report)
        result = verify_result(self.output, self.report, source_path=self.source)
        self.assertEqual(result["datasets_checked"], 2)
        self.assertTrue(result["explicit_source_sha256_checked"])
        self.assertEqual(sum(item["unknown_elements"] for item in result["datasets"]), 0)
        with h5py.File(self.output, "r+") as file:
            record = json.loads(self.report.read_text(encoding="utf-8"))["datasets"][0]
            path = record["report"]["metadata_group"] + "/report_json"
            file[path][()] = "{}"
        with self.assertRaisesRegex(ResultMismatch, "embedded dataset reports differ"):
            verify_result(self.output, self.report)

    def test_partial_map_and_saved_report_tampering_are_detected(self) -> None:
        self._selected_partial()
        result = verify_result(self.output, self.report, source_path=self.source)
        self.assertEqual((result["datasets"][0]["accepted_elements"],
                          result["datasets"][0]["unknown_elements"]), (4, 2))
        with h5py.File(self.output, "r+") as file:
            file["/_h5reclaim/chunk_status"][1] = 1
        with self.assertRaisesRegex(ResultMismatch, "totals|counts"):
            verify_result(self.output, self.report)
        with h5py.File(self.output, "r+") as file:
            file["/_h5reclaim/chunk_status"][1] = 2
        changed = json.loads(self.report.read_text(encoding="utf-8"))
        changed["dataset"]["shape"] = [100]
        self.report.write_text(json.dumps(changed), encoding="utf-8")
        with self.assertRaisesRegex(ResultMismatch, "saved and embedded"):
            verify_result(self.output, self.report)

    def test_missing_map_is_explicitly_unsupported_even_when_values_readable(self) -> None:
        with h5py.File(self.source, "w") as file:
            file.create_dataset("contiguous", data=np.array([2, 3, 5], dtype="i4"))
        export_readable(self.source, "/contiguous", self.output, self.report)
        with self.assertRaisesRegex(ResultUnsupported, "publishes no validity map"):
            verify_result(self.output, self.report)

    def test_budget_declared_codes_source_hash_and_cli_exit_status(self) -> None:
        self._selected_partial()
        with self.assertRaisesRegex(ResultUnsupported, "scan byte budget"):
            verify_result(self.output, self.report, max_map_bytes=2)
        stdout, stderr = StringIO(), StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            self.assertEqual(main(["verify-result", str(self.output), str(self.report), "--json"]), 0)
        self.assertEqual(json.loads(stdout.getvalue())["status"], "passed")
        self.source.write_bytes(b"tampered original")
        with self.assertRaisesRegex(ResultMismatch, "explicit source SHA-256"):
            verify_result(self.output, self.report, source_path=self.source)
        with h5py.File(self.output, "r+") as file:
            file["/_h5reclaim/chunk_status"][1] = 9
        stdout, stderr = StringIO(), StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            self.assertEqual(main(["verify-result", str(self.output), str(self.report), "--json"]), 1)
        self.assertIn("undeclared status code", json.loads(stdout.getvalue())["reason"])

    def test_external_and_soft_links_cannot_redirect_status_or_embedded_report(self) -> None:
        self._selected_partial()
        companion = self.root / "other.h5"
        with h5py.File(companion, "w") as file:
            file.create_dataset("status", data=np.array([1, 2, 1], dtype="u1"))
        with h5py.File(self.output, "r+") as file:
            del file["/_h5reclaim/chunk_status"]
            file["/_h5reclaim/chunk_status"] = h5py.ExternalLink(str(companion), "/status")
        with self.assertRaisesRegex(ResultMismatch, "soft/external link"):
            verify_result(self.output, self.report)
        with h5py.File(self.output, "r+") as file:
            del file["/_h5reclaim/chunk_status"]
            file["/_h5reclaim"].create_dataset("alternate", data=np.array([1, 2, 1], dtype="u1"))
            file["/_h5reclaim/chunk_status"] = h5py.SoftLink("/_h5reclaim/alternate")
        with self.assertRaisesRegex(ResultMismatch, "soft/external link"):
            verify_result(self.output, self.report)
        with h5py.File(self.output, "r+") as file:
            del file["/_h5reclaim/chunk_status"]
            file["/_h5reclaim"].create_dataset("chunk_status", data=np.array([1, 2, 1], dtype="u1"))
            embedded = file["/_h5reclaim/report_json"][()].decode("utf-8")
            file.create_dataset("moved_report", data=embedded, dtype=h5py.string_dtype("utf-8"))
            del file["/_h5reclaim/report_json"]
            file["/_h5reclaim/report_json"] = h5py.SoftLink("/moved_report")
        with self.assertRaisesRegex(ResultMismatch, "soft/external link"):
            verify_result(self.output, self.report)

    def test_value_payload_is_explicitly_outside_scope(self) -> None:
        self._selected_partial()
        with h5py.File(self.output, "r+") as file:
            file["/values"][0] = 999
        # This check does not independently hash or re-read the values.
        self.assertEqual(verify_result(self.output, self.report)["status"], "passed")

    def test_missing_source_annotation_is_explicitly_unsupported(self) -> None:
        self._selected_partial()
        with h5py.File(self.output, "r+") as file:
            del file["/_h5reclaim"].attrs["source_sha256"]
        with self.assertRaisesRegex(ResultUnsupported, "publishes no source SHA-256"):
            verify_result(self.output, self.report)

    def test_native_stream_chunk_map_and_source_annotation(self) -> None:
        with h5py.File(self.source, "w") as file:
            dataset = file.create_dataset("science", shape=(9,), dtype="i4", chunks=(4,))
            dataset[:4] = np.arange(4)
            dataset[8] = 8
        export_native_stream(self.source, "/science", self.output, self.report)
        result = verify_result(self.output, self.report)
        self.assertEqual((result["datasets"][0]["accepted_elements"],
                          result["datasets"][0]["unknown_elements"]), (5, 4))


if __name__ == "__main__":
    unittest.main()
