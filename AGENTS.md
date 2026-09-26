# H5Reclaim development instructions

Read `docs/project-brief.md`, `docs/status.md`, and relevant decisions before editing. Inspect the current workspace and preserve unrelated work.

Build a read-only HDF5 recovery engine for a declared support envelope. Never modify source evidence. Write a separate output and report. Do not impute measurements or silently treat unknown regions as valid data.

Keep parsing, candidate discovery, ownership reconciliation, extraction, output, and evaluation separable. Plausible bytes do not establish dataset ownership. Surface conflicting assignments.

Fixture generation and benchmark truth must not leak into recovery inputs. Evaluate exact values and coordinates, not merely whether output opens. Check source preservation and meaningful negative cases.

Consult the official HDF5 specification for binary layouts. Handle unsupported variants explicitly. Use bounded reads and resource limits. Do not load unknown filter plugins automatically.

Implement one reviewable milestone at a time. Run relevant checks and report actual results, including failures or commands not run. Update `docs/status.md` with completed work, remaining uncertainty, and the next concrete task.

Do not claim novelty, production readiness, or superiority without evidence. Explain consequential design choices so the owner can understand and defend them.
