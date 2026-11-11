"""Bounded filter framing and redundant decode-buffer parameters.

HDF5 stores chunk element counts in N-bit and scale-offset client data, and
the atomic element width at client-data index four. Neither value overrides
the dataset's independently framed datatype and chunk dimensions.
"""

from math import prod

from .format import FormatError, UnsupportedFormat
from .schema_codec import MAX_FILTER_PARAMETERS, FilterDescriptor


def pipeline_fields(raw, element_size):
    """Return descriptors and their client-data offsets within the message."""
    if raw is None:
        return ()
    if len(raw) < 2 or raw[0] not in (1, 2) or not 1 <= raw[1] <= 8:
        raise UnsupportedFormat("unsupported filter-pipeline version or count")
    older = raw[0] == 1
    pos = 8 if older else 2
    if older and (len(raw) < 8 or any(raw[2:8])):
        raise FormatError("invalid older filter-pipeline prefix")
    result, seen = [], set()
    for _ in range(raw[1]):
        if len(raw) - pos < 6:
            raise FormatError("truncated filter descriptor")
        identifier = int.from_bytes(raw[pos : pos + 2], "little")
        pos += 2
        name_length = 0
        if older or identifier >= 256:
            name_length = int.from_bytes(raw[pos : pos + 2], "little")
            pos += 2
        if name_length > 128 or older and name_length % 8 or len(raw) - pos < 4:
            raise FormatError("invalid filter name length or descriptor framing")
        flags = int.from_bytes(raw[pos : pos + 2], "little")
        count = int.from_bytes(raw[pos + 2 : pos + 4], "little")
        pos += 4
        if flags & ~1 or count > MAX_FILTER_PARAMETERS or len(raw) - pos < name_length + 4 * count:
            raise FormatError("invalid filter flags or bounded client-data length")
        name = raw[pos : pos + name_length]
        pos += name_length
        if name and (b"\0" not in name or any(name[name.index(0) :])):
            raise FormatError("filter name is not terminated and zero padded")
        values_at = pos
        values = tuple(
            int.from_bytes(raw[p : p + 4], "little") for p in range(pos, pos + count * 4, 4)
        )
        pos += count * 4
        if older and count % 2:
            if len(raw) - pos < 4 or any(raw[pos : pos + 4]):
                raise FormatError("invalid older filter client-data padding")
            pos += 4
        if identifier in seen:
            raise FormatError("duplicate filter in selected pipeline")
        seen.add(identifier)
        if identifier == 3 and (flags, values) != (0, ()):
            raise UnsupportedFormat("unsupported Fletcher32 parameters")
        if identifier == 1 and not (len(values) == 1 and 0 <= values[0] <= 9):
            raise UnsupportedFormat("unsupported DEFLATE parameters")
        if identifier == 2 and values != (element_size,):
            raise UnsupportedFormat("shuffle width contradicts the datatype")
        result.append((FilterDescriptor(identifier, flags, values), values_at))
    if pos > len(raw) or (any(raw[pos:]) if older else pos != len(raw)):
        raise FormatError("filter-pipeline trailing bytes")
    return tuple(result)


def chunk_dimensions(layout, shape, element_size, offset_size):
    if not shape or len(layout) < 3 or layout[1] != 2:
        raise FormatError("decode filter requires simple chunked storage")
    if layout[0] == 3:
        rank, width, pos = layout[2], 4, 3 + offset_size
    elif layout[0] in (4, 5) and len(layout) >= 5:
        rank, width, pos = layout[3], layout[4], 5
    else:
        raise UnsupportedFormat("decode-filter chunk layout version is unsupported")
    end = pos + rank * width
    if rank != len(shape) + 1 or width not in (1, 2, 4, 8) or end > len(layout):
        raise FormatError("decode-filter chunk dimensions contradict the dataspace")
    dimensions = tuple(
        int.from_bytes(layout[p : p + width], "little") for p in range(pos, end, width)
    )
    if any(n < 1 for n in dimensions) or dimensions[-1] != element_size:
        raise FormatError("decode-filter chunk dimensions contradict the datatype")
    return dimensions[:-1]


def redundant_fields(messages, shape, offset_size):
    """Locate mismatched redundant fields in already rooted header messages.

    No arbitrary filter parameters are guessed. Nested N-bit descriptions are
    left to their decoder; only atomic N-bit widths have this fixed position.
    """

    def selected(kind):
        matching = [message for message in messages if message.kind == kind]
        if len(matching) > 1:
            raise FormatError("duplicate schema message in decode-filter evidence")
        if matching and matching[0].flags & 2:
            raise UnsupportedFormat("shared decode-filter schema needs separate resolution")
        return matching[0] if matching else None

    pipeline = selected(11)
    if pipeline is None:
        return ()
    datatype, layout = selected(3), selected(8)
    if datatype is None or layout is None or len(datatype.data) < 8:
        raise FormatError("decode-filter schema is incomplete")
    raw_type = datatype.data
    element_size = int.from_bytes(raw_type[4:8], "little")
    if not 0 < element_size <= 1 << 20:
        raise UnsupportedFormat("decode-filter datatype width exceeds the bound")
    fields = pipeline_fields(pipeline.data, element_size)
    if not any(descriptor.id in (5, 6) for descriptor, _ in fields):
        return ()
    dimensions = chunk_dimensions(layout.data, shape, element_size, offset_size)
    elements = prod(dimensions)
    if not 0 < elements <= 0xFFFFFFFF:
        raise UnsupportedFormat("decode-filter chunk count cannot fit its stored field")
    corrections = []
    for descriptor, start in fields:
        values, identifier = descriptor.values, descriptor.id
        if identifier not in (5, 6):
            continue
        if identifier == 5:
            if len(values) < 4:
                raise FormatError("N-bit client data is truncated")
            if values[3] != 1:
                continue
            if (
                len(values) != 8
                or values[0] != 8
                or values[1] not in (0, 1)
                or raw_type[0] & 15 not in (0, 1)
                or len(raw_type) < 12
                or values[5] != (raw_type[1] & 1)
                or values[6] != int.from_bytes(raw_type[10:12], "little")
                or values[7] != int.from_bytes(raw_type[8:10], "little")
            ):
                raise FormatError("atomic N-bit description contradicts the datatype")
        elif len(values) != 20 or raw_type[0] & 15 not in (0, 1) or values[3] != (raw_type[0] & 15):
            raise FormatError("scale-offset description contradicts its atomic datatype")
        for index, expected, name in (
            (2, elements, "element_count"),
            (4, element_size, "element_size"),
        ):
            if values[index] != expected:
                corrections.append(
                    {
                        "offset": pipeline.absolute_offset + start + index * 4,
                        "before": values[index],
                        "after": expected,
                        "kind": f"restore_filter_{name}",
                        "filter": identifier,
                    }
                )
    return tuple(corrections)


def validate_native_buffers(dataset):
    """Check redundant sizes before allowing a native filter to allocate."""
    creation = dataset.id.get_create_plist()
    for index in range(creation.get_nfilters()):
        identifier, _, values, _ = creation.get_filter(index)
        if identifier not in (5, 6):
            continue
        if dataset.chunks is None or len(values) < 4:
            raise FormatError("decode filter has no complete chunked buffer description")
        if values[2] != prod(dataset.chunks):
            raise FormatError(
                "decode-filter element count contradicts the chunk dimensions; use plan-repair"
            )
        if identifier == 6 or values[3] == 1:
            if len(values) < 5 or values[4] != dataset.id.get_type().get_size():
                raise FormatError(
                    "decode-filter element width contradicts the datatype; use plan-repair"
                )
