import { redact } from '../utils/privacy';
import { config } from '../config';
import { CheckId, CheckOutcome, CheckResult, ScanResult, SiteType } from '../types';
import { Deadline, FetchResult, PROBE_BODY_BYTES, PROBE_TIMEOUT_MS, fetchUrl, fetchWithHttpFallback } from './fetcher';
import { HtmlDocument, typesOf } from './htmlDocument';
import { ScanContext } from './scanContext';
import { collectSitemapUrls, isSampleablePage, selectKeyPages } from './discovery';
import { ParsedRobots, parseRobotsTxt } from './robotsTxt';
import { detectSiteType, profileFor } from './siteType';
import { normalizeUrl } from '../utils/url';
import { SCORING_VERSION, buildSummary, scoreCheck, totalScore } from './scoring';

import { CONTROL_BOT, LIVE_PROBE_BOTS, checkBotAccess } from './checks/botAccess';
import { CONTENT_STRUCTURE_TITLE, checkContentStructure } from './checks/contentStructure';
import { AGENT_ARTIFACT_PATHS, checkAgentInterface } from './checks/agentInterface';
import { checkStructuredData } from './checks/structuredData';
import { SCHEMA_PROFILES } from './checks/schemaProfiles';
import { checkTrustSignals } from './checks/trustSignals';
import { checkMetaRobots } from './checks/metaRobots';
import { checkCrawlability, looksParked } from './checks/crawlability';

/** Report order — worst-to-fix-first is handled in the UI, this is stable order. */
const CHECK_ORDER: CheckId[] = [
  'bot_access',
  'structured_data',
  'content_structure',
  'agent_interface',
  'trust_signals',
  'meta_robots',
  'crawlability',
];

/**
 * Run all six checks against a URL.
 *
 * Never throws for site-side problems — an unreachable or hostile site is a
 * result, not an error. The only throw is InvalidUrlError from normalisation,
 * which is a problem with what the visitor typed.
 *
 * Order matters here. The homepage is fetched first because everything depends
 * on it; the site type is classified next because it decides which pages get
 * sampled, which agent artefacts get probed, and what the checks expect.
 */
export interface ScanOptions {
  /**
   * Skip classification and judge the site as this type.
   *
   * Detection is a heuristic and says so — it reports low confidence when the
   * signals are thin. This is how that admission becomes actionable: the
   * visitor can correct it and re-run, rather than reading a report built on a
   * wrong assumption.
   */
  siteType?: SiteType;
}

