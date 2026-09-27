# Independent broken-link benchmark

## Authentic GWOSC file, controlled damage

The repository includes the untouched 16 kHz Hanford strain file from GWOSC.
From the repository root, with Python, h5py, and NumPy available, run:

```sh
python benchmarks/run_gwosc_recovery.py
```

This one command verifies the original's SHA-256, checks all 128 raw B-tree
chunk records against h5py, creates a separate damaged copy, and changes one
verified interior root child pointer in that copy. It checks that ordinary
HDF5 reads fail or differ for the affected chunks and that unaffected chunks
remain exact. It invokes recovery in a subprocess with the damaged input,
dataset path, and new output paths. The subprocess receives neither the
pristine reference nor the mutation manifest. The evaluator then compares
every output `float64` bit pattern at the corresponding sample
coordinate against the untouched original. It checks output status, evidence
mappings, Fletcher32 verification labels, selected scalar attributes, embedded
and external reports, and both file hashes. The default console output is a
readable pass summary with native-reader failures, recovered and reconstructed
chunks, exact float64 bit matches, and the trial file paths. Add `--json` for
the complete evaluation summary on standard output. Either way, trial files
and JSON evidence are left in the displayed work directory. Use `--work-dir
path/to/new-dir` to choose a new or empty location; the default temporary
directory is retained.

The retained directory contains `inputs/damaged.hdf5`,
`results/recovered.hdf5`, `results/recovery.json`,
`truth/mutation.json`, and `truth/evaluation.json`. The recovered HDF5 file
includes `/_h5reclaim/chunk_status` and an embedded copy of the recovery
report. The independent evaluator's `truth/evaluation.json` records the
scored counts and source hash checks. The `results/recovery.json` file records
the recovery tool's per-chunk provenance. The original research file remains
in `corpus/files/`; the evaluator reads it only as the reference.

The recorded trial had 57 native-unavailable or incorrect chunks. H5Reclaim
reconstructed those 57 and exported all 128 chunks with zero wrong bits. The
scientific file's bytes are authentic; the pointer damage is controlled for
evaluation. This says nothing about unrelated layouts, organically damaged
files, or repairs to the original file. See [corpus sources](../corpus/README.md).

For a quick metadata coverage check across all four original files, run
`python benchmarks/run_real_corpus.py`. Its readable output summarizes the
verified originals and support classifications; `--json` returns every
machine-readable result. This corpus survey does not damage a copy, read
measurements, or attempt recovery. `python -m unittest discover -s tests -q`
runs Python's discovered tests in quiet mode; success does not independently
score a naturally damaged user file.

The GWOSC file has one interior leaf position that can be detached while
leaving two adjacent anchors. `tests/test_mutation_positions.py` separately
damages all four eligible positions in another synthetic layout with random
measurement values, passing only each damaged copy to a public recovery
subprocess. The fixture tool's optional `--child-index N` selects a verified
position; its default remains the first eligible position. This expands
position coverage for one break type, not arbitrary corruption coverage.

## Synthetic random-value trial

Run from the repository root in an environment with h5py and NumPy:

```sh
python benchmarks/run_recovery.py --work-dir work/trial
```

The work directory must be new or empty. `truth/pristine.h5` contains the
reference, `truth/challenge.json` records a random value seed, and
`truth/mutation.json` records the controlled change. `inputs/damaged.h5` is
the only source passed to the separate recovery process. Outputs are under
`results/`, and `truth/evaluation.json` holds the evaluation. Use `--seed N`
to reproduce the challenge values. The fixture creator first validates a
healthy round trip; the benchmark then replaces payloads with challenge values
known only to evaluation and checks the round trip again.

The recovery subprocess is not passed the pristine path, mutation manifest,
challenge seed, or expected chunk offsets. The `truth/`, `inputs/`, and
`results/` directories nevertheless share a parent and are accessible to a
program running with the same filesystem permissions. This is separation of
program inputs and responsibilities, not an adversarial filesystem isolation
boundary. The current recovery code does not read the truth directory.

The evaluator compares each chunk marked `recovered` with the pristine values
at the **same coordinate**. A mismatch that uniquely matches a different
reference coordinate counts as wrong placement; other mismatches count as
incorrect values. It counts chunks marked with an uncertainty status separately.
It also measures which chunks a normal
h5py read of the damaged source fails to return correctly and how many of
those H5Reclaim recovers. SHA-256 checks establish that both source and
reference are unchanged after recovery. The report's `complete` field means
all chunks have recovered status; `partial` is a valid completed attempt with
unresolved regions. The process exits nonzero if placement or values are wrong, no native
read was impaired, or the recovery has no demonstrated gain. The end-to-end
test additionally requires full exact recovery for this narrow fixture.

The evaluator never imports the recovery package. It invokes its public CLI
with a damaged path and dataset path. For evaluation against a different
implementation that might inspect neighboring files, give that process access
only to a separate copy of the damaged input, then score its results from the
evaluator's private reference.
