"""Experimental, evidence-based HDF5 recovery and validity-mapped export."""

from .masked_reader import MaskedReadError, read_masked

__all__ = ["MaskedReadError", "read_masked"]
