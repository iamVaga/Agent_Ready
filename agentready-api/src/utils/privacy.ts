/** Redact at egress boundaries, never mutate the DOM used for scan scoring. */
export const REDACTED = '[REDACTED]';
const sensitive = /email|phone|mobile|whatsapp|address|street|postal|zip|password|secret|token|api.?key|authorization|cookie|session|card|cvv|cvc|iban|expiry|expiration|routing|swift|account.?number|customer|first.?name|last.?name|full.?name/i;
const publicQuery = new Set(['id', 'product', 'product_id', 'variant', 'variant_id', 'quantity', 'page', 'sort', 'category', 'slug', 'q', 's']);
export function safeUrl(raw: string, log = false): string {
  try {
    const u = new URL(raw.replace(/&amp;/g, '&'));
    u.username = ''; u.password = ''; u.hash = '';
    for (const key of [...u.searchParams.keys()]) {
      if (log || sensitive.test(key) || !publicQuery.has(key.toLowerCase())) u.searchParams.delete(key);
      else { const values = u.searchParams.getAll(key); u.searchParams.delete(key); values.forEach(v => u.searchParams.append(key, redactText(v, false))); }
    }
    u.pathname = redactText(u.pathname, false).replace(/\/\w{32,}(?=\/|$)/g, '/' + REDACTED);
    return u.toString();
  } catch { return REDACTED; }
}
export function redactText(value: string, urls = true): string {
  let out = value;
  try { out = decodeURIComponent(out); } catch { /* preserve malformed text */ }
  if (urls) out = out.replace(/https?:\/\/[^\s<>"']+/gi, u => safeUrl(u));
  return out
    .replace(/\b(?:cookie|set-cookie|authorization)\s*:\s*[^\r\n]+/gi, REDACTED)
    .replace(/[\w.+%-]+@[\w.-]+\.[a-z]{2,}/gi, REDACTED)
    .replace(/\b[A-Z]{2}\d{2}(?:[ ]?[A-Z0-9]){11,30}\b/g, REDACTED)
    .replace(/\b(?:sk-[a-zA-Z0-9_-]{8,}|gh[pousr]_[a-zA-Z0-9_]{10,}|AKIA[A-Z0-9]{16}|eyJ[\w-]+\.[\w-]+\.[\w-]+)\b/g, REDACTED)
    .replace(/(["']?[\w-]*(?:token|secret|password|api[_-]?key|cookie|authorization|session|cvv|cvc|iban|card[_-]?number)[\w-]*["']?\s*[:=]\s*)(?:"[^"]*"|'[^']*'|[^\s,;<>&]+)/gi, '$1' + REDACTED)
    .replace(/\b\d{1,6}\s+(?:[\w.-]+\s+){0,5}(?:street|st|road|rd|avenue|ave|lane|ln|drive|boulevard|blvd|nagar)\b(?:\s*,?\s*(?:apt|unit|suite)\s*[\w-]+)?/gi, REDACTED)
    .replace(/(?<!\w)\+?\d[\d ().-]{6,}\d(?!\w)/g, v => { const n = v.replace(/\D/g, '').length; return n >= 8 && n <= 19 ? REDACTED : v; });
}
export function redact<T>(value: T): T {
  if (typeof value === 'string') return redactText(value) as T;
  if (Array.isArray(value)) return value.map(v => redact(v)) as T;
  if (value && typeof value === 'object' && Object.getPrototypeOf(value) === Object.prototype) {
    return Object.fromEntries(Object.entries(value).map(([k,v]) => [k, sensitive.test(k) && typeof v !== 'boolean' && !(v instanceof Date) ? REDACTED : redact(v)])) as T;
  }
  return value;
}
export function privacyLog(...values: unknown[]): void {
  console.error(...values.map(v => v instanceof Error ? v.name : redact(v)));
}

let installed = false;
export function installPrivacyLogging(): void {
  if (installed) return;
  installed = true;
  for (const level of ['log', 'info', 'warn', 'error', 'debug'] as const) {
    const original = console[level].bind(console);
    console[level] = (...values: unknown[]) => original(...values.map(v => {
      if (v instanceof Error) return v.name;
      if (typeof v === 'string') return redactText(v.replace(/https?:\/\/[^\s<>"']+/gi, u => safeUrl(u, true)));
      return redact(v);
    }));
  }
}
