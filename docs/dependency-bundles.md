# Related files and interrupted-write triage

HDF5 can refer to bytes outside the selected `.h5` file through external raw
storage, an external link, or a virtual dataset (VDS). A VDS read can return
its declared fill value when a source is missing. That value is not evidence
that the instrument measured it. H5Reclaim therefore inventories the selected
dataset's declared file names without following the links or reading values.

An optional related-file manifest maps each **exact declared filename** to an
explicit local path and expected SHA-256. Its JSON form is:

```json
{
  "schema_version": 1,
  "files": [
    {
      "declared_name": "run-01.h5",
      "path": "/absolute/path/to/run-01.h5",
      "sha256": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
    }
  ]
}
```

On Windows, use an absolute path such as `C:\\Lab\\run-01.h5` in JSON. Supply
the real SHA-256 of each file. The example digest above is only a shape
example; it is not a known hash of a scientific file. The manifest has a 64
KiB limit and at most 64 entries. Declared names never become filesystem
paths, and H5Reclaim does not scan sibling directories or expand wildcards.
It hashes explicitly supplied files in bounded blocks, up to 4 GiB for a
selected bundle. For a fixed-size external raw segment, its declared offset
and length must fit the supplied file. For VDS and external links, H5Reclaim
checks that the pinned HDF5 file has a local hard-linked target object without
reading values. A VDS target itself backed by other files remains unresolved;
unlimited external raw segments and dynamic VDS filename patterns also remain
unresolved.

A matching hash identifies the supplied bytes at inspection time. It does not
prove that the file is the original instrument output or that its scientific
values are correct. This release does not reconstruct VDS measurements or
export external raw data using the manifest.

## Superblock status

For a version-3 superblock only, a raw write-access bit can indicate an
interrupted writer. Earlier versions do not assign that meaning to their
consistency field. H5Reclaim records the raw end-of-address (EOA) and physical
end-of-file (EOF) relationship, while marking the raw superblock checksum
unvalidated. A discrepancy alone does not establish what bytes were lost.

If metadata will not open and the observed version-3 write bit is set,
`h5reclaim probe-status FILE --json` can optionally run `h5clear --status`
**only on a disposable private copy**. The executable must be installed
separately. It does not attempt `--increment`, remove metadata cache images,
or change the original. The probe checks whether the tool changed only the
status byte and superblock checksum and whether native HDF5 can open the
trial's metadata. The temporary file is then deleted. A successful probe
means only that the status-cleared copy's metadata opened; it does not verify
or recover measurements. EOA past the physical EOF is not eligible for this
status-only trial.

These distinctions follow the [HDF5 file format specification](https://support.hdfgroup.org/documentation/hdf5/latest/_f_m_t4.html)
and the HDF Group's [h5clear guide](https://support.hdfgroup.org/documentation/hdf5/latest/_h5_t_o_o_l__c_r__u_g.html),
which explicitly says h5clear is not a general file-corruption repair tool.
