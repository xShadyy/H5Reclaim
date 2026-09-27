"""Bounded observations of physical allocations owned by other local datasets.

HDF5's namespace is a graph, so inspect only local hard links and de-duplicate
objects by their on-disk address. These observations can *contradict* a selected
dataset's proposed payload address. Native enumeration is not a proof that no
unobserved allocation exists, especially when metadata is damaged.
"""

from __future__ import annotations

from contextlib import nullcontext
from dataclasses import dataclass
from pathlib import Path
from time import monotonic

import h5py

from .format import FormatError


MAX_OBJECTS = 4096
MAX_LINKS = 8192
MAX_ALLOCATIONS = 65536
MAX_SECONDS = 30.0
MAX_REASONS = 16
MAX_PATH_BYTES = 4096


@dataclass(frozen=True)
class OtherAllocation:
    start: int
    end: int
    path: str
    object_address: int
    layout: str


@dataclass(frozen=True)
class OwnershipInventory:
    allocations: tuple[OtherAllocation, ...]
    objects_seen: int
    sibling_datasets_seen: int
    links_seen: int
    complete: bool
    incomplete_reasons: tuple[str, ...]

    def report(self) -> dict[str, object]:
        return {
            "route": "native_local_hard_link_allocations",
            "complete": self.complete,
            "objects_seen": self.objects_seen,
            "sibling_datasets_seen": self.sibling_datasets_seen,
            "links_seen": self.links_seen,
            "sibling_allocations_checked": len(self.allocations),
            "incomplete_reasons": list(self.incomplete_reasons),
            "scope": (
                "Observed rooted local hard-linked sibling chunk and contiguous "
                "allocations only; native enumeration is not an independent "
                "historical ownership or full file-space proof."
            ),
        }


