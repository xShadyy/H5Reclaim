"""Repair uniquely checksum-justified superblock bytes in a disposable view."""

from contextlib import contextmanager
from pathlib import Path
import tempfile
import time

import h5py

from .format import FormatError, SIGNATURE
from .large_streaming import LargeBudget, _copy_sparse
from .modern_indexes import ModernH5File, lookup3
from .metadata import UnsupportedCase


@contextmanager
def _checked_root_view(source, budget=None):
    from .metadata_correction import _superblock_raw, _unique_one_byte
    from .metadata_fallback import _messages, _unique
    source = Path(source)
    budget = budget or LargeBudget()
    try:
        position, raw, width, root_start = _superblock_raw(source)
    except UnsupportedCase:
        yield source, None
        return
    checksum = int.from_bytes(raw[-4:], 'little')
    if lookup3(raw[:-4]) == checksum:
        yield source, None
        return
    corrected = bytearray(raw)
    if raw[:8] != SIGNATURE:
        differences = [index for index in range(8) if raw[index] != SIGNATURE[index]]
        if len(differences) != 1 or lookup3(SIGNATURE + raw[8:-4]) != checksum:
            raise FormatError('signature restoration lacks a unique original-checksum match')
        offset = differences[0]
        value = SIGNATURE[offset]
        kind = 'unique_superblock_signature_checksum_correction'
    else:
        offset, value = _unique_one_byte(raw, range(root_start, root_start + width), checksum)
        kind = 'unique_root_pointer_checksum_correction'
    corrected[offset] = value
    root = int.from_bytes(corrected[root_start:root_start + width], 'little')
    with tempfile.TemporaryDirectory(prefix='h5reclaim-checked-root-') as directory:
        view = Path(directory) / 'view.h5'
        with source.open('rb') as original, view.open('w+b') as target:
            _copy_sparse(original, target, size=source.stat().st_size, parent=Path(directory),
                         budget=budget, deadline=time.monotonic() + budget.max_seconds)
            target.seek(position)
            target.write(corrected)
        with ModernH5File(view) as reader:
            messages = _messages(reader, root)
            if _unique(messages, 1) is not None or _unique(messages, 8) is not None:
                raise FormatError('corrected root is a dataset rather than a group')
            if _unique(messages, 2) is None or _unique(messages, 10) is None:
                raise FormatError('corrected root has no checked modern group metadata')
        with h5py.File(view, 'r') as handle:
            if int(h5py.h5o.get_info(handle['/'].id).addr) != root:
                raise FormatError('native root address contradicts the corrected pointer')
        yield view, {'kind': kind,
                     'physical_byte': position + offset, 'before': raw[offset], 'after': value,
                     'root_address': root, 'original_checksum': checksum,
                     'checksum_bytes_changed': 0}


def metadata_corrections(source, budget=None):
    """Find count or original-checksum filter corrections on rooted objects."""
    from collections import deque
    from .preflight import _version
    from .metadata_fallback import _messages, _unique, _compact_links
    from .repair_plan import _object_header, _filter_proposal
    from .large_streaming import _deadline
    source, budget = Path(source), budget or LargeBudget()
    if _version(source) < 2:
        from .legacy_counts import count_corrections
        return [{"offset": item["offset"], "before_hex": item["before"].to_bytes(2, "little").hex(),
                 "after_hex": item["after"].to_bytes(2, "little").hex(), "repairs": [item["kind"]]}
                for item in count_corrections(source, budget=budget)]
    changes, seen, links_seen = [], set(), 0
    accounted_ranges, metadata_bytes, changed_bytes = 0, 0, 0
    deadline = time.monotonic() + budget.max_seconds
    with ModernH5File(source, large_sparse_scan=True) as reader:
        pending = deque([(reader.superblock.root_object_address, 0)])
        while pending:
            _deadline(deadline)
            address, depth = pending.popleft()
            if address in seen:
                continue
            if len(seen) >= budget.max_objects or depth > 64:
                raise UnsupportedCase("filter correction traversal exceeds its object or depth budget")
            seen.add(address)
            try:
                start, raw = _object_header(reader, address)
                proposed = _filter_proposal(reader, start, raw)
            except UnsupportedCase:
                proposed = None
            if proposed is not None:
                proposal, fields, _ = proposed
                changed_bytes += len(raw)
                if changed_bytes > budget.max_metadata_bytes:
                    raise UnsupportedCase("filter correction proposals exceed the metadata byte budget")
                changes.append({"offset": start, "before_hex": raw.hex(), "after_hex": proposal.hex(),
                                "repairs": sorted({field["kind"] for field in fields})})
                continue
            messages = _messages(reader, address)
            if _unique(messages, 1) is None and _unique(messages, 3) is None:
                links = _compact_links(reader, address)
                links_seen += len(links)
                if links_seen > budget.max_links:
                    raise UnsupportedCase("filter correction traversal exceeds its link budget")
                pending.extend((link.object_address, depth + 1) for link in links.values() if link is not None)
            metadata_bytes += sum(end - start for start, end, _ in reader.metadata_ranges[accounted_ranges:])
            accounted_ranges = len(reader.metadata_ranges)
            if metadata_bytes > budget.max_metadata_bytes:
                raise UnsupportedCase("filter correction traversal exceeds its metadata byte budget")
    return sorted(changes, key=lambda item: item["offset"])


@contextmanager
def checked_root_view(source, budget=None):
    """Yield a checked private metadata view; never reseal a checksum here."""
    budget = budget or LargeBudget()
    with _checked_root_view(source, budget) as (root_view, root_correction):
        try:
            changes = metadata_corrections(root_view, budget)
        except (FormatError, UnsupportedCase):
            # This optional, narrow parser must not prevent a native export
            # of other supported metadata encodings or surviving siblings.
            changes = []
        if not changes:
            yield root_view, root_correction
            return
        with tempfile.TemporaryDirectory(prefix="h5reclaim-checked-metadata-") as directory:
            view = Path(directory) / "view.h5"
            with Path(root_view).open("rb") as original, view.open("xb") as target:
                _copy_sparse(original, target, size=Path(root_view).stat().st_size, parent=view.parent,
                             budget=budget, deadline=time.monotonic() + budget.max_seconds)
            with view.open("r+b") as editable:
                for item in changes:
                    before, after = bytes.fromhex(item["before_hex"]), bytes.fromhex(item["after_hex"])
                    editable.seek(item["offset"])
                    if editable.read(len(before)) != before:
                        raise FormatError("checked metadata correction precondition changed")
                    editable.seek(item["offset"])
                    editable.write(after)
            from .preflight import inspect_metadata_local
            checked = inspect_metadata_local(view, budget=budget)
            if (checked["decision"] not in ("accept", "accept_with_warnings")
                    or not checked["analysis"]["complete"]):
                raise FormatError("corrected metadata view failed independent preflight")
            yield view, {"kind": "checked_metadata_corrections", "root_correction": root_correction,
                         "actions": changes, "checksum_bytes_changed": 0,
                         "basis": "rooted metadata framing or restored original header checksum; current structural coherence"}
