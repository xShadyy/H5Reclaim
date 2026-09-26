# 0001: Start with a controlled single-dataset fixture

Date: 2026-09-27

Use Python, h5py, and NumPy for an M0 fixture with fixed-size rank-two little-endian `uint32` values, divisible chunk dimensions, and no compression or filters. Create the file with `libver=("earliest", "v108")` for HDF5 1.8 format compatibility. This reduces variables while investigating whether the resulting dataset actually has a version-1 raw-data B-tree with an internal node.

The generator's known values are evaluation truth. Any future recovery code must receive only a damaged input and documented user-supplied dataset context. It must not import the fixture generator or consult a clean reference, generation parameters, or mutation manifest.

The compatibility setting is a hypothesis, not evidence about the generated index. Verify the dataset layout message's index address and inspect the node signature, type, level, and children before deciding where to mutate a copy. If this environment generates a different index, change the fixture method and record the evidence.
