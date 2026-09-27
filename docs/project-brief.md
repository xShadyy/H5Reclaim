# Technical project brief

Scientific measurements can remain in an HDF5 file after damage to the metadata used to locate chunked dataset data. H5Reclaim investigates whether it can recover some of those bytes at justified coordinates and expose uncertainty clearly.

The first supported case is one explicitly selected, fixed-size, unfiltered, rank-two dataset with canonical little-endian `uint32` storage whose version-1 raw-data B-tree has a broken root-to-leaf child link. Its dimensions must be divisible by chunk dimensions. The generated, fully written fixture's actual layout and the resulting standard-reader failure were verified. Other local datasets may coexist when the selected object's own metadata anchors its index; the tool never attributes data by matching dataset shapes.

The implementation reads the source without modifying it and analyzes a private snapshot so the HDF5 library and raw parser see the same bytes. Structural evidence connects an accepted chunk to a dataset and coordinate; parsed metadata extents and accepted payloads cannot overlap. The recovery engine does not infer missing measurement values. A separate output carries validity information, and a report distinguishes structural attribution from byte integrity; ground truth is available only to controlled evaluation. Original attributes, dimension scales, links, sibling objects, and other scientific context are not reproduced in the output.

The first experiment now has a reproducible healthy fixture, controlled broken-pointer copy, narrow recovery path, metadata-only `survey` command, and exact-placement evaluation. Each selected dataset is assessed independently, even in a file with multiple local datasets. Attribution of disconnected structures without the selected object's anchors, stale allocations, compression, arbitrary filters, partial edge chunks, other index families, and total schema loss remain outside the implemented support envelope. Unknown layouts are reported or refused instead of converted by guessing their schema.

Potential users include researchers and research software engineers handling interrupted workflows. A synthetic, structurally supported recovery capability has been demonstrated in the documented environment. Comparative advantage, independent use, production reliability, and publication have not been established.

Technical format reference: [HDF5 File Format Specification, Version 4.0](https://support.hdfgroup.org/documentation/hdf5/latest/_f_m_t4.html).
