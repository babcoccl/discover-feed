"""Keyword alerts: rules match articles as they arrive (title + feed summary at ingest, the
extracted text afterwards) and notify webhook / ntfy / email channels once per story.

Nothing here waits for clustering, summaries or the publish gate."""
