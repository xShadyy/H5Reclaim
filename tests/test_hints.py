"""Operator statements stay bounded and separate from file observations."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from h5reclaim.hints import (
    DatasetHints, HintsError, compare_hints, load_hints, parse_hints,
    require_no_conflicts,
)
from h5reclaim.metadata import DatasetSpec


class OperatorHintsTest(unittest.TestCase):
    def test_expected_metadata_can_be_compared_to_independent_observation(self) -> None:
        damaged_digest = "A0" * 32
        hints = parse_hints(json.dumps({
            "schema_version": 1,
            "dataset": {
                "path": "/experiment/raw", "shape": [256, 256],
                "chunks": [16, 16], "dtype": "<u4", "filters": [],
            },
            "source_sha256": damaged_digest,
            "note": "Copied from acquisition config",
        }).encode())
        self.assertEqual(hints.trust_level, "unverified_operator_assertion")
        self.assertEqual(hints.source_sha256, damaged_digest.lower())
        observed = DatasetSpec(
            path="/experiment/raw", object_address=2048, shape=(256, 256),
            chunks=(16, 16), dtype="<u4", filters=(),
        )
        checks = compare_hints(hints, observed_dataset=observed,
                               input_sha256=damaged_digest.lower())
        self.assertEqual({check.status for check in checks}, {"matches"})
        require_no_conflicts(checks)
        self.assertEqual(hints.trust_level, "unverified_operator_assertion")

    def test_conflicting_assertions_are_refused(self) -> None:
        hints = DatasetHints(path="/experiment/raw", shape=(32, 32),
                             source_sha256="a" * 64)
        observed = DatasetSpec(path="/experiment/raw", object_address=1,
                               shape=(64, 64), chunks=(8, 8))
        checks = compare_hints(hints, observed_dataset=observed, input_sha256="b" * 64)
        self.assertEqual([(check.field, check.status) for check in checks], [
            ("path", "matches"), ("shape", "conflicts"),
            ("source_sha256", "conflicts"),
        ])
        with self.assertRaisesRegex(HintsError, "shape, source_sha256"):
            require_no_conflicts(checks)

    def test_unobserved_assertions_do_not_become_verified(self) -> None:
        hints = parse_hints(b'{"schema_version":1,"dataset":{"path":"/data",'
                            b'"filters":[]},"source_sha256":"'
                            + b"f" * 64 + b'"}')
        checks = compare_hints(hints)
        self.assertEqual({check.status for check in checks}, {"unobserved"})
        self.assertIsNone(checks[0].observed)

    def test_partial_inventory_compares_only_exactly_observed_fields(self) -> None:
        hints = DatasetHints(path="/data", shape=(4, 6), chunks=(2, 3), dtype="<u4",
                             filters=(3, 1))
        checks = compare_hints(hints, observed_fields={
            "path": "/data", "shape": [4, 6], "chunks": None,
            # Inventory datatype strings may describe logical rather than raw
            # storage, so the caller omits dtype unless directly comparable.
            "filters": [3, 1],
        })
        self.assertEqual({c.field: c.status for c in checks}, {
            "path": "matches", "shape": "matches", "chunks": "unobserved",
            "dtype": "unobserved", "filters": "matches",
        })
        with self.assertRaises(HintsError):
            compare_hints(hints, observed_fields={"filters": [{"id": 3}]})
        with self.assertRaises(HintsError):
            compare_hints(hints, observed_dataset=DatasetSpec(
                path="/data", object_address=10, shape=(4, 6), chunks=(2, 3)),
                observed_fields={"path": "/data"})

    def test_rejects_duplicate_or_unknown_fields_and_unsupported_schema(self) -> None:
        for raw in (
            b'{"schema_version":1,"schema_version":1,"dataset":{"path":"/x"}}',
            b'{"schema_version":1,"dataset":{"path":"/x","shape":[2],"shape":[3]}}',
            b'{"schema_version":1,"dataset":{"path":"/x","is_verified":true}}',
            b'{"schema_version":true,"dataset":{"path":"/x"}}',
            b'{"schema_version":2,"dataset":{"path":"/x"}}',
            b'{"schema_version":1,"dataset":{"path":"/x"},"trust":"verified"}',
        ):
            with self.subTest(raw=raw), self.assertRaises(HintsError):
                parse_hints(raw)

    def test_rejects_invalid_dimensions_path_filters_and_hash(self) -> None:
        for dataset, root in (
            ({"path": "relative"}, {}),
            ({"path": "/x\x1b[31m"}, {}),
            ({"path": "/_h5reclaim/report_json"}, {}),
            ({"path": "/x", "shape": [True]}, {}),
            ({"path": "/x", "chunks": [0]}, {}),
            ({"path": "/x", "shape": [2], "chunks": [1, 1]}, {}),
            ({"path": "/x", "filters": [False]}, {}),
            ({"path": "/x", "filters": [65536]}, {}),
            ({"path": "/x"}, {"source_sha256": "not a digest"}),
            ({"path": "/x"}, {"note": "hello\x1b[31m"}),
            ({"path": "/x", "dtype": "u4\x00"}, {}),
        ):
            with self.subTest(dataset=dataset, root=root), self.assertRaises(HintsError):
                parse_hints(json.dumps({"schema_version": 1, "dataset": dataset, **root}).encode())

    def test_file_size_and_encoding_are_bounded(self) -> None:
        for content in (b" " * 65537, b"\xff", b'{"schema_version":NaN}'):
            with self.subTest(content=content[:30]), self.assertRaises(HintsError):
                parse_hints(content)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "hints.json"
            path.write_bytes(b" " * 65537)
            with self.assertRaisesRegex(HintsError, "65536 bytes"):
                load_hints(path)
            path.write_text('{"schema_version":1,"dataset":{"path":"/measurements"}}',
                            encoding="utf-8")
            self.assertEqual(load_hints(path).path, "/measurements")


if __name__ == "__main__":
    unittest.main()
