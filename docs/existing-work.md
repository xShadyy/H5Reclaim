# Existing-tool comparison status

H5Reclaim's synthetic result is not a claim that the method is new or better than an existing recovery tool. No existing recovery implementation has been run on the identical damaged fixture in this repository yet.

| Tool | Why it matters | Current comparison status |
| --- | --- | --- |
| [HDF Group `h5clear`](https://support.hdfgroup.org/documentation/hdf5/latest/_h5_t_o_o_l__c_r__u_g.html) | Addresses documented file-state issues | Not available in the test environment; applicability to this one-pointer case has not been measured |
| [HDF Group `h5patch` and hdf5-pickles](https://github.com/HDFGroup/hdf5-pickles) | Metadata inspection and repair mechanisms | Code and applicable repair cases still require a direct review and same-input run |
| [ESRF `h5recover`](https://gitlab.esrf.fr/hdf5/h5recover) | Prior scientific HDF5 recovery work | Capability and access review remains open |
| [hdf5-scope-recovery](https://github.com/tony-ko/hdf5-scope-recovery) | Specialized prior recovery approach | Supported layouts and assumptions have not been compared |
| [Rush Tools HDF5 Explorer](https://rush.tools/hdf5-explorer) | Commercial product advertising recovery features | No accessible same-input evaluation has been performed |

For a fair future baseline, record the version, build, options, exact damaged input hash, permitted dataset hints, success/failure details, and exact recovered values at coordinates. If a tool does not target this case or cannot be run, record that circumstance without assigning it a zero recovery score. If another implementation already solves the same case well, a useful next contribution might be stronger attribution validation, a negative corpus, or an upstream fix.
