"""Conservative provenance ledger for *proposed* HDF5 chunk assignments.

This module reconciles observations made by format-specific parsers. It does
not parse HDF5, establish that a recorded observation is true, or infer a lost
measurement. Only a verified parser should create an ``observed_index`` link
or mark a check as passed. A hash of bytes we read is not an independent
checksum of the historical scientific measurement.
"""

from __future__ import annotations

import hashlib
import heapq
import json
import os
import stat
import tempfile
import zipfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal, Mapping, Sequence


MAX_FRAGMENT_BYTES = 16 * 1024 * 1024
MAX_EXPORT_BYTES = 64 * 1024 * 1024
MAX_FRAGMENTS = 4096
MAX_CONTRADICTIONS = 100_000
MAX_REPORT_BYTES = 32 * 1024 * 1024
MAX_REPORT_RECORDS = 100_000
_REQUIRED_CHECKS = frozenset({
    "allocation", "coordinates", "datatype", "decoded_bytes", "filter_pipeline"
})
_BRIDGE_CHECKS = frozenset({
    "parent_key_interval", "reciprocal_sibling_links", "unique_node", "node_level"
})
_ARRAY_POINTER_BRIDGE_CHECKS = frozenset({
    "selected_header_anchor", "original_header_checksum_restored",
    "data_block_checksum_and_backpointer", "unique_data_block",
})
_MODERN_POINTER_BRIDGE_CHECKS = frozenset({
    "original_parent_checksum_restored", "unique_checked_child",
    "full_index_consistency",
})


def _digest(value: str) -> bool:
    return len(value) == 64 and all(ch in "0123456789abcdef" for ch in value)


@dataclass(frozen=True)
class SourceRecord:
    source_id: str
    size_bytes: int
    sha256: str

    def __post_init__(self) -> None:
        if not self.source_id or self.size_bytes < 0 or not _digest(self.sha256):
            raise ValueError("invalid source identity, size, or SHA-256")


@dataclass(frozen=True)
class PhysicalExtent:
    source_id: str
    offset: int
    length: int

    def __post_init__(self) -> None:
        if not self.source_id or self.offset < 0 or self.length <= 0:
            raise ValueError("physical extent needs a source, offset, and positive length")

    @property
    def end(self) -> int:
        return self.offset + self.length

    def overlaps(self, other: PhysicalExtent) -> bool:
        return (
            self.source_id == other.source_id
            and self.offset < other.end and other.offset < self.end
        )


@dataclass(frozen=True)
class DatasetAnchor:
    """A selected dataset's independently identified metadata and index root."""

    source_id: str
    path: str
    object_header: PhysicalExtent
    index_root_offset: int
    shape: tuple[int, ...]
    chunks: tuple[int, ...]
    element_size: int

    def __post_init__(self) -> None:
        if (
            not self.path.startswith("/") or self.path == "/"
            or self.object_header.source_id != self.source_id
            or self.index_root_offset < 0 or self.element_size <= 0
            or not self.shape or len(self.shape) != len(self.chunks)
            or any(n < 0 for n in self.shape)
            or any(n <= 0 for n in self.chunks)
        ):
            raise ValueError("invalid dataset anchor")


@dataclass(frozen=True)
class EvidenceCheck:
    """A parser observation, never a substitute for actually running a check."""

    code: str
    result: Literal["pass", "fail", "unknown"]
    detail: str = ""

    def __post_init__(self) -> None:
        if not self.code or self.result not in ("pass", "fail", "unknown"):
            raise ValueError("invalid evidence check")


