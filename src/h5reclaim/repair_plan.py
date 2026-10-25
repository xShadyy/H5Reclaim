"""Deterministic byte plans, new-file application and an audit trail.

Original-checksum repairs are distinct from optional metadata resealing.
Resealing establishes current structural coherence, not historical values.
Every proposed result must pass the independent covered-metadata preflight.
"""

from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import tempfile
import time

from .format import SIGNATURE, FormatError, UnsupportedFormat
from .large_streaming import LargeBudget, sparse_snapshot, _copy_sparse, _deadline
from .metadata import UnsupportedCase
from .metadata_correction import _unique_one_byte, _selected_layout_pointer
from .header_dimension_trial import _selected_dimension_field
from .modern_indexes import ModernH5File, lookup3
from .metadata_fallback import _messages, _unique
from .preflight import inspect_metadata_local
from .recovery import VERSION, RecoveryError, _validate_paths, _verify_source, sha256_file


MAX_PLAN_BYTES = 4 << 20
CATALOG_VERSION = 1


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def _copy(source, target, budget):
    with source.open("rb") as original, target.open("w+b") as destination:
        _copy_sparse(original, destination, size=source.stat().st_size, parent=target.parent,
                     budget=budget, deadline=time.monotonic() + budget.max_seconds)


def _read(path, offset, length):
    if not 0 <= offset <= path.stat().st_size or not 0 <= length <= min(1 << 20, path.stat().st_size - offset):
        raise FormatError("repair metadata range is outside the bounded source")
    with path.open("rb") as stream:
        stream.seek(offset)
        value = stream.read(length)
    if len(value) != length:
        raise FormatError("short metadata read")
    return value


def _write(path, offset, value):
    with path.open("r+b") as stream:
        stream.seek(offset)
        stream.write(value)


def _superblock(path, allow_reseal, touches):
    size, position, candidates = path.stat().st_size, 0, []
    while position + 12 <= size:
        prefix = _read(path, position, 12)
        if ((prefix[:8] == SIGNATURE or sum(a != b for a, b in zip(prefix[:8], SIGNATURE)) == 1)
                and prefix[8] in (0, 1, 2, 3)):
            candidates.append((position, prefix))
        position = 512 if position == 0 else position * 2
    if len(candidates) != 1:
        raise UnsupportedCase("repair needs one unambiguous superblock candidate at a documented offset")
    position, prefix = candidates[0]
    if prefix[8] < 2:
        if prefix[:8] != SIGNATURE:
            _write(path, position, SIGNATURE)
            touches.append((position, position + 8, "restore_legacy_signature", "unchecksummed structural validation"))
        return
    width, length_width, flags = prefix[9:12]
    if width not in (2, 4, 8) or length_width not in (2, 4, 8) or flags & ~5:
        raise UnsupportedCase("superblock address widths or consistency flags are unsupported")
    raw = _read(path, position, 16 + 4 * width)
    changed, kinds, basis = bytearray(raw), [], "original checksum restored"
    stored = int.from_bytes(raw[-4:], "little")
    if raw[:8] != SIGNATURE:
        changed[:8] = SIGNATURE
        kinds.append("restore_modern_signature")
    root_start = 12 + 3 * width
    if lookup3(changed[:-4]) != stored and not flags:
        try:
            offset, value = _unique_one_byte(bytes(changed), range(root_start, root_start + width), stored)
            changed[offset] = value
            kinds.append("restore_root_pointer")
        except FormatError:
            if not allow_reseal:
                raise
    if flags:
        changed[11] = 0
        kinds.append("clear_file_consistency_flags")
        basis = "current structural coherence after clearing status on a separate copy"
    base = int.from_bytes(changed[12:12 + width], "little")
    if base != position:
        if not allow_reseal:
            raise UnsupportedCase("base-address normalization requires --allow-reseal")
        changed[12:12 + width] = position.to_bytes(width, "little")
        kinds.append("normalize_superblock_base")
    eoa_start = 12 + 2 * width
    eoa = int.from_bytes(changed[eoa_start:eoa_start + width], "little")
    if eoa != size and allow_reseal:
        changed[eoa_start:eoa_start + width] = size.to_bytes(width, "little")
        kinds.append("reconcile_declared_eof")
    if lookup3(changed[:-4]) != stored:
        if not allow_reseal and not flags:
            raise UnsupportedCase("superblock cannot restore its original checksum")
        changed[-4:] = lookup3(changed[:-4]).to_bytes(4, "little")
        kinds.append("reseal_superblock_checksum")
        basis = "current structural coherence; original checksum was recomputed"
    if bytes(changed) != raw:
        _write(path, position, changed)
        touches.append((position, position + len(raw), ",".join(kinds), basis))


