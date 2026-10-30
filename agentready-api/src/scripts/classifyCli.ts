import { installPrivacyLogging } from '../utils/privacy';
installPrivacyLogging();
/**
 * Why did we classify this site the way we did?
 *
 *   npm run classify -- https://example.com [https://another.com ...]
 *
 * Prints every profile's score and the signals that fired, not just the winner.
 * Tuning detection from the final answer alone is guesswork — when a SaaS site
 * came back as a publication, the only useful question was "how many points did
 * each side get, and from which signal", and that was invisible until this
 * existed.
 */
import { Deadline, fetchUrl, fetchWithHttpFallback } from '../services/fetcher';
import { HtmlDocument } from '../services/htmlDocument';
import { collectSitemapUrls } from '../services/discovery';
import { parseRobotsTxt } from '../services/robotsTxt';
import { detectSiteType, scoreSiteTypes } from '../services/siteType';
import { normalizeUrl } from '../utils/url';

async function classify(input: string): Promise<void> {
  const { href, origin } = normalizeUrl(input);
  const deadline = new Deadline(15000);

  const homepage = new HtmlDocument(await fetchWithHttpFallback(href, deadline));
  const robotsTxt = await fetchUrl(`${origin}/robots.txt`, deadline);
  const robots = robotsTxt.ok && robotsTxt.body ? parseRobotsTxt(robotsTxt.body) : null;
  const sitemap = await collectSitemapUrls(origin, robots?.sitemaps ?? [], deadline);

  const verdict = detectSiteType(homepage, sitemap.urls, robots);
  const scores = scoreSiteTypes(homepage, sitemap.urls, robots);

  console.log(`\n${'='.repeat(72)}`);
  console.log(`${origin}`);
  console.log(
    `  homepage: ${homepage.text().length} chars of text, ${homepage.links().length} links · sitemap: ${sitemap.urls.length} URLs`,
  );
  console.log(`  → ${verdict.siteType.toUpperCase()} (${verdict.confidence} confidence)\n`);

  for (const entry of [...scores].sort((a, b) => b.score - a.score)) {
    const marker = entry.siteType === verdict.siteType ? '▶' : ' ';
    console.log(`  ${marker} ${entry.siteType.padEnd(16)} ${String(entry.score).padStart(3)}`);
    for (const reason of entry.evidence) console.log(`      · ${reason}`);
  }
}

async function main(): Promise<void> {
  const urls = process.argv.slice(2).filter((arg) => !arg.startsWith('-'));
  if (!urls.length) {
    console.error('Usage: npm run classify -- <url> [url ...]');
    process.exit(1);
  }

  for (const url of urls) {
    try {
      await classify(url);
    } catch (error) {
      console.error(`\n❌ ${url}: ${error instanceof Error ? error.message : String(error)}`);
    }
  }
}

main().catch((error) => {
  console.error(error);
  process.exit(1);
});