@dataclass(frozen=True)
class IndexLink:
    """An observed pointer, an independently bridged link, or a hypothesis.

    For a chunk, a chain starts at the selected dataset's index root and ends
    at the raw payload's absolute byte offset. Sibling observations are used
    only as corroboration, never as a coordinate-bearing path.
    """

    link_id: str
    source_id: str
    dataset_path: str
    parent_offset: int
    child_offset: int
    kind: Literal["observed_index", "observed_sibling", "bridged_index", "hypothesis"]
    pointer_extent: PhysicalExtent | None = None
    side: Literal["left", "right"] | None = None
    corroborator_ids: tuple[str, ...] = ()
    checks: tuple[EvidenceCheck, ...] = ()

    def __post_init__(self) -> None:
        if (
            not self.link_id or not self.source_id or not self.dataset_path.startswith("/")
            or self.parent_offset < 0 or self.child_offset < 0
            or self.kind not in ("observed_index", "observed_sibling", "bridged_index", "hypothesis")
            or (self.pointer_extent is not None and self.pointer_extent.source_id != self.source_id)
            or (self.kind == "observed_sibling" and self.side not in ("left", "right"))
        ):
            raise ValueError("invalid index-link observation")


@dataclass(frozen=True)
class ChecksumEvidence:
    algorithm: str
    stored_value: str | None
    result: Literal["passed", "failed", "absent", "unverified"]

    def __post_init__(self) -> None:
        if not self.algorithm or self.result not in ("passed", "failed", "absent", "unverified"):
            raise ValueError("invalid checksum observation")


@dataclass(frozen=True)
class ChunkProposal:
    """A candidate mapping, which may ultimately remain unassigned."""

    proposal_id: str
    extent: PhysicalExtent
    raw_sha256: str
    dataset_path: str | None
    coordinate: tuple[int, ...] | None
    index_link_ids: tuple[str, ...] = ()
    decoded_sha256: str | None = None
    decoded_length: int | None = None
    filter_mask: int | None = None
    checksum: ChecksumEvidence | None = None
    checks: tuple[EvidenceCheck, ...] = ()

    def __post_init__(self) -> None:
        if (
            not self.proposal_id or not _digest(self.raw_sha256)
            or (self.decoded_sha256 is not None and not _digest(self.decoded_sha256))
            or (self.decoded_length is not None and self.decoded_length < 0)
            or (self.filter_mask is not None and not 0 <= self.filter_mask <= 0xffffffff)
            or (self.dataset_path is not None and not self.dataset_path.startswith("/"))
            or (self.coordinate is not None and any(n < 0 for n in self.coordinate))
        ):
            raise ValueError("invalid chunk proposal")


@dataclass(frozen=True)
class Contradiction:
    code: str
    proposal_ids: tuple[str, ...]
    detail: str


@dataclass(frozen=True)
class Decision:
    proposal_id: str
    status: Literal["accepted", "unassigned", "contradicted"]
    reasons: tuple[str, ...]
    integrity: Literal["stored_checksum_passed", "not_independently_verified", "unknown"]


@dataclass(frozen=True)
class EvidenceReport:
    sources: tuple[SourceRecord, ...]
    datasets: tuple[DatasetAnchor, ...]
    links: tuple[IndexLink, ...]
    proposals: tuple[ChunkProposal, ...]
    metadata_extents: tuple[PhysicalExtent, ...]
    decisions: tuple[Decision, ...]
    contradictions: tuple[Contradiction, ...]

    def to_dict(self) -> dict:
        """JSON-native ledger with the evidence behind every decision."""
        # dataclasses.asdict preserves tuples. Convert them before embedding
        # in the recovery report so the returned report equals its saved JSON.
        return json.loads(json.dumps({"schema_version": 1, **asdict(self)}))


def _mapping(value: object, name: str) -> Mapping:
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be a JSON object")
    return value


def _array(value: object, name: str) -> list:
    if not isinstance(value, list) or len(value) > MAX_REPORT_RECORDS:
        raise ValueError(f"{name} must be a bounded JSON array")
    return value


def _number(value: object, name: str) -> int:
    if type(value) is not int:  # Reject JSON booleans and lossy float conversion.
        raise ValueError(f"{name} must be an integer")
    return value


