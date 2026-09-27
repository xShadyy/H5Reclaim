"""Adapt real HDF5 parser observations into accepted or refused provenance."""

from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
import unittest
import zipfile
from dataclasses import replace
from pathlib import Path

import h5py
import numpy as np

from h5reclaim.evidence_adapter import EvidenceAdapterError, build_recovery_evidence
from h5reclaim.evidence import export_unassigned_fragments
from h5reclaim.format import H5File
from h5reclaim.metadata import read_dataset_spec
from h5reclaim.recovery import ChunkRecord, _decode_chunk
from tools.make_broken_link_fixture import make_damage


def _record(reader: H5File, spec, leaf, entry, route="intact_tree") -> ChunkRecord:
    coordinate = tuple(entry.key.offsets[:-1])
    raw = reader.read_at(entry.address, entry.key.stored_size)
    return ChunkRecord(
        tuple(value // chunk for value, chunk in zip(coordinate, spec.chunks)),
        coordinate, entry.address, reader.absolute(entry.address), len(raw),
        leaf.address, route, {}, _decode_chunk(raw, spec, entry.key.filter_mask),
    )


class AdapterTests(unittest.TestCase):
    def _source(self, directory: Path, side: int, chunk: int) -> Path:
        path = directory / "pristine.h5"
        with h5py.File(path, "x", libver=("earliest", "v108")) as handle:
            handle.create_dataset(
                "measurements", data=np.arange(side * side, dtype="<u4").reshape(side, side),
                chunks=(chunk, chunk),
            )
        return path

    def test_direct_root_and_deep_tree_have_full_index_paths(self) -> None:
        for side, chunk, expected_min_level in ((16, 4, 0), (64, 1, 2)):
            with self.subTest(side=side), tempfile.TemporaryDirectory() as directory:
                path = self._source(Path(directory), side, chunk)
                spec = read_dataset_spec(path, "/measurements")
                with H5File(path) as reader:
                    layout = reader.read_dataset_layout(spec.object_address)
                    root = reader.read_tree(layout.root_address)
                    self.assertGreaterEqual(root.level, expected_min_level)
                    walk = reader.walk_tree(layout.root_address)
                    leaf = next(node for node in walk.nodes if node.level == 0)
                    rec = _record(reader, spec, leaf, leaf.entries[0])
                    report = build_recovery_evidence(
                        spec, reader, walk, root, {leaf.address: (leaf, "intact_tree", {})},
                        [rec], source_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                        decode_chunk=_decode_chunk,
                    )
                    self.assertEqual(report.decisions[0].status, "accepted")
                    self.assertEqual(len(report.proposals[0].index_link_ids), root.level + 1)
                    self.assertEqual(report.decisions[0].integrity, "not_independently_verified")
                    self.assertEqual(report.proposals[0].raw_sha256,
                                     hashlib.sha256(reader.read_at(rec.file_address, rec.length)).hexdigest())

    def test_detached_leaf_requires_unique_two_sided_reciprocal_bridge(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            original = self._source(folder, 12, 1)
            damaged = folder / "damaged.h5"
            make_damage(original, damaged, folder / "truth.json", "/measurements")
            spec = read_dataset_spec(damaged, "/measurements")
            with H5File(damaged) as reader:
                layout = reader.read_dataset_layout(spec.object_address)
                root = reader.read_tree(layout.root_address)
                self.assertEqual(root.level, 1)
                walk = reader.walk_tree(layout.root_address)
                candidate = reader.find_missing_child_candidates(layout.root_address)[0]
                leaf = candidate.node
                record = _record(reader, spec, leaf, leaf.entries[0], "reconstructed_link")
                leaves = {leaf.address: (leaf, "reconstructed_link", {})}
                kwargs = dict(
                    source_sha256=hashlib.sha256(damaged.read_bytes()).hexdigest(),
                    decode_chunk=_decode_chunk,
                )
                report = build_recovery_evidence(spec, reader, walk, root, leaves, [record], **kwargs)
                self.assertEqual(report.decisions[0].status, "accepted")
                self.assertEqual(len(report.proposals[0].index_link_ids), 2)
                self.assertEqual(sum(link.kind == "bridged_index" for link in report.links), 1)
                self.assertEqual(sum(link.kind == "observed_sibling" for link in report.links), 2)
                forged = replace(leaf, right_sibling=None)
                with self.assertRaisesRegex(EvidenceAdapterError, "reciprocal two-sided bridge"):
                    build_recovery_evidence(spec, reader, walk, root,
                                            {leaf.address: (forged, "reconstructed_link", {})},
                                            [record], **kwargs)

    def test_mismatched_payload_cannot_be_recast_as_verified(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = self._source(Path(directory), 16, 4)
            spec = read_dataset_spec(path, "/measurements")
            with H5File(path) as reader:
                layout = reader.read_dataset_layout(spec.object_address)
                root = reader.read_tree(layout.root_address)
                walk = reader.walk_tree(layout.root_address)
                record = _record(reader, spec, root, root.entries[0])
                wrong_payload = replace(record, payload=bytes(len(record.payload)))
                with self.assertRaisesRegex(EvidenceAdapterError, "record payload disagrees"):
                    build_recovery_evidence(
                        spec, reader, walk, root, {root.address: (root, "intact_tree", {})},
                        [wrong_payload], source_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                        decode_chunk=_decode_chunk,
                    )

    def test_indexed_but_undecodable_real_chunk_is_unassigned_raw_fragment(self) -> None:
        original = Path(__file__).resolve().parents[1] / "corpus/files/H-H1_GWOSC_4KHZ_R1-1126259447-32.hdf5"
        with tempfile.TemporaryDirectory() as directory:
            root_dir = Path(directory)
            damaged = root_dir / "damaged.hdf5"
            shutil.copyfile(original, damaged)
            with h5py.File(damaged, "r") as handle:
                offset = handle["/strain/Strain"].id.get_chunk_info(0).byte_offset
            with damaged.open("r+b") as handle:
                handle.seek(offset)
                handle.write(b"\x00")
            spec = read_dataset_spec(damaged, "/strain/Strain")
            with H5File(damaged) as reader:
                layout = reader.read_dataset_layout(spec.object_address, rank=1)
                root = reader.read_tree(layout.root_address, rank=1, element_size=8)
                walk = reader.walk_tree(layout.root_address, rank=1, element_size=8)
                entry = root.entries[0]
                ledger = build_recovery_evidence(
                    spec, reader, walk, root, {root.address: (root, "intact_tree", {})},
                    [], source_sha256=hashlib.sha256(damaged.read_bytes()).hexdigest(),
                    decode_chunk=_decode_chunk,
                    failed=[{"coordinate": [0], "source_address": entry.address}],
                )
                self.assertEqual(ledger.decisions[0].status, "unassigned")
                self.assertIn("check_failed", ledger.decisions[0].reasons)
                output = root_dir / "fragments.zip"
                export_unassigned_fragments(ledger, {"damaged": damaged}, output)
                with zipfile.ZipFile(output) as archive:
                    manifest = json.loads(archive.read("manifest.json"))
                    self.assertEqual(manifest["fragments"][0]["decision"]["status"], "unassigned")
                    self.assertEqual(archive.read("fragments/0000.bin"),
                                     reader.read_at(entry.address, entry.key.stored_size))


if __name__ == "__main__":
    unittest.main()
