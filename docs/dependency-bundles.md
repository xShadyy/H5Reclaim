# Related files and HDF5 driver bundles

Use companion-file manifests when an HDF5 dataset stores values outside its container. Entries map declared names or driver members to actual local files and their lowercase SHA-256 hashes. H5Reclaim can build the related-file manifest from a supplied directory. It snapshots and verifies supplied files before materializing local output datasets.

```sh
python -m h5reclaim related-manifest container.h5 --directory companion-files --output related.json
python -m h5reclaim rescue container.h5 --related-dir companion-files --output local.h5 --report evidence.json
```

Directory discovery reads declared names from local metadata, follows nested declarations, expands numbered `%b` filename patterns, and hashes matching regular files inside the selected directory. Missing files are listed as unresolved. Ambiguous names require an explicit mapping. Absolute declared paths outside the selected directory can be supplied in a hand-written manifest.

## External raw storage, virtual datasets, and external links

Save this shape as `related.json`, using the actual filename declared in HDF5 metadata and the supplied file's real digest:

```json
{
  "schema_version": 1,
  "files": [
    {
      "declared_name": "run-01.h5",
      "path": "/data/run-01.h5",
      "sha256": "REPLACE_WITH_64_LOWERCASE_HEX_CHARACTERS"
    }
  ]
}
```

```sh
python -m h5reclaim diagnose container.h5 --dataset /readings --related-files related.json
python -m h5reclaim rescue container.h5 --dataset /readings --related-files related.json --output local.h5 --report evidence.json
python -m h5reclaim rescue container.h5 --all --related-files related.json --output whole-local.h5 --report whole-evidence.json
```

The external raw route maps complete records through ordered segments, including a physically present prefix when a segment is shortened. Virtual recovery streams large mappings and nested virtual/external-link graphs under configured budgets. Unlimited mappings derive their current extent from pinned sources; numbered `%b` patterns support source files and dataset names. Overlapping virtual mappings follow the tested native mapping order. Missing source values remain unknown instead of accepting virtual fill. Fixed and heap-backed values retain their file datatype. Physical evidence names the file that owns the bytes.

Whole-file recovery uses a shared manifest for dependent datasets and their transitive sources. Local datasets continue independently when a dependency is unavailable. External group trees and datasets are materialized locally, including their attributes, aliases and references to recovered targets. Reference identities are scoped to their source file, so equal object addresses in two files cannot be confused.

Use escaped Windows JSON paths such as `C:\\Lab\\run-01.h5`. Each declared nested filename needs an entry. Manifests reject duplicate names and keys. With `--related-files`, declared names are matched exactly. `--related-dir` performs the explicit directory discovery described above.

## Family driver

A Family file spans numbered members. Supply the producer's actual member size and every member in order:

```json
{
  "schema_version": 1,
  "member_size": 1048576,
  "members": [
    {"index": 0, "path": "/data/run000.h5", "sha256": "REPLACE_WITH_ACTUAL_SHA256"},
    {"index": 1, "path": "/data/run001.h5", "sha256": "REPLACE_WITH_ACTUAL_SHA256"}
  ]
}
```

```sh
python -m h5reclaim rescue /data/run000.h5 --dataset /readings --family-members family.json --output local.h5 --report evidence.json
python -m h5reclaim rescue /data/run000.h5 --family-members family.json --resume-dir progress --output whole-local.h5 --report whole-evidence.json
```

The source argument is member zero. The route reads the pinned Family address space, verifies physically present member ranges, and records member provenance. Whole-file and selected exports use the common native streamer for fixed records, heap-backed values, scalar, empty and null datasets. Restart checkpoints pin every member. Use distinct physical files for distinct indices.

## Split driver

A Split manifest lists metadata followed by raw storage:

```json
{
  "schema_version": 1,
  "driver": "split",
  "members": [
    {"role": "metadata", "path": "/data/run-m.h5", "sha256": "REPLACE_WITH_ACTUAL_SHA256"},
    {"role": "raw", "path": "/data/run-r.h5", "sha256": "REPLACE_WITH_ACTUAL_SHA256"}
  ]
}
```

```sh
python -m h5reclaim rescue /data/run-m.h5 --dataset /readings --split-members split.json --output local.h5 --report evidence.json
python -m h5reclaim rescue /data/run-m.h5 --split-members split.json --output whole-local.h5 --report whole-evidence.json
```

The source argument is the metadata member. The route validates the stored two-member address map and supplied files, then exports whole files or selected datasets with current-value status maps. Metadata and raw members remain distinct physical inputs. Checkpoints include both hashes, so changed raw bytes cannot reuse earlier values.

## Interrupted-write status

Ordinary selected-dataset rescue automatically recognizes a version-3 write flag. `--status-trial` selects that route explicitly. The route checks the original superblock checksum, flags, and physical end before changing only status and checksum on a private copy. It then exports readable values.

`probe-status FILE --json` can run an installed `h5clear --status` on a disposable copy for diagnosis. The original is unchanged. A readable status trial describes current values; prior captures provide historical comparison.

See the [usage guide](usage.md) for command selection and the [HDF5 format specification](https://support.hdfgroup.org/documentation/hdf5/latest/_f_m_t4.html) for storage layouts.
