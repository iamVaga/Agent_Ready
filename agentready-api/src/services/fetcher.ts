import axios, { AxiosError, AxiosResponse } from 'axios';
import { safeGet } from './safeRequest';
import { gunzipSync } from 'zlib';
import { config } from '../config';

export type FetchFailure =
  | 'timeout'
  | 'dns'
  | 'ssl'
  | 'redirect_loop'
  | 'connection_refused'
  | 'network'
  /**
   * The page came back, but it is larger than we will hold in memory.
   *
   * A finding about the page, never an absence of one. mastersunion.org serves a
   * 4MB homepage against our 3MB cap; axios aborted, the error fell into the
   * `network` catch-all, and a live business school was told "nothing answered
   * at this address — no server accepted the connection".
   */
  | 'too_large'
  | 'deadline_exceeded';

export interface FetchResult {
  requestedUrl: string;
  /** URL after redirects. Falls back to requestedUrl when we never got a response. */
  finalUrl: string;
  status: number | null;
  ok: boolean;
  headers: Record<string, string>;
  body: string | null;
  contentType: string | null;
  /** Set when no usable response came back at all. */
  failure: FetchFailure | null;
  /**
   * True when the server answered but refused us: 401/403, or a Cloudflare
   * style interstitial. Distinct from `failure` — the site is up, it just does
   * not want a bot. Spec section 8 wants this reported as its own result.
   */
  blocked: boolean;
  /**
   * The server asked us to slow down (429, or 503 with Retry-After).
   *
   * Deliberately not `blocked`. See looksRateLimited — a rate limit says
   * something about how fast we asked, never about whether the visitor is
   * welcome, and conflating the two invented crawler blocks that did not exist.
   */
  rateLimited: boolean;
  /**
   * True when the page was longer than we were willing to hold and we stopped
   * reading. The content we did read is real; there is simply more of it.
   */
  truncated: boolean;
  durationMs: number;
}

/**
 * A single scan's time budget.
 *
 * The spec's 15-second ceiling is on the whole scan, not per request, so every
 * fetch asks the deadline how long it may take. Once the budget is gone,
 * `fetch` returns a `deadline_exceeded` result immediately instead of starting a
 * request that could not finish in time.
 */
export class Deadline {
  private endsAt: number;

  /**
   * Held back from the network budget for parsing, scoring and building the
   * report. Without it the fetches alone consume the entire ceiling and the scan
   * finishes *after* the time we promised — parsing six pages of HTML is not
   * free.
   *
   * Raised from 1500ms on measurement rather than instinct. Loading a real
   * homepage into cheerio and walking it for text, JSON-LD and links costs
   * around 500ms on the heavy end — themancompany.com ships 1.9MB and takes
   * 282ms to parse plus 236ms to walk. Six or seven of those is roughly three
   * seconds, so a 1500ms reserve was short by half and the overrun landed
   * outside the promised fifteen: that scan finished in 17.8 seconds.
   *
   * It costs network budget, which is a real trade — fewer pages get fetched on
   * a slow host. That is the right way round. A scan that samples one page fewer
   * says so in the report; a scan that runs three seconds long silently breaks
   * the only promise we make about it.
   */
  static readonly PROCESSING_RESERVE_MS = 3000;

  constructor(totalMs: number = config.scanner.totalTimeoutMs) {
    this.endsAt = Date.now() + Math.max(1000, totalMs - Deadline.PROCESSING_RESERVE_MS);
  }

  /**
   * A shorter deadline nested inside this one.
   *
   * For work that is a means rather than an end. Reading a sitemap exists only
   * to choose which five pages to fetch; on thesouledstore.com it spent 6.5 of
   * the 12 available seconds returning 4,995 URLs, and the five pages it chose
   * then got zero milliseconds and failed. The heaviest check in the report —
   * structured data — was reported as "we ran out of time" because a cheaper
   * step had already spent the budget it needed.
   *
   * Never extends the parent: the child expires at whichever comes first.
   */
  slice(maxMs: number): Deadline {
    const child = new Deadline();
    child.endsAt = Math.min(this.endsAt, Date.now() + maxMs);
    return child;
  }

  remainingMs(): number {
    return Math.max(0, this.endsAt - Date.now());
  }

  expired(): boolean {
    // Anything under a second cannot complete a useful request; treat it as gone
    // so callers stop rather than firing a request doomed to time out.
    return this.remainingMs() < 1000;
  }
}

/**
 * How much of a page we will hold in memory.
 *
 * Raised from 3MB once the probes stopped downloading whole pages — see
 * PROBE_BODY_BYTES. mastersunion.org serves 3.87MB, so 3MB was cutting real
 * sites in half for the sake of a limit that eleven redundant copies of the
 * homepage were the actual reason for.
 *
 * It is a ceiling, not a target, and there will always be a page above it. That
 * is why the reader truncates rather than failing: the cap decides how much we
 * keep, never whether the scan works.
 */
