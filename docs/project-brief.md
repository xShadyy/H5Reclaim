# Technical project brief

Scientific measurements can remain in an HDF5 file after damage to the metadata used to locate chunked dataset data. H5Reclaim investigates whether it can recover some of those bytes at justified coordinates and expose uncertainty clearly.

The proposed first supported case is one selected, fully written, fixed-size, uncompressed, rank-two `uint32` dataset whose version-1 raw-data B-tree has a broken internal child link. This choice is provisional until the actual file layout and controlled failure are verified. The initial experiment will use a single dataset and dimensions divisible by chunk dimensions.

The intended design reads the source without modifying it. Structural evidence must connect an accepted chunk to a dataset and coordinate. The recovery engine will not infer missing measurement values. A separate output should carry validity information, and a report should distinguish structural attribution, byte integrity evidence, and ground truth available only to controlled evaluation.

The first milestones are: reproduce and inspect a healthy fixture; create a controlled damaged copy; then attempt the narrow recovery with exact placement checks and explicit refusal cases. Multi-dataset ownership, stale allocations, compression, arbitrary filters, partial edge chunks, other index families, and total schema loss remain outside the initial support envelope.

Potential users include researchers and research software engineers handling interrupted workflows. No recovery capability, comparative advantage, independent use, or publication has been established yet.

Technical format reference: [HDF5 File Format Specification, Version 4.0](https://support.hdfgroup.org/documentation/hdf5/latest/_f_m_t4.html).
