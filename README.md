# H5Reclaim

H5Reclaim is an experimental project to recover correctly placed scientific data from a supported subset of damaged HDF5 chunk indexes. Its intended outputs are a separate readable file and an explicit account of recovered and uncertain regions. The input must remain unchanged.

**Current state:** M0 investigation only. This repository creates and checks a healthy fixture. It does not inspect damaged files, repair them, or offer a recovery command.

## Reproduce the healthy fixture

With Python 3.10 or newer:

```sh
python -m venv .venv
. .venv/bin/activate
python -m pip install -e .
python tools/make_healthy_fixture.py --output fixtures/healthy.h5
python -m unittest discover -s tests -v
```

On Windows PowerShell, activate with `.venv\Scripts\Activate.ps1`. The generator creates a new file and refuses to overwrite an existing one. It checks every value after reopening the file and prints the file hash and library versions. See `docs/status.md` for the exact environment and results already observed, and `docs/project-brief.md` for the technical scope.

The generator requests a dataset `/measurements` with rank two, type little-endian `uint32`, fixed dimensions divisible by its chunk dimensions, no filters, and a value at `(row, column)` equal to `row * width + column`. This simple pattern is fixture truth, not an input to the future recovery engine. Keep fixture creation and evaluation separate from any recovery process.

## Research boundary

Our first candidate damage case is a broken child link in a verified version-1 raw-data B-tree, with useful surviving anchors. The chosen HDF5 layout, tree depth, and failure behavior must be observed before any damage is introduced. The inspection procedure is in `docs/structure-plan.md`. We have not established an advantage over existing recovery software.
