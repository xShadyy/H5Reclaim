"""Capture-time independent chunk evidence, including refusal cases."""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

import h5py
import numpy as np

from h5reclaim.baseline import capture_baseline
from h5reclaim.metadata import UnsupportedCase
from h5reclaim.recovery import RecoveryError, analyze


class BaselineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.source = self.directory / "source.h5"
        self.baseline = self.directory / "baseline.json"

    def test_complete_capture_matches_nominal_chunk_payloads(self) -> None:
        with h5py.File(self.source, "x", libver="latest") as handle:
            handle.create_dataset("science", data=np.arange(30, dtype="<u4"), chunks=(8,), fletcher32=True)
        prior = hashlib.sha256(self.source.read_bytes()).hexdigest()
        captured = capture_baseline(self.source, "/science", self.baseline)
        self.assertEqual(captured, json.loads(self.baseline.read_text(encoding="utf-8")))
        self.assertEqual(captured["source_sha256"], prior)
        self.assertEqual([item["coordinate"] for item in captured["chunk_hashes"]], [[0], [8], [16], [24]])
        actual = analyze(self.source, "/science")
        self.assertEqual(
            [item["sha256"] for item in captured["chunk_hashes"]],
            [hashlib.sha256(record.payload).hexdigest() for record in actual.records],
        )
        self.assertEqual(hashlib.sha256(self.source.read_bytes()).hexdigest(), prior)

    def test_unknown_allocation_is_never_captured_as_fill(self) -> None:
        with h5py.File(self.source, "x", libver="latest") as handle:
            data = handle.create_dataset("science", shape=(24,), dtype="<u4", chunks=(8,))
            data[:8] = 12
        with self.assertRaisesRegex(UnsupportedCase, "every selected chunk"):
            capture_baseline(self.source, "/science", self.baseline)
        self.assertFalse(self.baseline.exists())

    def test_destination_cannot_replace_source_or_existing_record(self) -> None:
        with h5py.File(self.source, "x", libver="latest") as handle:
            handle.create_dataset("science", data=np.arange(8, dtype="<u4"), chunks=(8,))
        with self.assertRaises(RecoveryError):
            capture_baseline(self.source, "/science", self.source)
        self.baseline.write_text("keep", encoding="utf-8")
        with self.assertRaises(RecoveryError):
            capture_baseline(self.source, "/science", self.baseline)
        self.assertEqual(self.baseline.read_text(), "keep")