async function runScanInternal(submittedUrl: string, options: ScanOptions = {}): Promise<ScanResult> {
  const startedAt = Date.now();
  const { href, origin, domain } = normalizeUrl(submittedUrl);
  const deadline = new Deadline(config.scanner.totalTimeoutMs);

  /**
   * The homepage gets a slice too, so a failure can still be diagnosed.
   *
   * A stalling server holds the https attempt for the full per-request timeout,
   * then holds the http fallback for whatever is left — 17 seconds on
   * myntra.com, the entire budget, with nothing remaining to ask *why*. The
   * browser probe below then could not run, and the report fell back to "your
   * homepage did not respond" when the truthful answer was "your homepage
   * answers browsers and stalls everything else".
   *
   * Diagnosing a failure is worth more than waiting longer for it.
   */
  const homepage = new HtmlDocument(await fetchHomepage(href, deadline));

  /**
   * Stop before anything else if there is no website here to grade.
   *
   * Checked immediately after the homepage and before any other request,
   * because everything downstream — sampling pages, probing crawlers, judging
   * schema — is meaningless without a page, and running it produces a confident
   * grade for a domain nobody has built. Returning early also spares the ~20
   * requests.
   */
  const missing = whyNoWebsite(homepage);
  if (missing) {
    return noWebsiteResult(submittedUrl, domain, homepage, startedAt, missing);
  }

  /**
   * We got nothing, but is that about them or about us?
   *
   * One request, as an ordinary browser, purely to classify what just happened.
   * If a browser is served where our scanner was stalled or refused, the site is
   * plainly up and is treating non-browser visitors differently — which is the
   * finding, not an absence of one.
   *
   * The body is thrown away. Judging a site's content on a page obtained by
   * looking like something we are not would make every other number in the
   * report unverifiable, so this establishes one fact and nothing more.
   */
  const homepageFailed = !homepage.ok;
  const browserReachable = homepageFailed ? await confirmWithBrowserProbe(href, deadline) : false;
  /**
   * Follow the site to where it actually lives.
   *
   * `origin` comes from what the visitor typed, and plenty of sites answer on
   * the apex only to redirect to www (or the reverse). Everything downstream —
   * robots.txt, the sitemap, /llms.txt, the crawler probes — was still being
   * asked of the original host, which on snitch.com meant a different robots.txt
   * and a sitemap that yielded four fewer product pages.
   *
   * The visible cost was worse than the missing pages: the same shop scored
   * D/52 typed as "snitch.com" and C/60 typed as "www.snitch.com". A grade that
   * depends on how you typed the address is not a grade anyone can act on, and
   * two people comparing notes would each be sure the other had it wrong.
   *
   * A crawler follows the redirect and treats the destination as the site. So do
   * we now.
   */
  const canonicalOrigin = homepage.ok ? originOf(homepage.url) ?? origin : origin;
  const robotsTxt = await fetchUrl(`${canonicalOrigin}/robots.txt`, deadline);
  const robots: ParsedRobots | null =
    robotsTxt.ok && robotsTxt.body && !looksLikeHtml(robotsTxt) ? parseRobotsTxt(robotsTxt.body) : null;

  /**
   * The sitemap gets a slice of the budget, not the run of it.
   *
   * It is a means to an end: we read it to decide which five pages to fetch, and
   * those pages feed the heaviest check in the report. On thesouledstore.com it
   * consumed 6.5 of 12 available seconds and handed back 4,995 URLs; the five
   * pages it selected were then fetched with nothing left and every one failed,
   * so structured data came back "we ran out of time" — on a scan where the
   * expensive part had never been the pages at all.
   */
  const sitemap = await collectSitemapUrls(canonicalOrigin, robots?.sitemaps ?? [], deadline.slice(SITEMAP_BUDGET_MS));

  // Classify before sampling: a blog and a store want completely different
  // pages fetched, and fetching the wrong ones wastes the budget.
  // robots.txt is passed in because on sites that refuse our scanner it is the
  // only structural evidence we have — see robotsPathsOf in siteType.ts.
  const detected = detectSiteType(homepage, sitemap.urls, robots);
  const verdict = options.siteType
    ? { siteType: options.siteType, confidence: 'high' as const, evidence: ['you told us what kind of site this is'] }
    : detected;
  const profile = profileFor(verdict.siteType);

  const selection = selectKeyPages(homepage, sitemap.urls, verdict.siteType, config.scanner.maxKeyPages, sitemap.productUrls);
  const candidateUrls = selection.urls;

  // Agent artefacts and key pages are independent, so fetch them together —
  // sequential round trips would eat most of a 15-second budget on a slow host.
  const artifactSpecs = AGENT_ARTIFACT_PATHS[verdict.siteType];

  const [artifactResponses, keyPageResponses, probeResponses] = await Promise.all([
    Promise.all(artifactSpecs.map((spec) => fetchUrl(`${canonicalOrigin}${spec.path}`, deadline))),
    /**
     * No point asking for more pages from a server that would not give us one.
     *
     * If the homepage never arrived — stalled, refused, or errored — the same
     * will happen to every product page, and each one costs a full ten-second
     * wait to prove it. nykaa.com spent ten of its twenty seconds fetching four
     * pages that were always going to time out, on top of the wave of crawler
     * probes that had already told us the server stalls anything non-browser.
     *
     * Skipping them changes no verdict: the checks that need page content
     * already report "not scored" when there is none, and crawlability reports
     * the access problem itself. It only stops us paying for the same answer
     * twice.
     */
    !homepage.ok || deadline.expired() ? Promise.resolve([]) : Promise.all(candidateUrls.map((url) => fetchUrl(url, deadline))),
    // Ask for the homepage again as each named AI crawler. This is what
    // separates "robots.txt allows GPTBot" from "the server actually serves
    // GPTBot" — a gap that costs sites their entire AI visibility silently.
    probeAsCrawlers(homepage.url, deadline),
  ]);
  const botProbes: Record<string, FetchResult> = {};
  [...LIVE_PROBE_BOTS, CONTROL_BOT].forEach((bot, index) => {
    botProbes[bot.agent] = probeResponses[index];
  });

  const agentArtifacts: Record<string, FetchResult> = {};
  artifactSpecs.forEach((spec, index) => {
    agentArtifacts[spec.key] = artifactResponses[index];
  });

  const parseable = keyPageResponses
    .filter((result) => {
      // A discontinued page often redirects to a listing or landing page.
      // Judging that page for missing schema would report a gap the site does
      // not have, so drop anything that redirected somewhere not worth
      // sampling. A page that simply failed is kept: Check 6 needs to report
      // the broken page.
      if (result.failure || !result.ok) return true;
      const redirected = stripSlash(result.finalUrl) !== stripSlash(result.requestedUrl);
      return !redirected || isSampleablePage(result.finalUrl, verdict.siteType);
    })
    .filter(withinAnalysisBudget());

  /**
   * Parse until the clock says stop, not until the list runs out.
   *
   * cheerio runs before any check does, so no guard placed between checks can
   * bound it — and a byte budget turned out to be the wrong lever: nykaa.com's
   * pages are 0.59MB each and were the most expensive in the whole corpus, while
   * boat-lifestyle.com's are 2.5MB and parse quickly. Cost tracks DOM complexity,
   * which we cannot know without paying for it.
   *
   * Time is the thing we actually promise, so time is what governs this. Pages
   * are parsed one at a time and we stop when the budget is nearly spent, which
   * bounds the overshoot to a single page whatever the site does.
   */
  const parseBy = startedAt + Math.floor(config.scanner.totalTimeoutMs * 0.8);
  const keyPages: HtmlDocument[] = [];
  for (const result of parseable) {
    if (Date.now() > parseBy && keyPages.length) break;
    keyPages.push(new HtmlDocument(result));
  }
  const ranOutOfTime = deadline.expired();

  const context: ScanContext = {
    submittedUrl,
    origin: canonicalOrigin,
    domain,
    siteType: verdict.siteType,
    siteTypeConfidence: verdict.confidence,
    siteTypeEvidence: verdict.evidence,
    profile,
    homepage,
    keyPages,
    noKeyPagesFound: candidateUrls.length === 0,
    robotsTxt,
    robots,
    agentArtifacts,
    botProbes,
    browserReachable,
    sitemapFound: sitemap.found,
    sitemapUrls: sitemap.urls,
    siteName: detectSiteName(homepage, domain),
    deadline,
  };

  /**
   * The clock covers thinking, not just fetching.
   *
   * The deadline gated every network call and nothing else, so once the fetches
   * were done the checks ran to completion however long they took. On nykaa.com
   * that was 18.8 seconds of pure CPU — parsing six large pages and walking
   * their DOMs — against a three-second processing reserve, and a scan we
   * promise in twenty seconds finished in thirty-six.
   *
   * Reserving more time would not fix it: the work is proportional to how big
   * the pages are, so there is no reserve large enough for every site. The
   * answer is a ceiling that is actually enforced. A check that does not get to
   * run is reported as unverified — never as a failure — which is the same rule
   * applied everywhere else we could not look.
   */
  const hardStop = startedAt + config.scanner.totalTimeoutMs;
  const outOfTime = (): boolean => Date.now() >= hardStop;

  const run = (checkId: CheckId, check: () => CheckOutcome): CheckOutcome =>
    outOfTime() ? unableToVerify(checkId, context.siteType) : check();

  // Network-free checks first, so a tight budget costs us the cheapest check
  // rather than the most valuable one.
  const outcomes: CheckOutcome[] = [];
  outcomes.push(checkBotAccess(context));
  outcomes.push(checkAgentInterface(context));
  const structured = outOfTime()
    ? { outcome: unableToVerify('structured_data', context.siteType), timedOut: true }
    : resolveStructuredData(context, ranOutOfTime);
  outcomes.push(structured.outcome);
  outcomes.push(run('content_structure', () => checkContentStructure(context)));
  outcomes.push(run('meta_robots', () => checkMetaRobots(context)));
  const crawlability = checkCrawlability(context);
  outcomes.push(crawlability.outcome);
  // Last, because it is the only check that may still make requests.
  outcomes.push(outOfTime() ? unableToVerify('trust_signals', context.siteType) : await checkTrustSignals(context));
  const checks: CheckResult[] = CHECK_ORDER.map((checkId) => {
    const outcome = outcomes.find((candidate) => candidate.checkId === checkId);
    // Defensive: a missing outcome would silently drop weight from the score.
    return scoreCheck(outcome ?? unableToVerify(checkId, context.siteType), context.siteType);
  });

  const { score, grade } = totalScore(checks, verdict.siteType);

  /**
   * Nothing was in play, so there is nothing to grade.
   *
   * When every weighted check ends up skipped there are zero points available,
   * and the score falls through to F/0 — croma.com and wakefit.co both landed
   * there purely for refusing our requests. Their sites work perfectly for their
   * customers; we were turned away at the door and then published a failing
   * grade about what we never saw.
   */
  const unreadable = checks.every((check) => check.pointsPossible === 0);

  return {
    domain,
    submittedUrl,
    siteType: verdict.siteType,
    siteTypeConfidence: verdict.confidence,
    siteTypeEvidence: verdict.evidence,
    siteTypeOverridden: Boolean(options.siteType),
    overallScore: score,
    overallGrade: grade,
    summary: unreadable ? unreadableSummary(domain, browserReachable) : buildSummary(grade, checks, verdict.siteType),
    checks,
    pagesScanned: [homepage.url, ...keyPages.map((page) => page.url)],
    pagesDiscovered: selection.discovered,
    renderMode: crawlability.renderMode,
    scanDurationMs: Date.now() - startedAt,
    jsRenderWarning: crawlability.jsRenderWarning,
    /**
     * "Partial" means the deadline beat us, not that a check did not apply.
     *
     * The comment above said exactly that while the code said something much
     * narrower: it was wired to `structured.timedOut`, so a scan was only ever
     * declared partial when one specific check ran out of time. Seven scans in
     * the database overran the fifteen-second ceiling — one took 25 seconds —
     * and every single one of them recorded `partial: false`. Those reports were
     * built from half-finished crawls and presented as final, which is the worst
     * shape of error we can make: not wrong, but quietly wrong.
     *
     * Asked here, at the end, rather than at the midpoint where `ranOutOfTime`
     * is captured — the trust-signals check still makes requests after that
     * point, so a scan can exhaust its budget between the two.
     */
    partial: deadline.expired() || structured.timedOut || outOfTime(),
    noWebsite: false,
    unreadable,
    scoringVersion: SCORING_VERSION,
  };
}

