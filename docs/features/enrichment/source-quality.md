# Source quality

## Deny lists

Three layers, all applied to enrichment and context links alike:

- `enrichment.deny_domains` — your blocked publishers.
- `enrichment.quality.deny_domains` — confirmed content farms (defaults ship
  the eval's list; a YAML list replaces it entirely).
- `blocked_domains` table — persisted by the admin **Block this domain**
  action (on every web-search member on the story page, and on context links).
  Applying it like `deny_domains` on every later pass, including publisher
  aliases, it removes every enrichment member on the domain and queues the
  affected stories for one regeneration.

Settings > Pipeline lists the domains accepted most often in the last 7 days
so scraper sites are visible at a glance.

## The quality gate

The **source-quality gate** (`enrichment.quality`, `app/enrich/quality.py`)
flags — never rejects — accepted candidates whose domain looks like a content
farm:

- `quality.suspicious_tlds`: throwaway TLDs — `edu.pl`, `club`, `shop`,
  `online`, `site`, `xyz`, `top`, `icu`, `info`, `biz`, `click`, `link`,
  `work`, `buzz`, `vip`, `website`, `space`, `today`, `live`;
- `quality.suspicious_patterns`: keyword-stuffed names — a
  `news`/`wire`/`daily`/`hub`/`insider`/`feed` token, so `tech-insider.org` and
  `news24.site` flag but fused names like `thenews.com.pk`, `wired.com` and
  `dailynews.com` do not.

The flag (`tld:<suffix>` / `name:<token>`) is recorded in
`enrichment_candidates.flagged`, counted under "flagged sources" in Settings >
Pipeline with the top flagged domains of the last 7 days, and shown as a
"source quality uncertain" badge (reason in the tooltip) on the member —
still displayed and cited, since it passed every relevance check. `evaluate`
prints the flagged count and domains plus a `flag` column.

Confirmed farms go in `quality.deny_domains` or are blocked from the story
page.
