<p align="center">
  <img src="assets/h5reclaim-banner.png" alt="H5Reclaim: recovery for scientific HDF5 data" width="760">
</p>

<p align="center">
  <a href="pyproject.toml"><img alt="Python 3.10+" src="https://img.shields.io/badge/Python-3.10%2B-3559F0?style=flat-square&amp;logo=python&amp;logoColor=white"></a>
  <a href="LICENSE"><img alt="License: Apache 2.0" src="https://img.shields.io/badge/License-Apache%202.0-263557?style=flat-square"></a>
  <a href="https://github.com/xShadyy/H5Reclaim/blob/main/benchmarks/results/v100rc1-release-coverage.json"><img alt="Controlled benchmark: 95.4% useful recovery" src="https://img.shields.io/badge/Controlled%20benchmark-95.4%25-3559F0?style=flat-square"></a>
</p>

<p align="center">
  <a href="README.md" lang="en">English</a> · <a href="translations/README.pl.md" lang="pl">Polski</a> · <a href="translations/README.de.md" lang="de">Deutsch</a> · <a href="translations/README.fr.md" lang="fr">Français</a> · <a href="translations/README.es.md" lang="es">Español</a> · <a href="translations/README.pt-BR.md" lang="pt-BR">Português (Brasil)</a><br>
  <a href="translations/README.zh-CN.md" lang="zh-CN">简体中文</a> · <a href="translations/README.ja.md" lang="ja">日本語</a> · <a href="translations/README.ko.md" lang="ko">한국어</a> · <a href="translations/README.ru.md" lang="ru">Русский</a> · <a href="translations/README.ar.md" lang="ar">العربية</a>
</p>

H5Reclaim recovers scientific data from damaged HDF5 files. It brings surviving measurements into a usable new file after interrupted writes, broken indexes, metadata damage, and truncation. HDF5 is the format used by many scientific instruments and applications to store arrays, measurements, and their metadata.

**One command discovers your datasets, selects recovery methods, and creates a new HDF5 file with a clear JSON report.** Your original stays untouched. H5Reclaim keeps readable measurements from partially damaged datasets and continues recovering the rest of the file.

The 1.0.0 release achieved **95.4% useful recovery across 109 controlled damaged-file trials**: 77 fully exact recoveries and 27 partial recoveries, with zero wrong accepted values in this panel. [Explore the results](https://github.com/xShadyy/H5Reclaim/blob/main/benchmarks/results/v100rc1-release-coverage.json).

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

## Understand the result

The terminal summary shows complete or partial recovery and lists your output locations. The JSON report identifies recovered datasets, recovery methods, unresolved positions, and restored metadata.

When a route publishes a **status map**, it shows which positions contain accepted measurements. Use the map named in the report to select values for analysis; unknown positions can display a fill value such as zero. Some intact contiguous and null datasets have no checkable map, so use their route-specific report before analysis. Prior baselines and protection bundles add comparison with an earlier capture.

## License

Source code is available under the [Apache License 2.0](LICENSE). Bundled scientific files have [separate attribution and licenses](https://github.com/xShadyy/H5Reclaim/blob/main/corpus/README.md).
