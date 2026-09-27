"""Actual damaged-native-open recovery needs only the damaged copy."""

from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path

import h5py
import numpy as np

from h5reclaim.diagnose import diagnose
from h5reclaim.format import H5File
from h5reclaim.metadata_fallback import _messages, _old_messages
from h5reclaim.modern_indexes import ModernH5File, lookup3
from h5reclaim.recovery import recover


class FallbackRecoveryTests(unittest.TestCase):
    def test_damaged_optional_metadata_old_and_modern(self) -> None:
        for latest in (False, True):
            with self.subTest(latest=latest), tempfile.TemporaryDirectory() as temporary:
                base = Path(temporary)
                damaged = base / "damaged.h5"
                expected = np.arange(16, dtype="<u4").reshape((4, 4))
                with h5py.File(damaged, "w", libver="latest" if latest else "earliest") as f:
                    selected = f.create_group("lab").create_dataset(
                        "science", data=expected, chunks=(4, 4),
                    )
                    object_address = h5py.h5o.get_info(selected.id).addr
                with (ModernH5File(damaged) if latest else H5File(damaged)) as reader:
                    message = next(m for m in (
                        _messages(reader, object_address) if latest else
                        _old_messages(reader, object_address)
                    ) if m.kind == 5)
                raw = bytearray(damaged.read_bytes())
                raw[message.absolute_offset] = 255  # Native HDF5 refuses optional fill version.
                if latest:
                    flags = raw[object_address + 5]
                    size_width = 1 << (flags & 3)
                    prefix = 6 + (16 if flags & 0x20 else 0) + (4 if flags & 0x10 else 0) + size_width
                    chunk_size = int.from_bytes(raw[object_address + prefix - size_width:object_address + prefix], "little")
                    end = object_address + prefix + chunk_size
                    raw[end:end + 4] = lookup3(raw[object_address:end]).to_bytes(4, "little")
                damaged.write_bytes(raw)
                before = hashlib.sha256(raw).hexdigest()
                with h5py.File(damaged) as source:
                    with self.assertRaises((KeyError, OSError)):
                        source["/lab/science"]

                diagnosis = diagnose(damaged, "/lab/science")
                self.assertEqual(diagnosis["next_action"], "inspect_anchored_index")
                self.assertEqual(diagnosis["condition"], "rooted_metadata_fallback")
                output, report_path = base / "recovered.h5", base / "report.json"
                report = recover(damaged, "/lab/science", output, report_path)
                self.assertEqual(report["counts"]["recovered"], 1)
                self.assertEqual([entry["name"] for entry in
                                  report["metadata_resolution"]["selected_hard_link_chain"]],
                                 ["lab", "science"])
                self.assertTrue(report["evidence_ledger"]["decisions"][0]["status"] == "accepted")
                with h5py.File(output) as result:
                    np.testing.assert_array_equal(result["/lab/science"][:], expected)
                self.assertEqual(hashlib.sha256(damaged.read_bytes()).hexdigest(), before)


if __name__ == "__main__":
    unittest.main()
