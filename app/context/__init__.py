"""Background-and-context links for stories ("related, not the same event").

For each eligible story (brief published, ``context.min_priority`` or
``context.min_publishers`` reached), the finder searches the web for pages that add
background — history, context, data, opposing views — and stores accepted ones as
``Article`` rows with ``origin == "context"`` (``context_note`` / ``context_angle`` set,
pinned via ``story_id`` / ``discovered_for_story_id`` / ``anchor_article_id``).

Context links are never members of the event: excluded from ``profiles.get_story``'s
member list, so they never reach source counts, clustering, alerts, brief/report inputs,
citations, the word budget or profile visibility. They are shown only in the story
page's "Background and context" section and in the API's ``story.context[]``.
"""
