"""Bounded fixed-width HDF5 schema checks and raw chunk filter reversal.

No filter plugin is imported. Unknown filters remain explicit missing decoders,
not evidence of corrupt measurements. Filters are reversed in on-disk pipeline
order and a skipped optional filter is determined by its *position* in the
pipeline, never by its identifier. The output is the complete nominal chunk,
including any HDF5 padding beyond a partial dataset edge.

Format references: HDF5 File Format Specification IV.A.3.m and the raw chunk
filter-mask definition; H5Pset_filter and H5Dread_chunk documentation.
"""

from __future__ import annotations

import zlib
from dataclasses import dataclass
from math import prod

import h5py
import numpy as np


MAX_DECODED_CHUNK_BYTES = 1 << 20
MAX_STORED_CHUNK_BYTES = 2 << 20
MAX_FILTERS = 8
MAX_FILTER_PARAMETERS = 64
MAX_DATATYPE_ENCODING_BYTES = 16 * 1024
MAX_DATATYPE_MEMBERS = 64
MAX_DATATYPE_DEPTH = 4
KNOWN_FILTERS = frozenset((h5py.h5z.FILTER_SHUFFLE,
                            h5py.h5z.FILTER_DEFLATE,
                            h5py.h5z.FILTER_FLETCHER32))


class SchemaError(ValueError):
    """An on-disk datatype or pipeline is outside the validated envelope."""


class ChunkDecodeError(ValueError):
    """The supplied bytes contradict the declared chunk/filter information."""


class MissingFilterDecoder(ChunkDecodeError):
    """A stored chunk needs a decoder that is not implemented or installed."""


@dataclass(frozen=True)
class FilterDescriptor:
    id: int
    flags: int
    values: tuple[int, ...]


def canonical_numeric_dtype(datatype: h5py.h5t.TypeID, dtype: np.dtype) -> str:
    """Validate exact primitive integer or IEEE binary32/64 file storage.

    NumPy's visible dtype alone does not reveal reduced precision, shifted
    values, arbitrary HDF5 bit padding, or a non-IEEE floating-point layout.
    The accepted dtype string retains file byte order for direct raw output.
    """
    dtype = np.dtype(dtype)
    if dtype.fields or dtype.subdtype or dtype.hasobject or dtype.kind not in "iuf":
        raise SchemaError("structural export requires a primitive fixed-width numeric type")
    if dtype.itemsize not in (1, 2, 4, 8) or (dtype.kind == "f" and dtype.itemsize not in (4, 8)):
        raise SchemaError("integer width or IEEE floating-point width is unsupported")
    bits = 8 * dtype.itemsize
    if (datatype.get_size() != dtype.itemsize or datatype.get_precision() != bits
            or datatype.get_offset() != 0
            or datatype.get_pad() != (h5py.h5t.PAD_ZERO, h5py.h5t.PAD_ZERO)):
        raise SchemaError("numeric datatype has noncanonical precision, offset, or padding")
    order = datatype.get_order()
    if dtype.itemsize == 1:
        if order not in (h5py.h5t.ORDER_NONE, h5py.h5t.ORDER_LE, h5py.h5t.ORDER_BE):
            raise SchemaError("unsupported one-byte numeric order")
    elif not (
        (dtype.byteorder == "<" and order == h5py.h5t.ORDER_LE)
        or (dtype.byteorder == ">" and order == h5py.h5t.ORDER_BE)
        or (dtype.byteorder == "=" and order ==
            (h5py.h5t.ORDER_LE if np.little_endian else h5py.h5t.ORDER_BE))
    ):
        raise SchemaError("numeric datatype file byte order disagrees with NumPy dtype")
    if dtype.kind in "iu":
        expected_sign = h5py.h5t.SGN_2 if dtype.kind == "i" else h5py.h5t.SGN_NONE
        if datatype.get_class() != h5py.h5t.INTEGER or datatype.get_sign() != expected_sign:
            raise SchemaError("integer representation or sign is noncanonical")
    else:
        fields, bias = {4: ((31, 23, 8, 0, 23), 127),
                        8: ((63, 52, 11, 0, 52), 1023)}[dtype.itemsize]
        if (datatype.get_class() != h5py.h5t.FLOAT
                or datatype.get_fields() != fields or datatype.get_ebias() != bias
                or datatype.get_norm() != h5py.h5t.NORM_IMPLIED
                or datatype.get_inpad() != h5py.h5t.PAD_ZERO):
            raise SchemaError("floating-point representation is not IEEE binary32/64")
    return dtype.str


