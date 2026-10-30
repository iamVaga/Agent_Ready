import { redact } from '../utils/privacy';
import mongoose, { Document, Schema, Types } from 'mongoose';
import { AgentAccessRow, CheckId, CheckStatus, Confidence, FixLanguage, Grade, RenderMode, SiteType } from '../types';

export interface ScanCheckDoc {
  checkId: CheckId;
  title: string;
  status: CheckStatus;
  pointsAwarded: number;
  pointsPossible: number;
  details: string;
  humanExplanation: string;
  generatedFix: string | null;
  generatedFixLanguage: FixLanguage | null;
  generatedFixTarget: string | null;
  agentAccess?: AgentAccessRow[];
}

export interface ScanDoc extends Document {
  _id: Types.ObjectId;
  domain: string;
  submittedUrl: string;
  scannedAt: Date;
  /** What kind of site this was judged as — decides weights and expectations. */
  siteType: SiteType;
  siteTypeConfidence: Confidence;
  siteTypeEvidence: string[];
  /** True when the visitor chose the type rather than us detecting it. */
  siteTypeOverridden: boolean;
  overallScore: number;
  overallGrade: Grade;
  summary: string;
  checks: ScanCheckDoc[];
  pagesScanned: string[];
  /** How many pages were worth checking, before the sample was capped. */
  pagesDiscovered: number;
  renderMode: RenderMode;
  scanDurationMs: number;
  jsRenderWarning: boolean;
  /** True when the scan deadline cut it short and some checks are unverified. */
  partial: boolean;
  /** No website could be read at this address — see whyNoWebsite in scanEngine. */
  noWebsite: boolean;
  /** The site answered but refused every request, so nothing could be checked. */
  unreadable: boolean;
  /** Which version of the scoring rules produced overallScore. */
  scoringVersion: string;
  comparisonScanId: Types.ObjectId | null;
  /** True once a lead has unlocked this scan — gates the full report. */
  unlocked: boolean;
}

const checkSchema = new Schema<ScanCheckDoc>(
  {
    checkId: {
      type: String,
      required: true,
      enum: [
        'bot_access',
        'agent_interface',
        'structured_data',
        'content_structure',
        'trust_signals',
        'meta_robots',
        'crawlability',
      ],
    },
    title: { type: String, required: true },
    status: { type: String, required: true, enum: ['pass', 'warning', 'fail', 'skipped'] },
    pointsAwarded: { type: Number, required: true },
    pointsPossible: { type: Number, required: true },
    details: { type: String, default: '' },
    humanExplanation: { type: String, default: '' },
    generatedFix: { type: String, default: null },
    generatedFixLanguage: { type: String, enum: ['robots', 'json', 'html', 'markdown', null], default: null },
    generatedFixTarget: { type: String, default: null },
    // Only the bot-access check populates this. Stored so a saved report shows
    // the same per-crawler evidence the live scan did.
    agentAccess: {
      type: [
        new Schema(
          {
            agent: { type: String, required: true },
            label: { type: String, required: true },
            status: { type: String, required: true, enum: ['allowed', 'blocked_robots', 'blocked_server'] },
            liveTested: { type: Boolean, default: false },
            detail: { type: String, default: null },
          },
          { _id: false },
        ),
      ],
      default: undefined,
    },
  },
  { _id: false },
);

const scanSchema = new Schema<ScanDoc>({
  domain: { type: String, required: true, index: true },
  submittedUrl: { type: String, required: true },
  scannedAt: { type: Date, default: Date.now },
  siteType: {
    type: String,
    required: true,
    enum: ['ecommerce', 'content', 'saas', 'local_business', 'general'],
    default: 'general',
    index: true,
  },
  siteTypeConfidence: { type: String, enum: ['high', 'medium', 'low'], default: 'low' },
  siteTypeEvidence: { type: [String], default: [] },
  siteTypeOverridden: { type: Boolean, default: false },
  // The raw number is stored alongside the grade so before/after comparisons
  // can show real movement — a store going 41 → 58 is progress that the letter
  // grade alone hides.
  overallScore: { type: Number, required: true },
  overallGrade: { type: String, required: true, enum: ['A', 'B', 'C', 'D', 'F'] },
  summary: { type: String, default: '' },
  checks: { type: [checkSchema], default: [] },
  pagesScanned: { type: [String], default: [] },
  pagesDiscovered: { type: Number, default: 0 },
  renderMode: {
    type: String,
    enum: ['server_rendered', 'payload_only', 'empty_shell'],
    default: 'server_rendered',
  },
  scanDurationMs: { type: Number, default: 0 },
  jsRenderWarning: { type: Boolean, default: false },
  partial: { type: Boolean, default: false },
  noWebsite: { type: Boolean, default: false },
  unreadable: { type: Boolean, default: false },
  scoringVersion: { type: String, default: '' },
  comparisonScanId: { type: Schema.Types.ObjectId, ref: 'Scan', default: null },
  unlocked: { type: Boolean, default: false },
});

// Serves the 6-hour cache lookup: newest scan for a domain.
scanSchema.index({ domain: 1, scannedAt: -1 });

// Sanitize new records only. Existing records are never deleted or rewritten here.
scanSchema.pre('validate', function () {
  if (!this.isNew) return;
  const clean = redact(this.toObject());
  this.set(clean);
});

export const Scan = mongoose.model<ScanDoc>('Scan', scanSchema);
export default Scan;