def _extent_from_dict(value: object) -> PhysicalExtent:
    item = _mapping(value, "extent")
    return PhysicalExtent(item["source_id"], _number(item["offset"], "offset"),
                          _number(item["length"], "length"))


def _check_from_dict(value: object) -> EvidenceCheck:
    item = _mapping(value, "check")
    return EvidenceCheck(item["code"], item["result"], item["detail"])


def evidence_report_from_dict(value: object) -> EvidenceReport:
    """Validate a serialized ledger and recompute all decisions.

    JSON cannot authenticate parser observations. This makes an existing
    report internally consistent, then the fragment exporter checks supplied
    source bytes independently. Callers must never infer source paths from
    the report or interpret it as historical measurement truth.
    """
    try:
        item = _mapping(value, "evidence report")
        if _number(item["schema_version"], "schema_version") != 1:
            raise ValueError("unsupported evidence report schema")
        sources = tuple(
            SourceRecord(entry["source_id"], _number(entry["size_bytes"], "size_bytes"),
                         entry["sha256"])
            for entry in (_mapping(raw, "source") for raw in _array(item["sources"], "sources"))
        )
        datasets = tuple(
            DatasetAnchor(
                entry["source_id"], entry["path"], _extent_from_dict(entry["object_header"]),
                _number(entry["index_root_offset"], "index_root_offset"),
                tuple(_number(n, "shape entry") for n in _array(entry["shape"], "shape")),
                tuple(_number(n, "chunk entry") for n in _array(entry["chunks"], "chunks")),
                _number(entry["element_size"], "element_size"),
            ) for entry in (_mapping(raw, "dataset") for raw in _array(item["datasets"], "datasets"))
        )
        links = tuple(
            IndexLink(
                entry["link_id"], entry["source_id"], entry["dataset_path"],
                _number(entry["parent_offset"], "parent_offset"),
                _number(entry["child_offset"], "child_offset"), entry["kind"],
                _extent_from_dict(entry["pointer_extent"]) if entry["pointer_extent"] is not None else None,
                entry["side"],
                tuple(_array(entry["corroborator_ids"], "corroborator_ids")),
                tuple(_check_from_dict(check) for check in _array(entry["checks"], "checks")),
            ) for entry in (_mapping(raw, "link") for raw in _array(item["links"], "links"))
        )
        proposals = tuple(
            ChunkProposal(
                entry["proposal_id"], _extent_from_dict(entry["extent"]), entry["raw_sha256"],
                entry["dataset_path"],
                tuple(_number(n, "coordinate") for n in _array(entry["coordinate"], "coordinate"))
                if entry["coordinate"] is not None else None,
                tuple(_array(entry["index_link_ids"], "index_link_ids")),
                entry["decoded_sha256"],
                _number(entry["decoded_length"], "decoded_length") if entry["decoded_length"] is not None else None,
                _number(entry["filter_mask"], "filter_mask") if entry["filter_mask"] is not None else None,
                ChecksumEvidence(
                    entry["checksum"]["algorithm"], entry["checksum"]["stored_value"],
                    entry["checksum"]["result"],
                ) if entry["checksum"] is not None else None,
                tuple(_check_from_dict(check) for check in _array(entry["checks"], "checks")),
            ) for entry in (_mapping(raw, "proposal") for raw in _array(item["proposals"], "proposals"))
        )
        metadata = tuple(_extent_from_dict(raw) for raw in _array(item["metadata_extents"], "metadata_extents"))
        intrinsic = (
            tuple(dataset.object_header for dataset in datasets)
            + tuple(link.pointer_extent for link in links if link.pointer_extent is not None)
        )
        if intrinsic and metadata[-len(intrinsic):] != intrinsic:
            raise ValueError("intrinsic metadata extents were altered")
        explicit = metadata[:-len(intrinsic)] if intrinsic else metadata
        rebuilt = reconcile(sources, datasets, links, proposals, explicit)
        if json.loads(json.dumps(rebuilt.to_dict())) != item:
            raise ValueError("evidence report decisions or observations do not reconcile")
        return rebuilt
    except (KeyError, TypeError, AttributeError, IndexError) as exc:
        raise ValueError(f"invalid evidence report field: {exc}") from exc


