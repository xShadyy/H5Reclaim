# Original scientific HDF5 files

`files/` contains four unchanged files published by scientific data providers. They support offline checks against actual scientific HDF5 structures. URLs, citations, licenses, byte sizes, and SHA-256 hashes are recorded in [manifest.json](manifest.json).

| File | Research context | Representative dataset |
| --- | --- | --- |
| `H-H1_GWOSC_4KHZ_R1-1126259447-32.hdf5` | GWOSC Hanford detector data around GW150914 | `/strain/Strain` |
| `H-H1_GWOSC_16KHZ_R1-1126259447-32.hdf5` | The same event at 16 kHz | `/strain/Strain` |
| `fast_feedback_raw_data.h5` | Superconducting-qubit error correction | `/circuit_0/result/hard_measurements/36` |
| `PACE22_FMITALON_005.h5` | Aircraft cloud and aerosol measurements from PaCE 2022 | `/20220922_101405/columns/dataframe` |

The GWOSC files are credited to the Gravitational Wave Open Science Center, a service of the LIGO Scientific Collaboration, the Virgo Collaboration, and KAGRA. Follow [GWOSC acknowledgment guidance](https://gwosc.org/acknowledgement/) when publishing results. The qubit record credits Laura Caune and coauthors ([DOI 10.5281/zenodo.13961130](https://doi.org/10.5281/zenodo.13961130)); the cloud record credits Jessica Girdwood, David Brus, and Konstantinos Doulgeris ([DOI 10.5281/zenodo.14755046](https://doi.org/10.5281/zenodo.14755046)). All four original files are distributed under [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/). H5Reclaim source code has its own Apache 2.0 license.
