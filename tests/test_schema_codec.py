"""Differential checks against HDF5's actual fixed-width raw chunk writes."""

from __future__ import annotations

import itertools
import tempfile
import unittest
import zlib
from pathlib import Path

import h5py
import numpy as np

from h5reclaim.format import H5File
from h5reclaim.metadata import UnsupportedCase, read_dataset_spec
from h5reclaim.recovery import recover
from h5reclaim.schema_codec import (
    ChunkDecodeError, FilterDescriptor, MissingFilterDecoder,
    decode_chunk, validate_stored_size,
)


class SchemaCodecTests(unittest.TestCase):
    def test_rank_three_deep_and_rank_four_edge_indexes_preserve_values(self) -> None:
        fixtures = (
            ((10, 10, 10), (2, 2, 2), "<i2", False, 1),
            ((4, 5, 6, 7), (2, 2, 3, 4), ">f4", True, 0),
        )
        for shape, chunks, dtype, filtered, expected_root_level in fixtures:
            with self.subTest(shape=shape), tempfile.TemporaryDirectory() as directory:
                source = Path(directory) / "source.h5"
                output = Path(directory) / "output.h5"
                report = Path(directory) / "report.json"
                values = np.arange(np.prod(shape), dtype=dtype).reshape(shape)
                with h5py.File(source, "w", libver=("earliest", "v108")) as handle:
                    handle.create_dataset(
                        "data", data=values, chunks=chunks,
                        maxshape=(None, *shape[1:]),
                        compression="gzip" if filtered else None,
                        fletcher32=filtered,
                    )
                spec = read_dataset_spec(source, "/data")
                with H5File(source) as reader:
                    layout = reader.read_dataset_layout(spec.object_address, rank=len(shape))
                    root = reader.read_tree(
                        layout.root_address, rank=len(shape),
                        element_size=np.dtype(dtype).itemsize,
                    )
                    self.assertEqual(root.level, expected_root_level)
                source_before = source.read_bytes()
                result = recover(source, "/data", output, report)
                self.assertEqual(source.read_bytes(), source_before)
                self.assertTrue(result["complete"])
                self.assertEqual(result["counts"]["recovered"], int(np.prod(spec.chunk_grid)))
                with h5py.File(output, "r") as handle:
                    dataset = handle["data"]
                    self.assertEqual(dataset.maxshape, (None, *shape[1:]))
                    self.assertEqual(dataset.dtype, np.dtype(dtype))
                    np.testing.assert_array_equal(dataset[:], values)
                    self.assertTrue(np.all(handle["/_h5reclaim/chunk_status"][:] == 1))

    def test_dataset_selection_never_follows_external_or_soft_link(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            related = Path(directory) / "related.h5"
            source = Path(directory) / "selected.h5"
            with h5py.File(related, "w") as handle:
                handle.create_dataset("secret", data=np.arange(4, dtype="<u4"), chunks=(4,))
            with h5py.File(source, "w") as handle:
                group = handle.create_group("experiment")
                group.create_dataset("hard", data=np.arange(4, dtype="<u4"), chunks=(4,))
                group["soft"] = h5py.SoftLink("/experiment/hard")
                group["external"] = h5py.ExternalLink(str(related), "/secret")
                handle["external_group"] = h5py.ExternalLink(str(related), "/")
            self.assertEqual(read_dataset_spec(source, "/experiment/hard").shape, (4,))
            for selection in (
                "/experiment/soft", "/experiment/external", "/external_group/secret",
            ):
                with self.subTest(selection=selection):
                    with self.assertRaisesRegex(UnsupportedCase, "local hard links"):
                        read_dataset_spec(source, selection)
            for selection in ("experiment/hard", "/experiment//hard", "/experiment/../experiment/hard",
                              "/experiment/hard/children", "/_h5reclaim/data"):
                with self.subTest(selection=selection):
                    with self.assertRaises(UnsupportedCase):
                        read_dataset_spec(source, selection)

    def test_legacy_index_keys_own_edge_coordinates_for_other_widths(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            for dtype in ("u1", ">i2", "<f4", ">f8"):
                with self.subTest(dtype=dtype):
                    source = Path(directory) / "legacy.h5"
                    values = np.arange(30, dtype=dtype).reshape(5, 6)
                    with h5py.File(source, "w", libver=("earliest", "v108")) as handle:
                        handle.create_dataset("data", data=values, chunks=(4, 4))
                    spec = read_dataset_spec(source, "/data")
                    with H5File(source) as reader:
                        layout = reader.read_dataset_layout(spec.object_address, rank=2)
                        self.assertEqual(layout.element_size, np.dtype(dtype).itemsize)
                        walk = reader.walk_tree(
                            layout.root_address, rank=2, element_size=layout.element_size
                        )
                        positions = {
                            entry.key.offsets[:2] for node in walk.nodes
                            if node.level == 0 for entry in node.entries
                        }
                        self.assertEqual(positions, {(0, 0), (0, 4), (4, 0), (4, 4)})

    def test_numeric_endianness_edge_chunks_and_built_in_filter_pipeline(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            for file_dtype in ("u1", "<u2", ">i4", "<f4", ">f8"):
                for filtered in (False, True):
                    with self.subTest(dtype=file_dtype, filtered=filtered):
                        source = Path(directory) / "data.h5"
                        values = np.arange(30, dtype=file_dtype).reshape(5, 6)
                        with h5py.File(source, "w", libver=("earliest", "v108")) as handle:
                            handle.create_dataset(
                                "data", data=values, chunks=(4, 4),
                                shuffle=filtered,
                                compression="gzip" if filtered else None,
                                fletcher32=filtered,
                            )
                        spec = read_dataset_spec(source, "/data")
                        self.assertEqual(spec.chunk_grid, (2, 2))
                        self.assertEqual(np.dtype(spec.dtype), np.dtype(file_dtype))
                        self.assertEqual(spec.maxshape, (5, 6))
                        self.assertEqual(spec.filters, (2, 1, 3) if filtered else ())
                        with h5py.File(source, "r") as handle:
                            for coordinate in ((0, 0), (0, 4), (4, 0), (4, 4)):
                                mask, raw = handle["/data"].id.read_direct_chunk(coordinate)
                                payload = decode_chunk(raw, spec, mask)
                                decoded = np.frombuffer(payload, dtype=file_dtype).reshape(4, 4)
                                source_selection = tuple(
                                    slice(start, min(start + 4, length))
                                    for start, length in zip(coordinate, values.shape)
                                )
                                visible_selection = tuple(
                                    slice(0, part.stop - part.start) for part in source_selection
                                )
                                np.testing.assert_array_equal(
                                    decoded[visible_selection], values[source_selection]
                                )

    def test_nondefault_filter_order_and_growing_extent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "growing.h5"
            values = np.arange(30, dtype=">i2").reshape(5, 6)
            with h5py.File(source, "w", libver=("earliest", "v108")) as handle:
                space = h5py.h5s.create_simple((5, 6), (h5py.h5s.UNLIMITED, 6))
                creation = h5py.h5p.create(h5py.h5p.DATASET_CREATE)
                creation.set_chunk((4, 4))
                creation.set_fletcher32()
                creation.set_shuffle()
                creation.set_deflate(4)
                dataset = h5py.h5d.create(
                    handle.id, b"growing", h5py.h5t.py_create(np.dtype(">i2")),
                    space, dcpl=creation,
                )
                dataset.write(h5py.h5s.ALL, h5py.h5s.ALL, values)
            spec = read_dataset_spec(source, "/growing")
            self.assertEqual(spec.maxshape, (None, 6))
            self.assertEqual(spec.filters, (3, 2, 1))
            with h5py.File(source, "r") as handle:
                for coordinate in ((0, 0), (0, 4), (4, 0), (4, 4)):
                    mask, raw = handle["growing"].id.read_direct_chunk(coordinate)
                    decoded = np.frombuffer(decode_chunk(raw, spec, mask), dtype=">i2").reshape(4, 4)
                    rows, cols = min(4, 5 - coordinate[0]), min(4, 6 - coordinate[1])
                    np.testing.assert_array_equal(
                        decoded[:rows, :cols],
                        values[coordinate[0]:coordinate[0]+rows,
                               coordinate[1]:coordinate[1]+cols],
                    )
            output = Path(directory) / "export.h5"
            report = Path(directory) / "report.json"
            result = recover(source, "/growing", output, report)
            self.assertTrue(result["complete"])
            self.assertEqual(result["counts"]["recovered"], 4)
            with h5py.File(output, "r") as handle:
                self.assertEqual(handle["/growing"].maxshape, (None, 6))
                self.assertEqual(handle["/growing"].dtype, np.dtype(">i2"))
                np.testing.assert_array_equal(handle["/growing"][:], values)
                np.testing.assert_array_equal(
                    handle["/_h5reclaim/chunk_status"][:],
                    np.ones((2, 2), dtype="u1"),
                )

    def test_optional_filter_mask_and_mandatory_checksum(self) -> None:
        from h5reclaim.metadata import DatasetSpec
        spec = DatasetSpec("/x", 128, (16,), (16,), "<u4", (1, 3),
                           filter_pipeline=(FilterDescriptor(1, 1, (6,)),
                                            FilterDescriptor(3, 0, ())))
        nominal = np.arange(16, dtype="<u4").tobytes()
        # Compression skipped, checksum retained.
        from h5reclaim.schema_codec import fletcher32
        stored = nominal + fletcher32(nominal).to_bytes(4, "little")
        self.assertEqual(decode_chunk(stored, spec, 0b01), nominal)
        with self.assertRaisesRegex(ChunkDecodeError, "mandatory filter"):
            decode_chunk(nominal, spec, 0b10)
        with self.assertRaisesRegex(ChunkDecodeError, "absent filter"):
            decode_chunk(nominal, spec, 0b100)
        with self.assertRaisesRegex(ChunkDecodeError, "fixed-length"):
            validate_stored_size(spec, len(nominal), 0b01)

    def test_deflate_then_shuffle_then_checksum_real_write(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "unusual_order.h5"
            expected = np.arange(64, dtype="<u4")
            with h5py.File(source, "w") as handle:
                creation = h5py.h5p.create(h5py.h5p.DATASET_CREATE)
                creation.set_chunk((64,))
                creation.set_deflate(6)
                creation.set_shuffle()
                creation.set_fletcher32()
                space = h5py.h5s.create_simple((64,))
                dataset = h5py.h5d.create(handle.id, b"data", h5py.h5t.py_create(expected.dtype),
                                          space, dcpl=creation)
                dataset.write(h5py.h5s.ALL, h5py.h5s.ALL, expected)
            spec = read_dataset_spec(source, "/data")
            self.assertEqual(spec.filters, (1, 2, 3))
            with h5py.File(source, "r") as handle:
                mask, raw = handle["data"].id.read_direct_chunk((0,))
            self.assertEqual(decode_chunk(raw, spec, mask), expected.tobytes())

    def test_odd_byte_length_checksum_in_either_order(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            for order in ("checksum_first", "compression_first"):
                with self.subTest(order=order):
                    source = Path(directory) / "odd.h5"
                    expected = np.arange(31, dtype="u1")
                    with h5py.File(source, "w") as handle:
                        creation = h5py.h5p.create(h5py.h5p.DATASET_CREATE)
                        creation.set_chunk((31,))
                        if order == "checksum_first":
                            creation.set_fletcher32()
                            creation.set_deflate(5)
                        else:
                            creation.set_deflate(5)
                            creation.set_fletcher32()
                        dataset = h5py.h5d.create(
                            handle.id, b"data", h5py.h5t.py_create(expected.dtype),
                            h5py.h5s.create_simple((31,)), dcpl=creation,
                        )
                        dataset.write(h5py.h5s.ALL, h5py.h5s.ALL, expected)
                    spec = read_dataset_spec(source, "/data")
                    with h5py.File(source, "r") as handle:
                        mask, raw = handle["data"].id.read_direct_chunk((0,))
                    self.assertEqual(decode_chunk(raw, spec, mask), expected.tobytes())

    def test_every_order_of_three_builtin_filters_matches_native_values(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            values = np.arange(30, dtype="<u2").reshape(5, 6)
            methods = {"shuffle": "set_shuffle", "deflate": "set_deflate",
                       "fletcher": "set_fletcher32"}
            for order in itertools.permutations(methods):
                with self.subTest(order=order):
                    source = Path(directory) / "all_orders.h5"
                    with h5py.File(source, "w", libver=("earliest", "v108")) as handle:
                        creation = h5py.h5p.create(h5py.h5p.DATASET_CREATE)
                        creation.set_chunk((4, 4))
                        for name in order:
                            arguments = (6,) if name == "deflate" else ()
                            getattr(creation, methods[name])(*arguments)
                        dataset = h5py.h5d.create(
                            handle.id, b"data", h5py.h5t.py_create(values.dtype),
                            h5py.h5s.create_simple(values.shape), dcpl=creation,
                        )
                        dataset.write(h5py.h5s.ALL, h5py.h5s.ALL, values)
                    spec = read_dataset_spec(source, "/data")
                    with h5py.File(source, "r") as handle:
                        for coordinate in ((0, 0), (0, 4), (4, 0), (4, 4)):
                            mask, raw = handle["data"].id.read_direct_chunk(coordinate)
                            chunk = np.frombuffer(
                                decode_chunk(raw, spec, mask), dtype=values.dtype
                            ).reshape(4, 4)
                            rows = min(4, values.shape[0] - coordinate[0])
                            cols = min(4, values.shape[1] - coordinate[1])
                            np.testing.assert_array_equal(
                                chunk[:rows, :cols],
                                values[coordinate[0]:coordinate[0]+rows,
                                       coordinate[1]:coordinate[1]+cols],
                            )

    def test_missing_decoder_distinct_from_corrupt_bytes(self) -> None:
        from h5reclaim.metadata import DatasetSpec
        spec = DatasetSpec("/x", 128, (16,), (16,), "<u4", (32000, 3),
                           filter_pipeline=(FilterDescriptor(32000, 1, ()),
                                            FilterDescriptor(3, 0, ())))
        nominal = bytes(64)
        with self.assertRaisesRegex(MissingFilterDecoder, "32000"):
            decode_chunk(b"X" * 32, spec, 0)
        # The unknown optional filter is explicitly skipped, so checksum and
        # value bytes can be checked without loading a plugin.
        from h5reclaim.schema_codec import fletcher32
        raw = nominal + fletcher32(nominal).to_bytes(4, "little")
        self.assertEqual(decode_chunk(raw, spec, 1), nominal)
        with self.assertRaisesRegex(ChunkDecodeError, "checksum"):
            decode_chunk(raw[:-1] + b"\xff", spec, 1)

    def test_unknown_optional_filter_is_inventoried_without_a_plugin(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "unknown.h5"
            nominal = np.arange(4, dtype="<u4").tobytes()
            with h5py.File(source, "w", libver="latest") as handle:
                creation = h5py.h5p.create(h5py.h5p.DATASET_CREATE)
                creation.set_chunk((4,))
                creation.set_filter(32000, h5py.h5z.FLAG_OPTIONAL, ())
                space = h5py.h5s.create_simple((4,))
                dataset = h5py.h5d.create(handle.id, b"data", h5py.h5t.py_create(np.dtype("<u4")),
                                          space, dcpl=creation)
                dataset.write_direct_chunk((0,), nominal, filter_mask=0)
            spec = read_dataset_spec(source, "/data")
            self.assertEqual(spec.filters, (32000,))
            with h5py.File(source, "r") as handle:
                mask, raw = handle["data"].id.read_direct_chunk((0,))
            with self.assertRaises(MissingFilterDecoder):
                decode_chunk(raw, spec, mask)
            self.assertEqual(decode_chunk(raw, spec, 1), nominal)

    def test_missing_decoder_marks_partial_result_unknown(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "unknown.h5"
            output = Path(directory) / "partial.h5"
            report = Path(directory) / "partial.json"
            with h5py.File(source, "w", libver=("earliest", "v108")) as handle:
                creation = h5py.h5p.create(h5py.h5p.DATASET_CREATE)
                creation.set_chunk((4,))
                creation.set_filter(32000, h5py.h5z.FLAG_OPTIONAL, ())
                dataset = h5py.h5d.create(
                    handle.id, b"data", h5py.h5t.py_create(np.dtype("<u4")),
                    h5py.h5s.create_simple((4,)), dcpl=creation,
                )
                dataset.write_direct_chunk(
                    (0,), np.arange(4, dtype="<u4").tobytes(), filter_mask=0,
                )
            source_before = source.read_bytes()
            result = recover(source, "/data", output, report)
            self.assertFalse(result["complete"])
            self.assertEqual(result["counts"]["decoder_unavailable"], 1)
            self.assertEqual(result["counts"]["decode_failed"], 0)
            self.assertEqual(result["failed_chunks"][0]["status"], "decoder_unavailable")
            self.assertEqual(source.read_bytes(), source_before)
            with h5py.File(output, "r") as handle:
                self.assertEqual(int(handle["/_h5reclaim/chunk_status"][0]), 7)

    def test_bounded_deflate_and_trailing_stream_rejection(self) -> None:
        from h5reclaim.metadata import DatasetSpec
        spec = DatasetSpec("/x", 128, (16,), (16,), "<u4", (1,))
        data = bytes(spec.chunk_bytes)
        self.assertEqual(decode_chunk(zlib.compress(data), spec, 0), data)
        for raw in (zlib.compress(data + b"x"),
                    zlib.compress(data) + b"garbage", b"broken zlib"):
            with self.subTest(raw=raw[:8]):
                with self.assertRaises(ChunkDecodeError):
                    decode_chunk(raw, spec, 0)

    def test_reduced_precision_type_remains_refused(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "shifted.h5"
            with h5py.File(source, "w") as handle:
                datatype = h5py.h5t.STD_U32LE.copy()
                datatype.set_precision(28)
                datatype.set_offset(4)
                space = h5py.h5s.create_simple((4,))
                creation = h5py.h5p.create(h5py.h5p.DATASET_CREATE)
                creation.set_chunk((4,))
                h5py.h5d.create(handle.id, b"values", datatype, space, dcpl=creation)
            with self.assertRaisesRegex(UnsupportedCase, "noncanonical"):
                read_dataset_spec(source, "/values")


if __name__ == "__main__":
    unittest.main()
