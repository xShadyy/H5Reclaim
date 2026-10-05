<p align="center">
  <img src="assets/h5reclaim-banner.svg" alt="H5Reclaim: recover surviving scientific HDF5 data" width="840">
</p>

<p align="center">
  <a href="pyproject.toml"><img alt="Python 3.10+" src="https://img.shields.io/badge/Python-3.10%2B-3559F0?style=flat-square&amp;logo=python&amp;logoColor=white"></a>
  <a href="LICENSE"><img alt="License: Apache 2.0" src="https://img.shields.io/badge/License-Apache%202.0-263557?style=flat-square"></a>
  <a href="docs/coverage.md"><img alt="Controlled benchmark: 80.7% useful recovery" src="https://img.shields.io/badge/Controlled%20benchmark-80.7%25-3559F0?style=flat-square"></a>
</p>

H5Reclaim recovers scientific data from damaged HDF5 files. It brings surviving measurements into a usable new file after interrupted writes, broken indexes, metadata damage, and truncation. HDF5 is the format used by many scientific instruments and applications to store arrays, measurements, and their metadata.

**One command discovers your datasets, selects recovery methods, and creates a new HDF5 file with a clear JSON report.** Your original stays untouched. H5Reclaim keeps readable measurements from partially damaged datasets and continues recovering the rest of the file.

The 0.15.0 controlled benchmark achieved **80.7% useful recovery across 109 damaged-file trials**: 63 fully exact recoveries and 25 partial recoveries, with zero wrong accepted values. [Explore the results](docs/coverage.md).

[Quick start](#quick-start) · [Usage guide](docs/usage.md) · [Recovery coverage](docs/coverage.md) · [Report semantics](docs/evidence-model.md)

## Quick start

You need Python 3.10 or newer. Clone this repository, or download and extract its ZIP from GitHub:

```sh
git clone https://github.com/xShadyy/H5Reclaim.git
cd H5Reclaim
python -m venv .venv
```

Activate the environment:

| System | Command |
| --- | --- |
| Windows PowerShell | `.venv\Scripts\Activate.ps1` |
| Windows Command Prompt | `.venv\Scripts\activate.bat` |
| macOS / Linux | `. .venv/bin/activate` |

Install from the repository root and recover a file:

```sh
python -m pip install .
python -m h5reclaim rescue "damaged.h5"
```

This creates `damaged.recovered.h5` and `damaged.recovered.report.json` beside the source. Existing destinations are never overwritten. Use explicit paths to choose a different location or repeat a run:

```sh
python -m h5reclaim rescue "damaged.h5" --output "rescued.h5" --report "evidence.json"
```

The installed `h5reclaim` command also works. Run `python -m h5reclaim --help` to see commands, or `python -m h5reclaim rescue --help` for recovery options.

## Understand the result

The terminal summary shows complete or partial recovery and lists your output locations. The JSON report identifies recovered datasets, recovery methods, unresolved positions, and restored metadata.

Each dataset has a **status map** showing which positions contain recovered measurements. Use the map named in the report to select accepted values for analysis; unknown positions can display a fill value such as zero. Prior baselines and protection bundles add comparison with an earlier capture.

## Common workflows

| Goal | Command |
| --- | --- |
| Inspect a file before recovery | `python -m h5reclaim diagnose damaged.h5` |
| Recover one dataset | `python -m h5reclaim rescue damaged.h5 --dataset /experiment/readings` |
| Summarize an existing result | `python -m h5reclaim report damaged.recovered.report.json` |
| Resume a longer recovery | `python -m h5reclaim rescue damaged.h5 --resume-dir recovery-progress` |
| Supply companion files | `python -m h5reclaim rescue container.h5 --related-dir companion-files` |
| Find datasets after their names are lost | `python -m h5reclaim discover damaged.h5 --json` |

Some files need additional compression codecs. Install `python -m pip install ".[filters]"` from the repository root, then retry. The [usage guide](docs/usage.md) covers larger files, resource budgets, dependent files, resumable runs, and prior protection bundles.

## Recovery coverage

H5Reclaim reads each file's own dataset descriptions, so the same workflow works across experiments, dataset names, and array shapes.

| Area | Implemented coverage |
| --- | --- |
| Storage | Compact, contiguous, and chunked datasets; legacy and modern chunk indexes |
| Values | Numeric arrays, compound records, fixed and variable strings, ragged arrays, enums, references, empty and null datasets |
| Damage | Broken index-link recovery, checked metadata and modern signature repairs, interrupted-write flags, surviving dataset headers, unreadable chunks, and physical tail truncation |
| Compression | DEFLATE, LZF, shuffle, Fletcher32, and supported optional packaged codecs |
| File structure | Available groups, attributes, links, named datatypes, references, dimension scales, and application headers |
| Dependencies | External storage, virtual datasets, external links, and Family/Split files with explicitly supplied companion files or manifests |

For acquisitions protected before damage, retained replicas and parity can also reconstruct missing data. Companion-file discovery, resumable recovery, and streaming budgets support larger scientific workflows.

The **0.15.0 automatic-rescue benchmark** covers 23 generated data and layout families. Its **109 controlled damaged-file trials** produced 63 fully exact recoveries, 25 partial recoveries, and 21 refusals: **88 useful outputs (80.7%)**. It recovered 69.0% of the original elements at their exact coordinates, with zero wrong accepted values and zero changed sources. [View the complete case report](benchmarks/results/v015-release-coverage.json) or the [coverage breakdown](docs/coverage.md).

Recorded v0.14.0 evaluations include:

| Evaluation | Recorded result |
| --- | --- |
| [Four intact scientific files](benchmarks/results/v014-scientific-whole.json) | 251 datasets and 253 attributes compared exactly with retained originals |
| [Controlled broken GWOSC index link](benchmarks/results/v014-gwosc-controlled.json) | 128/128 chunks recovered at their exact coordinates; zero wrong chunks |
| [MATLAB 7.3, netCDF4, and NWB](benchmarks/results/v014-application-readers.json) | Independent readers opened six intact/controlled-damage outputs; zero wrong accepted elements, with damaged regions left unknown |

The [benchmark guide](benchmarks/README.md) explains how to reproduce evaluations. The [corpus notes](corpus/README.md) provide scientific-file attribution.

## Contributing and reporting problems

Open an [issue](https://github.com/xShadyy/H5Reclaim/issues) with the command, tool and Python versions, observed error, and expected result. A small reproducible file and its report help distinguish unsupported storage from a recovery bug.

For development, install `python -m pip install -e ".[filters]"` and run:

```sh
python -m unittest discover -s tests -q
```

Tests check recovery correctness, source preservation, and unresolved-data handling. Benchmarks score accepted values against separate originals. The [repository guide](docs/file-guide.md) maps the implementation and explains why those files are present.

## License

Source code is available under the [Apache License 2.0](LICENSE). Bundled scientific files have [separate attribution and licenses](corpus/README.md).

<sub>Created by Tymoteusz Netter.</sub>
