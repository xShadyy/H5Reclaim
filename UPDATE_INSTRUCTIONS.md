# Updating H5Reclaim to 1.1.0

This upgrade is supplied as a complete project ZIP with incremental Git
history. It has not been pushed to GitHub. The upgrade commits use:

```text
Tymoteusz Netter <tymoteusz.netter@gmail.com>
```

The starting point is upstream main at
`e09a8e48cbe074493d40f07adac8ceb9711bee5a`. The upgrade branch is
`feat/recovery-workbench`. A Git bundle in the ZIP preserves its history so
you can import the changes into your existing clone without replacing `.git`.

## Contents of the ZIP

| Item | Purpose |
| --- | --- |
| `H5Reclaim/` | Complete source checkout, tests, documentation and Git history |
| `H5Reclaim-update.bundle` | Portable history for importing into an existing clone |
| `packages/` | Validated 1.1.0 wheel, source distribution and SHA256SUMS |
| `UPDATE_INSTRUCTIONS.md` | These instructions |
| `CHANGE_SUMMARY.md` | Features, validation results, limits and next priorities |

## Recommended: import into your existing clone

Extract the ZIP first. Replace the bundle path below with its absolute path.
Run the commands from your existing H5Reclaim clone. Check `git status` and
commit or stash your own outstanding work before switching branches.

```sh
git status
git config user.name "Tymoteusz Netter"
git config user.email "tymoteusz.netter@gmail.com"
git fetch origin
git fetch "/absolute/path/to/H5Reclaim-update.bundle" refs/heads/feat/recovery-workbench:refs/heads/h5reclaim-v1.1.0
git switch h5reclaim-v1.1.0
git merge origin/main
```

If main has changed, review any merge changes and resolve conflicts before
continuing. Run the checks below, then publish the upgrade branch:

```sh
git push -u origin h5reclaim-v1.1.0
```

Open a pull request from `h5reclaim-v1.1.0` to `main` at
<https://github.com/xShadyy/H5Reclaim>, review the diff and wait for CI. Merge
after those checks pass. No force push or replacement of the repository's
history is needed.

If you prefer updating main directly, after validating the upgrade branch:

```sh
git switch main
git pull --ff-only origin main
git merge --ff-only h5reclaim-v1.1.0
git push origin main
```

If either fast-forward command refuses, use the pull request workflow to
review the divergent changes. Do not force push.

## Alternative: use the included complete project

The `H5Reclaim/` directory already contains the upgrade branch and its history.
Do not copy its `.git` directory over an existing clone. Open a terminal in
that directory and run:

```sh
git status
git remote -v
git fetch origin
git merge origin/main
```

The remote is configured as `https://github.com/xShadyy/H5Reclaim.git`. Use
your usual GitHub authentication, run the checks below, then:

```sh
git push -u origin feat/recovery-workbench
```

Open a pull request from that branch to main.

## Install and check the upgrade

Use Python 3.10 or newer. In the project directory:

```sh
python -m venv .venv
```

Activate it with `source .venv/bin/activate` on Linux/macOS or
`.venv\Scripts\Activate.ps1` in Windows PowerShell, then:

```sh
python -m pip install --upgrade pip
python -m pip install -e ".[filters,readers,applications]"
python -m h5reclaim doctor
python -m h5reclaim capabilities
python -m unittest discover -s tests -q
python benchmarks/run_streaming_recovery.py
```

The optional dependencies can require compatible wheels or build tools for
your platform. For the core package alone, use `python -m pip install -e .`.
Install `h5copy` and `h5repack` separately if you want the official-tool
commands. On Debian/Ubuntu these are in the `hdf5-tools` package. Tests for
unavailable optional dependencies are skipped.

To install the included wheel instead of a source checkout, from the
extracted ZIP's top directory:

```sh
python -m pip install packages/h5reclaim-1.1.0-py3-none-any.whl
```

pip still needs the declared Python dependencies. `packages/SHA256SUMS`
contains the wheel and source archive hashes. These packages contain the
same Python implementation as the checkout.

Try a recovery using new output paths:

```sh
python -m h5reclaim rescue damaged.h5 --dataset /experiment/measurements --output recovered.h5 --report recovery.json
python -m h5reclaim report recovery.json --html recovery.html
python -m h5reclaim verify-result recovered.h5 recovery.json --values
```

See `H5Reclaim/USAGE.md` for repair plans, budgets, batches, integrity checks,
alternative-reader exports, official copy/repack integration and the Python
API. Verify scientific meaning and use the recovery map when consuming
partial data.

## Optional GitHub release after merging

After the pull request is merged, CI passes and you are ready to prepare
the release:

```sh
git switch main
git pull --ff-only origin main
git tag -a v1.1.0 -m "H5Reclaim 1.1.0"
git push origin v1.1.0
```

The updated workflow validates and attaches the wheel, source distribution
and checksums to a GitHub release. If there is no release yet, it creates a
draft for review. Publishing that draft is a separate GitHub action. This
workflow does not upload to PyPI.

## Further priorities

1. Recover multiple independently justified broken index links per dataset,
   with explicit ambiguity refusal and adversarial ownership tests.
2. Extend severe metadata recovery to more legacy structures, detached
   objects and free-space damage. Keep unassigned bytes separate from
   accepted measurements.
3. Profile dense 100 GB and larger datasets, reduce physical scan costs and
   expand index and quota coverage. The current sparse 4 GiB test is not a
   dense throughput benchmark.
4. Add deeper NWB, netCDF and NeXus consistency checks after HDF5 recovery,
   plus more platform and optional-codec interoperability tests.

No collection or publication of actual recovery incidents was added.