def load_evidence_report(report_path: str | Path) -> EvidenceReport:
    """Read a bounded standalone ledger or recovery report with a ledger."""
    path = Path(report_path)
    with path.open("rb") as stream:
        data = stream.read(MAX_REPORT_BYTES + 1)
    if len(data) > MAX_REPORT_BYTES:
        raise ValueError("evidence report exceeds the file-size limit")

    def no_duplicate_keys(pairs: list[tuple[str, object]]) -> dict:
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON key in evidence report")
            result[key] = value
        return result

    wrapper = _mapping(json.loads(data, object_pairs_hook=no_duplicate_keys), "report")
    if "evidence_ledger" not in wrapper:
        return evidence_report_from_dict(wrapper)
    report = evidence_report_from_dict(wrapper["evidence_ledger"])
    outer_source = _mapping(wrapper.get("source"), "recovery source")
    if len(report.sources) != 1:
        raise ValueError("one-source recovery report has an inconsistent ledger")
    source = report.sources[0]
    if (
        outer_source.get("sha256_before") != source.sha256
        or outer_source.get("sha256_after") != source.sha256
        or outer_source.get("size_bytes") != source.size_bytes
    ):
        raise ValueError("recovery source and evidence ledger disagree")
    return report


def _inside(extent: PhysicalExtent, sources: Mapping[str, SourceRecord]) -> bool:
    source = sources.get(extent.source_id)
    return source is not None and extent.end <= source.size_bytes


def _passed(checks: Sequence[EvidenceCheck], required: frozenset[str]) -> bool:
    return all(any(c.code == code and c.result == "pass" for c in checks) for code in required)


def _link_chain_valid(
    proposal: ChunkProposal, anchor: DatasetAnchor, links: Mapping[str, IndexLink]
) -> bool:
    if not proposal.index_link_ids:
        return False
    current = anchor.index_root_offset
    seen = set()
    for link_id in proposal.index_link_ids:
        if link_id in seen or link_id not in links:
            return False
        seen.add(link_id)
        link = links[link_id]
        if (
            link.source_id != anchor.source_id or link.dataset_path != anchor.path
            or link.parent_offset != current
            or link.kind not in ("observed_index", "bridged_index")
            or link.pointer_extent is None
        ):
            return False
        if link.kind == "bridged_index":
            if any(check.result == "fail" for check in link.checks):
                return False
            if (_passed(link.checks, _ARRAY_POINTER_BRIDGE_CHECKS)
                    or _passed(link.checks, _MODERN_POINTER_BRIDGE_CHECKS)):
                if link.corroborator_ids:
                    return False
            else:
                if len(link.corroborator_ids) != 2 or not _passed(link.checks, _BRIDGE_CHECKS):
                    return False
                corroborators = [links.get(link_id) for link_id in link.corroborator_ids]
                if (
                    link.corroborator_ids[0] == link.corroborator_ids[1]
                    or any(item is None for item in corroborators)
                    or {item.side for item in corroborators} != {"left", "right"}
                    or any(
                        item.kind != "observed_sibling" or item.source_id != link.source_id
                        or item.dataset_path != link.dataset_path
                        or item.child_offset != link.child_offset
                        or item.pointer_extent is None
                        or item.parent_offset == link.child_offset
                        for item in corroborators
                    )
                    or corroborators[0].parent_offset == corroborators[1].parent_offset
                ):
                    return False
        current = link.child_offset
    return current == proposal.extent.offset