def fixed_file_datatype(datatype: h5py.h5t.TypeID) -> tuple[np.dtype, bytes]:
    """Accept bounded, self-contained HDF5 file types and retain their exact H5T.

    A NumPy dtype loses information such as string padding, enum names, opaque
    tags, and compound field offsets in some contexts. The encoded H5T is
    therefore carried to the output writer, while the dtype supplies only
    item size and a human-readable schema description. No pointers to a heap,
    another object, or variable-length storage are accepted.
    """
    try:
        encoded = datatype.encode()
        dtype = np.dtype(datatype.dtype)
        if (not 2 < len(encoded) <= MAX_DATATYPE_ENCODING_BYTES
                or dtype.hasobject or not 0 < datatype.get_size() <= 1_048_576
                or dtype.itemsize != datatype.get_size()):
            raise SchemaError("datatype is variable, oversized, or disagrees with file width")

        def check(type_id: h5py.h5t.TypeID, depth: int) -> None:
            if depth > MAX_DATATYPE_DEPTH:
                raise SchemaError("datatype nesting exceeds limit")
            kind = type_id.get_class()
            if kind in (h5py.h5t.INTEGER, h5py.h5t.FLOAT):
                canonical_numeric_dtype(type_id, type_id.dtype)
            elif kind == h5py.h5t.STRING:
                if (type_id.is_variable_str() or not 0 < type_id.get_size() <= 4096
                        or type_id.get_cset() not in (h5py.h5t.CSET_ASCII, h5py.h5t.CSET_UTF8)
                        or type_id.get_strpad() not in (h5py.h5t.STR_NULLTERM,
                                                       h5py.h5t.STR_NULLPAD,
                                                       h5py.h5t.STR_SPACEPAD)):
                    raise SchemaError("variable or unsupported fixed-width string")
            elif kind == h5py.h5t.OPAQUE:
                if not 0 < type_id.get_size() <= 4096 or len(type_id.get_tag()) > 256:
                    raise SchemaError("opaque type exceeds bounded tag or width")
            elif kind == h5py.h5t.ENUM:
                if not 0 < type_id.get_nmembers() <= MAX_DATATYPE_MEMBERS:
                    raise SchemaError("enum exceeds member limit")
                check(type_id.get_super(), depth + 1)
                names = [type_id.get_member_name(index) for index in range(type_id.get_nmembers())]
                if any(not name or len(name) > 128 for name in names) or len(set(names)) != len(names):
                    raise SchemaError("enum has duplicate or unbounded member names")
            elif kind == h5py.h5t.ARRAY:
                dimensions = type_id.get_array_dims()
                if not 1 <= len(dimensions) <= 4 or any(not 0 < n <= 4096 for n in dimensions):
                    raise SchemaError("array dimensions exceed limits")
                check(type_id.get_super(), depth + 1)
                if np.prod(dimensions, dtype=object) * type_id.get_super().get_size() != type_id.get_size():
                    raise SchemaError("array width disagrees with dimensions")
            elif kind == h5py.h5t.COMPOUND:
                if not 0 < type_id.get_nmembers() <= MAX_DATATYPE_MEMBERS:
                    raise SchemaError("compound exceeds member limit")
                names = set()
                ranges: list[tuple[int, int]] = []
                for index in range(type_id.get_nmembers()):
                    name = type_id.get_member_name(index)
                    if not name or len(name) > 128 or name in names:
                        raise SchemaError("compound has duplicate or unbounded member names")
                    names.add(name)
                    member = type_id.get_member_type(index)
                    check(member, depth + 1)
                    start = type_id.get_member_offset(index)
                    end = start + member.get_size()
                    if start < 0 or end > type_id.get_size():
                        raise SchemaError("compound member extends outside record")
                    ranges.append((start, end))
                if any(a[1] > b[0] for a, b in zip(sorted(ranges), sorted(ranges)[1:])):
                    raise SchemaError("overlapping compound members are not supported")
            else:
                raise SchemaError("datatype contains a reference, VLEN, heap, or unsupported class")

        check(datatype, 0)
        return dtype, encoded
    except SchemaError:
        raise
    except (ValueError, TypeError, OverflowError, RuntimeError) as exc:
        raise SchemaError(f"file datatype cannot be validated: {exc}") from exc


def datatype_from_message(raw: bytes, *, padded: bool = False) -> tuple[np.dtype, bytes]:
    """Validate a rooted raw datatype message against H5T's canonical encoding.

    Legacy object-header messages are rounded up to eight bytes. Other raw
    messages must match the re-encoded type byte-for-byte, so a parser cannot
    quietly reinterpret trailing bytes or accept an alternative schema.
    """
    if not 8 <= len(raw) <= MAX_DATATYPE_ENCODING_BYTES - 2:
        raise SchemaError("raw datatype message exceeds bounds")
    try:
        datatype = h5py.h5t.decode(b"\x03\x00" + raw)
        dtype, encoded = fixed_file_datatype(datatype)
    except (ValueError, TypeError, RuntimeError, OSError) as exc:
        raise SchemaError(f"raw datatype message cannot be decoded: {exc}") from exc
    body = encoded[2:]
    if padded:
        padding = (-len(body)) % 8
        if raw != body + b"\x00" * padding:
            raise SchemaError("older datatype message or padding differs from canonical encoding")
    elif raw != body:
        raise SchemaError("datatype message differs from canonical encoding")
    return dtype, encoded


