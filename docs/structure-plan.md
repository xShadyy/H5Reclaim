# Index verification and controlled damage

This procedure guided M0 and is implemented for the controlled fixture in `tools/make_broken_link_fixture.py`. The measured structure and results are in `docs/status.md`; addresses and node counts must be checked again for each generated file and HDF5 environment.

## Locate the selected dataset's index

1. Generate `work/truth/healthy.h5` and run the round-trip test described in the README. Record Python, h5py, NumPy, and HDF5 versions and the SHA-256 of the pristine file.
2. With HDF5 tools installed, run `h5ls -vr work/truth/healthy.h5`. Note the object-header address for `/measurements`. As another healthy-file check, h5py's `h5py.h5o.get_info(handle['/measurements'].id).addr` gives the dataset object address.
3. Run `h5debug work/truth/healthy.h5 <dataset-object-address>`. Confirm layout message `0x0008`, version 3, chunked storage, index type `v1 B-tree`, chunk dimensions `{16,16,4}`, and a defined root address. The last dimension is the four-byte datatype element size. If any field differs, investigate before changing bytes.
4. Run `h5debug work/truth/healthy.h5 <btree-root-address> 3`. The `3` is the chunk-key dimensionality, dataset rank plus one. Confirm the root is a `TREE` node of type 1, level one, with several leaf children. Inspect child nodes at their reported addresses using the same command. Confirm their level and left/right sibling links. A signature scan alone cannot assign a node to this dataset.
5. For the default `(512,512)` shape and `(16,16)` chunks, expect 1024 allocated chunks, each with 1024 payload bytes, filter mask zero, and key offsets `(16a,16b,0)` for `a,b` in `[0,32)`. Compare all reported coordinates and payload addresses with h5py's chunk-info API on the pristine file. Reject duplicate coordinates, out-of-bounds data, inconsistent node levels, or differing index types. Keep the actual HDF5 tool output and version with the experiment.

HDF5 stores metadata as unsigned little-endian fields. For a v1 node, the four bytes `TREE` are followed by node type (1 means raw-data chunks), level (0 means leaf), a two-byte count, two sibling addresses of `O` bytes each, and alternating keys and child addresses. The superblock defines `O` and the base address. For rank two, a chunk key has `8 + 8 * 3 = 32` bytes. A non-leaf child address is a B-tree node; a level-zero child address is a chunk payload. The selected dataset's layout message supplies the ownership anchor.

## Controlled damage after verification

Choose an interior level-zero child of a verified level-one root with reciprocal left/right siblings. The current tool rejects other root levels. Save the pristine file and its hash. Copy it to a separately named damaged file, then change only the selected parent child-pointer field to the format's all-`ff` undefined address. Do not mutate the original file.

The zero-based child `i` pointer begins at node-relative byte `8 + 2*O + i*(32+O) + 32` for this rank-two v1 case. Treat this as a derived address to verify, not as a blind patch recipe: parse the actual superblock's `O` and base address, confirm the bytes at that position equal the child address reported by `h5debug`, and verify the pointed-to leaf and its siblings before writing a disposable copy. Compare before/after byte ranges and input hashes to confirm one intentional edit. Record the manifest outside future recovery inputs.

On the damaged copy, attempt a full standard read and reads of both affected and unaffected coordinates. Opening the file is insufficient. The first recovery experiment must retrieve affected chunks through surviving structural evidence and compare exact coordinates and values against the clean reference in a separate evaluator. If the library still returns the affected values exactly, the chosen mutation has not demonstrated the intended failure. If reciprocal sibling links do not provide a defensible anchor, leave the region unresolved rather than assigning detached chunks by plausible shape.

Sources: [HDF5 File Format Specification, v4.0](https://support.hdfgroup.org/documentation/hdf5/latest/_f_m_t4.html), sections II.A, III.A.1, and IV.A.3.i; [HDF5 troubleshooting example using `h5ls` and `h5debug`](https://support.hdfgroup.org/documentation/hdf5/latest/_comp_t_s.html#autotoc_md418).
