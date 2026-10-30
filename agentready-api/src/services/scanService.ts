import { redact } from '../utils/privacy';
import { config } from '../config';
import { CheckId, CheckStatus, SiteType } from '../types';
import { Scan, ScanDoc } from '../models/Scan';
import { runScan } from './scanEngine';
import { SCAN_CEILING, SCORING_VERSION, countBlockers, weightsFor } from './scoring';
import { normalizeUrl } from '../utils/url';

/**
 * Persistence and caching around the scan engine.
 *
 * The cache does double duty: it keeps a casual visitor from re-scanning the
 * same store on every page refresh, and it stops the tool being used as a free
 * crawling proxy for someone else's site.
 *
 * One hour, not six. Six was long enough that someone could fix their site and
 * still be shown the old report at the end of the working day — the fix-then-
 * check-again loop is the whole point of this tool, and the cache was breaking
 * it. An hour still absorbs the repeat traffic the cache exists for, and the
 * report now both states the window and offers a button past it.
 */

export interface ScanOutcome {
  scan: ScanDoc;
  /** True when this came from the cache rather than a fresh crawl. */
  cached: boolean;
}

export interface ScanRequestOptions {
  siteType?: SiteType;
  /**
   * Ignore any stored result and crawl again.
   *
   * The cache exists to stop a site being hammered by many visitors and to stop
   * this tool being used as a free crawler — neither of which describes the one
   * person who just fixed something and wants to know whether it worked. That
   * person was being shown their own stale report with no way past it, which is
   * the exact moment the product is most useful and was least useful.
   *
   * It is not a loophole: a forced scan still spends one of the caller's hourly
   * allowance, so nobody gets more crawls than they could already get.
   */
  refresh?: boolean;
}

export async function scanUrl(submittedUrl: string, options: ScanRequestOptions = {}): Promise<ScanOutcome> {
  const { siteType, refresh } = options;
  const { domain } = normalizeUrl(submittedUrl);

  if (!refresh) {
    const cached = await findCachedScan(domain, siteType);
    if (cached) return { scan: cached, cached: true };
  }

  const result = await runScan(submittedUrl, { siteType });

  const scan = await Scan.create({
    domain: result.domain,
    submittedUrl: result.submittedUrl,
    scannedAt: new Date(),
    siteType: result.siteType,
    siteTypeConfidence: result.siteTypeConfidence,
    siteTypeEvidence: result.siteTypeEvidence,
    siteTypeOverridden: result.siteTypeOverridden,
    overallScore: result.overallScore,
    overallGrade: result.overallGrade,
    summary: result.summary,
    checks: result.checks,
    pagesScanned: result.pagesScanned,
    pagesDiscovered: result.pagesDiscovered,
    renderMode: result.renderMode,
    scanDurationMs: result.scanDurationMs,
    jsRenderWarning: result.jsRenderWarning,
    partial: result.partial,
    noWebsite: result.noWebsite,
    unreadable: result.unreadable,
    scoringVersion: result.scoringVersion,
    comparisonScanId: null,
    unlocked: false,
  });

  return { scan, cached: false };
}

async function findCachedScan(domain: string, siteType?: SiteType): Promise<ScanDoc | null> {
  const cutoff = new Date(Date.now() - config.scanner.cacheHours * 60 * 60 * 1000);

  const query: Record<string, unknown> = {
    domain,
    scannedAt: { $gte: cutoff },
    // A partial scan is not worth reusing at all — the next visitor deserves a
    // real attempt, so only complete scans are cacheable.
    partial: false,
    // Scores are only comparable within one version of the rules, so a scan
    // made under older weights must not be served as if it were current.
    scoringVersion: SCORING_VERSION,
  };

  // A detected scan and an overridden one are different judgements of the same
  // site, so neither may be served in place of the other. Without this, someone
  // pasting a URL cold could be handed a stranger's override — and read a
  // report that opens "you told us this is a SaaS product" when they said no
  // such thing.
  if (siteType) query.siteType = siteType;
  else query.siteTypeOverridden = false;

  return Scan.findOne(query).sort({ scannedAt: -1 }).exec();
}

// --- Public counters ------------------------------------------------------

export interface ScanStats {
  /** Every scan ever run, including repeats of the same domain. */
  totalScans: number;
  /** Distinct domains — the more honest "how many sites have been checked". */
  sitesChecked: number;
}

