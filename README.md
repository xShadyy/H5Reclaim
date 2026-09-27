<p align="center">
  <img src="assets/h5reclaim-banner.png" alt="H5Reclaim: evidence-based recovery for scientific HDF5 data" width="760">
</p>

<p align="center">
  <a href="pyproject.toml"><img alt="Python 3.10+" src="https://img.shields.io/badge/Python-3.10%2B-3776AB?style=flat-square&amp;logo=python&amp;logoColor=white"></a>
  <a href="LICENSE"><img alt="License: Apache 2.0" src="https://img.shields.io/badge/License-Apache%202.0-228F91?style=flat-square"></a>
  <a href="docs/status.md"><img alt="Status: experimental" src="https://img.shields.io/badge/Status-Experimental-496477?style=flat-square"></a>
</p>

H5Reclaim is an experimental, open-source project exploring recovery of scientific data when damage to HDF5 indexing structures makes surviving data difficult to reach. It aims to reconstruct only what the available evidence supports and to make the limits of each recovery attempt clear.

## Approach

- **Preserve the source.** Work from the damaged input and write recovered data to a separate file.
- **Keep the evidence visible.** Connect exported data to the structural evidence used to place it.
- **Represent uncertainty.** Make unresolved or unavailable regions explicit so they are not mistaken for measurements.

## Project status

The repository contains an experimental Python command-line tool, controlled fixtures, tests, and a small corpus of attributed scientific HDF5 files. Supported cases depend on the file's actual structure. H5Reclaim is not a general repair utility, and a readable output alone does not prove the historical integrity of its values.

For current support boundaries and verified results, see the [project status](docs/status.md) and [damage taxonomy](docs/damage-taxonomy.md). The [technical brief](docs/project-brief.md) explains the longer-term goal and design principles. No representative field success percentage has been established.

## Get started

Python 3.10 or newer is required. From the repository root, install the project in your environment and see the current command-line help:

```sh
python -m pip install -e .
h5reclaim --help
```

The [usage guide](docs/usage.md) covers current commands, supported structures, output interpretation, and safety limits. The [file guide](docs/file-guide.md) maps the source tree. The [corpus notes](corpus/README.md) and [benchmark guide](benchmarks/README.md) explain the controlled evaluations.

To inspect a damaged acquisition, choose the dataset, a new output path, and a separate evidence report:

```sh
python -m h5reclaim diagnose damaged.h5 --dataset /measurements
python -m h5reclaim rescue damaged.h5 --dataset /measurements --output derived.h5 --report evidence.json
```

The report distinguishes currently readable values from equality to an independently retained prior capture. `--strict-history` requires a pinned baseline, capsule, replica, or parity route.

For an intact future acquisition, `python -m h5reclaim protect healthy.h5 --dataset /measurements --output protection.zip` creates a prospective bundle. Store the printed manifest digest independently and run `verify-protection` and `drill-protection` while the source is intact. The [incident intake protocol](benchmarks/INCIDENT_INTAKE.md) separates recovery of consented naturally damaged files from later independent scoring. No representative field success rate is established.

## License

H5Reclaim is available under the [Apache License 2.0](LICENSE).

<sub>A project by Tymoteusz Netter.</sub>
