"""Independent damage cases for the new dependency and bundle routes.

These checks construct the byte-presence oracle separately from HDF5 and the
exporter. Only complete, independently supplied elements may be marked valid.
"""

from __future__ import annotations

import hashlib
import random
import tempfile
import unittest
from pathlib import Path

import h5py
import numpy as np

from h5reclaim.external_raw_export import export_external_raw
from h5reclaim.family_bundle import _manifest as family_manifest, export_family
from h5reclaim.format import FormatError
from h5reclaim.dependency_routes import DependencyError, load_dependency_manifest
from h5reclaim.metadata_fallback import _messages
from h5reclaim.metadata import UnsupportedCase
from h5reclaim.modern_indexes import ModernH5File, lookup3
from h5reclaim.nonchunked_recovery import export_nonchunked
from h5reclaim.recovery import RecoveryError
from h5reclaim.split_bundle import _load_manifest as split_manifest
from h5reclaim.vds_export import export_vds


def _hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _redirect_modern_contiguous(path: Path, selected_address: int,
                                layout_offset: int, other_offset: int) -> None:
    raw = bytearray(path.read_bytes())
    flags = raw[selected_address + 5]
    width = 1 << (flags & 3)
    chunk_start = (selected_address + 6 + (16 if flags & 0x20 else 0)
                   + (4 if flags & 0x10 else 0) + width)
    chunk_length = int.from_bytes(raw[chunk_start - width:chunk_start], "little")
    checksum_at = chunk_start + chunk_length
    raw[layout_offset + 2:layout_offset + 10] = other_offset.to_bytes(8, "little")
    raw[checksum_at:checksum_at + 4] = lookup3(raw[selected_address:checksum_at]).to_bytes(4, "little")
    path.write_bytes(raw)