/**
 * Cached for a minute.
 *
 * This is read on every homepage load by visitors who are not scanning
 * anything, so it must not put a `countDocuments` and a `distinct` on the
 * database each time. A counter that is up to sixty seconds stale is
 * indistinguishable from a live one to the person reading it.
 */
let statsCache: { value: ScanStats; expiresAt: number } | null = null;
const STATS_TTL_MS = 60_000;

export async function getScanStats(): Promise<ScanStats> {
  if (statsCache && statsCache.expiresAt > Date.now()) return statsCache.value;

  // `sitesChecked` is rendered as "N websites checked so far". A domain that
  // turned out to have no website on it is the one thing that cannot be counted
  // there without making the sentence false, so it is excluded — the scan still
  // counts in `totalScans`, because a scan did genuinely happen.
  const [totalScans, domains] = await Promise.all([
    Scan.estimatedDocumentCount(),
    // `$ne: true`, not `false`. Every scan stored before this field existed has
    // no `noWebsite` key at all — a schema default only applies to new
    // documents — so an equality match silently excluded the entire history and
    // took the public counter to zero.
    Scan.distinct('domain', { noWebsite: { $ne: true } }),
  ]);

  const value: ScanStats = { totalScans, sitesChecked: domains.length };
  statsCache = { value, expiresAt: Date.now() + STATS_TTL_MS };
  return value;
}

// --- History and run comparison -------------------------------------------
//
// Nothing new is measured here. Every scan has always been stored with its
// timestamp and the version of the rules that produced it; this reads back what
// is already there.
//
// The value is in the one question a single scan can never answer: *when did
// this break?* A theme update or a new security plugin can shut AI crawlers out
// on a Tuesday, and nothing tells the owner — the site still looks fine, and
// crawlers do not file complaints. Two runs side by side is how that surfaces
// in days instead of at the next quarterly review.

/** How many past runs of a domain we will show. */
const HISTORY_LIMIT = 12;

export interface RunSummary {
  scanId: string;
  scannedAt: Date;
  overallGrade: string;
  overallScore: number;
  siteType: SiteType;
  scoringVersion: string;
  partial: boolean;
  /**
   * True when this run found no website at all.
   *
   * These are stored with grade F and score 0 for want of anything else to put
   * in the columns, and the history list was rendering that as a red F — so a
   * site that was down for ten minutes appeared in its own timeline as having
   * scored zero. The flag exists so the list can say "not reachable" instead of
   * showing a grade the scan explicitly refused to give.
   */
  noWebsite: boolean;
}

const toRunSummary = (scan: ScanDoc): RunSummary => ({
  scanId: String(scan._id),
  scannedAt: scan.scannedAt,
  overallGrade: scan.overallGrade,
  overallScore: scan.overallScore,
  siteType: scan.siteType,
  scoringVersion: scan.scoringVersion || 'unknown',
  partial: scan.partial,
  noWebsite: scan.noWebsite,
});

/** Past runs of the same domain, newest first, excluding the one in hand. */
export async function listRuns(domain: string, excludeScanId?: string): Promise<RunSummary[]> {
  const runs = await Scan.find({ domain }).sort({ scannedAt: -1 }).limit(HISTORY_LIMIT + 1).exec();
  return runs.filter((run) => String(run._id) !== excludeScanId).slice(0, HISTORY_LIMIT).map(toRunSummary);
}

export type CheckChange = 'improved' | 'regressed' | 'unchanged' | 'appeared' | 'disappeared';

export interface CheckDiff {
  checkId: CheckId;
  title: string;
  before: { status: CheckStatus; pointsAwarded: number; pointsPossible: number } | null;
  after: { status: CheckStatus; pointsAwarded: number; pointsPossible: number } | null;
  change: CheckChange;
}

/**
 * Rank a status so a move between two of them has a direction.
 *
 * `skipped` sits deliberately outside this order rather than at the bottom. It
 * means "we could not look", which is not a worse result than a failure — it is
 * a different kind of statement, and calling a fail-to-skipped move an
 * improvement would be a lie about progress the site has not made.
 */
const STATUS_RANK: Record<CheckStatus, number> = { fail: 0, warning: 1, pass: 2, skipped: -1 };

