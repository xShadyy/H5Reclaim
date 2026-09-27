"""Authentic direct v1 B-tree root: exact export, integrity failure, safe refusal."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import h5py
import numpy as np

from h5reclaim.format import H5File


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "corpus/files/H-H1_GWOSC_4KHZ_R1-1126259447-32.hdf5"
DATASET = "/strain/Strain"
PINNED_SHA = "c5ea87beced5094b56b4d694f3a36e6d59f74d3a46e546b980a6a199d1f252a9"


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _invoke(source: Path, directory: Path) -> tuple[subprocess.CompletedProcess[str], Path, Path]:
    output, report_path = directory / "recovered.h5", directory / "report.json"
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT / "src") + os.pathsep + env.get("PYTHONPATH", "")
    process = subprocess.run(
        [sys.executable, "-m", "h5reclaim", "recover", str(source),
         "--dataset", DATASET, "--output", str(output), "--report", str(report_path)],
        cwd=directory, env=env, capture_output=True, text=True, timeout=60, check=False,
    )
    return process, output, report_path


class LevelZeroGWOscTests(unittest.TestCase):
    def test_direct_index_is_not_hardcoded_to_gwosc_shape_or_dtype(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "synthetic.h5"
            data = np.random.default_rng(4096).integers(
                0, 2**32, size=(64, 64), dtype=np.uint32,
            )
            with h5py.File(source, "x", libver=("earliest", "v108")) as handle:
                handle.create_dataset("measurements", data=data, dtype="<u4", chunks=(16, 16))
            with h5py.File(source, "r") as handle, H5File(source) as raw:
                selected = handle["/measurements"]
                layout = raw.read_dataset_layout(int(h5py.h5o.get_info(selected.id).addr))
                direct_root = raw.read_tree(layout.root_address)
                self.assertEqual(direct_root.level, 0)
                self.assertEqual(len(direct_root.entries), 16)
            output, report_path = root / "recovered.h5", root / "recovery.json"
            env = os.environ.copy()
            env["PYTHONPATH"] = str(ROOT / "src") + os.pathsep + env.get("PYTHONPATH", "")
            process = subprocess.run(
                [sys.executable, "-m", "h5reclaim", "recover", str(source),
                 "--dataset", "/measurements", "--output", str(output),
                 "--report", str(report_path)],
                cwd=root, env=env, capture_output=True, text=True, timeout=60, check=False,
            )
            self.assertEqual(process.returncode, 0, process.stderr)
            report = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertEqual(report["index"]["root_level"], 0)
            self.assertEqual(report["counts"]["recovered"], 16)
            self.assertEqual(report["reconstructed_chunks"], 0)
            self.assertEqual({m["integrity"] for m in report["mappings"]}, {"not_independently_verified"})
            with h5py.File(output, "r") as result:
                np.testing.assert_array_equal(result["/measurements"][...], data)

    def test_real_direct_index_exact_and_corruption_remains_uncertain(self) -> None:
        self.assertEqual(_sha(SOURCE), PINNED_SHA)
        with h5py.File(SOURCE, "r") as handle, H5File(SOURCE) as raw:
            selected = handle[DATASET]
            self.assertEqual(selected.shape, (131072,))
            self.assertEqual(selected.chunks, (2048,))
            self.assertEqual(selected.dtype, np.dtype("<f8"))
            self.assertEqual(selected.id.get_num_chunks(), 64)
            layout = raw.read_dataset_layout(int(h5py.h5o.get_info(selected.id).addr), rank=1)
            root = raw.read_tree(layout.root_address, rank=1, element_size=8)
            self.assertEqual(root.level, 0)
            self.assertEqual(len(root.entries), 64)
            self.assertEqual(len(raw.walk_tree(layout.root_address, rank=1, element_size=8).nodes), 1)
            for i, entry in enumerate(root.entries):
                info = selected.id.get_chunk_info(i)
                self.assertEqual(entry.key.offsets, (i * 2048, 0))
                self.assertEqual(
                    (raw.absolute(entry.address), entry.key.stored_size, entry.key.filter_mask),
                    (info.byte_offset, info.size, info.filter_mask),
                )
            first_payload = selected.id.get_chunk_info(0).byte_offset
            first_pointer_offset = root.entries[0].pointer_offset
            first_pointer = root.entries[0].address.to_bytes(raw.superblock.offset_size, "little")
            undefined_pointer = b"\xff" * raw.superblock.offset_size
            self.assertNotEqual(first_pointer, undefined_pointer)
            root_sibling_offset = raw.absolute(root.address) + 8
            unexpected_sibling = int(h5py.h5o.get_info(selected.id).addr).to_bytes(
                raw.superblock.offset_size, "little"
            )

        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            healthy_case = folder / "direct_root"; healthy_case.mkdir()
            # Recovery uses only the supplied file. The untouched scientific
            # source stays available to this test as independent truth.
            healthy_input = healthy_case / "input.h5"
            healthy_input.write_bytes(SOURCE.read_bytes())
            good, exported, report_path = _invoke(healthy_input, healthy_case)
            self.assertEqual(good.returncode, 0, good.stderr)
            report = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertTrue(report["complete"])
            self.assertEqual(report["counts"]["recovered"], 64)
            self.assertEqual(report["index"]["root_level"], 0)
            self.assertEqual(report["index"]["broken_links"], 0)
            self.assertEqual(report["reconstructed_chunks"], 0)
            self.assertEqual({m["route"] for m in report["mappings"]}, {"intact_tree"})
            self.assertEqual({m["integrity"] for m in report["mappings"]}, {"fletcher32_verified"})
            with h5py.File(SOURCE, "r") as truth, h5py.File(exported, "r") as output:
                np.testing.assert_array_equal(
                    truth[DATASET][...].view("<u8"), output[DATASET][...].view("<u8")
                )
                copied = set(report["dataset"]["attributes_copied"])
                omitted = set(report["dataset"]["attributes_omitted"])
                self.assertFalse(copied & omitted)
                self.assertEqual(copied | omitted, set(truth[DATASET].attrs))
                for key in copied:
                    self.assertIn(key, output[DATASET].attrs)
                    np.testing.assert_array_equal(truth[DATASET].attrs[key], output[DATASET].attrs[key])
                np.testing.assert_array_equal(
                    output["/_h5reclaim/chunk_status"][...], np.ones(64, dtype="u1")
                )

            payload_case = folder / "corrupt_payload"; payload_case.mkdir()
            payload_input = payload_case / "input.h5"
            image = bytearray(SOURCE.read_bytes())
            self.assertEqual(image[first_payload], 0x78)
            image[first_payload] = 0
            payload_input.write_bytes(image)
            before = _sha(payload_input)
            partial, partial_output, partial_path = _invoke(payload_input, payload_case)
            self.assertEqual(partial.returncode, 0, partial.stderr)
            self.assertEqual(_sha(payload_input), before)
            partial_report = json.loads(partial_path.read_text(encoding="utf-8"))
            self.assertEqual(partial_report["outcome"], "partial")
            self.assertEqual(partial_report["counts"]["recovered"], 63)
            self.assertEqual(partial_report["counts"]["decode_failed"], 1)
            self.assertEqual(partial_report["reconstructed_chunks"], 0)
            with h5py.File(SOURCE, "r") as truth, h5py.File(partial_output, "r") as output:
                statuses = output["/_h5reclaim/chunk_status"][...]
                self.assertEqual(int(statuses[0]), 6)
                self.assertTrue(np.all(statuses[1:] == 1))
                for index in range(1, 64):
                    start = index * 2048
                    np.testing.assert_array_equal(
                        output[DATASET][start:start + 2048].view("<u8"),
                        truth[DATASET][start:start + 2048].view("<u8"),
                    )

            pointer_case = folder / "missing_payload_pointer"; pointer_case.mkdir()
            pointer_input = pointer_case / "input.h5"
            pointer_bytes = bytearray(SOURCE.read_bytes())
            self.assertEqual(
                pointer_bytes[first_pointer_offset:first_pointer_offset + len(first_pointer)], first_pointer,
            )
            pointer_bytes[first_pointer_offset:first_pointer_offset + len(first_pointer)] = undefined_pointer
            pointer_input.write_bytes(pointer_bytes)
            before = _sha(pointer_input)
            refused, absent_output, absent_report = _invoke(pointer_input, pointer_case)
            self.assertEqual(refused.returncode, 2, refused.stdout + refused.stderr)
            self.assertFalse(absent_output.exists())
            self.assertFalse(absent_report.exists())
            self.assertEqual(_sha(pointer_input), before)

            sibling_case = folder / "false_root_sibling"; sibling_case.mkdir()
            sibling_input = sibling_case / "input.h5"
            sibling_bytes = bytearray(SOURCE.read_bytes())
            self.assertEqual(
                sibling_bytes[root_sibling_offset:root_sibling_offset + len(undefined_pointer)],
                undefined_pointer,
            )
            sibling_bytes[root_sibling_offset:root_sibling_offset + len(unexpected_sibling)] = unexpected_sibling
            sibling_input.write_bytes(sibling_bytes)
            refused, absent_output, absent_report = _invoke(sibling_input, sibling_case)
            self.assertEqual(refused.returncode, 2, refused.stdout + refused.stderr)
            self.assertFalse(absent_output.exists())
            self.assertFalse(absent_report.exists())
            self.assertEqual(_sha(SOURCE), PINNED_SHA)


if __name__ == "__main__":
    unittest.main()
