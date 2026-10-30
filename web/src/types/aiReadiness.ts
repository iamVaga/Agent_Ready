/**
 * Response shapes from the AI Readiness API (agentready-api/).
 *
 * Hand-written rather than shared through a package: the API deploys
 * separately, and a type that drifts is easier to spot in one short file than
 * inside a build step.
 */

export type CheckId =
  | 'bot_access'
  | 'agent_interface'
  | 'structured_data'
  | 'content_structure'
  | 'trust_signals'
  | 'meta_robots'
  | 'crawlability';

export type CheckStatus = 'pass' | 'warning' | 'fail' | 'skipped';
export type Grade = 'A' | 'B' | 'C' | 'D' | 'F';
export type SiteType = 'ecommerce' | 'content' | 'saas' | 'local_business' | 'general';
export type Confidence = 'high' | 'medium' | 'low';
export type FixLanguage = 'robots' | 'json' | 'html' | 'markdown';
export type RenderMode = 'server_rendered' | 'payload_only' | 'empty_shell';

/**
 * Everything a reader needs to judge whether to believe the score — the weight
 * table, the rules version, how much of the site was looked at. Shipped even
 * with the gated response, because withholding the method would undercut the
 * one thing that makes a number trustworthy.
 */
export interface AuditTrail {
  scoringVersion: string;
  weights: Record<CheckId, number>;
  /** Failures that are matters of fact, not of weighting. */
  blockingIssues: number;
  pagesChecked: number;
  pagesDiscovered: number;
  renderMode: RenderMode;
  siteTypeOverridden: boolean;
  /** How long a result is reused before we crawl again, in hours. */
  cacheHours: number;
  method: string;
}

export type AgentAccessStatus = 'allowed' | 'blocked_robots' | 'blocked_server';

/**
 * One crawler's verdict, with how it was reached.
 *
 * `liveTested` is the important column. "robots.txt allows GPTBot" is a reading
 * of a file; "the server returned 200 to a request from GPTBot" is an
 * observation. Showing which one produced each row is what makes this table
 * checkable against someone's own access logs.
 */
export interface AgentAccessRow {
  agent: string;
  label: string;
  status: AgentAccessStatus;
  liveTested: boolean;
  detail: string | null;
}

export interface ScanCheck {
  checkId: CheckId;
  title: string;
  status: CheckStatus;
  pointsAwarded: number;
  pointsPossible: number;
  details: string;
  /** null until the report is unlocked. */
  humanExplanation: string | null;
  generatedFix: string | null;
  generatedFixLanguage: FixLanguage | null;
  /** Exactly where the block goes — a file path, or a place in the page. */
  generatedFixTarget: string | null;
  /** Bot-access check only. Shipped before the gate — it is evidence, not advice. */
  agentAccess?: AgentAccessRow[];
  locked: boolean;
}

export interface TeaserScan {
  scanId: string;
  domain: string;
  siteType: SiteType;
  siteTypeConfidence: Confidence;
  siteTypeOverridden: boolean;
  audit: AuditTrail;
  overallGrade: Grade;
  overallScore: number;
  summary: string;
  /**
   * Every check, with its reasoning. `generatedFix` is null until unlocked.
   *
   * Was `teaserChecks` and carried two of seven. The rename is the point: this
   * is the report now, not a sample of it, and calling it a teaser would keep
   * inviting code that treats it as partial.
   */
  checks: ScanCheck[];
  /** How many checks have a fix waiting behind the form. */
  lockedChecks: number;
  fixesAvailable: number;
  jsRenderWarning: boolean;
  partial: boolean;
  /** No website could be read at this address — see NoGradeNotice. */
  noWebsite: boolean;
  /** The site answered but refused everything, so no check could run. */
  unreadable: boolean;
  scanDurationMs: number;
  cached: boolean;
}

