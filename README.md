<p align="center">
  <img src="assets/h5reclaim-banner.png" alt="H5Reclaim: recovery for scientific HDF5 data" width="760">
</p>

<p align="center">
  <a href="pyproject.toml"><img alt="Python 3.10+" src="https://img.shields.io/badge/Python-3.10%2B-3776AB?style=flat-square&amp;logo=python&amp;logoColor=white"></a>
  <a href="LICENSE"><img alt="License: Apache 2.0" src="https://img.shields.io/badge/License-Apache%202.0-228F91?style=flat-square"></a>
  <a href="pyproject.toml"><img alt="Version: 0.14.0" src="https://img.shields.io/badge/Version-0.14.0-496477?style=flat-square"></a>
</p>

H5Reclaim recovers data from HDF5 files into a new file with a JSON evidence report. It discovers datasets automatically, selects a recovery route for each, and keeps exporting other datasets when one cannot be recovered.

## Install and run

Use Python 3.10 or newer from the extracted repository root:

```sh
python -m pip install -e .
python -m h5reclaim rescue damaged.h5 --output rescued.h5 --report evidence.json
```

This processes the whole file. It restores available groups, large and null attributes, named datatypes and their shared identities, hard-link aliases, local soft links, object and region references, dimension labels, and dimension scales. Application headers are retained, and source-owned names are preserved when recovery metadata needs a different location. A failed chunk stays unknown while other readable chunks are retained. The report records every exported dataset, failure, and omitted piece of context.

Select a particular dataset when needed:

```sh
python -m h5reclaim diagnose damaged.h5 --dataset /experiment/readings
python -m h5reclaim rescue damaged.h5 --dataset /experiment/readings --output selected.h5 --report selected-evidence.json
```

The input stays unchanged. Choose new output and report paths.

## Recovery capabilities

- Older chunk B-trees and modern single-chunk, implicit, fixed-array, extensible-array, and version-2 B-tree indexes.
- Automatic routing for physical tail truncation, checked interrupted-write flags, structural recovery, native-readable values, and streaming exports.
- Numeric arrays, compound records with variable strings or references, enums, bitfields, array records, fixed strings, opaque records, variable strings, ragged numeric arrays, empty datasets, and null dataspaces through their applicable routes.
- Multidimensional fixed-record streaming through rank 32, including file numeric widths without a NumPy representation, with sparse allocation maps and configurable resource budgets.
- DEFLATE, LZF, shuffle, Fletcher32 and installed packaged Zstd, Blosc, Blosc2, Bitshuffle, LZ4, BZip2, ZFP, SZ, SZ3, SPERR, HTJ2K and FCI codecs. Pipelines that can change values are materialized without a second lossy encoding.
- Surviving legacy and modern dataset-header discovery when group links or the root are damaged, automatic uniquely checksum-justified modern root-pointer correction, checked hints and object-address exports.
- External raw storage, large and growing virtual datasets, nested dependency graphs and external group trees, using pinned companion files. Family and Split exports cover whole files and selected datasets.
- Automatic companion-file manifests from a supplied directory, one shared source image, batched heap reads, and verified restart checkpoints for completed datasets, chunks and contiguous blocks.
- Prior baselines, replicas, capsules, and parity for checking or reconstructing data using evidence retained before damage.

Read the validity map named in the report before using exported values. Unresolved positions can display a fill value. Current readable values and equality to a prior capture are reported separately; a newly readable output does not establish its pre-damage values.

Enable the additional codecs and retain recovery progress:

```sh
python -m pip install -e ".[filters]"
python -m h5reclaim rescue damaged.h5 --resume-dir recovery-progress --output rescued.h5 --report evidence.json
```

Rerun with the same input, options and progress directory to reuse completed datasets and native selections. Choose new final output and report paths for each run. `python -m h5reclaim discover damaged.h5 --json` lists checked surviving dataset headers when original names cannot be read.

For a file with companion files in one directory:

```sh
python -m h5reclaim rescue container.h5 --related-dir companion-files --output rescued.h5 --report evidence.json
```

MATLAB 7.3, netCDF4 and NWB examples are checked with independent Python writers and readers, on intact files and controlled payload damage. Install `.[applications]` to run those evaluations.

## Guides and verification

The [usage guide](docs/usage.md) covers commands and output interpretation. The [file guide](docs/file-guide.md) maps the implementation. See [related files](docs/dependency-bundles.md) for manifests and the [evidence model](docs/evidence-model.md) for report semantics.

```sh
python -m unittest discover -s tests -q
python -m unittest benchmarks.test_authentic_v09_routes benchmarks.test_heldout_trials benchmarks.test_incident_intake -q
python benchmarks/run_real_corpus.py
python benchmarks/run_gwosc_recovery.py
python benchmarks/run_whole_file_corpus.py
python benchmarks/run_application_corpus.py
```

The [benchmark guide](benchmarks/README.md) describes independent evaluation, and the [corpus notes](corpus/README.md) record the bundled scientific files and their attribution.

## License

H5Reclaim is available under the [Apache License 2.0](LICENSE).

<sub>A project by Tymoteusz Netter.</sub>