/**
 * Check 3 has two "cannot run" cases that must not be scored as failures: a
 * site with no discoverable key pages, and a scan that ran out of time before
 * it could fetch them.
 *
 * Only stores and publications are judged on their subpages — for a local
 * business, a SaaS product or an unclassified site the entity being described
 * is the site itself, so the homepage is enough and this never fires.
 */
function resolveStructuredData(
  context: ScanContext,
  ranOutOfTime: boolean,
): { outcome: CheckOutcome; timedOut: boolean } {
  const judgedOnSubpages = context.siteType === 'ecommerce' || context.siteType === 'content';
  if (!judgedOnSubpages || context.keyPages.length) {
    return { outcome: checkStructuredData(context), timedOut: false };
  }

  /**
   * Out of time before the product pages could be fetched.
   *
   * This has to be checked *before* the homepage fallback below, not after.
   * A store's homepage often carries a Product node for a featured item, and
   * judging that as though it were a product page produces a confident FAIL on
   * a shop whose real product pages are perfect — chumbak.com dropped from B to
   * F this way on the first scan after a cold start, purely because the
   * deadline expired and nobody noticed.
   *
   * We found pages worth checking and could not fetch them. That is "unable to
   * verify", which is the honest answer everywhere else in this file.
   */
  if (ranOutOfTime && !context.noKeyPagesFound) {
    return { outcome: unableToVerify('structured_data', context.siteType), timedOut: true };
  }

  // A single-product or one-page site: the homepage genuinely is the key page.
  const profile = SCHEMA_PROFILES[context.siteType];
  const homepageHasEntity = context.homepage.jsonLd().some((node) => typesOf(node).some((type) => profile.accepts(type)));
  if (homepageHasEntity) return { outcome: checkStructuredData(context), timedOut: false };

  return {
    timedOut: false,
    outcome: {
      checkId: 'structured_data',
      title: `Structured data (${profile.label})`,
      status: 'skipped',
      details: `No ${context.profile.keyPageLabel} could be identified from homepage links or the sitemap, so this check was not run.`,
      humanExplanation:
        `We could not identify any ${context.profile.keyPageLabel} on your site, so we could not check whether they carry structured data. ` +
        'That usually means one of three things: your links are generated by JavaScript, your URLs use an unusual pattern, or those pages do not exist yet. ' +
        'This check has been left out of your score rather than counted against you — but it is worth a manual look, because structured data is the single biggest factor in whether an AI assistant will describe you accurately.',
      generatedFix: null,
      generatedFixLanguage: null,
      generatedFixTarget: null,
    },
  };
}

