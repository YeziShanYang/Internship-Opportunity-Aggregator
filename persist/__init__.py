"""Level 1: all storage, read and write.

This sits *below* gather rather than at the end of the pipeline, and that correction
matters: `process` needs yesterday's snapshot and `classify` needs applied.tsv, so a
write-only stage at the end would forbid every read the pipeline actually makes.

Nothing else in the tree may open a file for writing. `tests/test_layering.py` checks
that mechanically, with an explicit exception list, so a violation is named rather than
discovered later in a diff.
"""
