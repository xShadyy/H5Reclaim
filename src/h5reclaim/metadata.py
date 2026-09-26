"""Supported dataset metadata, obtained without reading its chunk payloads."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import h5py


class UnsupportedCase(ValueError):
    """The selected file or dataset falls outside the declared support envelope."""


@dataclass(frozen=True)
class DatasetSpec:
    path: str
    object_address: int
    shape: tuple[int, int]
    chunks: tuple[int, int]

    @property
    def chunk_grid(self) -> tuple[int, int]:
        return (self.shape[0] // self.chunks[0], self.shape[1] // self.chunks[1])

    @property
    def chunk_bytes(self) -> int:
        return self.chunks[0] * self.chunks[1] * 4


def read_dataset_spec(source: Path, dataset_path: str) -> DatasetSpec:
    if not dataset_path.startswith("/"):
        raise UnsupportedCase("dataset path must be absolute, for example /measurements")
    try:
        with h5py.File(source, "r") as handle:
            selected = handle.get(dataset_path)
            if not isinstance(selected, h5py.Dataset):
                raise UnsupportedCase(f"dataset {dataset_path!r} was not found")
            if selected.name == "/_h5reclaim" or selected.name.startswith("/_h5reclaim/"):
                raise UnsupportedCase("the /_h5reclaim output namespace is reserved")
            if not Path(selected.file.filename).samefile(source):
                raise UnsupportedCase("external dataset links are not supported")

            dataset_addresses: set[int] = set()

            def collect(_name: str, obj: h5py.Group | h5py.Dataset) -> None:
                if isinstance(obj, h5py.Dataset):
                    dataset_addresses.add(int(h5py.h5o.get_info(obj.id).addr))

            handle.visititems(collect)
            if len(dataset_addresses) != 1:
                raise UnsupportedCase("this release requires exactly one dataset")

            shape = selected.shape
            chunks = selected.chunks
            if len(shape) != 2 or chunks is None or len(chunks) != 2:
                raise UnsupportedCase("expected a rank-two chunked dataset")
            if selected.maxshape != shape:
                raise UnsupportedCase("extendible datasets are not supported")
            if any(length <= 0 or length % chunk for length, chunk in zip(shape, chunks)):
                raise UnsupportedCase("dimensions must be positive and divisible by chunks")
            if shape[0] * shape[1] > 1_048_576:
                raise UnsupportedCase("dataset exceeds the current 1,048,576-element limit")
            if chunks[0] * chunks[1] * 4 > 1_048_576:
                raise UnsupportedCase("chunk exceeds the current 1 MiB limit")

            datatype = selected.id.get_type()
            if (
                datatype.get_class() != h5py.h5t.INTEGER
                or datatype.get_size() != 4
                or datatype.get_sign() != h5py.h5t.SGN_NONE
                or datatype.get_order() != h5py.h5t.ORDER_LE
            ):
                raise UnsupportedCase("expected little-endian unsigned 32-bit integers")

            creation = selected.id.get_create_plist()
            if creation.get_nfilters() != 0:
                raise UnsupportedCase("filtered or compressed chunks are not supported")
            if creation.get_external_count() != 0 or selected.is_virtual:
                raise UnsupportedCase("external and virtual storage are not supported")

            return DatasetSpec(
                path=selected.name,
                object_address=int(h5py.h5o.get_info(selected.id).addr),
                shape=(int(shape[0]), int(shape[1])),
                chunks=(int(chunks[0]), int(chunks[1])),
            )
    except UnsupportedCase:
        raise
    except (OSError, RuntimeError, ValueError) as exc:
        raise UnsupportedCase(f"HDF5 could not open selected dataset metadata: {exc}") from exc
