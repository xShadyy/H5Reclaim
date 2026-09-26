# 0002: Two-sided attribution and embedded validity

Date: 2026-09-27

The first recovery case requires a selected dataset's layout message to anchor the v1 B-tree root. A lost interior root child is accepted only if both adjacent reachable leaves independently identify the same detached node, its sibling pointers reciprocate, its level is zero, and its first/final key boundaries match the missing parent slot. Every accepted chunk must pass coordinate alignment, bounds, size, filter-mask, uniqueness, and payload-range checks. A file-wide `TREE` signature scan is insufficient evidence of ownership.

This rule deliberately leaves regions unresolved if one side of the bridge is missing or contradictory. The output stores a chunk-level status map inside HDF5 so an unknown region cannot be inferred from its fill value. The report gives the accepted evidence route and states that structural attribution does not prove historical integrity of unfiltered bytes.

The implementation is bounded to one dataset, one level-one root, and at most one broken link. This scope follows the verified fixture and preserves explicit failures for other cases rather than expanding from a single success without evidence.

The output and JSON report are written in private temporary directories and published only after the analysis and source-hash check succeed. These directories prevent another user in a shared destination directory from replacing the temporary HDF5 path while h5py opens it. A failed report publication removes the newly published output. The output embeds the full report at `/_h5reclaim/report_json`, so the finalized HDF5 file retains evidence even if the companion JSON is separated. Existing destinations and aliases are rejected. A finished partial output has `complete: false` and unknown chunks marked `allocation_unknown`.
