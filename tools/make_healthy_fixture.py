"""Create and independently reopen a small healthy HDF5 research fixture.

This module is fixture generation and evaluation code, never a recovery input.
"""

from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path

import h5py
import numpy as np


def positive_int(value: str) -> int:
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("dimensions must be positive")
    return number


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--rows", type=positive_int, default=512)
    parser.add_argument("--cols", type=positive_int, default=512)
    parser.add_argument("--chunk-rows", type=positive_int, default=16)
    parser.add_argument("--chunk-cols", type=positive_int, default=16)
    args = parser.parse_args()

    shape = (args.rows, args.cols)
    chunks = (args.chunk_rows, args.chunk_cols)
    if any(size % chunk for size, chunk in zip(shape, chunks)):
        parser.error("each dimension must be divisible by its chunk dimension")
    if args.rows * args.cols > 1_048_576:
        parser.error("M0 fixtures are limited to 1,048,576 elements")

    expected = np.arange(args.rows * args.cols, dtype="<u4").reshape(shape)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with h5py.File(args.output, "x", libver=("earliest", "v108")) as handle:
        dataset = handle.create_dataset(
            "measurements", shape=shape, dtype="<u4", chunks=chunks
        )
        dataset[...] = expected
        handle.flush()

    with h5py.File(args.output, "r") as handle:
        dataset = handle["/measurements"]
        if dataset.shape != shape or dataset.chunks != chunks:
            raise AssertionError("shape or chunk layout changed during round-trip")
        if dataset.dtype != np.dtype("<u4"):
            raise AssertionError("dtype changed during round-trip")
        if dataset.compression is not None or dataset.id.get_create_plist().get_nfilters():
            raise AssertionError("fixture unexpectedly has an active filter")
        allocated_chunks = dataset.id.get_num_chunks()
        expected_chunks = (args.rows // args.chunk_rows) * (
            args.cols // args.chunk_cols
        )
        if allocated_chunks != expected_chunks:
            raise AssertionError("not all chunks were allocated")
        actual = dataset[...]
        if not np.array_equal(actual, expected):
            raise AssertionError("healthy HDF5 round-trip differs from reference")

    file_hash = hashlib.sha256(args.output.read_bytes()).hexdigest()
    print(f"file: {args.output}")
    print(f"shape: {shape}, chunks: {chunks}, allocated chunks: {allocated_chunks}")
    print(f"verified elements: {expected.size}, SHA-256: {file_hash}")
    print(
        f"Python/NumPy/h5py/HDF5: "
        f"{sys.version.split()[0]}/{np.__version__}/"
        f"{h5py.__version__}/{h5py.version.hdf5_version}"
    )


if __name__ == "__main__":
    main()