def read_filter_pipeline(creation: h5py.h5p.PropDCID, element_size: int) -> tuple[FilterDescriptor, ...]:
    """Read bounded filter metadata without invoking an external decoder."""
    count = creation.get_nfilters()
    if count > MAX_FILTERS:
        raise SchemaError("filter pipeline exceeds the eight-filter limit")
    pipeline: list[FilterDescriptor] = []
    seen: set[int] = set()
    for position in range(count):
        identifier, flags, values, _name = creation.get_filter(position)
        identifier, flags = int(identifier), int(flags)
        values = tuple(int(value) for value in values)
        if (identifier < 0 or identifier > 65535 or flags & ~h5py.h5z.FLAG_OPTIONAL
                or len(values) > MAX_FILTER_PARAMETERS
                or any(value < 0 or value > 0xFFFFFFFF for value in values)):
            raise SchemaError("filter metadata exceeds the bounded representation")
        if identifier in seen:
            raise SchemaError("duplicate filter in dataset pipeline")
        seen.add(identifier)
        if identifier == h5py.h5z.FILTER_SHUFFLE and values != (element_size,):
            raise SchemaError("shuffle element width disagrees with dataset type")
        if identifier == h5py.h5z.FILTER_DEFLATE and (
            len(values) != 1 or not 0 <= values[0] <= 9
        ):
            raise SchemaError("deflate level is invalid")
        if identifier == h5py.h5z.FILTER_FLETCHER32 and values:
            raise SchemaError("Fletcher32 filter parameters are invalid")
        pipeline.append(FilterDescriptor(identifier, flags, values))
    return tuple(pipeline)


def _pipeline(spec: object) -> tuple[FilterDescriptor, ...]:
    # Existing user-independent tests construct DatasetSpec directly with
    # filter IDs; these ordinary defaults preserve that public data model.
    declared = getattr(spec, "filter_pipeline", ())
    if declared:
        if tuple(filter_.id for filter_ in declared) != tuple(spec.filters):
            raise ChunkDecodeError("filter IDs disagree with the validated pipeline")
        return declared
    result = []
    for identifier in spec.filters:
        result.append(FilterDescriptor(
            identifier,
            h5py.h5z.FLAG_OPTIONAL if identifier in
            (h5py.h5z.FILTER_DEFLATE, h5py.h5z.FILTER_SHUFFLE) else 0,
            (np.dtype(spec.dtype).itemsize,) if identifier == h5py.h5z.FILTER_SHUFFLE
            else (6,) if identifier == h5py.h5z.FILTER_DEFLATE else (),
        ))
    return tuple(result)


def _validate_common(spec: object, mask: int) -> tuple[tuple[FilterDescriptor, ...], int]:
    chunk_bytes = prod(spec.chunks) * np.dtype(spec.dtype).itemsize
    if chunk_bytes <= 0 or chunk_bytes > MAX_DECODED_CHUNK_BYTES:
        raise ChunkDecodeError("decoded chunk exceeds the bounded 1 MiB limit")
    pipeline = _pipeline(spec)
    if (not isinstance(mask, int) or mask < 0 or mask >> len(pipeline)
            or len(pipeline) > MAX_FILTERS):
        raise ChunkDecodeError("chunk filter mask references an absent filter")
    for position, filter_ in enumerate(pipeline):
        if mask & (1 << position) and not filter_.flags & h5py.h5z.FLAG_OPTIONAL:
            raise ChunkDecodeError("mandatory filter was marked skipped")
    return pipeline, chunk_bytes


def validate_stored_size(spec: object, stored_size: int, mask: int) -> None:
    """Reject impossible allocation lengths before attempting the raw read."""
    pipeline, chunk_bytes = _validate_common(spec, mask)
    if not isinstance(stored_size, int) or not 0 < stored_size <= MAX_STORED_CHUNK_BYTES:
        raise ChunkDecodeError("stored chunk exceeds the bounded 2 MiB limit")
    active = tuple(filter_ for index, filter_ in enumerate(pipeline)
                   if not mask & (1 << index))
    if not any(filter_.id not in KNOWN_FILTERS or filter_.id == h5py.h5z.FILTER_DEFLATE
               for filter_ in active):
        expected = chunk_bytes + 4 * sum(
            filter_.id == h5py.h5z.FILTER_FLETCHER32 for filter_ in active
        )
        if stored_size != expected:
            raise ChunkDecodeError("fixed-length filter pipeline has an unexpected stored size")


