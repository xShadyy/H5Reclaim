# Security policy

## Report a vulnerability

If the repository offers GitHub's private vulnerability reporting option,
use **Security → Report a vulnerability**. Otherwise, email
**tymoteusz.netter@gmail.com** with the subject `H5Reclaim security report`.
Please avoid a public issue for a vulnerability before a fix or safe
mitigation is available. The maintainer will coordinate disclosure with the
reporter; no response or fix time is guaranteed.

Include the affected version or commit, operating system, Python and HDF5
versions, command, expected and observed behavior, and a small reproduction
if you can share one. Do not send confidential scientific files or credentials
without first agreeing on a private transfer method. A minimal synthetic file
and a redacted report are usually easier to investigate. Reports can contain
local paths and metadata; check them before sharing.

Security reports may include unexpected writes to the source, path or archive
handling, denial of service, crashes on crafted files, unsafe dependency
resolution, or misleading acceptance of damaged values.

## Supported versions

The first 1.0 release candidate is under development. Reports are triaged
against the current branch and any published candidate. Earlier development
versions may require an upgrade; there is no separate long-term support line.

## Handling untrusted inputs

HDF5 parsing and optional compression filters use native code supplied by
HDF5, h5py, and installed plugins. H5Reclaim's resource budgets and worker
limits reduce some failure modes, but they are not a security sandbox. If a
file comes from an untrusted party, inspect it in an isolated environment
without access to sensitive files or credentials. Install optional filters
only from sources you trust. Keep the input separately retained and select
new output paths.

A recovery status marks what the tool could justify from the inspected bytes.
It does not authenticate the origin of those bytes or establish scientific
correctness. Use the report's validity map before analyzing recovered values.
