import { redact } from '../utils/privacy';
import { Resend } from 'resend';
import { config } from '../config';
import { ScanDoc } from '../models/Scan';
import { buildReportHtml, escapeHtml, reportUrl } from './reportHtml';

/**
 * Report delivery, on Resend — the same provider DealsPouch already sends
 * through, so there is no second email vendor to configure or warm up.
 */

let client: Resend | null = null;

function resend(): Resend {
  if (!config.email.apiKey) {
    throw new Error('RESEND_API_KEY is not configured');
  }
  if (!client) client = new Resend(config.email.apiKey);
  return client;
}

export function emailConfigured(): boolean {
  return Boolean(config.email.apiKey);
}

/**
 * Send the visitor their full report. Throws so the caller can record failure.
 *
 * The report is sent as the whole email body rather than a teaser linking to a
 * hosted copy. There used to be a "View this report in your browser" link
 * appended below it; it is gone deliberately. The entire report already travels
 * in the message, so the link offered nothing but a second copy — and the
 * shareable page it pointed at is the one someone forwards to their developer,
 * which is a different job from reading it yourself.
 */
export async function sendReportEmail(scan: ScanDoc, recipient: string): Promise<void> {
  const response = await resend().emails.send({
    from: config.email.from,
    to: recipient,
    subject: `Your site scored ${scan.overallGrade} for AI readiness — ${scan.domain}`,
    html: buildReportHtml(scan),
  });

  if (response.error) {
    throw new Error(`Resend API error: ${response.error.message}`);
  }
}

/**
 * Internal copy of a captured lead. Best-effort: a failure here must never
 * affect the visitor's response, so callers should not await-and-throw on it.
 */
export async function sendLeadNotification(scan: ScanDoc, _lead: unknown): Promise<void> {
  if (!config.email.leadNotify || !emailConfigured()) return;

  const rows = redact([
    ['Store', scan.domain],
    ['Grade', `${scan.overallGrade} (${scan.overallScore}/100)`],
    ['Failed checks', scan.checks.filter((check) => check.status === 'fail').map((check) => check.title).join(', ') || 'none'],
    ['Report', reportUrl(String(scan._id))],
  ]);

  await resend().emails.send({
    from: config.email.from,
    to: config.email.leadNotify,
    subject: `New AgentReady report — ${scan.domain} (${scan.overallGrade})`,
    html: `<table cellpadding="6" style="font-family:sans-serif;font-size:14px;border-collapse:collapse;">${rows
      .map(
        ([label, value]) =>
          `<tr><td style="color:#64748b;">${escapeHtml(label)}</td><td><strong>${escapeHtml(String(value))}</strong></td></tr>`,
      )
      .join('')}</table>`,
  });
}
