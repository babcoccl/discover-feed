# discover-feed build log

An Astro static site telling the history of how
[babcoccl/discover-feed](https://github.com/babcoccl/discover-feed) was built —
44 pull requests of Devin sessions, as a readable engineering log plus an
auto-generated timeline.

## Layout

- `src/content/posts/` — posts (Markdown). Frontmatter is validated by
  `src/content.config.ts`; invalid frontmatter fails the build.
- `src/pages/` — index, archive, post, chapter, tag, timeline, about, RSS, 404.
- `src/lib/prs.ts` — typed view over `data/prs.json` (bodies/commits stripped).
- `data/prs.json` — committed PR metadata; the build runs offline from it.
- `scripts/collect_history.mjs` — regenerates `data/prs.json` (manual, never
  part of the site build).
- `scripts/scan.mjs` — leak scan (`npm run scan`): private IPs, emails,
  secrets, and raw PR JSON/body text must not appear in `src/` or `dist/`.

## Refresh the PR data

```bash
GITHUB_TOKEN=... npm run history   # or rely on `gh api` auth
# review the diff, then commit data/prs.json
```

The script redacts private IPs, emails, token-shaped strings and blocklisted
hostnames (`HISTORY_BLOCKLIST=h1,h2`) and refuses to write if anything
suspicious survives.

## Write a post

Add `src/content/posts/my-slug.md` with frontmatter:

```yaml
---
title: "..."
description: "..."      # <= 160 chars
pubDate: 2026-10-26
chapter: 4              # optional, 1-6
prs: [18, 19]           # must exist in data/prs.json
tags: ["enrichment"]
draft: false            # drafts are excluded from builds
---
```

Then `npm run check && npm run build && npm run scan`.

## Build locally

```bash
npm ci
npm run build     # static site in dist/
npm run preview   # serve dist/ locally
```

Node >= 22.23.4 (`.node-version` pins 24.22.1; npm >= 9.6.5).

## Deploying (Cloudflare Pages)

- **Root directory:** `blog`
- **Build command:** `npm run build`
- **Build output directory:** `dist`
- **Production branch:** `main`
- **Build watch paths (include):** `blog/*`
- **Environment variables:** `SITE_URL` (defaults to
  `https://discover-feed-blog.pages.dev` — controls canonical, RSS and sitemap
  URLs); `NODE_VERSION=24.22.1` if the Pages default is older than required.

`public/_headers` sets security headers, long immutable caching for hashed
`/_astro/*` assets and no-cache for HTML. `data/prs.json` is build-time only —
nothing under `data/` is copied to `dist/`.
