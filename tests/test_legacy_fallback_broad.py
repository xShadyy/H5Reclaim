"""Legacy rooted metadata recovery across common scientific numeric layouts."""

from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path

import h5py
import numpy as np

from h5reclaim.format import FormatError, H5File, UnsupportedFormat
from h5reclaim.metadata_fallback import _old_messages, read_dataset_spec_fallback
from h5reclaim.metadata import read_dataset_spec
from h5reclaim.recovery import recover


class LegacyFallbackBroaderTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / "damaged.h5"

    def _make(self, shape, chunks, dtype, *, filters=False, attributes=0):
        expected = np.arange(np.prod(shape), dtype=dtype).reshape(shape)
        with h5py.File(self.source, "w", libver="earliest") as handle:
            group = handle.create_group("experiment")
            selected = group.create_dataset(
                "readings", data=expected, chunks=chunks,
                **({"shuffle": True, "compression": "gzip", "fletcher32": True}
                   if filters else {}),
            )
            address = int(h5py.h5o.get_info(selected.id).addr)
            for index in range(attributes):
                selected.attrs[f"calibration_{index:04}"] = f"temp-{index}"
        return expected, address

    def _break_optional_fill(self, address):
        with H5File(self.source) as reader:
            fill = [m for m in _old_messages(reader, address) if m.kind == 5]
        self.assertEqual(len(fill), 1)
        raw = bytearray(self.source.read_bytes())
        raw[fill[0].absolute_offset] = 255
        self.source.write_bytes(raw)
        with h5py.File(self.source) as handle:
            with self.assertRaises((KeyError, OSError)):
                _ = handle["/experiment/readings"]

    def test_damaged_optional_metadata_recovers_exact_3d_filtered_edge_chunks(self):
        expected, address = self._make((5, 4, 3), (2, 2, 3), ">f4", filters=True)
        self._break_optional_fill(address)
        damaged_digest = hashlib.sha256(self.source.read_bytes()).hexdigest()
        rooted = read_dataset_spec_fallback(self.source, "/experiment/readings")
        self.assertEqual(rooted.spec.shape, expected.shape)
        self.assertEqual(rooted.spec.chunks, (2, 2, 3))
        self.assertEqual(rooted.spec.dtype, ">f4")
        self.assertEqual(rooted.spec.filters, (2, 1, 3))
        result = recover(self.source, "/experiment/readings", self.root / "out.h5",
                         self.root / "out.json")
        self.assertTrue(result["complete"])
        self.assertEqual(result["counts"]["recovered"], 6)
        self.assertTrue(all(m["integrity"] == "fletcher32_verified" for m in result["mappings"]))
        with h5py.File(self.root / "out.h5") as output:
            np.testing.assert_array_equal(output["/experiment/readings"][:], expected)
        self.assertEqual(hashlib.sha256(self.source.read_bytes()).hexdigest(), damaged_digest)

    def test_damaged_optional_metadata_recovers_exact_4d_integer(self):
        expected, address = self._make((4, 3, 2, 2), (2, 3, 2, 2), "<i2")
        self._break_optional_fill(address)
        result = recover(self.source, "/experiment/readings", self.root / "out.h5",
                         self.root / "out.json")
        self.assertTrue(result["complete"])
        self.assertEqual(result["dataset"]["dtype"], "<i2")
        with h5py.File(self.root / "out.h5") as output:
            np.testing.assert_array_equal(output["/experiment/readings"][:], expected)

    def test_several_legacy_header_continuations_recover_selected_values(self):
        expected, address = self._make((20,), (8,), "<u1", attributes=500)
        with H5File(self.source) as reader:
            continuations = [m for m in _old_messages(reader, address) if m.kind == 16]
        self.assertGreaterEqual(len(continuations), 2)
        self._break_optional_fill(address)
        result = recover(self.source, "/experiment/readings", self.root / "out.h5",
                         self.root / "out.json")
        self.assertTrue(result["complete"])
        self.assertEqual(result["counts"]["recovered"], 3)
        with h5py.File(self.root / "out.h5") as output:
            np.testing.assert_array_equal(output["/experiment/readings"][:], expected)

    def test_older_growing_sparse_dataset_marks_absent_chunks_unknown(self):
        with h5py.File(self.source, "w", libver="earliest") as handle:
            selected = handle.create_dataset("readings", shape=(10, 10),
                maxshape=(None, 15), chunks=(4, 4), dtype="<u2")
            expected = np.arange(16, dtype="<u2").reshape(4, 4)
            selected[:4, :4] = expected
            address = int(h5py.h5o.get_info(selected.id).addr)
        with H5File(self.source) as reader:
            fill = next(m for m in _old_messages(reader, address) if m.kind == 5)
        damaged = bytearray(self.source.read_bytes())
        damaged[fill.absolute_offset] = 255
        self.source.write_bytes(damaged)
        result = recover(self.source, "/readings", self.root / "out.h5",
                         self.root / "out.json")
        self.assertFalse(result["complete"])
        self.assertEqual(result["dataset"]["maxshape"], [None, 15])
        self.assertEqual(result["counts"]["recovered"], 1)
        self.assertEqual(result["counts"]["allocation_unknown"], 8)
        with h5py.File(self.root / "out.h5") as output:
            np.testing.assert_array_equal(output["/readings"][:4, :4], expected)
            status = output["/_h5reclaim/chunk_status"][:]
            self.assertEqual(status.shape, (3, 3))
            self.assertEqual(status[0, 0], 1)
            self.assertTrue(np.all(status.reshape(-1)[1:] == 2))

    def test_older_shuffle_element_width_contradiction_refuses(self):
        _expected, address = self._make((12,), (6,), "<i4", filters=True)
        with H5File(self.source) as reader:
            pipeline = next(m for m in _old_messages(reader, address) if m.kind == 11)
        raw = bytearray(self.source.read_bytes())
        # The first v1 pipeline entry is shuffle: 8-byte entry, padded name,
        # followed by one 32-bit element-width parameter.
        parameter = pipeline.absolute_offset + 8 + 8 + 8
        self.assertEqual(raw[parameter:parameter + 4], (4).to_bytes(4, "little"))
        raw[parameter:parameter + 4] = (2).to_bytes(4, "little")
        self.source.write_bytes(raw)
        with self.assertRaisesRegex(UnsupportedFormat, "shuffle width contradicts"):
            read_dataset_spec_fallback(self.source, "/experiment/readings")

    def test_older_noncanonical_integer_precision_refuses(self):
        _expected, address = self._make((12,), (6,), "<i4")
        with H5File(self.source) as reader:
            dtype = next(m for m in _old_messages(reader, address) if m.kind == 3)
        raw = bytearray(self.source.read_bytes())
        self.assertEqual(raw[dtype.absolute_offset + 10], 32)
        raw[dtype.absolute_offset + 10] = 31
        self.source.write_bytes(raw)
        with self.assertRaisesRegex(UnsupportedFormat, "noncanonical fixed-point"):
            read_dataset_spec_fallback(self.source, "/experiment/readings")

    def test_repeated_continuation_pointer_refuses_before_layout(self):
        _expected, address = self._make((20,), (8,), "<u1", attributes=500)
        with H5File(self.source) as reader:
            messages = _old_messages(reader, address)
            continuations = [m for m in messages if m.kind == 16]
            self.assertGreaterEqual(len(continuations), 2)
            osize = reader.superblock.offset_size
        raw = bytearray(self.source.read_bytes())
        raw[continuations[1].absolute_offset:continuations[1].absolute_offset + osize] = (
            continuations[0].data[:osize]
        )
        self.source.write_bytes(raw)
        with H5File(self.source) as reader:
            with self.assertRaisesRegex(FormatError, "cyclic or repeated"):
                reader.read_dataset_layout(address, rank=1)
        with self.assertRaisesRegex(FormatError, "invalid or repeated older continuation"):
            read_dataset_spec_fallback(self.source, "/experiment/readings")

    def test_valid_cross_group_alias_and_empty_group_pass_rooted_census(self):
        with h5py.File(self.source, "w", libver="earliest") as handle:
            selected = handle.create_group("lab").create_dataset(
                "readings", data=np.arange(8, dtype="<u4"), chunks=(8,))
            elsewhere = handle.create_group("elsewhere")
            elsewhere["alias"] = selected
            elsewhere.create_group("empty")
            address = int(h5py.h5o.get_info(selected.id).addr)
        self.assertEqual(read_dataset_spec(self.source, "/lab/readings").object_address,
                         address)
        self.assertEqual(read_dataset_spec_fallback(
            self.source, "/lab/readings").spec.object_address, address)

    def test_v0_superblock_with_compact_group_links_and_external_sibling(self):
        related = self.root / "related.h5"
        with h5py.File(related, "w") as handle:
            handle.create_dataset("secret", data=np.arange(4, dtype="<u4"))
        # Default h5py creation can mix a v0 superblock, v1 object headers,
        # and modern-style compact link messages with eight-byte padding.
        with h5py.File(self.source, "w") as handle:
            group = handle.create_group("experiment")
            selected = group.create_dataset("hard", data=np.arange(4, dtype="<u4"),
                                            chunks=(4,))
            group["soft"] = h5py.SoftLink("/experiment/hard")
            group["external"] = h5py.ExternalLink(str(related), "/secret")
            handle.create_group("elsewhere")["alias"] = selected
            address = int(h5py.h5o.get_info(selected.id).addr)
        with H5File(self.source) as reader:
            self.assertEqual(reader.superblock.version, 0)
        for path in ("/experiment/hard", "/elsewhere/alias"):
            self.assertEqual(read_dataset_spec(self.source, path).object_address, address)
            self.assertEqual(read_dataset_spec_fallback(
                self.source, path).spec.object_address, address)
        with self.assertRaisesRegex(UnsupportedFormat, "not a local hard link"):
            read_dataset_spec_fallback(self.source, "/experiment/soft")
        with self.assertRaisesRegex(UnsupportedFormat, "not a local hard link"):
            read_dataset_spec_fallback(self.source, "/experiment/external")

    def test_mixed_legacy_compact_link_padding_must_be_zero(self):
        with h5py.File(self.source, "w") as handle:
            group = handle.create_group("experiment")
            selected = group.create_dataset(
                "hard", data=np.arange(4, dtype="<u4"), chunks=(4,))
            group["soft"] = h5py.SoftLink("/experiment/hard")
            group["external"] = h5py.ExternalLink("related.h5", "/secret")
            handle["root_soft"] = h5py.SoftLink("/experiment")
            address = int(h5py.h5o.get_info(selected.id).addr)
            group_address = int(h5py.h5o.get_info(group.id).addr)
        with H5File(self.source) as reader:
            link = next(m for m in _old_messages(reader, group_address)
                        if m.kind == 6 and b"hard" in m.data)
            self.assertEqual(reader.read_at(address, 1), b"\x01")
        raw = bytearray(self.source.read_bytes())
        # Version, flags, name-length, 4-byte name, and 8-byte address.
        padding = link.absolute_offset + 3 + len("hard") + 8
        self.assertEqual(raw[padding], 0)
        raw[padding] = 0x5a
        self.source.write_bytes(raw)
        with self.assertRaisesRegex(FormatError, "compact link length or padding"):
            read_dataset_spec_fallback(self.source, "/experiment/hard")

    def test_cross_group_alias_redirection_refuses_native_and_raw_routes(self):
        with h5py.File(self.source, "w", libver="earliest") as handle:
            group = handle.create_group("lab")
            group.create_dataset("science", data=np.arange(16, dtype="<u4").reshape(4, 4),
                                 chunks=(4, 4))
            other = group.create_dataset("distractor", data=np.full((4, 4), 99, dtype="<u4"),
                                         chunks=(4, 4))
            handle.create_group("elsewhere")["alias"] = other
            distractor_address = int(h5py.h5o.get_info(other.id).addr)
        anchored = read_dataset_spec_fallback(self.source, "/lab/science")
        entry = anchored.link_chain[-1]
        with H5File(self.source) as reader:
            osize, lsize = reader.superblock.offset_size, reader.superblock.length_size
        damaged = bytearray(self.source.read_bytes())
        pointer = entry.link_message_offset + lsize
        damaged[pointer:pointer + osize] = distractor_address.to_bytes(osize, "little")
        self.source.write_bytes(damaged)
        with h5py.File(self.source) as handle:
            # Native HDF5 alone silently follows the redirected link and
            # serves valid but scientifically wrong values.
            self.assertEqual(int(handle["/lab/science"][0, 0]), 99)
        for reader in (read_dataset_spec, read_dataset_spec_fallback):
            with self.subTest(reader=reader.__name__):
                with self.assertRaisesRegex(FormatError,
                                            "rooted hard-link count contradicts"):
                    reader(self.source, "/lab/science")
        output, report = self.root / "out.h5", self.root / "out.json"
        with self.assertRaisesRegex(FormatError, "rooted hard-link count contradicts"):
            recover(self.source, "/lab/science", output, report)
        self.assertFalse(output.exists())
        self.assertFalse(report.exists())


if __name__ == "__main__":
    unittest.main()
