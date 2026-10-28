import rss from '@astrojs/rss';
import { getCollection } from 'astro:content';
import type { APIContext } from 'astro';

export async function GET(context: APIContext) {
  const posts = (await getCollection('posts', ({ data }) => !data.draft))
    .sort((a, b) => a.data.pubDate.getTime() - b.data.pubDate.getTime());
  return rss({
    title: 'discover-feed build log',
    description: 'A candid engineering log of building a personal news aggregator with a coding agent.',
    site: context.site ?? 'https://discover-feed-blog.pages.dev',
    items: posts.map((post) => ({
      title: post.data.title,
      description: post.data.description,
      pubDate: post.data.pubDate,
      link: `/posts/${post.id}/`,
    })),
  });
}
