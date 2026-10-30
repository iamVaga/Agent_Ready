import { privacyLog, redact } from '../utils/privacy';
import { Request, Response } from 'express';
import mongoose from 'mongoose';
import { Scan, ScanDoc } from '../models/Scan';
import { Lead } from '../models/Lead';
import { compareRuns, getScanStats, listRuns, scanUrl, toFullScan, toGatedScan, toTeaser } from '../services/scanService';
import { emailConfigured, sendLeadNotification, sendReportEmail } from '../services/emailService';
import { buildReportHtml } from '../services/reportHtml';
import { HttpError } from '../middleware/errorHandler';
import { SiteType } from '../types';
import { normalizeUrl } from '../utils/url';

async function loadScan(scanId: string): Promise<ScanDoc> {
  if (!mongoose.isValidObjectId(scanId)) {
    throw new HttpError(400, 'That scan link is not valid.');
  }
  const scan = await Scan.findById(scanId).exec();
  if (!scan) throw new HttpError(404, 'We could not find that scan. It may have been a while — please run it again.');
  return scan;
}

const SITE_TYPES: SiteType[] = ['ecommerce', 'content', 'saas', 'local_business', 'general'];

/**
 * An optional site-type override. Detection is a heuristic that reports its own
 * confidence; this is how a visitor corrects it rather than reading a report
 * built on a wrong assumption.
 */
function parseSiteType(value: unknown): SiteType | undefined {
  if (typeof value !== 'string' || !value) return undefined;
  const candidate = value.trim().toLowerCase() as SiteType;
  if (!SITE_TYPES.includes(candidate)) {
    throw new HttpError(400, `"${value}" is not a site type we recognise.`);
  }
  return candidate;
}

/** POST /api/scan */
export async function createScan(req: Request, res: Response): Promise<void> {
  const url = typeof req.body?.url === 'string' ? req.body.url : '';
  const siteType = parseSiteType(req.body?.siteType);
  // Explicit opt-in only — a stray truthy value should not silently spend
  // someone's hourly allowance on a crawl they did not ask for.
  const refresh = req.body?.refresh === true;
  const { scan, cached } = await scanUrl(url, { siteType, refresh });
  res.json({ success: true, data: toTeaser(scan, cached) });
}

/**
 * GET /api/scan/stats — how much this tool has been used.
 *
 * Counts only, nothing about who ran what. Cached server-side because it is
 * read by every homepage visitor, most of whom never scan anything.
 */
export async function getStats(_req: Request, res: Response): Promise<void> {
  res.json({ success: true, data: await getScanStats() });
}

/** GET /api/scan/:scanId — verdicts without the explanations or the fixes. */
export async function getScan(req: Request, res: Response): Promise<void> {
  const scan = await loadScan(req.params.scanId);
  res.json({ success: true, data: toGatedScan(scan) });
}

/** POST /api/scan/:scanId/unlock — the lead-capture gate. */
export async function unlockScan(req: Request, res: Response): Promise<void> {
  const scan = await loadScan(req.params.scanId);

  // Nothing was withheld on a scan that found no website, so there is nothing to
  // unlock. The UI never shows the gate for one of these, but the endpoint has
  // to hold the same line — otherwise a direct call would take someone's details
  // and email them an F and 0 out of 100 for a domain that never answered.
  if (scan.noWebsite) {
    throw new HttpError(400, 'There is no report to unlock — no website could be read at this address.');
  }

  const { name, email, whatsapp, consent, consentText, consentSource } = validateLeadInput(req.body);

  const lead = await Lead.create({
    scanId: scan._id,
    name,
    email,
    whatsapp,
    consent,
    // Stamped server-side. A timestamp the client could set is not evidence.
    consentAt: new Date(),
    consentText: redact(consentText),
    consentSource: redact(consentSource),
    submittedAt: new Date(),
    followUpStatus: 'new',
    domain: scan.domain,
    overallGrade: scan.overallGrade,
    overallScore: scan.overallScore,
    reportEmailed: false,
    emailError: null,
  });

  if (!scan.unlocked) {
    scan.unlocked = true;
    await scan.save();
  }

  // The lead is captured the moment it is saved. Email is a delivery detail —
  // if Resend is down or unconfigured, the visitor still gets their report on
  // screen and we still keep the lead, with the failure recorded on it.
  let emailed = false;
  let emailError: string | null = null;

  if (emailConfigured()) {
    try {
      await sendReportEmail(scan, email);
      emailed = true;
    } catch {
      emailError = 'Report delivery failed';
      privacyLog(`Report email failed for lead ${lead._id}:`, emailError);
    }
  } else {
    emailError = 'RESEND_API_KEY not configured';
  }

  lead.reportEmailed = emailed;
  lead.emailError = emailError;
  await lead.save();

  // Internal notification is best-effort and must not delay the response.
  void sendLeadNotification(scan, lead).catch((error) => {
    privacyLog('Lead notification failed:', error instanceof Error ? error.message : error);
  });

  res.json({
    success: true,
    data: toFullScan(scan),
    meta: { emailed, leadId: String(lead._id) },
  });
}

/**
 * GET /api/scan/:scanId/history — earlier runs of the same domain.
 *
 * Deliberately returns nothing but dates, grades and rule versions. Someone
 * holding a scan id can already re-scan the domain themselves, so the runs
 * themselves are not a secret — but there is no reason for this route to carry
 * findings, explanations or anything about who ran them.
 */