/**
 * How many crawler probes go out at once, and how long we pause between waves.
 *
 * All eleven used to leave together. That is a burst no ordinary visitor
 * produces, and a fair number of servers answer the first few and then start
 * returning 429 — which we read back as "this site blocks AI crawlers". We were
 * measuring our own impatience and billing the site owner for it.
 *
 * Four at a time with a short pause is still far quicker than a real crawler
 * would ever be, and it costs about half a second of a fifteen-second budget.
 */
/**
 * The most we will spend choosing which pages to look at, before we look at
 * them. Roughly a quarter of a 20-second scan.
 */
const SITEMAP_BUDGET_MS = 4000;

/** The most we will wait for the homepage before diagnosing instead. */
const HOMEPAGE_BUDGET_MS = 10000;

/**
 * How much markup one scan will parse and analyse.
 *
 * Every check walks these pages, several of them more than once, so the cost
 * scales with total DOM size. nykaa.com spent 18.8 seconds of pure CPU after its
 * network work finished — cheerio parsing six large pages and the checks walking
 * them — and turned a twenty-second promise into fifty-five.
 *
 * A clock check between checks helped but could not stop work already underway,
 * and cheerio parsing happens before any check runs at all. Bounding the input
 * is the only thing that makes the work finite. It degrades in the right
 * direction: the homepage is never dropped, and the report already states how
 * many pages were checked against how many were found, so a scan that examines
 * four of six says so rather than pretending otherwise.
 *
 * Sized on measurement, and raised from 6MB after that first guess cost
 * boat-lifestyle.com half its sample: its pages are 2.5MB each, so six of them
 * need fifteen. A narrower sample is not a neutral saving — boat dropped from
 * B to C purely because the three pages that survived the cut happened to be
 * the ones without Product markup, which is sampling noise dressed up as a
 * finding.
 *
 * Worth knowing that bytes are only a proxy: nykaa.com's pages are 0.59MB and
 * were the slowest to analyse in the whole corpus, because cost tracks DOM
 * complexity rather than size. The budget is a backstop against pathological
 * input, not the mechanism that keeps scans quick — the probe and deadline
 * fixes do that.
 */