function changeOf(before: CheckStatus | null, after: CheckStatus | null): CheckChange {
  if (!before) return 'appeared';
  if (!after) return 'disappeared';
  if (before === after) return 'unchanged';
  if (before === 'skipped' || after === 'skipped') return 'unchanged';
  return STATUS_RANK[after] > STATUS_RANK[before] ? 'improved' : 'regressed';
}

export interface RunComparison {
  domain: string;
  before: RunSummary;
  after: RunSummary;
  /**
   * False when the two runs were produced by different rules or judged as
   * different kinds of site.
   *
   * This is the honest half of the feature. A score is only meaningful inside
   * one version of the weights — we have changed them five times — so showing a
   * confident "+12" across a version boundary would be inventing progress out
   * of our own edits. Per-check verdicts still survive the comparison and are
   * shown either way; only the number is withheld.
   */
  comparable: boolean;
  incomparableReason: string | null;
  scoreDelta: number | null;
  checks: CheckDiff[];
}

export async function compareRuns(beforeId: string, afterId: string): Promise<RunComparison | null> {
  const [a, b] = await Promise.all([Scan.findById(beforeId).exec(), Scan.findById(afterId).exec()]);
  if (!a || !b) return null;

  // Order by time rather than trusting the caller, so "before" always means
  // earlier no matter which way round the two ids arrived.
  const [before, after] = a.scannedAt <= b.scannedAt ? [a, b] : [b, a];

  // One of the two runs never produced a score, so there is no comparison to
  // make. Left to itself this returned "+81 points" for a site that had simply
  // been offline, and a check list where everything moved from pass to skipped
  // ranked as "unchanged" — a confident report of progress that never happened.
  if (before.noWebsite || after.noWebsite) {
    const dead = before.noWebsite ? before : after;
    return {
      domain: before.domain,
      before: toRunSummary(before),
      after: toRunSummary(after),
      comparable: false,
      incomparableReason:
        `No website could be read on ${dead.scannedAt.toLocaleDateString('en-GB', { day: 'numeric', month: 'short', year: 'numeric' })}, ` +
        'so that run has nothing in it to compare. Pick a run where the site actually answered.',
      scoreDelta: null,
      checks: [],
    };
  }

  const sameVersion = before.scoringVersion === after.scoringVersion;
  const sameType = before.siteType === after.siteType;

  const incomparableReason = !sameVersion
    ? `These runs were scored by different rule versions (${before.scoringVersion || 'unknown'} then ${
        after.scoringVersion || 'unknown'
      }), so the change in score reflects our updated weights as well as your site. The per-check results below are still directly comparable.`
    : !sameType
      ? `The first run was judged as a ${before.siteType.replace('_', ' ')} and the second as a ${after.siteType.replace(
          '_',
          ' ',
        )}, which means different checks carried different weight. Compare the individual results rather than the score.`
      : null;

  const comparable = !incomparableReason;

  const ids = [...new Set([...before.checks.map((c) => c.checkId), ...after.checks.map((c) => c.checkId)])];
  const checks: CheckDiff[] = ids.map((checkId) => {
    const from = before.checks.find((c) => c.checkId === checkId) || null;
    const to = after.checks.find((c) => c.checkId === checkId) || null;
    return {
      checkId,
      title: (to || from)!.title,
      before: from ? { status: from.status, pointsAwarded: from.pointsAwarded, pointsPossible: from.pointsPossible } : null,
      after: to ? { status: to.status, pointsAwarded: to.pointsAwarded, pointsPossible: to.pointsPossible } : null,
      change: changeOf(from?.status ?? null, to?.status ?? null),
    };
  });

  // Worst news first: a regression is the reason someone opened this.
  const order: Record<CheckChange, number> = { regressed: 0, improved: 1, appeared: 2, disappeared: 3, unchanged: 4 };
  checks.sort((x, y) => order[x.change] - order[y.change]);

  return {
    domain: after.domain,
    before: toRunSummary(before),
    after: toRunSummary(after),
    comparable,
    incomparableReason,
    scoreDelta: comparable ? after.overallScore - before.overallScore : null,
    checks,
  };
}

// --- Response shaping -----------------------------------------------------
//
// Three views of the same scan. The gate is only real if the locked fields never
// leave the server, so the stripping happens here rather than in the client —
// a field the browser is told to hide is not hidden.
//
// What is behind the gate changed: it used to be five of seven checks and every
// explanation, and about five visitors in eighty were willing to pay for that.
// Now it is the generated fix code and nothing else. The verdict and the
// reasoning are free, because a report that says "something is wrong" without
// saying what is not worth an email address — and the code to fix it is.