const MAX_BODY_BYTES = 5 * 1024 * 1024;

/**
 * How much of a page a *crawler probe* keeps.
 *
 * These eleven requests exist to learn one thing — did the server serve this
 * crawler, yes or no — and that is the status code. We were downloading the
 * entire homepage eleven times to read one number: 44MB of transfer on a 4MB
 * page, per scan, most of it the binding reason the cap had to stay low.
 *
 * 64KB is far more than the check needs. `looksChallenged` reads the first 4000
 * characters to spot a Cloudflare interstitial, and nothing else touches a probe
 * body.
 */
const PROBE_BODY_BYTES = 64 * 1024;

/**
 * How long a crawler probe waits before concluding the server will not answer.
 *
 * Four seconds, not the ten a normal page fetch gets. The probe asks one
 * question — does this server serve this user-agent? — and a server that has
 * said nothing after four seconds has answered it. nykaa.com stalls every
 * crawler-shaped request until the full timeout, so one wave of four probes was
 * consuming ten of the scan's seventeen network seconds to learn something the
 * first four told us.
 *
 * It is also the standard this report holds sites to. We tell owners that
 * assistants fetch competing sources in parallel and write the answer from
 * whatever arrives first; waiting ten seconds is not what a crawler does, so
 * measuring with a ten-second patience would not reflect what a crawler sees.
 */
const PROBE_TIMEOUT_MS = 4000;

const CHALLENGE_MARKERS = [
  'just a moment...',
  'cf-browser-verification',
  'attention required! | cloudflare',
  'checking your browser before accessing',
  'enable javascript and cookies to continue',
  'access denied',
  'request unsuccessful. incapsula',
  'pardon our interruption',
];

/** Node's TLS verification failures, as reported through axios's `code`. */
const CERT_ERRORS = new Set([
  'CERT_HAS_EXPIRED',
  'CERT_NOT_YET_VALID',
  'DEPTH_ZERO_SELF_SIGNED_CERT',
  'SELF_SIGNED_CERT_IN_CHAIN',
  'UNABLE_TO_GET_ISSUER_CERT',
  'UNABLE_TO_GET_ISSUER_CERT_LOCALLY',
  'UNABLE_TO_VERIFY_LEAF_SIGNATURE',
  'ERR_TLS_CERT_ALTNAME_INVALID',
  'HOSTNAME_MISMATCH',
  'CERT_UNTRUSTED',
]);

function classifyAxiosError(error: AxiosError): FetchFailure {
  const code = error.code || '';
  // ERR_CANCELED is what the deadline's AbortSignal produces, and
  // TimeoutError/ABORT_ERR is the underlying DOMException name.
  if (code === 'ECONNABORTED' || code === 'ETIMEDOUT' || code === 'ERR_CANCELED' || code === 'ABORT_ERR') return 'timeout';
  if (error.name === 'TimeoutError' || error.name === 'AbortError') return 'timeout';
  if (code === 'ENOTFOUND' || code === 'EAI_AGAIN') return 'dns';
  if (code === 'ECONNREFUSED') return 'connection_refused';
  if (code === 'ERR_FR_TOO_MANY_REDIRECTS') return 'redirect_loop';
  /**
   * The full set of certificate failures, not the three we happened to think of.
   *
   * clude.ai fails with UNABLE_TO_GET_ISSUER_CERT_LOCALLY — an incomplete chain,
   * where the server sends its own certificate but not the intermediate that
   * proves it. That was falling through to `network`, so the owner was told
   * "no server accepted the connection" when the server accepted it fine and the
   * problem was one missing file in their TLS config. Wrong diagnosis, wrong
   * person to call: hosting support rather than the certificate provider.
   */
  if (code.startsWith('ERR_TLS') || code.startsWith('ERR_SSL') || CERT_ERRORS.has(code)) {
    return 'ssl';
  }
  // axios reports the maxContentLength abort through a couple of different
  // codes depending on version and transport, so match the message too.
  if (code === 'ERR_FR_MAX_CONTENT_LENGTH_EXCEEDED' || /maxContentLength/i.test(error.message || '')) {
    return 'too_large';
  }
  return 'network';
}

/**
 * Read at most MAX_BODY_BYTES from a response stream, then stop.
 *
 * Truncating mid-document is safe: cheerio's parser is built for broken HTML and
 * closes what it finds open. Everything the checks read — title, meta, JSON-LD,
 * headings, navigation — lives near the top of a document, while the tail of a
 * page this size is inlined CSS and base64 images no check ever looks at.
 */
