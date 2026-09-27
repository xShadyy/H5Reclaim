"""Translate verified v1 chunk-tree parsing into the generic evidence ledger.

This adapter runs while the original parser's stable private snapshot is
open. It deliberately rechecks tree paths and chunk decoding instead of
turning a recovery report's descriptive mapping dictionaries into proof.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping, Sequence
from typing import Any

import numpy as np

from .evidence import (
    ChecksumEvidence, ChunkProposal, DatasetAnchor, EvidenceCheck, EvidenceReport,
    IndexLink, PhysicalExtent, SourceRecord, reconcile,
)
from .format import H5File, TreeNode, TreeWalk
from .metadata import DatasetSpec


class EvidenceAdapterError(ValueError):
    """A proposed assignment cannot be represented by verified parser evidence."""


_PASS = tuple(EvidenceCheck(code, "pass") for code in (
    "allocation", "coordinates", "datatype", "decoded_bytes", "filter_pipeline",
))
_BRIDGE_PASS = tuple(EvidenceCheck(code, "pass") for code in (
    "parent_key_interval", "reciprocal_sibling_links", "unique_node", "node_level",
))


def build_recovery_evidence(
    spec: DatasetSpec,
    reader: H5File,
    walk: TreeWalk,
    root: TreeNode,
    leaves: Mapping[int, tuple[TreeNode, str, Mapping[str, Any]]],
    records: Sequence[Any],
    *,
    source_sha256: str,
    decode_chunk: Callable[[bytes, DatasetSpec, int], bytes],
    failed: Sequence[Mapping[str, Any]] = (),
    source_id: str = "damaged",
) -> EvidenceReport:
    """Create and reconcile a ledger for accepted and decode-failed chunks.

    ``records`` are recovery ``ChunkRecord`` objects. ``failed`` are its
    decode-failure dictionaries, whose raw address/size is resolved again
    through a verified leaf. No arbitrary scanned byte extent is accepted.
    The callback must be the same supported, bounded decoder used by
    recovery. A mismatch or unjustified accepted record raises an error.
    """
    try:
        return _build(
            spec, reader, walk, root, leaves, records, source_sha256,
            decode_chunk, failed, source_id,
        )
    except (IndexError, KeyError, TypeError, ValueError, OverflowError) as exc:
        if isinstance(exc, EvidenceAdapterError):
            raise
        raise EvidenceAdapterError(f"cannot justify recorded chunk evidence: {exc}") from exc


def _build(
    spec: DatasetSpec, reader: H5File, walk: TreeWalk, root: TreeNode,
    leaves: Mapping[int, tuple[TreeNode, str, Mapping[str, Any]]],
    records: Sequence[Any], source_sha256: str,
    decode_chunk: Callable[[bytes, DatasetSpec, int], bytes],
    failed: Sequence[Mapping[str, Any]], source_id: str,
) -> EvidenceReport:
    nodes = {node.address: node for node in walk.nodes}
    if len(nodes) != len(walk.nodes) or nodes.get(root.address) != root:
        raise EvidenceAdapterError("root is not uniquely present in the parsed tree")
    header_start = reader.absolute(spec.object_address)
    header_ranges = [
        (start, end) for start, end, kind in reader.metadata_ranges
        if kind == "selected object header" and start == header_start
    ]
    if len(header_ranges) != 1:
        raise EvidenceAdapterError("selected object header range is not uniquely parsed")
    header_start, header_end = header_ranges[0]
    anchor = DatasetAnchor(
        source_id, spec.path, PhysicalExtent(source_id, header_start, header_end - header_start),
        reader.absolute(root.address), spec.shape, spec.chunks, np.dtype(spec.dtype).itemsize,
    )
    source = SourceRecord(source_id, reader.size, source_sha256)
    offset_size = reader.superblock.offset_size
    link_records: list[IndexLink] = []
    link_ids: set[str] = set()
    parent_links: dict[int, str] = {}
    parent_addresses: dict[int, int] = {}
    for node in walk.nodes:
        if node.level == 0:
            continue
        for slot, entry in enumerate(node.entries):
            if entry.address is None:
                continue
            child = nodes.get(entry.address)
            if child is None or child.level != node.level - 1:
                raise EvidenceAdapterError("reachable index pointer has no parsed child at the expected level")
            if child.address in parent_links:
                raise EvidenceAdapterError("multiple parents claim one tree node")
            link_id = f"index:{node.address}:{slot}"
            link_records.append(IndexLink(
                link_id, source_id, spec.path, reader.absolute(node.address),
                reader.absolute(child.address), "observed_index",
                PhysicalExtent(source_id, entry.pointer_offset, offset_size),
            ))
            link_ids.add(link_id)
            parent_links[child.address] = link_id
            parent_addresses[child.address] = node.address

    for leaf_address, (leaf, route, _details) in leaves.items():
        if leaf.address != leaf_address or leaf.level != 0:
            raise EvidenceAdapterError("provided leaf does not match its parsed address and level")
        if route == "intact_tree":
            if nodes.get(leaf_address) != leaf:
                raise EvidenceAdapterError("intact leaf is not present in the rooted traversal")
            continue
        if route != "reconstructed_link" or leaf_address in nodes:
            raise EvidenceAdapterError("leaf's evidence route is not justified")
        # Only the parser's narrow two-sided, level-one interior bridge is
        # admitted. Rechecking here means a guessed detached leaf cannot be
        # promoted simply by putting an address in ``leaves``.
        matches = []
        for broken in walk.broken_links:
            parent = nodes.get(broken.parent_address)
            if parent is None or parent.level != 1:
                continue
            slot = broken.entry_index
            if slot <= 0 or slot + 1 >= len(parent.entries):
                continue
            left_addr = parent.entries[slot - 1].address
            right_addr = parent.entries[slot + 1].address
            if left_addr is None or right_addr is None:
                continue
            left, right = nodes.get(left_addr), nodes.get(right_addr)
            if left is None or right is None or left.level != 0 or right.level != 0:
                continue
            if (
                leaf.entries[0].key == broken.key_start and leaf.final_key == broken.key_end
                and left.right_sibling == leaf_address and right.left_sibling == leaf_address
                and leaf.left_sibling == left_addr and leaf.right_sibling == right_addr
            ):
                matches.append((broken, left, right))
        if len(matches) != 1:
            raise EvidenceAdapterError("detached leaf lacks one unique reciprocal two-sided bridge")
        broken, left, right = matches[0]
        if leaf_address in parent_links:
            raise EvidenceAdapterError("detached leaf is also reachable through an observed pointer")
        bridge_id = f"bridge:{broken.parent_address}:{broken.entry_index}"
        left_id, right_id = f"sibling:left:{leaf_address}", f"sibling:right:{leaf_address}"
        left_pointer = reader.absolute(left.address) + 8 + offset_size  # right sibling field
        right_pointer = reader.absolute(right.address) + 8  # left sibling field
        link_records.extend((
            IndexLink(left_id, source_id, spec.path, reader.absolute(left.address),
                      reader.absolute(leaf.address), "observed_sibling",
                      PhysicalExtent(source_id, left_pointer, offset_size), "left"),
            IndexLink(right_id, source_id, spec.path, reader.absolute(right.address),
                      reader.absolute(leaf.address), "observed_sibling",
                      PhysicalExtent(source_id, right_pointer, offset_size), "right"),
            IndexLink(bridge_id, source_id, spec.path, reader.absolute(broken.parent_address),
                      reader.absolute(leaf.address), "bridged_index",
                      PhysicalExtent(source_id, broken.pointer_offset, offset_size),
                      corroborator_ids=(left_id, right_id), checks=_BRIDGE_PASS),
        ))
        link_ids.update((left_id, right_id, bridge_id))
        parent_links[leaf.address] = bridge_id
        parent_addresses[leaf.address] = broken.parent_address

    def path_to_leaf(address: int) -> tuple[str, ...]:
        current = address
        trail: list[str] = []
        seen: set[int] = set()
        while current != root.address:
            if current in seen or current not in parent_links:
                raise EvidenceAdapterError("leaf has no unique path from the selected index root")
            seen.add(current)
            trail.append(parent_links[current])
            current = parent_addresses[current]
        trail.reverse()
        return tuple(trail)

    proposals: list[ChunkProposal] = []
    leaf_entries: dict[tuple[int, tuple[int, ...], int], Any] = {}
    for address, (leaf, _route, _details) in leaves.items():
        for slot, entry in enumerate(leaf.entries):
            if entry.address is None:
                raise EvidenceAdapterError("accepted leaf has an undefined payload pointer")
            coordinate = tuple(entry.key.offsets[:-1])
            key = (address, coordinate, entry.address)
            if key in leaf_entries:
                raise EvidenceAdapterError("duplicate payload entry in leaf")
            leaf_entries[key] = (slot, entry)

    def add_proposal(
        proposal_id: str, leaf_address: int, coordinate: tuple[int, ...],
        source_address: int, accepted_payload: bytes | None,
    ) -> None:
        if leaf_address not in leaves:
            raise EvidenceAdapterError("record's leaf was not selected by parser")
        selected = leaf_entries.get((leaf_address, coordinate, source_address))
        if selected is None:
            raise EvidenceAdapterError("record does not match a coordinate and pointer in its leaf")
        slot, entry = selected
        if entry.key.offsets[-1] != 0:
            raise EvidenceAdapterError("nonzero datatype-element offset in chunk record")
        length = entry.key.stored_size
        absolute = reader.absolute(source_address)
        raw = reader.read_at(source_address, length)
        link_id = f"payload:{leaf_address}:{slot}"
        if link_id not in link_ids:
            link_records.append(IndexLink(
                link_id, source_id, spec.path, reader.absolute(leaf_address), absolute,
                "observed_index", PhysicalExtent(source_id, entry.pointer_offset, offset_size),
            ))
            link_ids.add(link_id)
        if accepted_payload is None:
            try:
                decode_chunk(raw, spec, entry.key.filter_mask)
            except ValueError:
                pass
            else:
                raise EvidenceAdapterError("reported decode failure no longer reproduces")
            decoded_sha = decoded_length = None
            checks = tuple(c for c in _PASS if c.code != "decoded_bytes") + (
                EvidenceCheck("decoded_bytes", "fail", "supported decoder rejected stored chunk"),
            )
            checksum = ChecksumEvidence(
                "fletcher32" if spec.filters else "none", None, "unverified",
            )
        else:
            decoded = decode_chunk(raw, spec, entry.key.filter_mask)
            if decoded != accepted_payload:
                raise EvidenceAdapterError("record payload disagrees with newly decoded source bytes")
            decoded_sha, decoded_length = hashlib.sha256(decoded).hexdigest(), len(decoded)
            checks = _PASS
            checksum = ChecksumEvidence(
                "fletcher32" if spec.filters else "none", None,
                "passed" if spec.filters else "absent",
            )
        proposals.append(ChunkProposal(
            proposal_id, PhysicalExtent(source_id, absolute, length), hashlib.sha256(raw).hexdigest(),
            spec.path, coordinate, path_to_leaf(leaf_address) + (link_id,),
            decoded_sha, decoded_length, entry.key.filter_mask, checksum, checks,
        ))

    for index, record in enumerate(records):
        address = int(record.file_address)
        if (
            reader.absolute(address) != record.absolute_offset
            or int(record.length) <= 0
            or tuple(record.coordinate) != tuple(
                chunk * step for chunk, step in zip(record.index, spec.chunks)
            )
        ):
            raise EvidenceAdapterError("recovery record's address, length, or coordinate is inconsistent")
        selected = leaf_entries.get((record.leaf_address, tuple(record.coordinate), address))
        if selected is None or selected[1].key.stored_size != record.length:
            raise EvidenceAdapterError("recovery record does not match its physical leaf entry")
        add_proposal(f"accepted:{index}", record.leaf_address, tuple(record.coordinate), address, record.payload)

    for index, item in enumerate(failed):
        address = int(item["source_address"])
        coordinate = tuple(int(n) for n in item["coordinate"])
        matching_leaves = [
            leaf_address for leaf_address in leaves
            if (leaf_address, coordinate, address) in leaf_entries
        ]
        if len(matching_leaves) != 1:
            raise EvidenceAdapterError("decode-failed record does not have one anchored leaf")
        add_proposal(f"decode_failed:{index}", matching_leaves[0], coordinate, address, None)

    ranges = tuple(
        PhysicalExtent(source_id, start, end - start)
        for start, end, _kind in reader.metadata_ranges
    )
    report = reconcile((source,), (anchor,), tuple(link_records), tuple(proposals), ranges)
    for decision in report.decisions[:len(records)]:
        if decision.status != "accepted":
            raise EvidenceAdapterError(
                f"accepted chunk {decision.proposal_id} has unresolved evidence: {', '.join(decision.reasons)}"
            )
    if any(decision.status == "accepted" for decision in report.decisions[len(records):]):
        raise EvidenceAdapterError("a decode-failed chunk was wrongly accepted")
    return report
