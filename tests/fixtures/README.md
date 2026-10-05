# Shared-message regression fixture

`sohm_shared_dataspace.h5` was created by HDF5 2.0.0 with a latest-format
file-access property list and these file-creation settings:

- `H5Pset_shared_mesg_nindexes(fcpl, 1)`
- `H5Pset_shared_mesg_index(fcpl, 0, 2, 8)` (simple dataspace enabled)
- `H5Pset_shared_mesg_phase_change(fcpl, 4, 2)`

It has four chunked datasets `/d0` through `/d3`, each holding the uint32
sequence 0 through 7. The library wrote `/d1`'s dataspace as a v3 shared
object-header message in a managed fractal heap, reached through its
checksummed SOHM master table and record list. This 3,200-byte fixture is
copied before test mutations. Its generated values make shared-message
recovery reproducible.

SHA-256: `0ab54ef33d52617630ce8c6e28128d2e0eb3723c81251e664cf2ca4c0ecbbd47`

`sohm_shared_datatype.h5` uses the same HDF5 version and latest-format
settings, with `H5Pset_shared_mesg_index(fcpl, 0, 8, 8)` to share datatype
messages. It contains four `/d0` through `/d3` datasets of 128 uint32
elements, 32-element chunks, shuffle, DEFLATE, and Fletcher32. Its `/d1`
datatype resolves through the same bounded SOHM list and managed heap path.
Its generated values exercise shared datatype recovery.

SHA-256: `59bc1829a4ab4958a65e0175d016f4d6a8665dc67a95661a378dd96d139f7f21`

`sohm_shared_filter_pipeline.h5` uses index flags `2048` for the filter
pipeline, otherwise the same shape, values, chunking, and filters as the
datatype fixture. `/d1`'s v3 shared filter pipeline resolves through the
checked SOHM list and managed heap. It is also generated test data.

SHA-256: `327e3f17fc47337df48da06611d5f8923e3597d03bdbe4148ac3ee9d5fa9a87c`

`sohm_btree_leaf.h5` and `sohm_btree_internal.h5` use the simple-dataspace
sharing settings of the first fixture, but contain eight and 45 distinct
dataset shapes respectively, each repeated twice. This pushes the SOHM
index from SMLI to the HDF5 version-2 B-tree client type 7, first as a leaf
and then with an internal root. Each `/shape_N_J` dataset holds the uint32
sequence 0 through N-1 in one chunk. Both are generated data.

- Leaf fixture SHA-256: `bf3a44df4aa1f001e0002c5a9ad0fdc7512b6d56f65cf58d5ba3bf9649ac1e59`
- Internal fixture SHA-256: `18d2197ba52c9c3037ed73689533a4cd4fb406622fced16b552f2a4bcccf56ea`
