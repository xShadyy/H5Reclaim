"""Create a flagged fixture from a closed writer, including a valid checksum."""

from pathlib import Path

from h5reclaim.modern_indexes import lookup3


def copy_with_write_flag(source: Path, destination: Path) -> None:
    raw = bytearray(source.read_bytes())
    if raw[:8] != b"\x89HDF\r\n\x1a\n" or raw[8] not in (2, 3) or raw[11] != 0:
        raise ValueError("fixture needs a closed modern HDF5 writer")
    size = 16 + 4 * raw[9]
    if lookup3(raw[:size - 4]) != int.from_bytes(raw[size - 4:size], "little"):
        raise ValueError("fixture superblock checksum is invalid")
    raw[11] = 1
    raw[size - 4:size] = lookup3(raw[:size - 4]).to_bytes(4, "little")
    with destination.open("xb") as handle:
        handle.write(raw)