async function readCapped(stream: NodeJS.ReadableStream, limit: number): Promise<{ body: string; truncated: boolean }> {
  const chunks: Buffer[] = [];
  let total = 0;
  let truncated = false;

  for await (const chunk of stream) {
    const buffer = Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk as string);
    chunks.push(buffer);
    total += buffer.length;
    if (total >= limit) {
      truncated = true;
      break;
    }
  }

  // Stop the download rather than letting the rest arrive unread — on a 4MB page
  // that is a megabyte of someone's bandwidth we have no use for.
  (stream as unknown as { destroy?: () => void }).destroy?.();

  const raw = Buffer.concat(chunks).subarray(0, limit);
  return { body: decompressIfGzipped(raw).toString('utf8'), truncated };
}

/**
 * A gzipped *file*, as opposed to a gzip-encoded response.
 *
 * axios already handles `Content-Encoding: gzip` — that is the transport
 * compressing a response in flight, and it is undone before we ever see it.
 * This is the other thing: a file that is itself a .gz, served as ordinary
 * bytes. Nothing unwraps that for us.
 *
 * It matters because gzipped sitemaps are the norm at scale, not an oddity —
 * Google recommends them for large sites. booking.com declares 354 sitemaps
 * whose children are all `.xml.gz`; we fetched them, read the compressed bytes
 * as text, found no <loc> tags, and concluded the site had no pages worth
 * sampling. Every large site using .gz has been giving us nothing to work with.
 *
 * Detected by the two magic bytes rather than the file extension, because the
 * extension is a convention and the bytes are a fact.
 */
function decompressIfGzipped(raw: Buffer): Buffer {
  if (raw.length < 2 || raw[0] !== 0x1f || raw[1] !== 0x8b) return raw;
  try {
    return gunzipSync(raw, { maxOutputLength: MAX_BODY_BYTES });
  } catch {
    // A truncated archive cannot be unwrapped. Returning the raw bytes leaves
    // the caller exactly where it was rather than throwing away the request.
    return raw;
  }
}

function headersOf(response: AxiosResponse): Record<string, string> {
  const out: Record<string, string> = {};
  const raw = response.headers as unknown as Record<string, unknown>;
  for (const [key, value] of Object.entries(raw || {})) {
    if (value == null) continue;
    out[key.toLowerCase()] = Array.isArray(value) ? value.join(', ') : String(value);
  }
  return out;
}

function looksChallenged(status: number, body: string | null): boolean {
  if (status === 401 || status === 403) return true;
  if (status !== 503 || !body) return false;
  const head = body.slice(0, 4000).toLowerCase();
  return CHALLENGE_MARKERS.some((marker) => head.includes(marker));
}

/**
 * Told to slow down — not turned away.
 *
 * 429 used to be lumped in with 401 and 403 as `blocked`, and that one line cost
 * sites up to 25 points for something they never did. We ask for the homepage as
 * eleven different crawlers; plenty of servers answer the first and rate-limit
 * the rest. We were then reporting our own burst back to the owner as
 * "your server refuses AI crawlers" — proven on mrisoftware.com, where the first
 * Amazonbot request returned 200 and the next two returned 429.
 *
 * A rate limit is a statement about request volume, not about identity. It tells
 * us nothing about whether the real crawler is welcome, so the only honest thing
 * to do is record that we could not find out.
 *
 * 503 with a Retry-After header is the same message in a different envelope —
 * but a 503 *without* one is kept out, because that is where CDN challenge pages
 * live and `looksChallenged` needs to keep seeing them.
 */
function looksRateLimited(status: number, headers: Record<string, string>): boolean {
  if (status === 429) return true;
  return status === 503 && Boolean(headers['retry-after']);
}

export interface FetchOptions {
  /**
   * Wait no longer than this for a response, in milliseconds.
   *
   * Still bounded by the scan deadline — this only ever shortens the wait, it
   * cannot extend it past the budget.
   */
  timeoutMs?: number;
  /**
   * Keep at most this many bytes of the body.
   *
   * Defaults to MAX_BODY_BYTES. Callers that only need the status code pass
   * something far smaller rather than paying for a page they will not read.
   */
  maxBytes?: number;
  /**
   * Identify as a different crawler for this request.
   *
   * Used to test whether a site's firewall actually serves the AI crawlers its
   * robots.txt claims to allow — the two disagree more often than owners
   * realise. The strings passed in still name us as the prober (see
   * checks/botAccess.ts), so this reveals stricter treatment rather than
   * evading it.
   */
  userAgent?: string;
}