export async function getScanHistory(req: Request, res: Response): Promise<void> {
  const scan = await loadScan(req.params.scanId);
  const runs = await listRuns(scan.domain, String(scan._id));
  res.json({ success: true, data: { domain: scan.domain, current: String(scan._id), runs } });
}

/** GET /api/scan/:scanId/diff/:otherScanId — what moved between two runs. */
export async function diffScans(req: Request, res: Response): Promise<void> {
  const { scanId, otherScanId } = req.params;
  if (!mongoose.isValidObjectId(scanId) || !mongoose.isValidObjectId(otherScanId)) {
    throw new HttpError(400, 'That comparison link is not valid.');
  }
  if (scanId === otherScanId) {
    throw new HttpError(400, 'That is the same run twice — pick a different one to compare against.');
  }

  const comparison = await compareRuns(otherScanId, scanId);
  if (!comparison) throw new HttpError(404, 'We could not find one of those scans. It may have been a while — please run it again.');

  res.json({ success: true, data: comparison });
}

/** GET /api/scan/:scanId/report — the shareable HTML linked from the email. */
export async function getScanReport(req: Request, res: Response): Promise<void> {
  const scan = await loadScan(req.params.scanId);

  // Only scans someone has already unlocked render in full here, otherwise this
  // route would be a way around the lead-capture gate.
  if (!scan.unlocked) {
    throw new HttpError(403, 'This report has not been unlocked yet. Please run the scan and enter your details to see it.');
  }

  res.type('html').send(buildReportHtml(scan));
}

/** POST /api/scan/:scanId/compare — one-off, private, side-by-side comparison. */
export async function compareScan(req: Request, res: Response): Promise<void> {
  const scan = await loadScan(req.params.scanId);
  const competitorUrl = typeof req.body?.competitorUrl === 'string' ? req.body.competitorUrl : '';

  // Comparing a store against itself produces a table of identical columns and
  // would burn a scan from the visitor's hourly allowance for nothing.
  if (normalizeUrl(competitorUrl).domain === scan.domain) {
    throw new HttpError(400, 'That is the same store you just scanned. Paste a competitor\'s URL instead.');
  }

  const { scan: competitor } = await scanUrl(competitorUrl, {});

  scan.comparisonScanId = competitor._id;
  await scan.save();
  competitor.comparisonScanId = scan._id;
  await competitor.save();

  // The comparison respects whatever gate the visitor is already past: if they
  // unlocked their own report, they see both in full; if they have not, the
  // comparison itself becomes the reason to (spec section 3a, feature B).
  const shape = scan.unlocked ? toFullScan : toGatedScan;

  res.json({
    success: true,
    data: { yourScan: shape(scan), competitorScan: shape(competitor) },
  });
}

// --- Validation -----------------------------------------------------------

const EMAIL_PATTERN = /^[^\s@]+@[^\s@]+\.[a-z]{2,}$/i;

interface LeadInput {
  name: string;
  email: string;
  whatsapp: string;
  /** Must be true. Validated, not merely recorded — see validateLeadInput. */
  consent: boolean;
  /** The exact wording shown to the person, stored verbatim. */
  consentText: string;
  /** Which form it came from, so a second capture point stays distinguishable. */
  consentSource: string;
}

/**
 * Used when the client sends consent but not the wording. Should not happen
 * from our own form, which sends both — but a lead with an unknown agreement
 * is worse than one with a recorded fallback.
 */
const DEFAULT_CONSENT_TEXT =
  'Report delivery only. Contact details are used transiently and are not retained by AgentReady.';

function validateLeadInput(body: unknown): LeadInput {
  const source = (body || {}) as Record<string, unknown>;

  const name = String(source.name ?? '').trim();
  const email = String(source.email ?? '').trim().toLowerCase();
  const whatsappRaw = String(source.whatsapp ?? '').trim();

  if (name.length > 100) {
    throw new HttpError(400, 'That name is too long.');
  }
  if (!EMAIL_PATTERN.test(email) || email.length > 200) {
    throw new HttpError(400, 'Please enter a valid email address — that is where your report goes.');
  }

  /**
   * The phone number is optional, and that is a deliberate trade.
   *
   * Demanding it was costing us almost every lead: roughly five people in eighty
   * gave their details, and a phone number is the field most people stop at. An
   * email address we can actually reach is worth more than a phone number we
   * never receive, and someone who wants a call will give a number without being
   * forced to.
   *
   * It is still validated when supplied — a malformed number in the database is
   * worse than an empty one, because it looks like a way to reach someone.
   */
  const whatsapp = whatsappRaw.replace(/[^\d+]/g, '');
  const digits = whatsapp.replace(/\D/g, '');
  if (whatsapp && (digits.length < 8 || digits.length > 15)) {
    throw new HttpError(400, 'That phone number does not look right. Leave it blank if you would rather not share it.');
  }

  /**
   * Consent is enforced here, not just in the form.
   *
   * The checkbox in the UI is the honest place to *ask*, but it is not where
   * the rule can be kept — anything that only lives in the browser can be
   * skipped by calling this endpoint directly. A lead reaching the database
   * without a recorded agreement is one we could never lawfully contact, so it
   * is refused at the boundary instead.
   */
  if (source.consent !== true) {
    throw new HttpError(400, 'Please tick the box to allow report delivery.');
  }

  const consentText = DEFAULT_CONSENT_TEXT;
  const consentSource = 'ai-readiness-gate';

  // An empty name is recorded as such rather than refused. The email is the
  // part we need; everything else is a courtesy the visitor may decline.
  return { name: name || 'Not given', email, whatsapp, consent: true, consentText, consentSource };
}

