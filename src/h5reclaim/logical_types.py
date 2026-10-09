"""Portable logical tokens for heap-backed records and HDF5 references."""

from __future__ import annotations

import base64
import json

import h5py
import numpy as np

from .metadata import UnsupportedCase


def type_label(typ):
    """Describe types even when NumPy has no matching numeric width."""
    try:
        return np.dtype(typ.dtype).str
    except (TypeError, ValueError):
        return f'hdf5:class-{typ.get_class()}:bytes-{typ.get_size()}'


def contains_pointers(typ) -> bool:
    kind = typ.get_class()
    if kind in (h5py.h5t.VLEN, h5py.h5t.REFERENCE):
        return True
    if kind == h5py.h5t.STRING:
        return typ.is_variable_str()
    if kind == h5py.h5t.COMPOUND:
        return any(contains_pointers(typ.get_member_type(i)) for i in range(typ.get_nmembers()))
    if kind == h5py.h5t.ARRAY:
        return contains_pointers(typ.get_super())
    return False


def validate_type(typ, depth=0, *, budget=None):
    from .large_streaming import LargeBudget
    budget = budget or LargeBudget()
    if (depth > budget.max_type_depth or len(typ.encode()) > budget.max_metadata_bytes
            or not 0 < typ.get_size() <= budget.max_chunk_bytes):
        raise UnsupportedCase("datatype nesting or size exceeds the streaming budget")
    kind = typ.get_class()
    if kind == h5py.h5t.COMPOUND:
        if not 0 < typ.get_nmembers() <= budget.max_type_members:
            raise UnsupportedCase("compound datatype has too many members")
        names, ranges = set(), []
        for i in range(typ.get_nmembers()):
            name, child = typ.get_member_name(i), typ.get_member_type(i)
            start = typ.get_member_offset(i)
            if not name or name in names or len(name) > budget.max_metadata_bytes or start + child.get_size() > typ.get_size():
                raise UnsupportedCase("compound member name or extent is invalid")
            names.add(name)
            ranges.append((start, start + child.get_size()))
            validate_type(child, depth + 1, budget=budget)
        ranges.sort()
        if any(a[1] > b[0] for a, b in zip(ranges, ranges[1:])):
            raise UnsupportedCase("compound member extents overlap")
    elif kind in (h5py.h5t.VLEN, h5py.h5t.ARRAY, h5py.h5t.ENUM):
        validate_type(typ.get_super(), depth + 1, budget=budget)
    elif kind == h5py.h5t.REFERENCE:
        if not (typ.equal(h5py.h5t.STD_REF_OBJ) or typ.equal(h5py.h5t.STD_REF_DSETREG)):
            raise UnsupportedCase("this reference representation is not available through h5py")
    elif kind == h5py.h5t.STRING:
        if typ.get_cset() not in (h5py.h5t.CSET_ASCII, h5py.h5t.CSET_UTF8):
            raise UnsupportedCase("unknown string character set")
    elif kind not in (h5py.h5t.INTEGER, h5py.h5t.FLOAT, h5py.h5t.BITFIELD, h5py.h5t.OPAQUE, h5py.h5t.TIME):
        raise UnsupportedCase("datatype class cannot be represented through native HDF5")


def encode_value(value, typ, handle, *, address_map=None, max_bytes=4 * 1024**2, max_depth=64):
    """Encode logical fields, never the process-local pointers in object dtypes."""
    def visit(item, type_id, depth=0):
        if depth > max_depth:
            raise ValueError("logical value nesting exceeds the budget")
        kind = type_id.get_class()
        if kind == h5py.h5t.COMPOUND:
            if np.dtype(type_id.dtype).kind == "c":
                raw = np.asarray(item, dtype=type_id.dtype).tobytes()
                return {"bytes": base64.b64encode(raw).decode("ascii")}
            return {"compound": [visit(item[type_id.get_member_name(i).decode("utf-8")],
                                      type_id.get_member_type(i), depth + 1)
                                 for i in range(type_id.get_nmembers())]}
        if kind in (h5py.h5t.ARRAY, h5py.h5t.VLEN):
            array = np.asarray(item)
            if array.size > max_bytes:
                raise ValueError("logical array exceeds the element budget")
            return {"array": [visit(part, type_id.get_super(), depth + 1) for part in array.flat],
                    "shape": list(array.shape)}
        if kind == h5py.h5t.REFERENCE:
            if not item:
                return {"reference": None}
            target = handle[item]
            address = int(h5py.h5o.get_info(target.id).addr)
            if address_map is not None:
                address = address_map[address]
            region = None
            if type_id.equal(h5py.h5t.STD_REF_DSETREG):
                space = h5py.h5r.get_region(item, handle.id)
                if space.get_simple_extent_dims() != target.shape:
                    raise ValueError("reference region extent differs from its target")
                region = space.encode().hex()
            return {"reference": {"address": address, "path": target.name, "region": region}}
        if kind == h5py.h5t.STRING:
            raw = item.encode("utf-8") if isinstance(item, str) else bytes(item)
            if len(raw) > max_bytes:
                raise ValueError("logical string exceeds its byte budget")
            return {"bytes": base64.b64encode(raw).decode("ascii")}
        if type_id.get_size() > max_bytes:
            raise ValueError("logical field exceeds its byte budget")
        raw = np.asarray(item, dtype=type_id.dtype).tobytes()
        return {"bytes": base64.b64encode(raw).decode("ascii")}

    token = visit(value, typ)
    if len(token_bytes(token)) > max_bytes:
        raise ValueError("decoded logical record exceeds its byte budget")
    return token


