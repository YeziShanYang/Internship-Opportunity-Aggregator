"""Level 0 of the pipeline: shapes and constants, and no I/O of any kind.

Nothing in here opens a file, makes a request or reads the clock except through
`clock.utcnow`. That is what lets every other stage import it freely without creating
the import cycles that made `state.py` impossible to split for so long.
"""