/**
 * The facts a reader needs to judge whether to believe the number: which rules
 * produced it, what each check was worth, how much of the site was looked at,
 * and how many of the failures are matters of fact rather than of weighting.
 *
 * Shipped with the gated response too. Withholding the methodology behind a
 * score would undercut the one thing that makes it trustworthy.
 */
function auditTrail(scan: ScanDoc): Record<string, unknown> {
  return {
    scoringVersion: scan.scoringVersion || 'unknown',
    weights: weightsFor(scan.siteType),
    blockingIssues: countBlockers(scan.checks),
    pagesChecked: scan.pagesScanned.length,
    pagesDiscovered: Math.max(scan.pagesDiscovered, scan.pagesScanned.length - 1),
    renderMode: scan.renderMode,
    siteTypeOverridden: scan.siteTypeOverridden,
    // Stated rather than implied. A reader who does not know results are reused
    // cannot tell a stale report from a fresh one, and would have no reason to
    // look for the button that forces a new crawl.
    cacheHours: config.scanner.cacheHours,
    method:
      'Static HTML only — no JavaScript is executed. Checks that could not be verified are excluded from the score rather than counted as zero, ' +
      `and a scan cannot award more than ${SCAN_CEILING} of 100: the remaining points belong to what a crawl cannot see — whether an assistant actually cites you, ` +
      'content that only appears once JavaScript runs, the pages beyond the handful we sample, and the further signals this phase does not test. ' +
      'When part of the assessment could not run at all, the grade is capped at B, because an A would claim we had looked at everything.',
  };
}

function toFullScanInternal(scan: ScanDoc): Record<string, unknown> {
  return {
    scanId: String(scan._id),
    domain: scan.domain,
    submittedUrl: scan.submittedUrl,
    scannedAt: scan.scannedAt,
    siteType: scan.siteType,
    siteTypeConfidence: scan.siteTypeConfidence,
    siteTypeEvidence: scan.siteTypeEvidence,
    overallScore: scan.overallScore,
    overallGrade: scan.overallGrade,
    summary: scan.summary,
    checks: scan.checks.map((check) => ({
      checkId: check.checkId,
      title: check.title,
      status: check.status,
      pointsAwarded: check.pointsAwarded,
      pointsPossible: check.pointsPossible,
      details: check.details,
      humanExplanation: check.humanExplanation,
      generatedFix: check.generatedFix,
      generatedFixLanguage: check.generatedFixLanguage,
      generatedFixTarget: check.generatedFixTarget,
      agentAccess: check.agentAccess,
      locked: false,
    })),
    pagesScanned: scan.pagesScanned,
    scanDurationMs: scan.scanDurationMs,
    jsRenderWarning: scan.jsRenderWarning,
    partial: scan.partial,
    noWebsite: scan.noWebsite,
    unreadable: scan.unreadable,
    audit: auditTrail(scan),
    comparisonScanId: scan.comparisonScanId ? String(scan.comparisonScanId) : null,
    unlocked: true,
  };
}

/** Every check and its verdict, but no explanations and no generated fixes. */
function toGatedScanInternal(scan: ScanDoc): Record<string, unknown> {
  return {
    scanId: String(scan._id),
    domain: scan.domain,
    submittedUrl: scan.submittedUrl,
    scannedAt: scan.scannedAt,
    siteType: scan.siteType,
    siteTypeConfidence: scan.siteTypeConfidence,
    siteTypeEvidence: scan.siteTypeEvidence,
    overallScore: scan.overallScore,
    overallGrade: scan.overallGrade,
    summary: scan.summary,
    checks: scan.checks.map((check) => ({
      checkId: check.checkId,
      title: check.title,
      status: check.status,
      pointsAwarded: check.pointsAwarded,
      pointsPossible: check.pointsPossible,
      details: check.details,
      humanExplanation: null,
      generatedFix: null,
      generatedFixLanguage: check.generatedFixLanguage,
      generatedFixTarget: null,
      // Factual, not advisory: this is the evidence behind the verdict, so it
      // stays visible before the gate. Withholding it would make the one
      // checkable claim in the report unverifiable.
      agentAccess: check.agentAccess,
      locked: true,
    })),
    pagesScanned: scan.pagesScanned,
    scanDurationMs: scan.scanDurationMs,
    jsRenderWarning: scan.jsRenderWarning,
    partial: scan.partial,
    noWebsite: scan.noWebsite,
    unreadable: scan.unreadable,
    audit: auditTrail(scan),
    comparisonScanId: scan.comparisonScanId ? String(scan.comparisonScanId) : null,
    unlocked: false,
    fixesAvailable: countFixes(scan),
  };
}

