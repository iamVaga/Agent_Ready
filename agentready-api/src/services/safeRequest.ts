import axios, { AxiosRequestConfig, AxiosResponse } from 'axios';
import { lookup } from 'dns/promises';
import { Agent as HttpAgent } from 'http';
import { Agent as HttpsAgent } from 'https';
import { isIP } from 'net';

const MAX_REDIRECTS = 5;

export function isPublicIp(ip: string): boolean {
  const v = ip.toLowerCase().replace(/^\[|\]$/g, '');

  if (isIP(v) === 4) {
    const a = v.split('.').map(Number);
    const [x, y, z] = a;
    return !(x === 0 || x === 10 || x === 127 || x >= 224 ||
      x === 169 && y === 254 || x === 172 && y >= 16 && y <= 31 ||
      x === 192 && y === 168 || x === 100 && y >= 64 && y <= 127 ||
      x === 192 && y === 0 && (z === 0 || z === 2) || x === 192 && y === 88 && z === 99 ||
      x === 198 && (y === 18 || y === 19 || y === 51 && z === 100) ||
      x === 203 && y === 0 && z === 113 || x >= 240);
  }
  if (isIP(v) !== 6) return false;
  // Expand before testing prefixes: compressed and embedded IPv4 forms must agree.
  const halves = v.split('::');
  const words = (part: string) => part ? part.split(':').flatMap(w => w.includes('.')
    ? [((Number(w.split('.')[0]) << 8) | Number(w.split('.')[1])).toString(16),
       ((Number(w.split('.')[2]) << 8) | Number(w.split('.')[3])).toString(16)] : [w]) : [];
  const left = words(halves[0]), right = words(halves[1] || '');
  const full = [...left, ...Array(8 - left.length - right.length).fill('0'), ...right].map(w => parseInt(w, 16));
  // Reject mapped addresses, translation/tunnel, documentation and special-purpose space.
  return full[0] >= 0x2000 && full[0] < 0x4000 &&
    !(full[0] === 0x2001 && (full[1] < 0x200 || full[1] === 0xdb8)) &&
    full[0] !== 0x2002 && !(full[0] === 0x3fff && full[1] < 0x1000);
}

export async function resolvePublicTarget(raw: string): Promise<{ url: URL; address: string; family: 4 | 6 }> {
  const url = new URL(raw);
  if (!['http:', 'https:'].includes(url.protocol) || url.username || url.password ||
      (url.port && url.port !== (url.protocol === 'https:' ? '443' : '80'))) {
    throw new Error('Unsafe outbound URL');
  }
  const host = url.hostname.replace(/\.$/, '').replace(/^\[|\]$/g, '');
  if (!host || host === 'localhost' || host.endsWith('.localhost') ||
      host.endsWith('.local') || host.endsWith('.internal')) throw new Error('Unsafe hostname');
  const answers = isIP(host) ? [{ address: host, family: isIP(host) }] : await lookup(host, { all: true, verbatim: true });
  if (!answers.length || answers.some(a => !isPublicIp(a.address))) throw new Error('Unsafe destination IP');
  const chosen = answers[0];
  return { url, address: chosen.address, family: chosen.family as 4 | 6 };
}

/** DNS is resolved and validated before each hop, then pinned for the actual socket. */
export async function safeGet<T = unknown>(raw: string, options: AxiosRequestConfig = {}): Promise<{ response: AxiosResponse<T>; finalUrl: string }> {
  let current = raw;
  for (let redirects = 0; redirects <= MAX_REDIRECTS; redirects++) {
    const target = await resolvePublicTarget(current);
    const pinnedLookup = (_hostname: string, _opts: unknown, cb: (err: NodeJS.ErrnoException | null, address: string, family: number) => void) =>
      {
      if ((_opts as { all?: boolean })?.all) {
        (cb as unknown as (err: null, addresses: unknown[]) => void)(null, [{ address: target.address, family: target.family }]);
      } else cb(null, target.address, target.family);
    };
    const response = await axios.get<T>(target.url.toString(), {
      maxContentLength: 5 * 1024 * 1024,
      headers: options.headers,
      signal: options.signal ?? AbortSignal.timeout(options.timeout || 10000),
      timeout: options.timeout ?? 10000,
      adapter: 'http',
      transport: undefined,
      socketPath: undefined,
      baseURL: undefined,
      params: undefined,
      responseType: 'stream',
      auth: undefined,
      httpAgent: new HttpAgent({ lookup: pinnedLookup }),
      httpsAgent: new HttpsAgent({ lookup: pinnedLookup }),
      proxy: false,
      maxRedirects: 0,
      validateStatus: () => true,
    });
    if (![301, 302, 303, 307, 308].includes(response.status) || !response.headers.location) {
      return { response, finalUrl: target.url.toString() };
    }
    (response.data as { destroy?: () => void })?.destroy?.();
    if (redirects === MAX_REDIRECTS) throw new Error('Too many redirects');
    const next = new URL(response.headers.location, target.url);
    if (next.origin !== target.url.origin) options = { ...options, headers: undefined };
    current = next.toString();
  }
  throw new Error('Too many redirects');
}
