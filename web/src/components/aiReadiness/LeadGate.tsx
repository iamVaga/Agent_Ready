import React, { useState } from 'react';
import { Loader2, ShieldCheck } from 'lucide-react';
import { CONSENT_SOURCE, CONSENT_TEXT, type LeadInput } from '../../types/aiReadiness';

const EMAIL_PATTERN = /^[^\s@]+@[^\s@]+\.[a-z]{2,}$/i;

/**
 * The lead-capture gate.
 *
 * Validation mirrors the API's rather than trusting it: a round trip to be told
 * "that email is invalid" is where people abandon a form. The API still
 * validates — this only saves the trip.
 *
 * The consent checkbox is not decoration. India's DPDP Act requires consent to
 * be free, specific, informed and given by a clear affirmative action before
 * personal data is processed, and TRAI's rules require a recorded opt-in before
 * any promotional WhatsApp message. A pre-ticked box would satisfy neither.
 */
export const LeadGate: React.FC<{
  lockedChecks: number;
  fixesAvailable: number;
  onSubmit: (lead: LeadInput) => void;
  busy: boolean;
  error: string | null;
}> = ({ lockedChecks, fixesAvailable, onSubmit, busy, error }) => {
  const name = '';
  const [email, setEmail] = useState('');
  const whatsapp = '';
  const [consent, setConsent] = useState(false);
  const [touched, setTouched] = useState(false);

  const problems = validate({ name, email, whatsapp, consent });
  const valid = Object.keys(problems).length === 0;

  return (
    <div className="relative overflow-hidden border rounded-3xl border-primary/30 bg-gradient-to-br from-primary/15 via-surface to-surface">
      <div className="absolute inset-0 opacity-40 bg-glow" aria-hidden="true" />

      <div className="relative z-10 p-6 sm:p-9">
        <h2 className="text-2xl font-bold text-white font-orbitron sm:text-3xl">Get the fix code</h2>
        <p className="max-w-2xl mt-3 leading-relaxed text-neutral-300">
          {describeGate(lockedChecks, fixesAvailable)} We will email the whole report with the code in it, so you can
          forward it straight to whoever maintains your site. The code is yours either way — there is no trial and
          nothing to cancel.
        </p>

        <form
          noValidate
          onSubmit={(event) => {
            event.preventDefault();
            setTouched(true);
            if (valid && !busy) {
              onSubmit({
                name: name.trim(),
                email: email.trim(),
                whatsapp: whatsapp.trim(),
                consent,
                consentText: CONSENT_TEXT,
                consentSource: CONSENT_SOURCE,
              });
              setEmail('');
            }
          }}
          className="grid gap-5 mt-8"
        >
          <div className="grid gap-5 sm:grid-cols-1">
            <Field id="lead-email" label="Email" error={touched ? problems.email : undefined}>
              <input
                id="lead-email"
                type="email"
                inputMode="email"
                value={email}
                onChange={(event) => setEmail(event.target.value)}
                autoComplete="email"
                autoCapitalize="none"
                disabled={busy}
                className={inputClass}
              />
            </Field>

          </div>

          <label className="flex items-start gap-3 text-sm cursor-pointer text-neutral-300">
            <input
              type="checkbox"
              checked={consent}
              onChange={(event) => setConsent(event.target.checked)}
              disabled={busy}
              className="mt-1 h-4 w-4 flex-shrink-0 rounded border-white/25 bg-white/10 accent-[#8948FF]"
            />
            <span>{CONSENT_TEXT}</span>
          </label>
          {touched && problems.consent ? <p className="-mt-3 text-xs text-error">{problems.consent}</p> : null}

          {error ? (
            <div className="px-4 py-3 text-sm border rounded-xl border-error/30 bg-error/10 text-error" role="alert">
              {error}
            </div>
          ) : null}

          <div className="flex flex-col items-start gap-4 sm:flex-row sm:items-center">
            <button
              type="submit"
              disabled={busy || (touched && !valid)}
              className="inline-flex items-center justify-center w-full gap-2 px-8 py-4 font-semibold text-white transition-all duration-300 rounded-full sm:w-auto bg-primary hover:bg-primary/90 hover:shadow-lg hover:shadow-primary/30 disabled:opacity-50"
            >
              {busy ? <Loader2 size={18} className="animate-spin" aria-hidden="true" /> : null}
              {busy ? 'Sending…' : 'Show me the fix code'}
            </button>

            <p className="inline-flex items-center gap-2 text-xs text-neutral-400">
              <ShieldCheck size={16} className="text-accent" aria-hidden="true" />
              No newsletter. We never resell your details.
            </p>
          </div>
        </form>
      </div>
    </div>
  );
};

const inputClass =
  'w-full rounded-xl border border-white/15 bg-white/5 px-4 py-3 text-white outline-none transition-colors placeholder:text-neutral-500 focus:border-primary focus:ring-1 focus:ring-primary disabled:opacity-60';

const Field: React.FC<{ id: string; label: string; error?: string; children: React.ReactNode }> = ({
  id,
  label,
  error,
  children,
}) => (
  <div>
    <label htmlFor={id} className="block mb-2 text-sm font-medium text-neutral-200">
      {label}
    </label>
    {children}
    {error ? <p className="mt-1.5 text-xs text-error">{error}</p> : null}
  </div>
);

/** Only the fields a person actually fills in — the wording is a constant. */
type LeadFields = Pick<LeadInput, 'name' | 'email' | 'whatsapp' | 'consent'>;

function validate(lead: LeadFields): Partial<Record<keyof LeadFields, string>> {
  const problems: Partial<Record<keyof LeadFields, string>> = {};

  /**
   * Email is the only thing we insist on.
   *
   * Every required field is a place someone leaves. Roughly five visitors in
   * eighty finished this form when it demanded a name, an email and a phone
   * number — and the phone number is where most people stop, because handing a
   * stranger a number they can ring is a bigger ask than an address they can
   * write to. We need one way to send the report; the rest is theirs to offer.
   */
  if (!EMAIL_PATTERN.test(lead.email.trim())) problems.email = 'That does not look like a valid email address.';

  // Validated only when supplied. A malformed number is worse than a blank one,
  // because it looks like a way to reach someone. Same bounds the API enforces.
  const digits = lead.whatsapp.replace(/\D/g, '');
  if (lead.whatsapp.trim() && (digits.length < 8 || digits.length > 15)) {
    problems.whatsapp = 'That does not look right. Leave it blank if you would rather not.';
  }

  if (!lead.consent) problems.consent = 'Please tick the box to allow report delivery.';

  return problems;
}

/**
 * What is actually behind the form, in the plainest words available.
 *
 * It used to describe how many *checks* were hidden, because the gate hid the
 * report. It hides the code now, and the code is a far easier thing to want —
 * so the sentence names it, counts it, and says where it goes.
 */
function describeGate(lockedChecks: number, fixesAvailable: number): string {
  if (fixesAvailable <= 0) {
    return 'Nothing here needs fixing, so there is no code to hand you — but we will email you the report if it is useful.';
  }

  const fixes = `${fixesAvailable} ready-to-paste fix${fixesAvailable === 1 ? '' : 'es'}`;
  const problems =
    lockedChecks === 1 ? 'the problem we found' : `the ${lockedChecks} problems we found`;

  return `We have already written ${fixes} for ${problems} above — the actual code, not a description of it.`;
}


export default LeadGate;
