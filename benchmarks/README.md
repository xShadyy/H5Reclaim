# Independent broken-link benchmark

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
with a damaged path and dataset path, omitting the pristine path, mutation
manifest, challenge seed, and generator settings.