def token_bytes(token):
    def canonical_region(encoded):
        # HDF5 can encode the same region with different dataspace versions
        # in files created with different library bounds. Compare the extent
        # and copied selection in one fresh H5S representation, retaining
        # point order and hyperslabs without enumerating potentially huge
        # selections. The original token still carries a decodable region.
        source = h5py.h5s.decode(bytes.fromhex(encoded))
        shape = source.get_simple_extent_dims()
        canonical = (h5py.h5s.create_simple(shape) if shape else
                     h5py.h5s.create(source.get_simple_extent_type()))
        if hasattr(canonical, "select_copy"):
            canonical.select_copy(source)
        else:
            # h5py 3.10 does not expose this public HDF5 operation yet.
            # Resolve it from the same linked HDF5 library as the SpaceIDs.
            import ctypes
            from .native_bindings import public_function
            copy_selection = public_function("H5Sselect_copy", [ctypes.c_int64, ctypes.c_int64])
            if copy_selection(canonical.id, source.id) < 0:
                raise ValueError("HDF5 could not normalize a reference selection")
        return canonical.encode().hex()

    def identity(item):
        if isinstance(item, dict):
            return {key: (canonical_region(value) if key == "region" and isinstance(value, str)
                          else identity(value))
                    for key, value in item.items() if key != "path"}
        if isinstance(item, list):
            return [identity(value) for value in item]
        return item
    return json.dumps(identity(token), sort_keys=True, separators=(",", ":")).encode("utf-8")


def reference_addresses(token):
    if "reference" in token:
        return {token["reference"]["address"]} if token["reference"] else set()
    result = set()
    for key in ("compound", "array"):
        for child in token.get(key, []):
            result.update(reference_addresses(child))
    return result


def decode_value(token, typ, handle, paths):
    kind = typ.get_class()
    if kind == h5py.h5t.REFERENCE:
        ref = token["reference"]
        if ref is None:
            return h5py.RegionReference() if typ.equal(h5py.h5t.STD_REF_DSETREG) else h5py.Reference()
        path = paths[ref["address"]]
        target = handle[path]
        if ref["region"] is None:
            return target.ref
        space = h5py.h5s.decode(bytes.fromhex(ref["region"]))
        if space.get_simple_extent_dims() != target.shape:
            raise ValueError("remapped region extent differs from target")
        return h5py.h5r.create(handle.id, path.encode("utf-8"), h5py.h5r.DATASET_REGION, space)
    if kind == h5py.h5t.COMPOUND and "compound" in token:
        # A scalar structured array broadcasts a nested VLEN ndarray into
        # one scalar slot, sometimes storing its first integer instead of the
        # sequence. A one-record array gives each object field a true slot.
        result = np.zeros((1,), dtype=typ.dtype)
        for i, child in enumerate(token["compound"]):
            name = typ.get_member_name(i).decode("utf-8")
            result[name][0] = decode_value(child, typ.get_member_type(i), handle, paths)
        return result[0]
    if kind in (h5py.h5t.ARRAY, h5py.h5t.VLEN):
        base = typ.get_super()
        result = np.empty(tuple(token["shape"]), dtype=base.dtype)
        for index, child in enumerate(token["array"]):
            result.flat[index] = decode_value(child, base, handle, paths)
        return result
    raw = base64.b64decode(token["bytes"], validate=True)
    if kind == h5py.h5t.STRING:
        return raw
    return np.frombuffer(raw, dtype=typ.dtype, count=1)[0]


def write_value(dataset, index, value):
    typ = dataset.id.get_type()
    if typ.get_class() == h5py.h5t.VLEN and typ.get_super().get_class() in (h5py.h5t.INTEGER, h5py.h5t.FLOAT):
        from .variable_readable import _write_value
        _write_value(dataset, index, value, "numeric", np.dtype(typ.get_super().dtype))
    else:
        dataset[index] = value
