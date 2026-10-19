# Extending

## Add a source adapter

```python
# app/sources/mysite.py
@register(SourceType.MYSITE)
class MySiteAdapter(SourceAdapter):
    config_model = MySiteConfig  # pydantic model in app/config.py

    async def fetch(self, ctx):
        items = []
        for url in self.source.fetch_urls:
            resp = await ctx.http.get(url)
            items.append(await self._normalize(resp))  # RawArticle list
        return items
```

Return `RawArticle`s; normalization, dedup, limits and per-source health stay
upstream. Add the `SourceType` enum value and its config model in
`app/config.py`, expose it in the source form
(`templates/settings_source_form.html`), and point new topics' `source_config`
at it.

## Add a search provider

```python
# app/search/myprov.py
@register("myprov")
class MyProv(SearchProvider):
    name = "myprov"
    host = "api.myprov.com"

    async def search(self, ctx, query, max_results): ...
    def count_results(self, query, items): ...
```

Add `MyProvConfig` in `app/config.py` and a `myprov:` block under
`enrichment.search` / `context.search` in `profiles.yaml`. Rate-limit and
cooldown helpers come from `ProviderEnv` (`interval_seconds`, cooldowns,
usage/caching namespaced `search:<name>` / `context:<name>`); `domains()`
returns the hosts the SSRF `Guard` allows.

## Rules for any change

- Keep `docs/` in sync — the page for the feature and
  [configuration reference](../configuration/reference.md) tables.
  `scripts/check_docs.py` fails the build when a field goes undocumented.
- If you add a `/api/v1` route, run `make openapi` and check in the new
  snapshot.
