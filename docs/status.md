# Current status

Updated: 2026-09-27

## What exists

- A minimal Python project and public technical brief. The private project handoff is outside this repository.
- `tools/make_healthy_fixture.py`, which is designed to create one fixed-size `(512,512)` dataset with `(16,16)` chunks, little-endian `uint32`, 1024 allocated chunks, and no filters. It reopens the file and checks all 262144 values exactly. These properties are intended and **have not been executed in this environment**.
- `tests/test_fixture.py`, a separate small-file round-trip and no-overwrite test. It has not run successfully here.
- `docs/structure-plan.md`, an inspection procedure and a conservative plan for one damaged-copy mutation. There is no damage tool or recovery engine.

## Environment and checks actually run

| Check | Result |
| --- | --- |
| `python3 --version` | Python 3.12.14 |
| `python3 -c 'import numpy; print(numpy.__version__)'` | NumPy 2.3.5 |
| h5py import | Failed: `ModuleNotFoundError: No module named 'h5py'` |
| HDF5 library and `h5ls`/`h5debug`/`h5dump` tools | Not found in this environment |
| `python3 -m compileall -q src tools tests` | Passed syntax compilation after the final compatibility-bound edit |
| `python3 -m unittest discover -s tests -v` | Failed at import because h5py is unavailable; no test assertions ran |
| `python3 tools/make_healthy_fixture.py --output fixtures/healthy.h5` | Failed at import because h5py is unavailable; no fixture was created |
| `python3 -m pip install --target /tmp/h5reclaim_install_probe --no-deps --timeout 5 --retries 0 h5py` | Failed while fetching wheel metadata from `files.pythonhosted.org` due to a connection timeout |

There is no known h5py version, HDF5 library version, generated file hash, verified index family, or measured tree depth yet. The library constraints in `pyproject.toml` are proposed minimums and have not been validated in this environment.

## Next task

In a Python environment that can install packages, run the README commands. Record the actual Python, h5py, NumPy, and HDF5 versions and the healthy file's hash. Then install HDF5 inspection tools and follow `docs/structure-plan.md` from the selected dataset object header to the B-tree root and leaves. Save the inspection output. Confirm that the root has an internal level and multiple leaves before implementing a damaged-copy mutation. If the generated layout differs, adjust the fixture and record why.

The smallest subsequent implementation is a controlled mutation utility that accepts verified addresses, checks the original pointer bytes, writes only to a copy, and logs the exact byte diff. It must also demonstrate that a standard read of the intended region fails. Do not start the recovery engine before this evidence exists.
