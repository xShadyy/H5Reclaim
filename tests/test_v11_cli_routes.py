"""Public protection and recovery evidence are usable without private APIs."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import h5py
import numpy as np


def cli(*args: object) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, "-m", "h5reclaim", *map(str, args)],
                          capture_output=True, text=True, check=False)


class V11PublicRoutesTests(unittest.TestCase):
    def test_family_bundle_reports_context_as_uninspected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with h5py.File(str(root / "part%03d.h5"), "w", driver="family", memb_size=1024) as handle:
                handle.create_dataset("readings", data=np.arange(100, dtype="<u4"), chunks=(20,))
            members = sorted(root.glob("part[0-9][0-9][0-9].h5"))
            manifest = root / "family.json"
            manifest.write_text(json.dumps({"schema_version": 1, "member_size": 1024,
                                            "members": [{"index": index, "path": str(path),
                                                         "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
                                                        for index, path in enumerate(members)]}),
                                encoding="utf-8")
            output, report_path = root / "derived.h5", root / "evidence.json"
            result = cli("rescue", members[0], "--dataset", "/readings",
                         "--family-members", manifest, "--output", output, "--report", report_path)
            self.assertEqual(result.returncode, 0, result.stderr)
            report = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertEqual(report["scientific_context"]["status"], "uninspected")
            self.assertIsNone(report["historical_integrity"]["capture_sha256"])
            with h5py.File(output, "r") as handle:
                self.assertEqual(json.loads(handle["/_h5reclaim/report_json"][()]), report)
                np.testing.assert_array_equal(handle["readings"][:], np.arange(100, dtype="<u4"))

    def test_protect_verify_and_disposable_drill(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, bundle = root / "acquisition.h5", root / "prior.zip"
            with h5py.File(source, "w", libver="latest") as handle:
                handle.create_dataset("readings", data=np.arange(16, dtype="<u4"), chunks=(4,))
            original_sha = hashlib.sha256(source.read_bytes()).hexdigest()
            captured = cli("protect", source, "--dataset", "/readings", "--output", bundle)
            self.assertEqual(captured.returncode, 0, captured.stderr)
            self.assertIn("Retain this manifest SHA-256 separately", captured.stdout)
            from h5reclaim.protection_bundle import verify_protection_bundle
            # An evaluator pins the returned digest separately from the ZIP.
            digest = captured.stdout.split("Retain this manifest SHA-256 separately before damage: ")[1].splitlines()[0]
            verified = cli("verify-protection", bundle, "--manifest-sha256", digest,
                           "--source", source)
            self.assertEqual(verified.returncode, 0, verified.stderr)
            self.assertIn("verification | passed", verified.stdout)
            self.assertEqual(verify_protection_bundle(bundle, digest)["dataset_path"], "/readings")
            drilled = cli("drill-protection", bundle, source, "--manifest-sha256", digest)
            self.assertEqual(drilled.returncode, 0, drilled.stderr)
            self.assertIn("Parity losses rebuilt exactly:", drilled.stdout)
            self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), original_sha)
            damaged = root / "broken-root.h5"
            damaged.write_bytes(source.read_bytes())
            with damaged.open("r+b") as stream:
                stream.write(b"X" * 8)
            output, evidence = root / "restored.h5", root / "restored.json"
            rescued = cli("rescue", damaged, "--dataset", "/readings",
                          "--protection-bundle", bundle,
                          "--protection-manifest-sha256", digest, "--strict-history",
                          "--output", output, "--report", evidence)
            self.assertEqual(rescued.returncode, 0, rescued.stderr)
            report = json.loads(evidence.read_text(encoding="utf-8"))
            self.assertEqual(report["scientific_context"]["status"], "uninspected")
            self.assertEqual(report["historical_integrity"]["matching_units"], 16)
            self.assertEqual(report["protection_bundle"]["manifest_sha256"], digest)
            with h5py.File(output, "r") as handle:
                np.testing.assert_array_equal(handle["readings"][:], np.arange(16, dtype="<u4"))
                self.assertEqual(json.loads(handle["/_h5reclaim/report_json"][()]), report)
            parity_damaged = root / "broken-chunks.h5"
            parity_damaged.write_bytes(source.read_bytes())
            with h5py.File(source, "r") as intact, parity_damaged.open("r+b") as stream:
                for origin in ((0,), (4,)):
                    info = intact["readings"].id.get_chunk_info_by_coord(origin)
                    stream.seek(info.byte_offset)
                    old = stream.read(1)
                    stream.seek(info.byte_offset)
                    stream.write(bytes([old[0] ^ 0x5a]))
            parity_output, parity_report_path = root / "parity.h5", root / "parity.json"
            parity_result = cli(
                "rescue", parity_damaged, "--dataset", "/readings",
                "--protection-bundle", bundle, "--protection-manifest-sha256", digest,
                "--protection-method", "erasure", "--strict-history",
                "--output", parity_output, "--report", parity_report_path,
            )
            self.assertEqual(parity_result.returncode, 0, parity_result.stderr)
            parity_report = json.loads(parity_report_path.read_text(encoding="utf-8"))
            self.assertEqual(parity_report["reconstructed_from_erasure"], 2)
            self.assertEqual(parity_report["historical_integrity"]["matching_units"], 4)
            with h5py.File(parity_output, "r") as handle:
                np.testing.assert_array_equal(handle["readings"][:], np.arange(16, dtype="<u4"))
                self.assertEqual(json.loads(handle["/_h5reclaim/report_json"][()]), parity_report)

    def test_rescue_reports_history_and_omitted_scientific_context(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, output, report_path = (root / name for name in
                                           ("source.h5", "derived.h5", "evidence.json"))
            with h5py.File(source, "w", libver="latest") as handle:
                handle.attrs["lab"] = "station-7"
                group = handle.create_group("experiment")
                group.attrs["instrument"] = "sensor"
                dataset = group.create_dataset("readings", data=np.arange(8, dtype="<u4"), chunks=(2,))
                dataset.attrs["units"] = "m/s"
                group.create_dataset("calibration", data=np.arange(3, dtype="<u4"))
            result = cli("rescue", source, "--dataset", "/experiment/readings",
                         "--output", output, "--report", report_path)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("History: current-source evidence", result.stdout)
            report = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertEqual(report["historical_integrity"]["matching_units"], 0)
            self.assertEqual(report["scientific_context"]["status"], "bounded_observation")
            self.assertIn("calibration", [row["name"] for row in
                          report["scientific_context"]["source_metadata"]["ancestor_groups"][-1]["links"]])
            with h5py.File(output, "r") as handle:
                status = handle["/_h5reclaim/historical_status"][:]
                self.assertTrue(np.all(status == 0))
                self.assertEqual(json.loads(handle["/_h5reclaim/report_json"][()]), report)
                np.testing.assert_array_equal(handle["/experiment/readings"][:], np.arange(8, dtype="<u4"))


if __name__ == "__main__":
    unittest.main()
