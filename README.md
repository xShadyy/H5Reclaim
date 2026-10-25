<p align="center">
  <img src="assets/h5reclaim-banner.png" alt="H5Reclaim: recovery for scientific HDF5 data" width="760">
</p>

<p align="center">
  <a href="pyproject.toml"><img alt="Python 3.10+" src="https://img.shields.io/badge/Python-3.10%2B-3559F0?style=flat-square&amp;logo=python&amp;logoColor=white"></a>
  <a href="LICENSE"><img alt="License: Apache 2.0" src="https://img.shields.io/badge/License-Apache%202.0-263557?style=flat-square"></a>
  <a href="https://github.com/xShadyy/H5Reclaim/blob/main/benchmarks/results/v130-release-coverage.json"><img alt="Controlled benchmark: 95.4% useful recovery" src="https://img.shields.io/badge/Controlled%20benchmark-95.4%25-3559F0?style=flat-square"></a>
</p>

<p align="center">
  <a href="README.md" lang="en">English</a> · <a href="translations/README.pl.md" lang="pl">Polski</a> · <a href="translations/README.de.md" lang="de">Deutsch</a> · <a href="translations/README.fr.md" lang="fr">Français</a> · <a href="translations/README.es.md" lang="es">Español</a> · <a href="translations/README.pt-BR.md" lang="pt-BR">Português (Brasil)</a><br>
  <a href="translations/README.zh-CN.md" lang="zh-CN">简体中文</a> · <a href="translations/README.ja.md" lang="ja">日本語</a> · <a href="translations/README.ko.md" lang="ko">한국어</a> · <a href="translations/README.ru.md" lang="ru">Русский</a> · <a href="translations/README.ar.md" lang="ar">العربية</a>
</p>

H5Reclaim recovers scientific data from damaged HDF5 files. It brings surviving measurements into a usable new file after interrupted writes, broken indexes, metadata damage, and truncation. HDF5 is the format used by many scientific instruments and applications to store arrays, measurements, and their metadata.

**One command discovers your datasets, selects recovery methods, and creates a new HDF5 file with a clear JSON report.** Your original stays untouched. H5Reclaim keeps readable measurements from partially damaged datasets and continues recovering the rest of the file.

Across **109 controlled damaged-file trials**, the 1.3.0 build produced 104 useful outputs: 77 fully exact and 27 partial recoveries, with five refusals and zero wrong accepted values. This is the same result as the earlier 1.0.0 panel. [Explore the 1.3.0 results](benchmarks/results/v130-release-coverage.json).

Version 1.3.0 adds checked repairs for two broken links in one index parent, native relocation of selected heap-backed values and references, broader file-space repair, and checked detached group aliases. Its [separate ten-case compressed streaming panel](benchmarks/results/v130-streaming-recovery.json) includes same-parent faults and found zero wrong accepted values.

See the [usage guide](USAGE.md) for the recovery workbench, supported formats, verification, and batch workflows.

## Quick start

You need Python 3.10 or newer. Clone this repository, or download and extract its ZIP from GitHub:

```sh
git clone https://github.com/xShadyy/H5Reclaim.git
cd H5Reclaim
```

Install from the repository root and recover a file:

```sh
python -m pip install .
python -m h5reclaim rescue "damaged.h5"
```

The filename above is an example. Replace it with the path to an existing HDF5
file, quoting paths that contain spaces.

## Understand the result

The terminal summary shows complete or partial recovery and lists your output locations. The JSON report identifies recovered datasets, recovery methods, unresolved positions, and restored metadata.

When a route publishes a **status map**, it shows which positions contain accepted measurements. Use the map named in the report to select values for analysis; unknown positions can display a fill value such as zero. Some intact contiguous and null datasets have no checkable map, so use their route-specific report before analysis. Prior baselines and protection bundles add comparison with an earlier capture.

## License

Source code is available under the [Apache License 2.0](LICENSE). Bundled scientific files have [separate attribution and licenses](https://github.com/xShadyy/H5Reclaim/blob/main/corpus/README.md).