const ANALYSIS_BYTE_BUDGET = 16 * 1024 * 1024;

/**
 * Measured on the raw response, not the parsed document — the whole point is to
 * decide *before* paying cheerio's cost, and a DOM we have already built has
 * already cost us the thing we were trying to avoid.
 */
function withinAnalysisBudget(): (result: FetchResult) => boolean {
  let spent = 0;
  return (result: FetchResult): boolean => {
    const size = result.body?.length ?? 0;
    if (spent + size > ANALYSIS_BYTE_BUDGET) return false;
    spent += size;
    return true;
  };
}

const PROBE_WAVE_SIZE = 4;
const PROBE_WAVE_GAP_MS = 250;
const RATE_LIMIT_BACKOFF_MS = 800;

const pause = (ms: number): Promise<void> => new Promise((resolve) => setTimeout(resolve, ms));

/**
 * Ask for one page as every named crawler, without hammering the server.
 *
 * A probe that comes back rate-limited is retried once after a longer pause,
 * because the first answer told us about our own request rate and nothing about
 * the crawler. If the retry is rate-limited too, the result carries that fact
 * through to the check, which reports "could not test" rather than "blocked".
 */
/**
 * One diagnostic request as a real browser, to tell "down" from "stalling bots".
 *
 * The same idea as the Googlebot control in the bot-access check: a single
 * comparison request that turns a guess into an observation. Only ever sent
 * after a failure, only ever used to classify it.
 */
async function confirmWithBrowserProbe(url: string, deadline: Deadline): Promise<boolean> {
  if (deadline.expired()) return false;
  const probe = await fetchUrl(url, deadline.slice(BROWSER_PROBE_MS), { userAgent: BROWSER_UA, maxBytes: PROBE_BODY_BYTES });
  return probe.ok;
}

const BROWSER_PROBE_MS = 6000;
const BROWSER_UA =
  'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36';

