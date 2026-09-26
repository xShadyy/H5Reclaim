# Technical project brief

Scientific measurements can remain in an HDF5 file after damage to the metadata used to locate chunked dataset data. H5Reclaim investigates whether it can recover some of those bytes at justified coordinates and expose uncertainty clearly.

The first supported case is one selected, fully written, fixed-size, uncompressed, rank-two `uint32` dataset whose version-1 raw-data B-tree has a broken root-to-leaf child link. The generated fixture's actual layout and the resulting standard-reader failure were verified. The current implementation requires a single dataset and dimensions divisible by chunk dimensions.

The implementation reads the source without modifying it. Structural evidence connects an accepted chunk to a dataset and coordinate. The recovery engine does not infer missing measurement values. A separate output carries validity information, and a report distinguishes structural attribution from byte integrity; ground truth is available only to controlled evaluation.

The first experiment now has a reproducible healthy fixture, controlled broken-pointer copy, narrow recovery path, and independent exact-placement evaluation. Multi-dataset ownership, stale allocations, compression, arbitrary filters, partial edge chunks, other index families, and total schema loss remain outside the implemented support envelope.

Potential users include researchers and research software engineers handling interrupted workflows. A synthetic, structurally supported recovery capability has been demonstrated in the documented environment. Comparative advantage, independent use, production reliability, and publication have not been established.

Technical format reference: [HDF5 File Format Specification, Version 4.0](https://support.hdfgroup.org/documentation/hdf5/latest/_f_m_t4.html).