def _object_header(reader, address):
    prefix = reader.read_at(address, 6)
    if prefix[:5] != b"OHDR\x02" or prefix[5] & 0xC0:
        raise UnsupportedCase("repair needs a bounded v2 object header")
    flags = prefix[5]
    extra = (16 if flags & 32 else 0) + (4 if flags & 16 else 0)
    width = 1 << (flags & 3)
    prefix_size = 6 + extra + width
    size = int.from_bytes(reader.read_at(address + prefix_size - width, width), "little")
    if not 4 <= size <= 16384:
        raise UnsupportedCase("object-header repair exceeds its 16 KiB quota")
    return reader.absolute(address), reader.read_at(address, prefix_size + size + 4)


def _object_repairs(path, inspection, allow_reseal, touches):
    changed_any = False
    if not inspection.get("objects"):
        return False
    with ModernH5File(path, large_sparse_scan=True) as reader:
        for descriptor in inspection["objects"]:
            address = descriptor["address"]
            try:
                start, raw = _object_header(reader, address)
            except (FormatError, UnsupportedCase):
                continue
            expected = int.from_bytes(raw[-4:], "little")
            if lookup3(raw[:-4]) != expected:
                restored = None
                for locator in ("layout", "dimensions"):
                    try:
                        if locator == "layout":
                            _, candidate_raw, field, _ = _selected_layout_pointer(reader, address)
                            positions = range(field, field + reader.superblock.offset_size)
                        else:
                            _, candidate_raw, positions, _, _ = _selected_dimension_field(reader, address)
                        offset, value = _unique_one_byte(candidate_raw, positions, expected)
                        proposal = bytearray(candidate_raw)
                        proposal[offset] = value
                        if restored is not None and restored != bytes(proposal):
                            raise FormatError("different fields produce ambiguous original-checksum repairs")
                        restored = bytes(proposal)
                    except UnsupportedCase:
                        continue
                    except FormatError as exc:
                        if "ambiguous" in str(exc):
                            raise
                if restored is not None:
                    _write(path, start, restored)
                    touches.append((start, start + len(raw), "restore_object_layout_or_dimension", "original checksum restored"))
                    changed_any = True
                    continue
                if allow_reseal:
                    proposal = raw[:-4] + lookup3(raw[:-4]).to_bytes(4, "little")
                    _write(path, start, proposal)
                    touches.append((start, start + len(raw), "reseal_object_header", "current structural coherence; checksum recomputed"))
                    changed_any = True
                    continue
            if not allow_reseal:
                continue
            try:
                messages = _messages(reader, address)
                typ, layout = _unique(messages, 3), _unique(messages, 8)
                if typ is None or layout is None or len(typ.data) < 8 or len(layout.data) < 6:
                    continue
                layout_data = layout.data
                if layout_data[0] not in (4, 5) or layout_data[1] != 2:
                    continue
                ndim, dimwidth = layout_data[3:5]
                if not 2 <= ndim <= 6 or dimwidth not in (1, 2, 4, 8):
                    continue
                element_size = int.from_bytes(typ.data[4:8], "little")
                field = 5 + (ndim - 1) * dimwidth
                if field + dimwidth >= len(layout_data):
                    continue
                stored_size = int.from_bytes(layout_data[field:field + dimwidth], "little")
                if stored_size != element_size and 0 < element_size < 1 << (8 * dimwidth):
                    proposal = bytearray(raw)
                    relative = layout.absolute_offset - start + field
                    proposal[relative:relative + dimwidth] = element_size.to_bytes(dimwidth, "little")
                    proposal[-4:] = lookup3(proposal[:-4]).to_bytes(4, "little")
                    _write(path, start, proposal)
                    touches.append((start, start + len(raw), "restore_layout_element_width", "unique inline datatype width and current structural coherence"))
                    changed_any = True
                    continue
                # An index header is owned by the checked selected layout.
                kind_offset = 5 + ndim * dimwidth
                kind = layout_data[kind_offset]
                if kind not in (3, 4, 5):
                    continue
                offsize, lensize = reader.superblock.offset_size, reader.superblock.length_size
                target = int.from_bytes(layout_data[-offsize:], "little")
                header_size = {3: 12 + lensize + offsize, 4: 16 + 6 * lensize + offsize,
                               5: 22 + offsize + lensize}[kind]
                header = reader.read_at(target, header_size)
                if header[:5] != {3: b"FAHD\0", 4: b"EAHD\0", 5: b"BTHD\0"}[kind]:
                    continue
                proposal = bytearray(header)
                if kind == 5 and int.from_bytes(header[12:14], "little") == 0:
                    count = int.from_bytes(header[16 + offsize:18 + offsize], "little")
                    total = int.from_bytes(header[18 + offsize:18 + offsize + lensize], "little")
                    if count != total:
                        proposal[18 + offsize:18 + offsize + lensize] = count.to_bytes(lensize, "little")
                proposal[-4:] = lookup3(proposal[:-4]).to_bytes(4, "little")
                if proposal != header:
                    physical = reader.absolute(target)
                    _write(path, physical, proposal)
                    touches.append((physical, physical + len(header), "reconcile_or_reseal_index_header",
                                    "layout-owned index and current structural coherence; checksum recomputed"))
                    changed_any = True
            except (FormatError, UnsupportedCase, IndexError, OverflowError):
                continue
    return changed_any