export interface FullScan {
  scanId: string;
  domain: string;
  submittedUrl: string;
  scannedAt: string;
  siteType: SiteType;
  siteTypeConfidence: Confidence;
  siteTypeEvidence: string[];
  siteTypeOverridden: boolean;
  audit: AuditTrail;
  overallScore: number;
  overallGrade: Grade;
  summary: string;
  checks: ScanCheck[];
  pagesScanned: string[];
  scanDurationMs: number;
  jsRenderWarning: boolean;
  partial: boolean;
  noWebsite: boolean;
  unreadable: boolean;
  comparisonScanId: string | null;
  unlocked: boolean;
  fixesAvailable?: number;
}

/**
 * The exact wording a visitor agrees to, defined once.
 *
 * Rendered as the checkbox label *and* sent to the API to be stored verbatim
 * with the lead. Keeping one constant is what makes those two identical — a
 * consent record that does not match what was on screen evidences nothing, and
 * wording drifts the moment the two live in separate files.
 */
/**
 * Both reasons a scan comes back without a grade.
 *
 * Derived in one place on purpose. Two booleans that each mean "do not show a
 * score" is exactly the shape of thing where one gets checked and the other
 * forgotten, and the cost of forgetting is publishing F/0 about a website
 * nobody could see.
 */
export const isUngraded = (scan: { noWebsite: boolean; unreadable: boolean }): boolean =>
  scan.noWebsite || scan.unreadable;

export const CONSENT_TEXT =
  'I agree to use my email for this report delivery. AgentReady does not retain my contact details or use them for follow-up. The email provider processes my address to deliver the report.';

/** Which form the consent came from, so a second capture point stays distinct. */
export const CONSENT_SOURCE = 'ai-readiness-gate';

export interface LeadInput {
  name: string;
  email: string;
  whatsapp: string;
  /** DPDP Act 2023 requires a clear affirmative action before we may contact them. */
  consent: boolean;
  consentText: string;
  consentSource: string;
}

export interface UnlockResult {
  scan: FullScan;
  emailed: boolean;
}

/** One past scan of the same domain, as listed in the history panel. */
export interface RunSummary {
  scanId: string;
  scannedAt: string;
  overallGrade: Grade;
  overallScore: number;
  siteType: SiteType;
  scoringVersion: string;
  partial: boolean;
  /** True when this run found no website — show "not reachable", not a grade. */
  noWebsite: boolean;
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
 * Two runs of the same site held against each other.
 *
 * `comparable` is false when the runs came from different scoring rules or a
 * different site-type judgement. The per-check verdicts survive that; only the
 * score difference is withheld, because a number that mixes our weight changes
 * with the site's actual changes is worse than no number.
 */
export interface RunComparison {
  domain: string;
  before: RunSummary;
  after: RunSummary;
  comparable: boolean;
  incomparableReason: string | null;
  scoreDelta: number | null;
  checks: CheckDiff[];
}

export interface ScanStats {
  /** Every scan ever run, including repeats of the same domain. */
  totalScans: number;
  /** Distinct domains — the honest "how many sites have been checked". */
  sitesChecked: number;
}

export interface ScanHistory {
  domain: string;
  current: string;
  runs: RunSummary[];
}

export interface ComparisonResult {
  yourScan: FullScan;
  competitorScan: FullScan;
}

export const SITE_TYPE_LABEL: Record<SiteType, string> = {
  ecommerce: 'online store',
  content: 'content or publishing site',
  saas: 'software or SaaS product',
  local_business: 'local business',
  general: 'website',
};

export const GRADE_COLOUR: Record<Grade, string> = {
  A: '#00E676',
  B: '#00F5D4',
  C: '#FFD600',
  D: '#FF9100',
  F: '#FF5252',
};

export const STATUS_STYLE: Record<CheckStatus, { label: string; text: string; bg: string; ring: string }> = {
  pass: { label: 'Pass', text: 'text-success', bg: 'bg-success/10', ring: 'ring-success/30' },
  warning: { label: 'Needs work', text: 'text-warning', bg: 'bg-warning/10', ring: 'ring-warning/30' },
  fail: { label: 'Fail', text: 'text-error', bg: 'bg-error/10', ring: 'ring-error/30' },
  skipped: { label: 'Not scored', text: 'text-neutral-400', bg: 'bg-white/5', ring: 'ring-white/10' },
};
