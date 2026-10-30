import { redact, REDACTED } from '../utils/privacy';
import mongoose, { Document, Schema, Types } from 'mongoose';

export type FollowUpStatus = 'new' | 'contacted' | 'converted' | 'not_interested';

export interface LeadDoc extends Document {
  _id: Types.ObjectId;
  scanId: Types.ObjectId;
  name: string;
  email: string;
  whatsapp: string;
  submittedAt: Date;
  followUpStatus: FollowUpStatus;
  /** Denormalised so the outreach list is useful without joining every scan. */
  domain: string;
  overallGrade: string;
  overallScore: number;
  /** Whether the report email actually went out — a silent failure loses a lead. */
  reportEmailed: boolean;
  emailError: string | null;

  /**
   * Proof of consent, kept with the contact details it authorises.
   *
   * India's DPDP Act requires consent to be free, specific, informed and given
   * by a clear affirmative action — and requires us to be able to *demonstrate*
   * it. TRAI's rules go further for promotional WhatsApp or SMS: a timestamped
   * opt-in record per contact.
   *
   * `consentText` stores the exact wording the person agreed to, not a
   * reference to it. Wording changes over time; a record that says only "they
   * consented" cannot answer "to what?" a year later.
   *
   * Deliberately no IP address here. It would be one more piece of personal
   * data to hold, and the timestamped wording is what actually evidences
   * consent — DPDP asks us to collect the minimum necessary, and this is it.
   */
  consent: boolean;
  consentAt: Date;
  consentText: string;
  consentSource: string;
}

const leadSchema = new Schema<LeadDoc>({
  scanId: { type: Schema.Types.ObjectId, ref: 'Scan', required: true, index: true },
  name: { type: String, required: true, trim: true },
  email: { type: String, required: true, trim: true, lowercase: true, index: true },
  whatsapp: { type: String, required: false, trim: true },
  submittedAt: { type: Date, default: Date.now },
  followUpStatus: {
    type: String,
    enum: ['new', 'contacted', 'converted', 'not_interested'],
    default: 'new',
    index: true,
  },
  domain: { type: String, default: '' },
  overallGrade: { type: String, default: '' },
  overallScore: { type: Number, default: 0 },
  reportEmailed: { type: Boolean, default: false },
  emailError: { type: String, default: null },

  // Required with no default: a lead may not exist without recorded consent.
  consent: { type: Boolean, required: true },
  consentAt: { type: Date, required: true },
  consentText: { type: String, required: true },
  consentSource: { type: String, required: true },
});

leadSchema.index({ submittedAt: -1 });

// Sanitize new records only. Existing records are never deleted or rewritten here.
leadSchema.pre('validate', function () {
  if (!this.isNew) return;
  const clean = redact(this.toObject());
  clean.name = REDACTED; clean.email = REDACTED; clean.whatsapp = '';
  this.set(clean);
});

export const Lead = mongoose.model<LeadDoc>('Lead', leadSchema);
export default Lead;
