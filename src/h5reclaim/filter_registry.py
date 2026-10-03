"""Register packaged HDF5 decoders and distinguish reversible output filters."""

from __future__ import annotations


OPTIONAL_FILTERS = {32015: "zstd", 32001: "blosc", 32026: "blosc2",
                    32008: "bshuf", 32004: "lz4", 307: "bzip2",
                    32018: "fcidecomp", 32033: "htj2k", 32028: "sperr",
                    32017: "sz", 32024: "sz3", 32013: "zfp"}

REVERSIBLE_FILTERS = frozenset((1, 2, 3, 4, 5, 32000, 32015, 32001,
                              32008, 32004, 307))


def preserve_output_filters(identifiers) -> bool:
    """Avoid a second lossy encoding and accept decoder-only installations."""
    import h5py
    return all(identifier in REVERSIBLE_FILTERS and
               h5py.h5z.get_filter_info(identifier) & h5py.h5z.FILTER_CONFIG_ENCODE_ENABLED
               for identifier in identifiers)


def creation_filter_options(identifier, values, itemsize):
    """Remove only documented set-local metadata before constructing a filter."""
    if identifier == 32008:
        if len(values) not in (5, 6) or values[2] != itemsize:
            raise ValueError('Bitshuffle parameters disagree with the dataset type')
        return values[3:]
    if identifier in (32013, 32017, 32024, 32028, 32018, 32033):
        import hdf5plugin
        if hasattr(hdf5plugin, 'from_filter_options'):
            return tuple(hdf5plugin.from_filter_options(identifier, values)['compression_opts'])
        if identifier == 32024 and len(values) >= 13 and len(values) == values[0] + 12:
            return values[-9:]
        raise ValueError('the installed codec cannot reconstruct the persisted filter options')
    return values


def register_optional() -> dict[int, str]:
    """Use installed hdf5plugin libraries, without searching HDF5_PLUGIN_PATH.

    Registration is explicit and occurs inside the isolated worker. Installing
    the ``filters`` extra enables these codecs; unknown filter IDs still refuse.
    """
    try:
        import hdf5plugin
    except ImportError:
        return {}
    import h5py
    available = {}
    for identifier, name in OPTIONAL_FILTERS.items():
        try:
            hdf5plugin.register(filters=name, force=False)
            if h5py.h5z.filter_avail(identifier):
                available[identifier] = name
        except (OSError, RuntimeError, ValueError):
            continue
    return available


def supported_filters() -> frozenset[int]:
    from .readable_export import NATIVE_FILTERS
    return NATIVE_FILTERS | register_optional().keys()
