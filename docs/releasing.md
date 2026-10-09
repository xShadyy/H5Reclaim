# Release candidate runbook

The package is currently `1.0.0rc1`. A candidate tag triggers the complete
CI matrix, then builds and checks a source archive and wheel. The passing
archives and `SHA256SUMS` are retained as a workflow artifact. The workflow
does not create a GitHub Release or publish to PyPI.

## Before tagging

Work from a clean `main` checkout. Review [the changelog](../CHANGELOG.md),
[the report contract](report-schema.md), and [the measured coverage](coverage.md).
The coverage panel uses generated files and declared faults. For a stable 1.0
claim, evaluate independent held-out files and record their refusals, unknown
coordinates, and wrong accepted values as well.

```sh
python -m unittest discover -s tests -q
python -m unittest benchmarks.test_release_coverage -q
python benchmarks/run_release_coverage.py --seed 20261005 --work-dir release-panel
python -m pip install build twine
python -m build
python tools/check_release_artifacts.py dist --tag v1.0.0rc1
python -m twine check --strict dist/*.tar.gz dist/*.whl
```

Use a new empty panel directory. Confirm the generated `coverage.json` has
`passed: true`, `tested_source_unchanged: true`, and a `tested_source` fingerprint
matching the final source. The committed candidate result is
[`v100rc1-release-coverage.json`](../benchmarks/results/v100rc1-release-coverage.json).
The archive checker verifies exact version/tag alignment, package code parity,
required metadata, and that development corpus files are absent. It emits
`dist/SHA256SUMS` after checking the archives.

## Tag and review

After merging a tested candidate into `main`, create and push its annotated
tag from that commit:

```sh
git tag -a v1.0.0rc1 -m "H5Reclaim 1.0.0rc1"
git push origin main
git push origin v1.0.0rc1
```

Wait for the tag-triggered `H5Reclaim checks` workflow to pass. Download its
`h5reclaim-release-candidate` artifact, verify both files against
`SHA256SUMS`, and install the wheel in a clean environment for a final CLI
smoke test. Inspect the candidate's refusals and reader compatibility before
deciding whether to publish it. Publication is a separate maintainer action.

For stable `1.0.0`, change both `pyproject.toml` and
`src/h5reclaim/recovery.py`, rerun the panel and tests against those exact
bytes, update the coverage report and changelog, and repeat the archive and
tag checks with `v1.0.0`. Do not relabel the candidate archives as stable.