def reconcile(
    sources: Sequence[SourceRecord], datasets: Sequence[DatasetAnchor],
    links: Sequence[IndexLink], proposals: Sequence[ChunkProposal],
    metadata_extents: Sequence[PhysicalExtent] = (),
) -> EvidenceReport:
    """Accept unique, checked mappings; never use hints/scans to assign bytes.

    The caller supplies parse results and must independently verify every
    passed check and observed pointer against the source snapshot. This pass
    enforces consistency across those observations; it cannot prove that a
    dishonest or defective parser's claims match the file.
    """
    if len({s.source_id for s in sources}) != len(sources):
        raise ValueError("duplicate source id")
    if len({(d.source_id, d.path) for d in datasets}) != len(datasets):
        raise ValueError("duplicate dataset anchor")
    if len({link.link_id for link in links}) != len(links):
        raise ValueError("duplicate link id")
    if len({p.proposal_id for p in proposals}) != len(proposals):
        raise ValueError("duplicate proposal id")
    source_by_id = {s.source_id: s for s in sources}
    dataset_by_key = {(d.source_id, d.path): d for d in datasets}
    link_by_id = {link.link_id: link for link in links}
    metadata = (
        tuple(metadata_extents)
        + tuple(d.object_header for d in datasets)
        + tuple(link.pointer_extent for link in links if link.pointer_extent is not None)
    )
    for extent in metadata:
        if not _inside(extent, source_by_id):
            raise ValueError("metadata extent lies outside its source")
    for anchor in datasets:
        source = source_by_id.get(anchor.source_id)
        if source is None or anchor.index_root_offset >= source.size_bytes:
            raise ValueError("dataset root lies outside its source")
    for link in links:
        source = source_by_id.get(link.source_id)
        if (
            source is None or link.parent_offset >= source.size_bytes
            or link.child_offset >= source.size_bytes
        ):
            raise ValueError("index-link address lies outside its source")

    reasons: dict[str, set[str]] = {p.proposal_id: set() for p in proposals}
    contradictions: list[Contradiction] = []

    def conflict(code: str, members: Sequence[ChunkProposal], detail: str) -> None:
        if len(contradictions) >= MAX_CONTRADICTIONS:
            raise ValueError("too many contradictory assignments to reconcile safely")
        ids = tuple(sorted({member.proposal_id for member in members}))
        contradictions.append(Contradiction(code, ids, detail))
        for member in members:
            reasons[member.proposal_id].add(code)

    for proposal in proposals:
        name = proposal.proposal_id
        if not _inside(proposal.extent, source_by_id):
            reasons[name].add("extent_outside_source")
        anchor = dataset_by_key.get((proposal.extent.source_id, proposal.dataset_path))
        if anchor is None:
            reasons[name].add("no_dataset_anchor")
        if proposal.coordinate is None or anchor is None:
            reasons[name].add("no_justified_coordinate")
        elif (
            len(proposal.coordinate) != len(anchor.shape)
            or any(
                coord >= dim or coord % chunk != 0
                for coord, dim, chunk in zip(proposal.coordinate, anchor.shape, anchor.chunks)
            )
        ):
            reasons[name].add("coordinate_out_of_bounds_or_unaligned")
        if anchor is None or not _link_chain_valid(proposal, anchor, link_by_id):
            reasons[name].add("no_anchored_index_path")
        if not _passed(proposal.checks, _REQUIRED_CHECKS):
            reasons[name].add("required_checks_unverified")
        if any(check.result == "fail" for check in proposal.checks):
            reasons[name].add("check_failed")
        if proposal.checksum is not None and proposal.checksum.result == "failed":
            reasons[name].add("stored_checksum_failed")
        if proposal.decoded_sha256 is None or proposal.decoded_length is None:
            reasons[name].add("decoded_payload_unverified")
        if anchor is not None and proposal.coordinate is not None and len(proposal.coordinate) == len(anchor.shape):
            full_bytes = anchor.element_size
            edge_bytes = anchor.element_size
            for dim, chunk, coord in zip(anchor.shape, anchor.chunks, proposal.coordinate):
                full_bytes *= chunk
                edge_bytes *= max(0, min(chunk, dim - coord))
            if proposal.decoded_length not in (full_bytes, edge_bytes):
                reasons[name].add("decoded_length_mismatch")
        if any(proposal.extent.overlaps(extent) for extent in metadata):
            conflict("payload_overlaps_metadata", (proposal,), "payload intersects recorded metadata")

    coordinates: dict[tuple[str, str, tuple[int, ...]], list[ChunkProposal]] = {}
    physical: dict[str, list[ChunkProposal]] = {}
    for proposal in proposals:
        physical.setdefault(proposal.extent.source_id, []).append(proposal)
        if proposal.dataset_path is not None and proposal.coordinate is not None:
            key = (proposal.extent.source_id, proposal.dataset_path, proposal.coordinate)
            coordinates.setdefault(key, []).append(proposal)
    for group in coordinates.values():
        if len(group) > 1:
            conflict("competing_coordinate", group, "multiple proposals claim one dataset coordinate")
    for group in physical.values():
        active: dict[int, ChunkProposal] = {}
        ending: list[tuple[int, int]] = []
        for sequence, proposal in enumerate(sorted(group, key=lambda item: item.extent.offset)):
            while ending and ending[0][0] <= proposal.extent.offset:
                _end, previous = heapq.heappop(ending)
                active.pop(previous, None)
            for previous in active.values():
                conflict("overlapping_payloads", (previous, proposal), "two proposals claim overlapping physical bytes")
            active[sequence] = proposal
            heapq.heappush(ending, (proposal.extent.end, sequence))

    decisions = []
    conflict_codes = {c.code for c in contradictions}
    for proposal in proposals:
        faults = tuple(sorted(reasons[proposal.proposal_id]))
        status = "contradicted" if any(reason in conflict_codes for reason in faults) else (
            "unassigned" if faults else "accepted"
        )
        integrity = "unknown"
        if status == "accepted":
            integrity = (
                "stored_checksum_passed"
                if proposal.checksum is not None and proposal.checksum.result == "passed"
                else "not_independently_verified"
            )
        decisions.append(Decision(proposal.proposal_id, status, faults, integrity))
    return EvidenceReport(
        tuple(sources), tuple(datasets), tuple(links), tuple(proposals), metadata,
        tuple(decisions), tuple(contradictions),
    )


