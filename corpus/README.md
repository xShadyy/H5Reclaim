# Original scientific HDF5 files

`files/` contains four **unchanged** files published by scientific data providers. They are bundled so an offline checkout can inspect real HDF5 structures without generating test data or downloading anything. The URLs, citations, license, file sizes, and SHA-256 hashes are pinned in [`manifest.json`](manifest.json). `benchmarks/run_real_corpus.py` checks the hashes before reading any HDF5 metadata.

| File | Research context | Representative dataset | Structural feature |
| --- | --- | --- | --- |
| `H-H1_GWOSC_4KHZ_R1-1126259447-32.hdf5` | GWOSC Hanford detector data around GW150914 | `/strain/Strain` | One-dimensional, compressed floating-point strain; its intact level-zero chunk-tree root directly indexes 64 chunks |
| `H-H1_GWOSC_16KHZ_R1-1126259447-32.hdf5` | Same GWOSC event at 16 kHz | `/strain/Strain` | Supported metadata and a level-one chunk tree; an interior index link has two neighbors for the controlled recovery trial |
| `fast_feedback_raw_data.h5` | Experimental superconducting-qubit error correction | `/circuit_0/result/hard_measurements/36` | Nested groups and many contiguous, differently typed datasets |
| `PACE22_FMITALON_005.h5` | Aircraft cloud and aerosol measurements from PaCE 2022 | `/20220922_101405/columns/dataframe` | Contiguous compound table with floating-point fields |

The GWOSC files are credited to the Gravitational Wave Open Science Center, a service of the LIGO Scientific Collaboration, the Virgo Collaboration, and KAGRA. Follow [GWOSC's acknowledgment and citation guidance](https://gwosc.org/acknowledgement/) when publishing results. The qubit record credits Laura Caune and coauthors ([DOI 10.5281/zenodo.13961130](https://doi.org/10.5281/zenodo.13961130)); the cloud record credits Jessica Girdwood, David Brus, and Konstantinos Doulgeris ([DOI 10.5281/zenodo.14755046](https://doi.org/10.5281/zenodo.14755046)). All four original files are distributed under [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/). H5Reclaim's source code has its own Apache 2.0 license.

## Run the offline coverage check

With the project's Python dependencies available, run from the repository root:

```sh
python benchmarks/run_real_corpus.py
```

The command prints a readable per-file result by default: verified originals,
dataset totals, candidates, unsupported cases, representative datasets and
their reasons, and whether the pinned coverage baseline still matches. Use
`python benchmarks/run_real_corpus.py --json` for the complete structured
report on standard output. It checks every bundled file's size and SHA-256,
inventories its local datasets through `survey`, and summarizes support
reasons. It exits with code 2 when a file is missing or its bytes have changed;
a survey classification change from the pinned baseline exits with code 1 for
review, and a matching baseline exits with code 0. It may make and remove
bounded private analysis snapshots, but makes **no network requests**, changes
no originals, reads no dataset measurement values, and does not need the
experimental fixture generator.

On Windows PowerShell, run `py -3 -m venv .venv`, then `./.venv/Scripts/python.exe -m pip install -e .`, then `./.venv/Scripts/python.exe benchmarks/run_real_corpus.py`. The editable install is from this local directory; it does not retrieve `h5reclaim` from the Python package index. Pip may retrieve the project's declared `h5py` and NumPy dependencies unless they are installed already.

## What this checks

The current survey classifies 249 datasets as unsupported and two GWOSC strain datasets as candidates. That is a useful finding about **real input coverage**. The files have different storage layouts, ranks, types, and filters. The 4 kHz strain has an intact level-zero chunk-tree root. It can be exported from its directly indexed chunks when metadata remains readable, but there is no safe detached-link reconstruction for a broken pointer in that root. Do not count `candidate` as recovered data: the inventory does not read measurement values or attempt repair.

Run `python benchmarks/run_gwosc_recovery.py` for the separate controlled recovery trial on the authentic 16 kHz file. It changes one verified pointer in a copy, confirms 57 native-reader failures or wrong chunks, and compares all 128 recovered chunks against original float64 bits at the same coordinates. It checks bounded scalar attributes that are safe to copy, reports omitted attributes, and verifies unchanged source hashes. The output omits the original `/meta` and `/quality` datasets; it is a recovery artifact rather than a replacement research file.

Run `python benchmarks/run_damage_catalog.py` for controlled trials on both GWOSC layouts and safe-refusal checks on the two other scientific layouts. The authentic 4 kHz file has an intact direct chunk index; it yields 64 exact chunks in the baseline trial and 63 exact chunks plus one explicitly failed payload in a corruption trial. A missing direct payload pointer is refused because there is no justified chunk location to reconstruct.

All bundled originals are intact. None is evidence that an actual researcher's damaged file has been restored. A scored recovery experiment needs a disposable damaged copy, the pristine original as evaluator-only truth, a verified native-reader failure, an independent coordinate-by-coordinate comparison, and a report of unsupported or ambiguous regions. Keep those trials and their results separate from this format-coverage survey. Authentic intact data plus artificial index damage tests a specific failure mode against real bytes; natural corruptions and real users are additional evidence that must be evaluated separately.

An unfamiliar HDF5 structure cannot safely be converted into an existing chunk-index type by changing its shape. A parser must understand the original layout, filter pipeline, datatype, and surviving ownership evidence before exporting justified measurements. Unsupported cases should remain explicit until their individual handling is implemented and verified.
