const {test} = require('node:test');
const assert = require('node:assert/strict');
const dns = require('node:dns/promises');
const axios = require('axios');
const {Readable} = require('node:stream');
const {isPublicIp, resolvePublicTarget, safeGet} = require('../dist/services/safeRequest');
test('rejects private, mapped, transition and special IPs', () => {
 for (const ip of ['127.0.0.1','10.1.2.3','169.254.169.254','100.64.0.1','192.168.1.1','::1','fc00::1','fe80::1','::ffff:7f00:1','2002:7f00:1::','2001::1','2001:db8::1','3fff::1']) assert.equal(isPublicIp(ip), false, ip);
 for (const ip of ['8.8.8.8','1.1.1.1','2606:4700:4700::1111']) assert.equal(isPublicIp(ip), true, ip);
});
test('URL checks, all DNS answers, pinning, redirects and proxy override', async () => {
 const oldLookup = dns.lookup, oldGet = axios.default.get;
 let calls=0, destroyed=false;
 try {
 dns.lookup = async () => [{address:'93.184.216.34',family:4}];
 for(const url of ['http://2130706433','http://[::ffff:127.0.0.1]','http://example.com:22','ftp://example.com','https://a:b@example.com']) await assert.rejects(resolvePublicTarget(url));
 assert.equal((await resolvePublicTarget('https://example.com')).address,'93.184.216.34');
 dns.lookup = async () => [{address:'8.8.8.8',family:4},{address:'127.0.0.1',family:4}];
 await assert.rejects(resolvePublicTarget('https://example.com'));
 dns.lookup = async () => [{address:'93.184.216.34',family:4}];
 axios.default.get = async (url, opts) => {
 calls++; assert.equal(opts.proxy,false); assert.equal(opts.maxRedirects,0); assert.equal(opts.responseType,'stream'); assert.equal(opts.transport,undefined);
 opts.httpsAgent.options.lookup('example.com',{},(err,ip) => assert.equal(ip,'93.184.216.34'));
 const data = new Readable({read(){}}); data.on('close',()=>{}); const destroy=data.destroy.bind(data); data.destroy=()=>{destroyed=true; return destroy()};
 return {status:302,headers:{location:'http://127.0.0.1/admin'},data};
 };
 await assert.rejects(safeGet('https://example.com',{proxy:{host:'localhost'},transport:{}}));
 assert.equal(calls,1); assert.equal(destroyed,true);
 } finally {dns.lookup=oldLookup; axios.default.get=oldGet;}
});
