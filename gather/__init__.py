"""Level 2: network I/O for sources, and the policy that decides whether to fetch.

Along with `enrich` and `deliver`, one of only three packages permitted to import
`httpx`. Nothing here parses, filters or renders -- a fetcher hands back bytes and the
facts about how it got them, and `process` decides what they mean.
"""