def inventory_other_allocations(
    snapshot: Path, selected_object_address: int,
    *, max_objects: int = MAX_OBJECTS, max_links: int = MAX_LINKS,
    max_allocations: int = MAX_ALLOCATIONS, max_seconds: float = MAX_SECONDS,
    opened_file: h5py.File | None = None, address_space_size: int | None = None,
) -> OwnershipInventory:
    """Observe currently allocated sibling ranges without reading payload values.

    Failure to open or enumerate an object leaves an explicitly incomplete
    inventory. A partial inventory still detects conflicts with ranges it did
    observe. Source bytes are a private snapshot, opened read-only.
    """
    if min(max_objects, max_links, max_allocations) <= 0 or max_seconds <= 0:
        raise ValueError("ownership inventory limits must be positive")
    if opened_file is None:
        size = Path(snapshot).stat().st_size
    else:
        if type(address_space_size) is not int or not 0 < address_space_size < 1 << 64:
            raise ValueError("an opened VFD inventory needs a bounded logical address space")
        size = address_space_size
        # Family and Split use their own virtual address spaces. Walk the same
        # already-open, read-only VFD view rather than reopening one member as
        # a stand-alone HDF5 file. The caller retains ownership of the handle.
    deadline = monotonic() + max_seconds
    ranges: list[OtherAllocation] = []
    seen: set[int] = set()
    objects = siblings = links = 0
    problems: list[str] = []

    def incomplete(reason: str) -> None:
        if len(problems) < MAX_REASONS:
            problems.append(reason)

    try:
        with (h5py.File(snapshot, "r") if opened_file is None
              else nullcontext(opened_file)) as handle:
            pending: list[tuple[str, h5py.Group | h5py.Dataset]] = [("/", handle["/"])]
            while pending:
                if monotonic() > deadline:
                    incomplete("native local namespace inventory exceeded its time limit")
                    break
                path, item = pending.pop()
                try:
                    address = int(h5py.h5o.get_info(item.id).addr)
                except (OSError, RuntimeError, ValueError) as exc:
                    incomplete(f"cannot inspect object at {path[:128]}: {type(exc).__name__}")
                    continue
                if address in seen:
                    continue
                if objects >= max_objects:
                    incomplete("native local namespace inventory exceeded its object limit")
                    break
                seen.add(address)
                objects += 1
                if isinstance(item, h5py.Dataset):
                    if address == selected_object_address:
                        continue
                    siblings += 1
                    try:
                        creation = item.id.get_create_plist()
                        if creation.get_external_count() or item.is_virtual:
                            # These do not allocate the declared raw values in
                            # this snapshot; dependent-value routes handle them.
                            continue
                        layout = creation.get_layout()
                        if layout == h5py.h5d.CHUNKED:
                            count = int(item.id.get_num_chunks())
                            if count < 0 or count > max_allocations - len(ranges):
                                incomplete(f"sibling {path[:128]} exceeds allocation limit")
                                continue
                            for index in range(count):
                                if monotonic() > deadline:
                                    incomplete("native sibling chunk inventory exceeded its time limit")
                                    break
                                info = item.id.get_chunk_info(index)
                                offset, length = info.byte_offset, info.size
                                if (not isinstance(offset, int) or not isinstance(length, int)
                                        or offset < 0 or length <= 0 or offset > size
                                        or length > size - offset):
                                    incomplete(f"sibling {path[:128]} has an invalid chunk range")
                                    continue
                                ranges.append(OtherAllocation(offset, offset + length,
                                                              path, address, "chunked"))
                        elif layout == h5py.h5d.CONTIGUOUS:
                            offset, length = item.id.get_offset(), item.id.get_storage_size()
                            if length:
                                if (not isinstance(offset, int) or not isinstance(length, int)
                                        or offset < 0 or length < 0 or offset > size
                                        or length > size - offset):
                                    incomplete(f"sibling {path[:128]} has an invalid contiguous range")
                                elif len(ranges) >= max_allocations:
                                    incomplete("native sibling allocation inventory exceeded its limit")
                                else:
                                    ranges.append(OtherAllocation(offset, offset + length,
                                                                  path, address, "contiguous"))
                        elif layout != h5py.h5d.COMPACT:
                            incomplete(f"sibling {path[:128]} has unhandled local layout")
                    except (OSError, RuntimeError, ValueError, OverflowError) as exc:
                        incomplete(f"cannot enumerate sibling {path[:128]}: {type(exc).__name__}")
                    continue
                if not isinstance(item, h5py.Group):
                    continue
                try:
                    for name in item:
                        links += 1
                        if links > max_links:
                            incomplete("native local namespace inventory exceeded its link limit")
                            break
                        link = item.get(name, getlink=True)
                        if not isinstance(link, h5py.HardLink):
                            continue
                        child_path = path.rstrip("/") + "/" + name
                        if len(child_path.encode("utf-8", "surrogateescape")) > MAX_PATH_BYTES:
                            incomplete("rooted local path exceeds inventory length limit")
                            continue
                        child = item[name]
                        pending.append((child_path, child))
                    if links > max_links:
                        break
                except (OSError, RuntimeError, ValueError, KeyError) as exc:
                    incomplete(f"cannot enumerate group {path[:128]}: {type(exc).__name__}")
    except (OSError, RuntimeError, ValueError, KeyError) as exc:
        incomplete(f"native file or root could not be opened: {type(exc).__name__}")
    return OwnershipInventory(tuple(sorted(ranges, key=lambda a: (a.start, a.end))),
                              objects, siblings, links, not problems, tuple(problems))


def reject_sibling_overlap(
    selected: list[tuple[int, int, tuple[int, ...]]], inventory: OwnershipInventory,
) -> None:
    """A positively observed competing owner contradicts the selected index."""
    # Both inputs are small and bounded by the recovery and inventory quotas.
    others = inventory.allocations
    index = 0
    for start, end, coordinate in sorted(selected):
        while index < len(others) and others[index].end <= start:
            index += 1
        probe = index
        while probe < len(others) and others[probe].start < end:
            other = others[probe]
            if start < other.end:
                raise FormatError(
                    f"selected chunk {coordinate} overlaps rooted sibling dataset "
                    f"{other.path} {other.layout} allocation at byte {other.start}"
                )
            probe += 1