/**
 * Fetch a URL within the scan's remaining budget. Never throws — every failure
 * mode is reported in the result, because a failed fetch is itself a finding.
 */
export { PROBE_BODY_BYTES, PROBE_TIMEOUT_MS };

export async function fetchUrl(url: string, deadline: Deadline, options: FetchOptions = {}): Promise<FetchResult> {
  const startedAt = Date.now();

  const base: FetchResult = {
    requestedUrl: url,
    finalUrl: url,
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

  if (deadline.expired()) {
    return { ...base, failure: 'deadline_exceeded', durationMs: 0 };
  }

  const timeout = Math.min(options.timeoutMs ?? config.scanner.requestTimeoutMs, config.scanner.requestTimeoutMs, deadline.remainingMs());

  try {
    const { response, finalUrl } = await safeGet<string>(url, {
      timeout,
      /**
       * axios's `timeout` starts once the socket is connected, so DNS
       * resolution, TLS negotiation and a slow body stream all sit outside it —
       * which is how a scan with a 15-second ceiling was finishing in 19. This
       * signal covers the whole request, wall-clock, and is what actually
       * enforces the deadline.
       */
      signal: AbortSignal.timeout(timeout),
      maxRedirects: 0,
      /**
       * We police the size ourselves, so axios must not.
       *
       * `maxContentLength` aborts the whole request and throws once the body
       * passes the cap, which returns *nothing at all*. mastersunion.org serves
       * a 4MB homepage; we threw away the 3MB we had already received, and a
       * live business school was told nothing answered at that address. Asking
       * the server for a Range instead was no better — that one ignores the
       * header and sends the lot again.
       *
       * Reading the stream and stopping when we have enough depends on nothing
       * the server chooses to support.
       */
      maxContentLength: Infinity,
      maxBodyLength: Infinity,
      responseType: 'stream',
      // We inspect non-2xx bodies (a 403 challenge page, a 404 .well-known),
      // so never let axios turn a status code into an exception.
      validateStatus: () => true,
      decompress: true,
      headers: {
        'User-Agent': options.userAgent || config.scanner.userAgent,
        Accept: 'text/html,application/xhtml+xml,application/json,text/plain;q=0.9,*/*;q=0.8',
        'Accept-Language': 'en-IN,en;q=0.9',
      },
    });

    const headers = headersOf(response);
    const { body, truncated } = await readCapped(response.data as unknown as NodeJS.ReadableStream, options.maxBytes ?? MAX_BODY_BYTES);
    const status = response.status;

    return {
      ...base,
      finalUrl,
      status,
      // 206 lands inside this range already — a partial body is exactly what the
      // oversize retry asked for, and every check reads the head of the document.
      ok: status >= 200 && status < 300,
      headers,
      body,
      contentType: headers['content-type'] || null,
      blocked: looksChallenged(status, body),
      truncated,
      rateLimited: looksRateLimited(status, headers),
      durationMs: Date.now() - startedAt,
    };
  } catch (error) {
    const classified = axios.isAxiosError(error) ? classifyAxiosError(error) : 'network';

    /**
     * Whose clock ran out — theirs or ours?
     *
     * The effective timeout is `min(requestTimeoutMs, time left in the scan)`.
     * When the deadline is what clamped it, a timeout says nothing about the
     * site: it says we ran out of budget while asking. Reported as `timeout`,
     * that became "this site did not respond in time" and — because the
     * no-website check treats a timeout as a dead domain — a perfectly healthy
     * site could be told it does not exist. Reproduced by squeezing the budget
     * to four seconds: codenixlabs.com, which is plainly up, came back F/0 with
     * "that can mean the site is down".
     *
     * Same mistake as reading a 429 as a refusal. A limit we imposed is not
     * evidence about them.
     */
    const ourClockRanOut = classified === 'timeout' && timeout < config.scanner.requestTimeoutMs;
    const failure = ourClockRanOut ? 'deadline_exceeded' : classified;

    return { ...base, failure, durationMs: Date.now() - startedAt };
  }
}

/**
 * Fetch a URL over http:// if the https:// attempt failed at the transport
 * level. Plenty of small Indian D2C stores still have a broken or missing
 * certificate on the apex domain, and reporting "we couldn't reach you" when
 * the site is plainly online would be wrong.
 */
export async function fetchWithHttpFallback(url: string, deadline: Deadline): Promise<FetchResult> {
  const first = await fetchUrl(url, deadline);
  const transportFailed = first.failure && first.failure !== 'deadline_exceeded';

  if (!transportFailed || !url.startsWith('https://') || deadline.expired()) {
    return first;
  }

  const fallback = await fetchUrl(url.replace(/^https:/, 'http:'), deadline);
  return fallback.failure ? first : fallback;
}
