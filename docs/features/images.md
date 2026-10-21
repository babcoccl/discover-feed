# Card images

## What it does

Cards show the best validated image of a story, never a repeated placeholder
(`app/images/`).

## How it works

1. **Page images at no extra cost.** When the extractor fetches an article's
   HTML it also reads `og:image`, `twitter:image`, JSON-LD `image` (incl.
   `@graph`/`ImageObject`) and, as a fallback, `trafilatura.extract_metadata`,
   plus the page's `apple-touch-icon`/favicon. Relative URLs are resolved
   against the page; only `http(s)` is kept. Stored as
   `Article.page_image_url` / `icon_url`; the feed's own `Article.image_url` is
   never overwritten.
2. **Validation** (`app/images/fetch.py`): 10 s timeout, 3 MB cap (header and
   streamed), `image/*` content type, decoded with Pillow (pixel limit
   `max_pixels`, 40 MP, against decompression bombs). Rejected: SVG, ICO,
   tracking pixels (tiny, or `pixel`/`spacer`/`beacon` URLs), anything under
   320x180. Failures are recorded per URL (`images.reason`). The same URL
   on 3+ articles of one domain (a site logo/seal) is marked `generic` and only
   used when nothing else is available. Each URL is validated and stored once
   (`images.key` = hash of the URL): re-encountering a stored row is a cache
   hit (`cache_hits` in the run detail), never a failure.
3. **Cache and proxy** (`images.cache`, default on): each validated image is
   downloaded once, resized to at most 640 px wide as WebP into
   `DISCOVER_IMAGES_DIR` (`/data/images` in Docker) keyed by a hash of its URL,
   and served from `/img/{hash}` with `Cache-Control: public,
   max-age=31536000, immutable`. Least-recently-used files are evicted above
   `images.max_mb` (500). The DB keeps the original URL (`/img/{hash}`
   redirects to it if the file was evicted); with `cache: false` cards hotlink
   the original. Fetches send no cookies or referrer (`<img
   referrerpolicy="no-referrer">` too). **SSRF guard**
   (`app/images/safety.py`): `http(s)` only, every hostname is resolved and all
   addresses must be public (no private, loopback, link-local, CGNAT, multicast
   or reserved IPs, incl. IPv4-mapped IPv6), checked again for every redirect
   (max 3) and at connect time, so DNS rebinding can't slip through.
   `DISCOVER_IMAGES_ALLOW_HOSTS` exempts named hosts (the Docker e2e fixture
   service).
4. **Story image** (`app/images/select.py`): the largest valid non-generic
   image among the story's members; for one article, the page image wins over
   a feed image narrower than `tiny_feed_width` (640). Recomputed (and the
   story republished as a new version) when members change or an image check
   finishes. Published as `image` (`url`, `key`, `source`, size) and `logo` in
   the story document, the cards, and API v1 (`image: {url, proxy_url, source,
   width, height}`; `image_url` stays for compatibility). `image_source` on
   articles and stories is `feed | page | member | logo | none`.
5. **No-image fallback** (`images.no_image_style`): `logo` (default) shows the
   publisher's icon (page or `/favicon.ico`, validated and cached like photos)
   on a tint derived from it, with the publisher name; `text_only` drops the
   image area (compact card); `gradient` picks a gradient per card. Preview any
   style with `?no_image=logo|text_only|gradient` on a feed page.
6. **Backfill**: the `images-backfill` job (every `backfill_interval_minutes`,
   10) checks up to `backfill_per_run` (20) articles of stories published in
   the last `backfill_days` (7), re-fetching the page only for articles
   extracted before image support, one request at a time with a delay. It is
   resumable: articles with `image_checked_at` set are never re-checked. The
   Settings > Pipeline panel shows published stories by image source, missing
   count, cache size and failures by reason.

## Configure

```yaml
images:                 # top level of profiles.yaml; all optional
  cache: true
  max_mb: 500
  no_image_style: logo  # logo | text_only | gradient
  backfill_days: 7
```

Every key: [images block](../configuration/reference.md#images).

## Troubleshooting

| Symptom | Likely cause | Fix |
| --- | --- | --- |
| Cards show icons only | Pages lack `og:image` or images failed validation | Check Pipeline > image stats for rejection reasons |
| `private_address` rejections | SSRF guard blocking LAN hosts (e.g. fixtures) | `DISCOVER_IMAGES_ALLOW_HOSTS` for that host |
| Cache growing | `images.max_mb` LRU cap | Lower it; evicted entries redirect to the original URL |

Not verified against real publishers: hotlink blocking / `403` for non-browser
fetches (the original-URL fallback may then fail too), real image sizes and
aspect ratios, CDN URLs that change per request (which defeats generic-image
detection and the cache), and favicons that are only `.ico` (rejected as
photos, accepted as icons when Pillow can decode them).