async function probeAsCrawlers(url: string, deadline: Deadline): Promise<FetchResult[]> {
  const bots = [...LIVE_PROBE_BOTS, CONTROL_BOT];
  const results: FetchResult[] = [];

  for (let start = 0; start < bots.length; start += PROBE_WAVE_SIZE) {
    if (deadline.expired()) {
      // Out of time. Record the untested ones as such rather than leaving holes
      // the check would have to guess about.
      for (let i = results.length; i < bots.length; i += 1) {
        results.push({ ...EMPTY_PROBE, requestedUrl: url, finalUrl: url, failure: 'deadline_exceeded' });
      }
      break;
    }

    if (start > 0) await pause(PROBE_WAVE_GAP_MS);
    const wave = bots.slice(start, start + PROBE_WAVE_SIZE);
    const answers = await Promise.all(
      wave.map((bot) => fetchUrl(url, deadline, { userAgent: bot.userAgent, maxBytes: PROBE_BODY_BYTES, timeoutMs: PROBE_TIMEOUT_MS })),
    );
    results.push(...answers);

    /**
     * The whole wave was met with silence, so the next two will be as well.
     *
     * A server that holds four crawler-shaped requests open until they time out
     * is not making per-crawler decisions — it is stalling anything that is not
     * a browser, and we have already established that. nykaa.com spent all
     * seventeen seconds of its network budget proving the same point three
     * times, leaving nothing for the pages that carry the actual content.
     *
     * The remaining crawlers are recorded as untested rather than allowed or
     * blocked, because that is what they are: we chose not to ask.
     */
    const waveStalled = answers.every((answer) => answer.failure === 'timeout' || answer.failure === 'deadline_exceeded');
    if (waveStalled && start + PROBE_WAVE_SIZE < bots.length) {
      for (let i = results.length; i < bots.length; i += 1) {
        results.push({ ...EMPTY_PROBE, requestedUrl: url, finalUrl: url, failure: 'timeout' });
      }
      break;
    }
  }

  const limited = results.map((result, index) => ({ result, index })).filter(({ result }) => result.rateLimited);
  if (limited.length && !deadline.expired()) {
    await pause(RATE_LIMIT_BACKOFF_MS);
    for (const { index } of limited) {
      if (deadline.expired()) break;
      results[index] = await fetchUrl(url, deadline, { userAgent: bots[index].userAgent, maxBytes: PROBE_BODY_BYTES, timeoutMs: PROBE_TIMEOUT_MS });
    }
  }

  return results;
}

const EMPTY_PROBE: FetchResult = {
  requestedUrl: '',
  finalUrl: '',
  status: null,
  ok: false,
  headers: {},
  body: null,
  contentType: null,
  failure: null,
  blocked: false,
  rateLimited: false,
  truncated: false,
  durationMs: 0,
};

function titleFor(checkId: CheckId, siteType: SiteType): string {
  if (checkId === 'structured_data') return `Structured data (${SCHEMA_PROFILES[siteType].label})`;
  if (checkId === 'content_structure') return CONTENT_STRUCTURE_TITLE;
  if (checkId === 'agent_interface') return 'Machine-readable agent interface';
  if (checkId === 'bot_access') return 'AI bot access';
  if (checkId === 'trust_signals') return 'Trust and identity pages';
  if (checkId === 'meta_robots') return 'Search & AI indexability';
  return 'Crawlability & response health';
}

type NoWebsiteReason = 'parked' | 'dns' | 'unreachable' | 'timeout' | 'ssl' | 'redirect_loop';

/**
 * Is there anything here to grade at all?
 *
 * Two shapes of nothing, and the first fix only caught one of them. A parked
 * domain answers 200 with a placeholder; an unregistered one does not answer at
 * all. shubhtanna.com does not resolve — no DNS record anywhere — and we gave it
 * F, 35 out of 100, with a lead-capture form under it. That is a confident
 * judgement about a website that has never existed.
 *
 * The general rule is simply: if no page ever reached us, there is nothing to
 * have an opinion about. Every content check would be skipped anyway, so the
 * only score left comes from the handful that pass trivially — which is exactly
 * how a domain that is not registered scored 35.
 */
export function whyNoWebsite(homepage: HtmlDocument): NoWebsiteReason | null {
  if (looksParked(homepage)) return 'parked';

  switch (homepage.failure) {
    case 'dns':
      return 'dns';
    case 'connection_refused':
      return 'unreachable';
    /**
     * `network` is deliberately absent, and so is `too_large`.
     *
     * `network` is the catch-all for everything we could not classify, so
     * routing it here meant any unrecognised transport hiccup declared a domain
     * nonexistent. That is how mastersunion.org — a live business school serving
     * a 4MB homepage over HTTP 200 — was told "nothing answered at this address".
     * Saying a website does not exist is the strongest claim this tool makes and
     * it should rest on evidence, not on the bucket labelled "something else".
     */
    /**
     * `timeout` is deliberately absent, and this is the expensive lesson.
     *
     * myntra.com answers a browser in 1.0 seconds with 448KB and HTTP 200. It
     * never answers us at all — it holds the connection open and says nothing,
     * which is what a WAF does to an unrecognised bot instead of returning 403.
     * We waited 17 seconds and told one of India's largest retailers that it had
     * no website.
     *
     * A timeout cannot distinguish "this server is down" from "this server is
     * stalling us specifically", and those are opposite findings — the second is
     * the single most valuable thing this tool can report, because the AI
     * crawlers are being stalled the same way and will never mention it either.
     * So we no longer guess: see confirmWithBrowserProbe, which asks.
     */
    /**
     * `deadline_exceeded` is deliberately absent.
     *
     * It means our own scan budget ran out, not that the site failed to answer.
     * Routing it here told healthy sites they did not exist — the honest result
     * is an ordinary scan that comes back marked partial, saying we ran out of
     * time, which is what actually happened.
     */
    case 'ssl':
      return 'ssl';
    case 'redirect_loop':
      return 'redirect_loop';
    default:
      return null;
  }
}

