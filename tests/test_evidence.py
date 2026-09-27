"""Adversarial reconciliation and raw-fragment export behavior."""

from __future__ import annotations

import hashlib
import copy
import json
import tempfile
import unittest
import zipfile
from dataclasses import replace
from pathlib import Path

from h5reclaim.evidence import (
    ChecksumEvidence, ChunkProposal, DatasetAnchor, EvidenceCheck, IndexLink,
    PhysicalExtent, SourceRecord, export_unassigned_fragments, reconcile,
    evidence_report_from_dict, load_evidence_report,
)


def digest(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def verified_checks() -> tuple[EvidenceCheck, ...]:
    return tuple(EvidenceCheck(name, "pass") for name in (
        "allocation", "coordinates", "datatype", "decoded_bytes", "filter_pipeline",
    ))


class EvidenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.raw = bytes(range(128))
        self.source = SourceRecord("damaged", len(self.raw), digest(self.raw))
        self.anchor = DatasetAnchor("damaged", "/experiment", PhysicalExtent("damaged", 8, 8), 32, (8,), (4,), 1)
        self.direct = IndexLink("root-to-raw", "damaged", "/experiment", 32, 64, "observed_index", PhysicalExtent("damaged", 36, 8))
        self.proposal = ChunkProposal(
            "candidate-a", PhysicalExtent("damaged", 64, 4), digest(self.raw[64:68]),
            "/experiment", (0,), ("root-to-raw",), digest(self.raw[64:68]), 4,
            checksum=ChecksumEvidence("fletcher32", "1234", "passed"), checks=verified_checks(),
        )

    def ledger(self, *, sources=None, datasets=None, links=None, proposals=None, metadata=()):
        return reconcile(
            [self.source] if sources is None else sources,
            [self.anchor] if datasets is None else datasets,
            [self.direct] if links is None else links,
            [self.proposal] if proposals is None else proposals,
            metadata,
        )

    def test_direct_path_accepts_only_checked_unique_mapping(self) -> None:
        report = self.ledger()
        self.assertEqual(report.decisions[0].status, "accepted")
        self.assertEqual(report.decisions[0].integrity, "stored_checksum_passed")
        encoded = json.loads(json.dumps(report.to_dict()))
        self.assertEqual(encoded["proposals"][0]["coordinate"], [0])
        self.assertEqual(encoded["links"][0]["pointer_extent"]["offset"], 36)
        self.assertEqual(encoded["sources"][0]["sha256"], self.source.sha256)

    def test_suggested_shape_and_scan_without_anchored_path_remains_unassigned(self) -> None:
        scanned = replace(self.proposal, index_link_ids=())
        report = self.ledger(proposals=[scanned])
        self.assertEqual(report.decisions[0].status, "unassigned")
        self.assertIn("no_anchored_index_path", report.decisions[0].reasons)
        wrong = replace(self.direct, kind="hypothesis")
        self.assertEqual(self.ledger(links=[wrong]).decisions[0].status, "unassigned")

    def test_duplicate_coordinate_and_overlapping_bytes_invalidate_both_claims(self) -> None:
        duplicate = replace(self.proposal, proposal_id="candidate-b", extent=PhysicalExtent("damaged", 66, 4),
                            raw_sha256=digest(self.raw[66:70]), coordinate=(0,))
        report = self.ledger(proposals=[self.proposal, duplicate])
        self.assertEqual([d.status for d in report.decisions], ["contradicted", "contradicted"])
        self.assertEqual({item.code for item in report.contradictions}, {"competing_coordinate", "overlapping_payloads"})

    def test_cross_dataset_physical_alias_is_contradiction(self) -> None:
        second_anchor = replace(self.anchor, path="/other", object_header=PhysicalExtent("damaged", 16, 8))
        other_link = replace(self.direct, link_id="other", dataset_path="/other", child_offset=65)
        other = replace(self.proposal, proposal_id="other-candidate", dataset_path="/other", coordinate=(4,),
                        index_link_ids=("other",), extent=PhysicalExtent("damaged", 65, 4), raw_sha256=digest(self.raw[65:69]))
        report = self.ledger(datasets=[self.anchor, second_anchor], links=[self.direct, other_link],
                             proposals=[self.proposal, other])
        self.assertTrue(all(d.status == "contradicted" for d in report.decisions))
        self.assertIn("overlapping_payloads", {item.code for item in report.contradictions})

    def test_metadata_overlap_and_malformed_coordinate_are_not_exported_as_data(self) -> None:
        report = self.ledger(metadata=[PhysicalExtent("damaged", 64, 2)])
        self.assertEqual(report.decisions[0].status, "contradicted")
        self.assertIn("payload_overlaps_metadata", report.decisions[0].reasons)
        # Parsed pointer bytes are metadata even when the caller omits them
        # from the additional reserved-range list.
        overlapping_pointer = replace(self.direct, pointer_extent=PhysicalExtent("damaged", 64, 2))
        self.assertIn("payload_overlaps_metadata", self.ledger(links=[overlapping_pointer]).decisions[0].reasons)
        for coordinate in ((1,), (8,), (0, 0)):
            with self.subTest(coordinate=coordinate):
                proposal = replace(self.proposal, coordinate=coordinate)
                decision = self.ledger(proposals=[proposal]).decisions[0]
                self.assertEqual(decision.status, "unassigned")
                self.assertIn("coordinate_out_of_bounds_or_unaligned", decision.reasons)

    def test_failed_filter_check_or_stored_checksum_refuses_assignment(self) -> None:
        failed = replace(self.proposal, checks=verified_checks() + (EvidenceCheck("filter_pipeline", "fail"),))
        self.assertIn("check_failed", self.ledger(proposals=[failed]).decisions[0].reasons)
        bad_checksum = replace(self.proposal, checksum=ChecksumEvidence("fletcher32", "1234", "failed"))
        self.assertEqual(self.ledger(proposals=[bad_checksum]).decisions[0].status, "unassigned")
        no_checksum = replace(self.proposal, checksum=ChecksumEvidence("none", None, "absent"))
        self.assertEqual(self.ledger(proposals=[no_checksum]).decisions[0].integrity, "not_independently_verified")
        no_pointer_evidence = replace(self.direct, pointer_extent=None)
        self.assertIn("no_anchored_index_path", self.ledger(links=[no_pointer_evidence]).decisions[0].reasons)

    def test_bridged_link_needs_two_distinct_recorded_sides_and_checks(self) -> None:
        left = IndexLink("left", "damaged", "/experiment", 16, 48, "observed_sibling", PhysicalExtent("damaged", 20, 8), "left")
        right = IndexLink("right", "damaged", "/experiment", 80, 48, "observed_sibling", PhysicalExtent("damaged", 84, 8), "right")
        bridge = IndexLink(
            "bridge", "damaged", "/experiment", 32, 48, "bridged_index",
            PhysicalExtent("damaged", 36, 8), corroborator_ids=("left", "right"),
            checks=tuple(EvidenceCheck(name, "pass") for name in (
                "parent_key_interval", "reciprocal_sibling_links", "unique_node", "node_level",
            )),
        )
        leaf_chunk = IndexLink("leaf-chunk", "damaged", "/experiment", 48, 64, "observed_index", PhysicalExtent("damaged", 52, 8))
        candidate = replace(self.proposal, index_link_ids=("bridge", "leaf-chunk"))
        self.assertEqual(self.ledger(links=[left, right, bridge, leaf_chunk], proposals=[candidate]).decisions[0].status, "accepted")
        bad_bridge = replace(bridge, corroborator_ids=("left", "left"))
        self.assertIn("no_anchored_index_path", self.ledger(links=[left, right, bad_bridge, leaf_chunk],
                            proposals=[candidate]).decisions[0].reasons)
        failed_bridge = replace(bridge, checks=(EvidenceCheck("parent_key_interval", "fail"),))
        self.assertIn("no_anchored_index_path", self.ledger(links=[left, right, failed_bridge, leaf_chunk],
                            proposals=[candidate]).decisions[0].reasons)

    def test_fragment_export_uses_only_unassigned_raw_bytes_and_checks_source_hash(self) -> None:
        scan = replace(self.proposal, index_link_ids=(), dataset_path="/possibly", coordinate=(0,))
        report = self.ledger(proposals=[scan])
        with tempfile.TemporaryDirectory() as directory:
            source_path = Path(directory) / "damaged.h5"
            destination = Path(directory) / "fragments.zip"
            source_path.write_bytes(self.raw)
            export_unassigned_fragments(report, {"damaged": source_path}, destination)
            self.assertEqual(source_path.read_bytes(), self.raw)
            with zipfile.ZipFile(destination) as archive:
                manifest = json.loads(archive.read("manifest.json"))
                self.assertEqual(archive.read("fragments/0000.bin"), self.raw[64:68])
                self.assertEqual(manifest["fragments"][0]["proposed_dataset_path_unverified"], "/possibly")
                self.assertEqual(manifest["fragments"][0]["decision"]["status"], "unassigned")
            with self.assertRaises(FileExistsError):
                export_unassigned_fragments(report, {"damaged": source_path}, destination)
            source_path.write_bytes(b"x" + self.raw[1:])
            with self.assertRaisesRegex(ValueError, "source hash differs"):
                export_unassigned_fragments(report, {"damaged": source_path}, Path(directory) / "bad.zip")
            self.assertFalse((Path(directory) / "bad.zip").exists())
            with self.assertRaisesRegex(ValueError, "only unresolved"):
                export_unassigned_fragments(self.ledger(), {"damaged": source_path}, Path(directory) / "accepted.zip",
                                            proposal_ids=["candidate-a"])

    def test_invalid_fragment_digest_or_outside_extent_cannot_publish(self) -> None:
        wrong_digest = replace(self.proposal, index_link_ids=(), raw_sha256="0" * 64)
        outside = replace(self.proposal, index_link_ids=(), extent=PhysicalExtent("damaged", 127, 4))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "source.h5"
            path.write_bytes(self.raw)
            for proposal in (wrong_digest, outside):
                with self.subTest(proposal=proposal):
                    output = Path(directory) / "out.zip"
                    with self.assertRaises(ValueError):
                        export_unassigned_fragments(self.ledger(proposals=[proposal]), {"damaged": path}, output)
                    self.assertFalse(output.exists())

    def test_reloaded_report_recomputes_decisions_and_checks_wrapper_source(self) -> None:
        scan = replace(self.proposal, index_link_ids=())
        encoded = json.loads(json.dumps(self.ledger(proposals=[scan]).to_dict()))
        loaded = evidence_report_from_dict(encoded)
        self.assertEqual(loaded.decisions[0].status, "unassigned")
        with tempfile.TemporaryDirectory() as directory:
            source_path = Path(directory) / "source.h5"
            source_path.write_bytes(self.raw)
            outer = {
                "source": {
                    "path": "/attacker/path/is/never/used.h5",
                    "size_bytes": self.source.size_bytes,
                    "sha256_before": self.source.sha256,
                    "sha256_after": self.source.sha256,
                },
                "evidence_ledger": encoded,
            }
            report_path = Path(directory) / "report.json"
            report_path.write_text(json.dumps(outer), encoding="utf-8")
            checked = load_evidence_report(report_path)
            destination = Path(directory) / "raw.zip"
            export_unassigned_fragments(checked, {"damaged": source_path}, destination)
            self.assertTrue(destination.exists())
            altered = copy.deepcopy(outer)
            altered["evidence_ledger"]["decisions"][0]["status"] = "accepted"
            report_path.write_text(json.dumps(altered), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "do not reconcile"):
                load_evidence_report(report_path)
            altered = copy.deepcopy(outer)
            altered["source"]["sha256_before"] = "0" * 64
            report_path.write_text(json.dumps(altered), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "disagree"):
                load_evidence_report(report_path)
            report_path.write_text('{"schema_version":1,"schema_version":1}', encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "duplicate JSON key"):
                load_evidence_report(report_path)


if __name__ == "__main__":
    unittest.main()
