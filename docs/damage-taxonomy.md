# Damage families and evidence gates

HDF5 has multiple superblock, object-header, group, datatype, layout, index,
filter, and file-driver variants. The same byte change can have different
effects in each. This map describes the routes implemented in H5Reclaim 0.8
and the evidence still missing. It is a fault taxonomy, not a probability
distribution or a guarantee that a listed case is recoverable.

The [HDF5 file-format specification](https://support.hdfgroup.org/documentation/hdf5/latest/_f_m_t4.html),
[dataset layout documentation](https://support.hdfgroup.org/documentation/hdf5/latest/group___d_c_p_l.html),
[filter guide](https://support.hdfgroup.org/documentation/hdf5/latest/group___h5_z.html),
and [h5clear guide](https://support.hdfgroup.org/documentation/hdf5/latest/_h5_t_o_o_l__c_r__u_g.html)
are the primary format and tool references. HDF5 can store measurements in
compact headers, contiguous extents, independent chunks, other physical
files, or virtual mappings, so no signature scan alone can assign scientific
coordinates.

| Damaged or missing evidence | Implemented safe route | Boundary or independent evidence needed |
| --- | --- | --- |
| Version-3 write-status flag after an interrupted close | Explicit `rescue --status-trial` validates original superblock checksum and EOA, clears only flag and checksum in a disposable copy, then exports bounded currently readable values with ownership checks | Does not prove the flag was stale or the acquisition completed; reserved bits, bad original checksum, or EOA past physical EOF refuse. `h5clear` is a separate status tool, not a general corruption repair. |
| Signature, superblock, root, or selected object-header destruction | `diagnose` observes signatures/status; some optional selected-message damage can be bypassed by rooted raw metadata fallback | No reliable root or datatype means no justified dataset. A guessed file signature or shape cannot supply a missing coordinate anchor. |
| Older symbol-table link or object-header auxiliary damage | Bounded v0/v1 rooted hard-link traversal, up to eight continuations, canonical rank-one through four numeric schema and supported filters | Older metadata has no on-disk checksum. Redirected links and contradictory global counts refuse; unrelated damaged groups may make enumeration incomplete. |
| Modern compact/dense groups, shared messages, committed type | Checked v2 headers, bounded dense fractal-heap links, selected committed datatype, SOHM list and type-7 B-tree leaf or one internal level with managed heap | Deeper/shared variants, mixed records, huge/tiny/filtered heap IDs and lost ownership anchors refuse. A valid metadata checksum is not an independent historical-value hash. |
| One missing older chunk-tree leaf pointer | Exact parent interval and reciprocal neighboring leaves can bridge one version-1 link | Missing internal subtree, multiple links, stale or ambiguous detached nodes refuse. |
| Modern chunk-index damage | Five families have bounded intact paths; one FAHD-to-FADB pointer may be reconstructed only when substituting a unique child address restores the original FAHD checksum and the checked child points back | Other fixed/extensible-array, B-tree, or paged links; checksum field damage; multiple changes; ambiguous candidate; or scan beyond 512 MiB refuse. |
| Contiguous tail truncation | Complete physically present canonical numeric elements at rooted offsets are retained, missing and partial elements marked unknown | Overwritten elements, header/payload overlap, unprovable original extent, or unknown type cannot be inferred from nearby values. |
| Compact payload or object-header damage | Intact rooted compact numeric values can be exported | A destroyed compact header may also destroy the only payload; no generic reconstruction follows. |
| Unfiltered silent payload bit flip | Prior element or chunk hash baseline can identify exact matches and withhold mismatches; without prior evidence structural placement is labeled historically unverified | Hashes captured after damage do not establish old values; a hash detects but cannot reconstruct a lost byte. The evaluator explicitly counts wrong accepted values without prior checksums. |
| Filtered payload or decoder failure | Declared shuffle, DEFLATE, Fletcher32 and per-chunk masks are checked structurally; native-readable route supports selected built-ins with plugin loading disabled | Failed checksum or decode makes values unknown; unknown filter plugins, lossy precision, or corrupt compressed bytes need a supported decoder or independent redundancy. |
| Independently retained copy or parity | Prior coordinate hashes gate matching HDF5 replicas; prospective XOR sidecar restores at most one missing chunk per stripe | The baseline and copies must predate damage and be independently trustworthy; two losses in one stripe or missing selected schema remain unresolved. |
| External raw, virtual, and external-link sources | Explicit SHA-256-pinned files, physical segment/finite mapping checks, local hard-link target validation and per-element/chunk validity | Missing bytes, dynamic/transitive VDS, recursive links, absent target files, and unsupported selections remain unknown or refuse. Native fill is never evidence of measurement. |
| Family and Split virtual-file drivers | Explicit ordered member maps, hashes, virtual address checks, sibling ownership, alias refusal | Generic Multi/Subfiling and absent driver metadata are unsupported. Duplicate physical members cannot be treated as distinct storage. |
| Strings, compound records, references, variable-length graphs | Bounded native-readable fixed-size schema and selected local references preserve currently accessible values | Structural reconstruction of arbitrary compound/VLEN/global-heap graphs and foreign references requires new parsers and logical-target verification. |
| Multiple simultaneous failures or stale, freed, overlapping data | Evidence ledger records observed links, ranges, checksums, owner contradictions, and unknowns; compatible independent evidence can be reconciled | A convincing detached block may be stale or belong elsewhere. Without a unique surviving owner and coordinate it stays unassigned. |

## What a percentage would require

`benchmarks/run_heldout_trials.py` accepts a pinned, previously unused panel
and separately counts planned, eligible, and excluded file/fault pairs. It
scores accepted coordinates against evaluator-only truth and reports exact,
wrong, unknown, and refused elements by fault class. The bundled scientific
files are **development calibration**, not independent held-out evidence.
Injected faults are correlated within an original and cannot estimate the
frequency of damage causes in the field. A 50% claim needs a declared target
population, representative sampling, naturally damaged or independently
blinded cases, exactness criteria, and a false-accept rate alongside recovery.

Some cases have a hard information limit: erased unique measurement bytes,
all lost coordinate anchors, or a missing external source cannot be recreated
from a lone damaged file. The correct output there is an unknown map, an
unassigned fragment, or a refusal, even if native HDF5 returns a fill value.