def _build_plan(original, working, digest, budget, allow_reseal):
    touches, unresolved = [], []
    deadline = time.monotonic() + budget.max_seconds
    try:
        _superblock(working, allow_reseal, touches)
        for _ in range(16):
            _deadline(deadline)
            inspection = inspect_metadata_local(working, budget=budget)
            if inspection["index_repairs"]:
                with ModernH5File(working) as reader:
                    width = reader.superblock.offset_size
                for repair in inspection["index_repairs"]:
                    at = repair["pointer_offset"]
                    _write(working, at, int(repair["target_address"]).to_bytes(width, "little"))
                    touches.append((at, at + width, repair["kind"], "unique original parent checksum restored"))
                continue
            if inspection["decision"] in ("accept", "accept_with_warnings") and inspection["analysis"]["complete"]:
                break
            try:
                if _object_repairs(working, inspection, allow_reseal, touches):
                    continue
            except (FormatError, UnsupportedCase):
                pass
            break
        inspection = inspect_metadata_local(working, budget=budget)
        executable = inspection["decision"] in ("accept", "accept_with_warnings") and inspection["analysis"]["complete"]
        unresolved = inspection["findings"] if not executable else []
        validation = {"decision": inspection["decision"], "analysis": inspection["analysis"]}
    except (FormatError, UnsupportedCase, OSError, ValueError) as exc:
        executable = False
        unresolved = [{"code": "NO_JUSTIFIED_REPAIR", "message": str(exc)[:700]}]
        validation = {"decision": "unresolved"}
    # Coalesce repeated repairs of one header into exact nonoverlapping writes.
    merged = []
    for start, end, kind, basis in sorted(touches):
        if merged and start < merged[-1][1]:
            before, old_end, kinds, bases = merged[-1]
            merged[-1] = (before, max(old_end, end), kinds | {kind}, bases | {basis})
        else:
            merged.append((start, end, {kind}, {basis}))
    actions = []
    for start, end, kinds, bases in merged:
        before, after = _read(original, start, end - start), _read(working, start, end - start)
        if before != after:
            actions.append({"offset": start, "before_hex": before.hex(), "after_hex": after.hex(),
                            "repairs": sorted(kinds), "evidence": sorted(bases)})
    result = {"schema_version": 1, "tool": "h5reclaim", "tool_version": VERSION,
        "kind": "metadata_repair_plan", "catalog_version": CATALOG_VERSION,
        "source": {"sha256": digest, "size_bytes": original.stat().st_size},
        "options": {"allow_reseal": allow_reseal, "budget": asdict(budget)},
        "actions": actions, "executable": executable, "postcheck": validation, "unresolved": unresolved,
        "scope": "metadata repair on a separate copy; recomputed checksums establish structural coherence only; raw data and historical measurement correctness are not restored or authenticated"}
    result["plan_sha256"] = hashlib.sha256(_canonical(result)).hexdigest()
    return result


