"""Machine-readable capability boundaries for operators and integrations."""

from dataclasses import asdict
from .large_streaming import LargeBudget
from .recovery import VERSION


def capabilities():
    return {"tool": "h5reclaim", "version": VERSION,
        "large_structural": {"indexes": ["fixed_array", "extensible_array", "v2_btree"],
            "rank": "1..32 where the observed index family permits",
            "record_types": "bounded fixed-width records, including compound, array, enum, opaque and fixed string types; no heap pointers or references",
            "maximum_index_repairs_per_parent": 2,
            "multiple_index_repairs_per_dataset": True,
            "chunk_grid_bound": "min(max_chunks, max_grid, family-dependent worker-memory reserve)",
            "maximum_decoded_chunk_bytes": 64 << 20,
            "required_evidence": "rooted schema, unique original checksum-restoring checked child assignment for each damaged parent, checked index and nonoverlapping allocations",
            "filters": ["gzip", "shuffle", "Fletcher32", "LZF", "N-bit", "scale-offset (possibly lossy)", "SZIP when installed", "optional packaged hdf5plugin codecs"],
            "unresolved": "missing or undecodable chunks remain nonaccepted"},
        "repair_planner": {"default": ["original-checksum root pointer and signature corrections",
                "original-checksum object layout pointer and dimension corrections", "checked FA/EA/B-tree child links",
                "rooted v1 file-space declarations with checksummed v0 file-client FSHD/FSSE metadata",
                "one checksum-justified compact group hard link to a checked detached dataset header",
                "stale modern consistency flags", "structurally validated legacy signature restoration"],
            "requires_allow_reseal": ["superblock base/EOF normalization and checksum resealing",
                "reachable v2 object-header checksum resealing", "typed chunk-layout element-width correction",
                "layout-owned FA/EA/B-tree header checksum resealing", "depth-zero B-tree total-record reconciliation"],
            "file_space_requires_allow_reseal": "structurally checked FSHD/FSSE checksum resealing when original checksum restoration is unavailable",
            "publication": "exact regenerated plan, pinned input hash, independent covered-metadata preflight and audit JSONL",
            "not_implemented": ["missing metadata without independently retained earlier bytes or unique structural evidence",
                                "all file-space manager encodings and arbitrary orphan pruning",
                                "unconstrained checksum rewriting"]},
        "metadata_snapshots": "capture checked reachable metadata before damage, then restore exact bytes only if uncaptured bytes match and complete original hash is regained",
        "inspection": ["raw reachable-metadata and supported file-space preflight", "bounded marker candidates", "object-message and hex inspection"],
        "exports": ["whole-file rescue with checked detached group trees under synthetic paths",
                    "selected-dataset rescue", "bounded resumed batches", "optional Pyfive cross-reader export",
                    "official h5copy/h5repack integration when installed"],
        "verification": {"report_maps": True, "accepted_fixed_chunk_digests": True,
            "artifact_fingerprints": True, "historical_authenticity": "requires appropriate separately retained earlier evidence"},
        "reporting": ["JSON", "terminal", "self-contained searchable HTML"],
        "default_streaming_budget": asdict(LargeBudget()),
        "scope": "capability envelope, not a prediction of recovery success for arbitrary damaged files"}