/**
 * What the reader is told, per cause.
 *
 * Each one says what we observed and what it means, because "we could not
 * check your site" is useless on its own — an expired certificate, a domain
 * that was never pointed anywhere and a server that is merely slow are three
 * completely different problems with three different owners.
 */
function explainNoWebsite(domain: string, reason: NoWebsiteReason): string {
  switch (reason) {
    case 'parked':
      return (
        `${domain} does not appear to have a website on it yet. The address answers, but it returns an almost empty page ` +
        'that forwards visitors to a domain-parking holder rather than serving any content. ' +
        'There is nothing here for an AI assistant — or for us — to read, so we have not given it a score.'
      );
    case 'dns':
      return (
        `${domain} does not exist as far as the internet is concerned — there is no DNS record for it at all, so nothing ` +
        'can connect to it, including us and every AI assistant. Check the spelling; if it is right, the domain is either ' +
        'unregistered or has never been pointed at a server. There is nothing to score until it is.'
      );
    case 'unreachable':
      return (
        `Nothing answered at ${domain}. The address is known, but no server accepted the connection — usually a site that ` +
        'is switched off, between hosts, or pointed at a server that is no longer running. Until it responds there is ' +
        'nothing for us to look at.'
      );
    case 'timeout':
      return (
        `${domain} did not respond in time. That can mean the site is down, or simply very slow — which matters in its own ` +
        'right, because assistants work to a deadline and drop sources that arrive late. Try again in a few minutes: if it ' +
        'answers then, we will give you a real report.'
      );
    case 'ssl':
      return (
        `${domain} has a broken or expired security certificate, so nothing could load it — not a browser, not us, and not ` +
        'an AI assistant. This is worth fixing today whatever else is true of the site, because visitors are seeing a ' +
        'security warning instead of your homepage. Your hosting provider can renew it.'
      );
    case 'redirect_loop':
      return (
        `${domain} sends visitors round in a redirect loop — each address forwards to another that forwards back, so no ` +
        'page is ever delivered. Browsers and AI crawlers both give up at that point. It is usually a misconfigured www ' +
        'or https redirect rule, and until it is untangled there is no page here to read.'
      );
  }
}

/**
 * The result for a domain that has no website to grade.
 *
 * Every check is `skipped`, so nothing is scored out of anything and no letter
 * grade is implied. `noWebsite` is what the report reads to replace the whole
 * score panel with an explanation — a D would say "your website has problems"
 * and an F would say "your website is terrible", when the truth is that there
 * is no website to have an opinion about.
 */
function noWebsiteResult(
  submittedUrl: string,
  domain: string,
  homepage: HtmlDocument,
  startedAt: number,
  reason: NoWebsiteReason,
): ScanResult {
  const checks: CheckResult[] = CHECK_ORDER.map((checkId) =>
    scoreCheck(
      {
        checkId,
        title: titleFor(checkId, 'general'),
        status: 'skipped',
        details: 'Not checked — no website could be read at this address.',
        humanExplanation:
          'There is nothing at this address for us to check yet, so we have not scored it.',
        generatedFix: null,
        generatedFixLanguage: null,
        generatedFixTarget: null,
      },
      'general',
    ),
  );

  return {
    domain,
    submittedUrl,
    siteType: 'general',
    siteTypeConfidence: 'low',
    siteTypeEvidence: ['no website could be read at this address'],
    siteTypeOverridden: false,
    overallScore: 0,
    overallGrade: 'F',
    summary: explainNoWebsite(domain, reason),
    checks,
    pagesScanned: [homepage.url],
    pagesDiscovered: 0,
    renderMode: 'empty_shell',
    scanDurationMs: Date.now() - startedAt,
    jsRenderWarning: false,
    partial: false,
    noWebsite: true,
    unreadable: false,
    scoringVersion: SCORING_VERSION,
  };
}

