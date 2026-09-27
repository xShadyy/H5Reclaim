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
copied before test mutations. It is generated data, not an authentic
scientific measurement or a naturally damaged file.

SHA-256: `0ab54ef33d52617630ce8c6e28128d2e0eb3723c81251e664cf2ca4c0ecbbd47`

`sohm_shared_datatype.h5` uses the same HDF5 version and latest-format
settings, with `H5Pset_shared_mesg_index(fcpl, 0, 8, 8)` to share datatype
messages. It contains four `/d0` through `/d3` datasets of 128 uint32
elements, 32-element chunks, shuffle, DEFLATE, and Fletcher32. Its `/d1`
datatype resolves through the same bounded SOHM list and managed heap path.
It is also generated test data, not a naturally damaged scientific file.

SHA-256: `59bc1829a4ab4958a65e0175d016f4d6a8665dc67a95661a378dd96d139f7f21`

`sohm_shared_filter_pipeline.h5` uses index flags `2048` for the filter
pipeline, otherwise the same shape, values, chunking, and filters as the
datatype fixture. `/d1`'s v3 shared filter pipeline resolves through the
checked SOHM list and managed heap. It is also generated test data.

SHA-256: `327e3f17fc47337df48da06611d5f8923e3597d03bdbe4148ac3ee9d5fa9a87c`
