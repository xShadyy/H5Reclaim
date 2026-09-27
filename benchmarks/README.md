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
remain exact. It invokes recovery in a subprocess with only the damaged input,
then compares every output `float64` bit pattern at the corresponding sample
coordinate against the untouched original. It checks output status, evidence
mappings, Fletcher32 verification labels, selected scalar attributes, embedded
and external reports, and both file hashes. Trial files and JSON evidence are
left in the path printed by the command. Use `--work-dir path/to/new-dir` to
choose an empty location.

The recorded trial had 57 native-unavailable or incorrect chunks. H5Reclaim
reconstructed those 57 and exported all 128 chunks with zero wrong bits. The
scientific file's bytes are authentic; the pointer damage is controlled for
evaluation. This says nothing about unrelated layouts, organically damaged
files, or repairs to the original file. See [corpus sources](../corpus/README.md).

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