/** Spec section 8: on timeout, report the remainder as unable to verify. */
function unableToVerify(checkId: CheckId, siteType: SiteType): CheckOutcome {
  return {
    checkId,
    title: titleFor(checkId, siteType),
    status: 'skipped',
    details: 'Unable to verify — the scan hit its time limit before this check could complete.',
    humanExplanation:
      'We ran out of time before finishing this check, usually because the site responded slowly. ' +
      'It has been left out of your score rather than counted against you. Re-run the scan in a few minutes and it will normally complete — and if it keeps timing out, that slowness is itself worth looking at, because crawlers give up on slow sites too.',
    generatedFix: null,
    generatedFixLanguage: null,
    generatedFixTarget: null,
  };
}

/** robots.txt served as an HTML page means there is no real robots.txt. */
function looksLikeHtml(response: { contentType: string | null; body: string | null }): boolean {
  if (response.contentType && /text\/html/i.test(response.contentType)) return true;
  return Boolean(response.body && /^\s*<(!doctype|html)/i.test(response.body));
}

const stripSlash = (url: string): string => url.replace(/\/+$/, '');

/**
 * Fetch the homepage, and give a transient failure one second chance.
 *
 * Everything in the report hangs off this one request: if it fails, every
 * content check is skipped and the grade collapses. snitch.com scores C/60 on
 * three consecutive runs and scored F/33 on a fourth, purely because its
 * homepage happened to time out that once — the scan was honest about it, but
 * the visitor still saw an F for a healthy shop, and would have no way of
 * knowing to try again.
 *
 * One retry, on a short leash, and only for failures that are plausibly
 * transient. A refusal is not retried: a server that returns 403 will return
 * 403 again, and asking twice would just be rude. A site that is genuinely down
 * costs us a few extra seconds and still reports as down.
 */
const RETRYABLE_FAILURES = new Set(['timeout', 'network', 'deadline_exceeded']);
const HOMEPAGE_RETRY_MS = 6000;

async function fetchHomepage(href: string, deadline: Deadline): Promise<FetchResult> {
  const first = await fetchWithHttpFallback(href, deadline.slice(HOMEPAGE_BUDGET_MS));
  if (first.ok || !first.failure || !RETRYABLE_FAILURES.has(first.failure) || deadline.expired()) {
    return first;
  }

  const second = await fetchWithHttpFallback(href, deadline.slice(HOMEPAGE_RETRY_MS));
  return second.ok ? second : first;
}

/**
 * What we say when a site would not let us look at it.
 *
 * Deliberately not a verdict. The site is up and serving its customers; we were
 * refused, and the only honest report is what we tried and what happened. The
 * one thing that changes the message is whether an ordinary browser got in
 * where we did not — that is the difference between "your bot protection is
 * broad" and "you are filtering by network, and we are on the wrong network".
 */
function unreadableSummary(domain: string, browserReachable: boolean): string {
  if (browserReachable) {
    return (
      `${domain} is online and loads in a browser, but refused every request we made as a scanner, so we could not check a single thing. ` +
      'That is worth knowing on its own: bot protection set this broadly usually turns away ChatGPT, Claude and Perplexity too, and they will not tell you either. ' +
      'We have given no score, because a grade would be a judgement about pages we never saw.'
    );
  }
  return (
    `${domain} refused every request we made — including one shaped like an ordinary browser — so we could not check anything. ` +
    'That pattern usually means the filtering is by network address rather than by who is asking, in which case your site is working perfectly for your customers and this tells you nothing about how you treat AI crawlers. ' +
    'We have given no score rather than invent one. To find out for certain, ask whoever manages your hosting to search your server logs for GPTBot, ClaudeBot and PerplexityBot over the last month.'
  );
}

/** The scheme-and-host of a URL, or null if it will not parse. */
function originOf(url: string): string | null {
  try {
    return new URL(url).origin;
  } catch {
    return null;
  }
}

function detectSiteName(homepage: HtmlDocument, domain: string): string {
  const ogSiteName = homepage.$?.('meta[property="og:site_name"]').first().attr('content')?.trim();
  if (ogSiteName) return ogSiteName;

  // Page titles are almost always "Brand – tagline" or "Page | Brand"; the
  // longest-lived segment is the first one.
  const title = homepage.title;
  if (title) {
    const segment = title.split(/[|–—·:]/)[0].trim();
    if (segment.length >= 2 && segment.length <= 60) return segment;
  }

  return domain;
}

export async function runScan(submittedUrl: string, options: ScanOptions = {}): Promise<ScanResult> {
  return redact(await runScanInternal(submittedUrl, options));
}
