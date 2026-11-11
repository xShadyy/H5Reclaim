"""Infer redundant legacy counts only from bounded, rooted metadata.

Object-header framing and continuation lengths determine message counts.
Symbol counts additionally require the complete live prefix, zeroed unused
slots, local targets, ordered heap names and an exact parent upper key.
These unchecksummed structures establish current coherence, not history.
"""

import time
from collections import deque
from pathlib import Path

from .format import FormatError, H5File, UnsupportedFormat
from .large_streaming import LargeBudget, _deadline
from .metadata_fallback import (
    _old_compact_group,
    _old_group,
    _old_messages,
    _old_root_address,
    _unique,
)


def count_corrections(path, *, budget=None):
    """Return exact two-byte replacements without changing the source."""
    budget = budget or LargeBudget()
    deadline = time.monotonic() + budget.max_seconds
    changes, symbol_nodes = {}, set()

    def record(offset, before, after, kind):
        if before == after:
            return
        item = {"offset": offset, "before": before, "after": after, "kind": kind}
        if offset in changes and changes[offset] != item:
            raise FormatError("legacy count evidence gives contradictory replacements")
        changes[offset] = item

    def symbols(reader, address, heap, low, high, capacity):
        _deadline(deadline)
        start = reader.absolute(address)
        if start in symbol_nodes:
            raise FormatError("legacy symbol node has competing rooted parents")
        symbol_nodes.add(start)
        osize, lsize = reader.superblock.offset_size, reader.superblock.length_size
        entry_size = lsize + osize + 24
        length = 8 + capacity * entry_size
        if length > min(1 << 20, budget.max_metadata_bytes):
            raise UnsupportedFormat("legacy symbol node exceeds the metadata budget")
        raw = reader.read_at(address, length)
        declared = int.from_bytes(raw[6:8], "little")
        if 1 <= declared <= capacity:
            last = 8 + (declared - 1) * entry_size
            try:
                if heap.name(int.from_bytes(raw[last : last + lsize], "little")) == high:
                    return declared
            except FormatError:
                pass
        names = []
        for index in range(capacity):
            _deadline(deadline)
            entry = raw[8 + index * entry_size : 8 + (index + 1) * entry_size]
            if not any(entry):
                if any(raw[8 + index * entry_size :]):
                    raise FormatError("legacy symbol count has noncontiguous or stale unused slots")
                break
            name = heap.name(int.from_bytes(entry[:lsize], "little"))
            if (
                not low < name <= high
                or names
                and name <= names[-1]
                or any(entry[lsize + osize + 4 : lsize + osize + 8])
            ):
                raise FormatError("legacy symbol count lacks an ordered complete prefix")
            cache_type = int.from_bytes(entry[lsize + osize : lsize + osize + 4], "little")
            if cache_type not in (0, 1):
                # Do not infer a new count from a soft link or unknown cache.
                raise UnsupportedFormat("legacy count inference needs local hard-link targets")
            target = int.from_bytes(entry[lsize : lsize + osize], "little")
            _old_messages(reader, target, check_count=False)
            names.append(name)
        if not names or names[-1] != high:
            raise FormatError("legacy symbol count does not reach its rooted parent upper key")
        record(start + 6, declared, len(names), "restore_legacy_symbol_count")
        return len(names)

    with H5File(Path(path)) as reader:
        root, cached = _old_root_address(reader)
        pending = deque([(root, cached, 0)])
        seen, links_seen = set(), 0
        accounted_ranges, metadata_bytes = 0, 0
        while pending:
            _deadline(deadline)
            address, cached, depth = pending.popleft()
            if address in seen:
                continue
            if len(seen) >= budget.max_objects or depth > 64:
                raise UnsupportedFormat("legacy count traversal exceeds the object or depth budget")
            seen.add(address)
            messages = _old_messages(reader, address, check_count=False)
            declared = int.from_bytes(reader.read_at(address + 2, 2), "little")
            if not messages:
                raise FormatError("legacy object header has no framed messages")
            record(
                reader.absolute(address) + 2,
                declared,
                len(messages),
                "restore_legacy_message_count",
            )
            if _unique(messages, 1) is None and _unique(messages, 3) is None:
                if _unique(messages, 0x11) is not None:
                    links = _old_group(
                        reader,
                        address,
                        cached,
                        check_message_count=False,
                        symbol_count_resolver=symbols,
                    )
                else:
                    links = _old_compact_group(reader, address, messages)
                links_seen += len(links)
                if links_seen > budget.max_links:
                    raise UnsupportedFormat("legacy count traversal exceeds the link budget")
                pending.extend(
                    (link.object_address, link.cached_group, depth + 1)
                    for link in links.values()
                    if link is not None
                )
            metadata_bytes += sum(
                end - start for start, end, _ in reader.metadata_ranges[accounted_ranges:]
            )
            accounted_ranges = len(reader.metadata_ranges)
            if metadata_bytes > budget.max_metadata_bytes:
                raise UnsupportedFormat("legacy count traversal exceeds the metadata byte budget")
    return sorted(changes.values(), key=lambda item: item["offset"])


def repair_counts(path, touches, *, budget=None):
    changes = count_corrections(path, budget=budget)
    with Path(path).open("r+b") as stream:
        for item in changes:
            stream.seek(item["offset"])
            if stream.read(2) != item["before"].to_bytes(2, "little"):
                raise FormatError("legacy count bytes changed during independent inspection")
            stream.seek(item["offset"])
            stream.write(item["after"].to_bytes(2, "little"))
            touches.append(
                (
                    item["offset"],
                    item["offset"] + 2,
                    item["kind"],
                    "rooted complete metadata framing; unchecksummed current structural coherence",
                )
            )
    return changes