def export_unassigned_fragments(
    report: EvidenceReport, source_paths: Mapping[str, str | Path], output: str | Path,
    *, proposal_ids: Sequence[str] | None = None,
) -> Path:
    """Publish only unresolved raw extents in a separate, coordinate-free ZIP.

    The caller must request a destination. Source identities are checked while
    file descriptors remain open, and the archive is published without
    replacing an existing path. It is not an HDF5 recovery output.
    """
    by_id = {p.proposal_id: p for p in report.proposals}
    decisions = {d.proposal_id: d for d in report.decisions}
    selected_ids = tuple(proposal_ids) if proposal_ids is not None else tuple(
        d.proposal_id for d in report.decisions if d.status != "accepted"
    )
    if not selected_ids:
        raise ValueError("no unresolved raw fragments in evidence report")
    if len(selected_ids) != len(set(selected_ids)) or len(selected_ids) > MAX_FRAGMENTS:
        raise ValueError("duplicate fragments or fragment count exceeds limit")
    if any(item not in by_id or decisions[item].status == "accepted" for item in selected_ids):
        raise ValueError("only unresolved proposals can be exported")
    selected = [by_id[item] for item in selected_ids]
    total = sum(item.extent.length for item in selected)
    if total > MAX_EXPORT_BYTES or any(item.extent.length > MAX_FRAGMENT_BYTES for item in selected):
        raise ValueError("raw fragment export size limit exceeded")
    sources = {item.source_id: item for item in report.sources}
    requested_sources = {item.extent.source_id for item in selected}
    if any(source_id not in sources or source_id not in source_paths for source_id in requested_sources):
        raise ValueError("missing source for raw fragment")
    destination = Path(output)
    if destination.exists() or destination.is_symlink():
        raise FileExistsError(destination)
    fds: dict[str, int] = {}
    initial: dict[str, tuple[int, int, int, int, int]] = {}
    temp_name: str | None = None

    def identity(st: os.stat_result) -> tuple[int, int, int, int, int]:
        return st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns

    try:
        for source_id in sorted(requested_sources):
            flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
            path = Path(source_paths[source_id])
            if path.is_symlink():
                raise ValueError("source symlinks are not accepted")
            fd = os.open(path, flags)
            fds[source_id] = fd
            before = os.fstat(fd)
            if not stat.S_ISREG(before.st_mode) or before.st_size != sources[source_id].size_bytes:
                raise ValueError("source is not the recorded regular file")
            initial[source_id] = identity(before)
            digest = hashlib.sha256()
            os.lseek(fd, 0, os.SEEK_SET)
            while block := os.read(fd, 1024 * 1024):
                digest.update(block)
            if digest.hexdigest() != sources[source_id].sha256:
                raise ValueError("source hash differs from the evidence report")
            if identity(os.fstat(fd)) != initial[source_id] or identity(path.stat()) != initial[source_id]:
                raise ValueError("source changed during verification")
        raw_blobs: list[tuple[str, bytes, ChunkProposal]] = []
        for index, proposal in enumerate(selected):
            extent = proposal.extent
            if not _inside(extent, sources):
                raise ValueError("unassigned fragment is outside source")
            os.lseek(fds[extent.source_id], extent.offset, os.SEEK_SET)
            payload = bytearray()
            while len(payload) < extent.length:
                block = os.read(fds[extent.source_id], extent.length - len(payload))
                if not block:
                    raise ValueError("source shortened while reading fragment")
                payload.extend(block)
            if hashlib.sha256(payload).hexdigest() != proposal.raw_sha256:
                raise ValueError("raw fragment hash differs from the evidence report")
            raw_blobs.append((f"fragments/{index:04d}.bin", bytes(payload), proposal))
        for source_id in requested_sources:
            path = Path(source_paths[source_id])
            if identity(os.fstat(fds[source_id])) != initial[source_id] or identity(path.stat()) != initial[source_id]:
                raise ValueError("source changed while exporting fragments")
        manifest = {
            "schema_version": 1,
            "warning": "Unassigned bytes only. Proposed dataset paths/coordinates are unverified and no values have been restored.",
            "sources": [asdict(sources[item]) for item in sorted(requested_sources)],
            "fragments": [
                {
                    "filename": filename,
                    "proposal_id": proposal.proposal_id,
                    "source_id": proposal.extent.source_id,
                    "physical_offset": proposal.extent.offset,
                    "length": proposal.extent.length,
                    "sha256": proposal.raw_sha256,
                    "decision": asdict(decisions[proposal.proposal_id]),
                    "proposed_dataset_path_unverified": proposal.dataset_path,
                    "proposed_coordinate_unverified": proposal.coordinate,
                }
                for filename, _payload, proposal in raw_blobs
            ],
        }
        with tempfile.NamedTemporaryFile(dir=destination.parent, prefix=".h5reclaim-fragments-", suffix=".zip", delete=False) as temp:
            temp_name = temp.name
        with zipfile.ZipFile(temp_name, "w", compression=zipfile.ZIP_STORED) as archive:
            archive.writestr("manifest.json", json.dumps(manifest, indent=2, sort_keys=True) + "\n")
            for filename, payload, _proposal in raw_blobs:
                archive.writestr(filename, payload)
        for source_id in requested_sources:
            path = Path(source_paths[source_id])
            if identity(os.fstat(fds[source_id])) != initial[source_id] or identity(path.stat()) != initial[source_id]:
                raise ValueError("source changed before fragment publication")
        os.link(temp_name, destination)
        return destination
    finally:
        for fd in fds.values():
            os.close(fd)
        if temp_name is not None:
            Path(temp_name).unlink(missing_ok=True)
