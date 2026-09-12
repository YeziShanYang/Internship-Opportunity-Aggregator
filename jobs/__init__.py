"""Jobs compose stages into something runnable. A job is not itself a stage.

`discover.py` is the reason this package exists. It does GitHub search (gather),
snapshot mining (a read), `record()` (persist) and `lines()` (render), so it cannot sit
at any single level of the stage graph -- it is a second *job* over the same stages.
Same for the daily digest, and for the ATS re-resolution pass.
"""