def fletcher32_applied(spec: object, filter_mask: int) -> bool:
    """Whether this chunk actually used the Fletcher32 filter in its pipeline."""
    pipeline, _chunk_bytes = _validate_common(spec, filter_mask)
    return any(filter_.id == h5py.h5z.FILTER_FLETCHER32
               and not filter_mask & (1 << position)
               for position, filter_ in enumerate(pipeline))


def fletcher32(data: bytes) -> int:
    """HDF5's Fletcher32 over big-endian 16-bit words, 360 words per fold."""
    sum1 = sum2 = 0
    even_length = len(data) & ~1
    for start in range(0, even_length, 720):
        for position in range(start, min(start + 720, even_length), 2):
            sum1 += (data[position] << 8) | data[position + 1]
            sum2 += sum1
        sum1 = (sum1 & 0xFFFF) + (sum1 >> 16)
        sum2 = (sum2 & 0xFFFF) + (sum2 >> 16)
    if len(data) & 1:
        sum1 += data[-1] << 8
        sum2 += sum1
        sum1 = (sum1 & 0xFFFF) + (sum1 >> 16)
        sum2 = (sum2 & 0xFFFF) + (sum2 >> 16)
    sum1 = (sum1 & 0xFFFF) + (sum1 >> 16)
    sum2 = (sum2 & 0xFFFF) + (sum2 >> 16)
    return (sum2 << 16) | sum1


def _inflate(data: bytes, limit: int) -> bytes:
    try:
        decoder = zlib.decompressobj()
        result = decoder.decompress(data, limit + 1)
        if (len(result) > limit or not decoder.eof or decoder.unused_data
                or decoder.unconsumed_tail):
            raise ChunkDecodeError("DEFLATE output limit or stream boundary is invalid")
        return result
    except zlib.error as exc:
        raise ChunkDecodeError("DEFLATE stream is corrupt") from exc


def _unshuffle(data: bytes, element_size: int) -> bytes:
    if element_size < 1:
        raise ChunkDecodeError("shuffle element width is invalid")
    count = len(data) // element_size
    result = bytearray(len(data))
    for offset in range(element_size):
        result[offset:count * element_size:element_size] = data[
            offset * count:(offset + 1) * count
        ]
    # HDF5's shuffle leaves a final incomplete element untouched. This can
    # occur if shuffle follows another filter that changes the byte count.
    result[count * element_size:] = data[count * element_size:]
    return bytes(result)


def decode_chunk(raw: bytes, spec: object, filter_mask: int) -> bytes:
    """Decode one raw chunk with exact final length and bounded intermediates."""
    validate_stored_size(spec, len(raw), filter_mask)
    pipeline, chunk_bytes = _validate_common(spec, filter_mask)
    # Decoder availability is a property of the declared pipeline, not an
    # inference from what a later checksum or decompressor does to these
    # bytes. Report it first so lack of a plugin cannot be mislabeled damage.
    for position, filter_ in enumerate(pipeline):
        if not filter_mask & (1 << position) and filter_.id not in KNOWN_FILTERS:
            raise MissingFilterDecoder(f"filter {filter_.id} decoder is unavailable")
    data = raw
    for position in reversed(range(len(pipeline))):
        if filter_mask & (1 << position):
            continue
        filter_ = pipeline[position]
        if filter_.id == h5py.h5z.FILTER_FLETCHER32:
            if len(data) < 4 or fletcher32(data[:-4]) != int.from_bytes(data[-4:], "little"):
                raise ChunkDecodeError("Fletcher32 checksum mismatch")
            data = data[:-4]
        elif filter_.id == h5py.h5z.FILTER_DEFLATE:
            data = _inflate(data, chunk_bytes + 4)
        elif filter_.id == h5py.h5z.FILTER_SHUFFLE:
            data = _unshuffle(data, np.dtype(spec.dtype).itemsize)
        else:
            raise MissingFilterDecoder(f"filter {filter_.id} decoder is unavailable")
        # A valid compressed stream can be larger than its input (even when
        # DEFLATE is declared optional), so only the decompressor's *output*
        # is capped at the nominal chunk plus one possible checksum word.
        if len(data) > MAX_STORED_CHUNK_BYTES:
            raise ChunkDecodeError("filter stage exceeds the stored chunk size bound")
    if len(data) != chunk_bytes:
        raise ChunkDecodeError("decoded chunk has an unexpected nominal length")
    return data
