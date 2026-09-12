"""Level 3: pure transformation. No network, no model, no disk.

Everything here takes what `gather` fetched plus what `persist` remembered, and answers
what it means: did the fetch land where we asked, does this payload parse, what moved
since yesterday, what should be filtered out and reported as filtered.
"""