/** Grade, summary and the first two checks — what a visitor sees for free. */
function toTeaserInternal(scan: ScanDoc, cached: boolean): Record<string, unknown> {
  return {
    scanId: String(scan._id),
    domain: scan.domain,
    // Shown in the teaser: "we scanned this as an online store" is the first
    // thing a visitor sanity-checks, and getting it visibly wrong is better
    // than getting it invisibly wrong.
    siteType: scan.siteType,
    siteTypeConfidence: scan.siteTypeConfidence,
    siteTypeOverridden: scan.siteTypeOverridden,
    audit: auditTrail(scan),
    overallGrade: scan.overallGrade,
    overallScore: scan.overallScore,
    summary: scan.summary,
    /**
     * Every check, every explanation. Only the code is held back.
     *
     * The gate used to sit in front of the whole report — two checks visible,
     * five hidden, no explanations — and roughly five visitors in eighty went
     * through it. That is not a conversion problem so much as a bad offer: we
     * were asking for contact details in exchange for reading the rest of a
     * verdict we had already started telling them.
     *
     * The trade now matches what people actually want. Findings and reasoning
     * are free, because knowing your site is broken is worthless if you cannot
     * see what is broken. The ready-to-paste fix is what someone will give an
     * email address for, so that is the only thing behind the form.
     */
    checks: scan.checks.map((check) => ({
      checkId: check.checkId,
      title: check.title,
      status: check.status,
      pointsAwarded: check.pointsAwarded,
      pointsPossible: check.pointsPossible,
      details: check.details,
      humanExplanation: check.humanExplanation,
      generatedFix: null,
      generatedFixLanguage: check.generatedFixLanguage,
      // Named even while withheld: "the fix goes in your robots.txt" tells
      // someone what they are being offered, which is the whole point of
      // showing a locked block rather than hiding that one exists.
      generatedFixTarget: check.generatedFixTarget,
      agentAccess: check.agentAccess,
      locked: Boolean(check.generatedFix),
    })),
    /**
     * The old name, kept deliberately for one release.
     *
     * Renaming `teaserChecks` to `checks` broke every browser that had already
     * loaded the previous bundle: it read `teaser.checks`, got undefined, and
     * white-screened on `.map`. The same is true in reverse for anyone holding
     * a cached copy of the old frontend right now.
     *
     * Two views of the same array, so neither side can be caught out. Delete it
     * once the deployed frontend has been on `checks` long enough that no cached
     * bundle is still asking — there is no rush, it costs one key.
     */
    teaserChecks: scan.checks.map((check) => ({
      checkId: check.checkId,
      title: check.title,
      status: check.status,
      details: check.details,
      agentAccess: check.agentAccess,
      locked: Boolean(check.generatedFix),
    })),
    lockedChecks: countFixes(scan),
    // Named in the teaser on purpose: "we generated the code to fix this" is
    // the strongest reason a visitor has to hand over an email (spec 3a).
    fixesAvailable: countFixes(scan),
    jsRenderWarning: scan.jsRenderWarning,
    partial: scan.partial,
    noWebsite: scan.noWebsite,
    unreadable: scan.unreadable,
    scanDurationMs: scan.scanDurationMs,
    cached,
  };
}

export function countFixes(scan: ScanDoc): number {
  return scan.checks.filter((check) => Boolean(check.generatedFix)).length;
}

export function toFullScan(scan: ScanDoc): Record<string, unknown> { return redact(JSON.parse(JSON.stringify(toFullScanInternal(scan)))); }
export function toGatedScan(scan: ScanDoc): Record<string, unknown> { return redact(JSON.parse(JSON.stringify(toGatedScanInternal(scan)))); }
export function toTeaser(scan: ScanDoc, cached = false): Record<string, unknown> { return redact(JSON.parse(JSON.stringify(toTeaserInternal(scan, cached)))); }
