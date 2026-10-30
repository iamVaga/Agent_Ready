import assert from 'node:assert/strict';
import { isPublicIp, resolvePublicTarget } from '../services/safeRequest';

async function main() {
  for (const ip of ['127.0.0.1', '10.1.2.3', '172.16.0.1', '192.168.1.1', '169.254.169.254', '0.0.0.0', '::1', 'fc00::1', 'fe80::1', '::ffff:127.0.0.1']) {
    assert.equal(isPublicIp(ip), false, ip);
  }
  for (const ip of ['8.8.8.8', '1.1.1.1', '2606:4700:4700::1111']) {
    assert.equal(isPublicIp(ip), true, ip);
  }
  for (const url of ['http://127.0.0.1/', 'http://169.254.169.254/latest/meta-data/', 'http://localhost/', 'file:///etc/passwd', 'http://user:pass@example.com/', 'http://8.8.8.8:8080/']) {
    await assert.rejects(() => resolvePublicTarget(url), new RegExp("Unsafe"), url);
  }
  const target = await resolvePublicTarget('https://1.1.1.1/');
  assert.equal(target.address, '1.1.1.1');
  console.log('SSRF validation checks passed');
}
main().catch(err => { console.error(err); process.exitCode = 1; });
