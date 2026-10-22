import os

# The background catalog crawl would hit the mock shops with dozens of queries;
# tests that need it start it themselves.
os.environ["INDEX_CRAWL"] = "0"