class AdversarialRouteTests(unittest.TestCase):
    def test_duplicate_manifest_keys_are_refused_at_every_nesting_level(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            related = root / "related.json"
            related.write_text('{"schema_version":1,"files":[{"declared_name":"a",'
                               '"path":"/tmp/a","sha256":"' + '0' * 64 + '",'
                               '"path":"/tmp/b"}]}', encoding="utf-8")
            with self.assertRaisesRegex(DependencyError, "duplicate.*path"):
                load_dependency_manifest(related)

            family = root / "family.json"
            family.write_text('{"schema_version":1,"member_size":1024,"members":[],'
                              '"members":[{"index":0,"path":"/tmp/a",'
                              '"sha256":"' + '0' * 64 + '"}]}', encoding="utf-8")
            with self.assertRaisesRegex(RecoveryError, "duplicate.*members"):
                family_manifest(family)
            with self.assertRaisesRegex(RecoveryError, "invalid family manifest version"):
                family_manifest({"schema_version": True, "member_size": 1024, "members": [{
                    "index": 0, "path": "/tmp/a", "sha256": "0" * 64,
                }]})

            split = root / "split.json"
            split.write_text('{"schema_version":1,"driver":"split",'
                             '"members":[],"driver":"other"}', encoding="utf-8")
            with self.assertRaisesRegex(RecoveryError, "duplicate.*driver"):
                split_manifest(split)

    def test_nonchunked_payload_redirected_into_rooted_sibling_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "source.h5"
            with h5py.File(source, "x", libver="latest") as file:
                selected = file.create_dataset("science/selected", data=np.arange(12, dtype="<i4"))
                other = file.create_dataset("science/other", data=np.full(12, 777, dtype="<i4"))
                address = int(h5py.h5o.get_info(selected.id).addr)
                other_offset = int(other.id.get_offset())
            with ModernH5File(source) as reader:
                layout = next(item for item in _messages(reader, address) if item.kind == 8)
            # Recompute the real object-header checksum. The payload is now
            # simultaneously claimed by two rooted dataset layout messages.
            _redirect_modern_contiguous(source, address, layout.absolute_offset, other_offset)
            with self.assertRaisesRegex(FormatError, "overlap|conflict|claim"):
                export_nonchunked(source, "/science/selected",
                                  root / "result.h5", root / "result.json")
            self.assertFalse((root / "result.h5").exists())

    def test_nonchunked_payload_redirected_into_chunked_sibling_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "source.h5"
            with h5py.File(source, "x", libver="latest") as file:
                selected = file.create_dataset("science/selected", data=np.arange(12, dtype="<i4"))
                chunked = file.create_dataset("elsewhere/other", data=np.full(12, 888, dtype="<i4"),
                                              chunks=(12,))
                address = int(h5py.h5o.get_info(selected.id).addr)
                other_offset = int(chunked.id.get_chunk_info_by_coord((0,)).byte_offset)
            with ModernH5File(source) as reader:
                layout = next(item for item in _messages(reader, address) if item.kind == 8)
            _redirect_modern_contiguous(source, address, layout.absolute_offset, other_offset)
            with self.assertRaisesRegex(FormatError, "overlap|conflict|claim"):
                export_nonchunked(source, "/science/selected",
                                  root / "result.h5", root / "result.json")
            self.assertFalse((root / "result.h5").exists())

    def test_external_segments_match_independent_byte_presence_oracle(self) -> None:
        rng = random.Random(825602)
        for trial in range(18):
            with self.subTest(trial=trial), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                dtype = np.dtype((">" if trial % 2 else "<") + ("u2" if trial % 3 else "i4"))
                shape = (3, 4)
                logical_length = int(np.prod(shape)) * dtype.itemsize
                expected = bytearray(logical_length)
                present = bytearray(logical_length)
                external = []
                manifest = {"schema_version": 1, "files": []}
                cursor = 0
                segment_number = 0
                while cursor < logical_length:
                    capacity = min(logical_length - cursor, rng.randint(1, 11))
                    name = f"raw_{segment_number}.bin"
                    path = root / name
                    offset = rng.randint(0, 3)
                    available = rng.randint(0, capacity)
                    # A declared source may be absent, or physically shorter
                    # than the extent the HDF5 layout says it covers.
                    supplied = segment_number == 0 or rng.randrange(3) != 0
                    contents = bytes(rng.randrange(256) for _ in range(offset + available))
                    if supplied:
                        path.write_bytes(contents)
                        manifest["files"].append({"declared_name": name,
                                                  "path": str(path), "sha256": _hash(path)})
                        expected[cursor:cursor + available] = contents[offset:]
                        present[cursor:cursor + available] = b"\x01" * available
                    external.append((name, offset, capacity))
                    cursor += capacity
                    segment_number += 1
                main = root / "main.h5"
                with h5py.File(main, "x") as file:
                    file.create_dataset("science", shape=shape, dtype=dtype,
                                        external=external)
                output, report = root / "result.h5", root / "result.json"
                valid = np.array([int(all(present[index:index + dtype.itemsize]))
                                  for index in range(0, logical_length, dtype.itemsize)],
                                 dtype="u1").reshape(shape)
                if not np.any(valid):
                    with self.assertRaises(UnsupportedCase):
                        export_external_raw(main, "/science", manifest, output, report)
                    self.assertFalse(output.exists())
                    continue
                summary = export_external_raw(main, "/science", manifest, output, report)
                self.assertEqual(summary["accepted_elements"], int(valid.sum()))
                with h5py.File(output, "r") as file:
                    np.testing.assert_array_equal(file["/_h5reclaim/validity"][:], valid)
                    # Unknown elements are deliberately omitted from the
                    # comparison; their zero representation is not evidence.
                    actual = file["science"][:].tobytes()
                    for element, accepted in enumerate(valid.flat):
                        if accepted:
                            start = element * dtype.itemsize
                            self.assertEqual(actual[start:start + dtype.itemsize],
                                             expected[start:start + dtype.itemsize])

    def test_vds_damaged_filtered_source_does_not_poison_other_mapping(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            broken, healthy, virtual = (root / name for name in
                                        ("broken.h5", "healthy.h5", "virtual.h5"))
            with h5py.File(broken, "x") as file:
                file.create_dataset("science", data=np.arange(4, dtype="<i4"),
                                    chunks=(4,), compression="gzip", fletcher32=True)
            with h5py.File(broken, "r") as file:
                address = file["science"].id.get_chunk_info_by_coord((0,)).byte_offset
            with broken.open("r+b") as stream:
                stream.seek(address)
                first = stream.read(1)
                stream.seek(address)
                stream.write(bytes([first[0] ^ 0x42]))
            with h5py.File(healthy, "x") as file:
                file["science"] = np.arange(40, 44, dtype="<i4")
            layout = h5py.VirtualLayout((8,), "<i4")
            layout[:4] = h5py.VirtualSource("broken.h5", "science", shape=(4,))
            layout[4:] = h5py.VirtualSource("healthy.h5", "science", shape=(4,))
            with h5py.File(virtual, "x") as file:
                file.create_virtual_dataset("science", layout, fillvalue=-123)
            manifest = {"schema_version": 1, "files": [
                {"declared_name": name, "path": str(path), "sha256": _hash(path)}
                for name, path in (("broken.h5", broken), ("healthy.h5", healthy))]}
            result = export_vds(virtual, "/science", root / "result.h5",
                                root / "result.json", manifest)
            self.assertEqual(result["accepted_elements"], 4)
            with h5py.File(root / "result.h5") as file:
                np.testing.assert_array_equal(file["/_h5reclaim/validity"][:],
                                              [0, 0, 0, 0, 1, 1, 1, 1])
                np.testing.assert_array_equal(file["science"][4:], [40, 41, 42, 43])

    def test_family_sparse_chunk_is_unknown_even_when_native_returns_fill(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            template = str(root / "member%03d.h5")
            with h5py.File(template, "w", driver="family", memb_size=1024) as file:
                dataset = file.create_dataset("science", shape=(12,), chunks=(4,),
                                              dtype="<i4", fillvalue=707)
                dataset[:4] = [1, 2, 3, 4]
                dataset[8:] = [9, 10, 11, 12]
            members = sorted(root.glob("member[0-9][0-9][0-9].h5"))
            manifest = {"schema_version": 1, "member_size": 1024, "members": [
                {"index": index, "path": str(path), "sha256": _hash(path)}
                for index, path in enumerate(members)]}
            result = export_family(manifest, "/science", root / "result.h5",
                                   root / "result.json")
            self.assertEqual((result["outcome"], result["accepted_elements"]), ("partial", 8))
            with h5py.File(root / "result.h5") as file:
                np.testing.assert_array_equal(file["/_h5reclaim/validity"][:], [1, 0, 1])
                np.testing.assert_array_equal(file["science"][:4], [1, 2, 3, 4])
                np.testing.assert_array_equal(file["science"][8:], [9, 10, 11, 12])


if __name__ == "__main__":
    unittest.main()
