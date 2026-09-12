"""Level 4: the second source-network stage, named rather than hidden.

A single "gather everything up front" stage is not achievable, and pretending otherwise
would be the dishonest kind of tidy. Which postings to fetch is only knowable *after*
diffing: bodies are fetched for the handful of rows that changed, not for the ~4,000
postings sitting on the boards. So there are exactly two source-network stages, and the
pipeline says so out loud.

This is O(changes), not O(sources). It used to hide inside `classify.classify`, which
meant the model-call stage made network requests in the middle of itself and there was
no way to see what it had fetched.
"""