def plan_repair(source, destination=None, *, budget=None, allow_reseal=False):
    source, budget = Path(source), budget or LargeBudget()
    with sparse_snapshot(source, budget=budget) as (image, digest, identity, size, copied), \
            tempfile.TemporaryDirectory(prefix="h5reclaim-repair-plan-") as directory:
        working = Path(directory) / "working.h5"
        _copy(image, working, budget)
        plan = _build_plan(image, working, digest, budget, allow_reseal)
        _verify_source(source, identity, digest)
    if destination is not None:
        destination = Path(destination)
        with tempfile.TemporaryDirectory(prefix=".h5reclaim-plan-", dir=destination.parent) as directory:
            staged = Path(directory) / "plan.json"
            staged.write_text(json.dumps(plan, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            if staged.stat().st_size > MAX_PLAN_BYTES:
                raise UnsupportedCase("repair plan exceeds the JSON byte quota")
            os.link(staged, destination)
    return plan


def load_plan(path):
    with Path(path).open("rb") as stream:
        raw = stream.read(MAX_PLAN_BYTES + 1)
    if len(raw) > MAX_PLAN_BYTES:
        raise ValueError("repair plan exceeds the JSON byte quota")
    plan = json.loads(raw)
    if not isinstance(plan, dict) or plan.get("kind") != "metadata_repair_plan" or plan.get("tool") != "h5reclaim":
        raise ValueError("expected an H5Reclaim metadata repair plan")
    observed = dict(plan)
    expected = observed.pop("plan_sha256", None)
    if hashlib.sha256(_canonical(observed)).hexdigest() != expected:
        raise ValueError("repair plan content differs from its recorded digest")
    return plan


def apply_repair(source, plan_path, output, audit):
    source, output, audit = Path(source), Path(output), Path(audit)
    _validate_paths(source, output, audit)
    plan = load_plan(plan_path)
    if plan.get("executable") is not True:
        raise UnsupportedCase("plan has unresolved metadata; no repaired copy can be published")
    budget = LargeBudget(**plan["options"]["budget"])
    with sparse_snapshot(source, budget=budget) as (image, digest, identity, size, copied):
        if plan["source"] != {"sha256": digest, "size_bytes": size}:
            raise RecoveryError("source differs from the planned input")
        with tempfile.TemporaryDirectory(prefix=".h5reclaim-repair-", dir=output.parent) as outdir, \
             tempfile.TemporaryDirectory(prefix=".h5reclaim-repair-", dir=audit.parent) as logdir:
            working = Path(outdir) / "repaired.h5"
            _copy(image, working, budget)
            regenerated = _build_plan(image, working, digest, budget, plan["options"]["allow_reseal"])
            if regenerated != plan:
                raise RecoveryError("regenerated repair plan differs from the supplied plan")
            # The regenerated private copy already contains exactly these writes.
            records = [{"event": "repair_applied", "offset": a["offset"], "before_hex": a["before_hex"],
                        "after_hex": a["after_hex"], "repairs": a["repairs"]} for a in plan["actions"]]
            result = {"event": "repair_verified", "tool": "h5reclaim", "tool_version": VERSION,
                "plan_sha256": plan["plan_sha256"], "source_sha256": digest,
                "output_sha256": sha256_file(working), "postcheck": plan["postcheck"],
                "scope": plan["scope"]}
            staged_log = Path(logdir) / "audit.jsonl"
            staged_log.write_text("".join(json.dumps(r, sort_keys=True) + "\n" for r in [*records, result]), encoding="utf-8")
            _verify_source(source, identity, digest)
            _validate_paths(source, output, audit)
            os.link(working, output)
            try:
                os.link(staged_log, audit)
            except Exception:
                output.unlink(missing_ok=True)
                raise
            return result
