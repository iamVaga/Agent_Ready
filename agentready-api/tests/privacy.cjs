const {test} = require('node:test');
const assert = require('node:assert/strict');
const {redact, redactText, safeUrl, privacyLog} = require('../dist/utils/privacy');
const {Scan} = require('../dist/models/Scan');
const {Lead} = require('../dist/models/Lead');
const {buildReportHtml} = require('../dist/services/reportHtml');
const {toFullScan,toGatedScan,toTeaser} = require('../dist/services/scanService');
const secret = 'alice.private@example.net +91 98765 43210 123 Maple Street sk-secret123456789 4111 1111 1111 1111 session_token=opaque-session-value';
const forbidden = ['alice.private@example.net','98765','Maple','sk-secret123456789','4111','opaque-session-value','url-private-secret','password-secret'];
function clean(value) { const text = typeof value==='string'?value:JSON.stringify(value); for(const token of forbidden) assert.ok(!text.includes(token), `leaked ${token}`); }
function scan() { return new Scan({domain:'example.com', submittedUrl:'https://user:password-secret@example.com/products/shoe?id=42&session=url-private-secret',overallScore:80,overallGrade:'B',summary:secret,siteType:'ecommerce',pagesScanned:['https://example.com/?email=alice.private@example.net'],checks:[{checkId:'structured_data',title:'Product schema',status:'fail',pointsAwarded:0,pointsPossible:20,details:secret,humanExplanation:secret,generatedFix:secret,agentAccess:[{agent:'GPTBot',label:'GPTBot',status:'allowed',detail:secret}]}]}); }
test('redacts nested identifiers, headers, encoded data and retains public product URLs', () => {
 clean(redact({evidence:secret,headers:{cookie:'url-private-secret'},card_number:'4111 1111 1111 1111',address:'123 Maple Street'}));
 clean(redactText(encodeURIComponent(secret)));
 clean(redactText('Cookie: sid=url-private-secret; customer=alice.private@example.net'));
 assert.equal(safeUrl('https://example.com/products/shoe?id=42&page=2&token=secret'),'https://example.com/products/shoe?id=42&page=2');
 assert.equal(safeUrl('https://user:pass@example.com/p?id=42#secret',true),'https://example.com/p');
 assert.equal(redactText('Shoes ₹2,500, Product schema, HTTP 200'), 'Shoes ₹2,500, Product schema, HTTP 200');
});
test('new Mongo records are redacted before persistence; existing records untouched', async () => {
 const doc = scan(); await doc.validate(); clean(doc.toObject());
 const lead = new Lead({scanId:doc._id,name:'Real Customer',email:'alice.private@example.net',whatsapp:'+91 98765 43210',consent:true,consentAt:new Date(),consentText:secret,consentSource:'report'});
 await lead.validate(); clean(lead.toObject()); assert.notEqual(lead.name,'Real Customer');
 const existing = scan(); existing.isNew=false; await existing.validate(); assert.equal(existing.summary,secret);
});
test('all response shapes and HTML redact legacy scan records', () => {
 const doc=scan(); for(const shape of [toFullScan,toGatedScan,toTeaser]) clean(shape(doc));
 clean(buildReportHtml(doc,'Real Customer')); assert.ok(!buildReportHtml(doc,'Real Customer').includes('Real Customer'));
});
test('privacy logger never emits raw error payloads or credentials', () => {
 const original=console.error; const values=[]; console.error=(...args)=>values.push(args);
 try {privacyLog(secret,new Error('body='+secret),{cookie:'url-private-secret'}); clean(values);} finally {console.error=original;}
});
test('API response redacts a stored legacy record', async () => {
 const {createApp}=require('../dist/app'); const original=Scan.findById;
 Scan.findById=()=>({exec:async()=>scan()});
 const server=createApp().listen(0,'127.0.0.1');
 try {
 await new Promise(resolve=>server.once('listening',resolve));
 const response=await fetch(`http://127.0.0.1:${server.address().port}/api/scan/0123456789abcdef01234567`);
 assert.equal(response.status,200); assert.equal(response.headers.get('cache-control'),'no-store'); clean(await response.json());
 } finally {Scan.findById=original; await new Promise(resolve=>server.close(resolve));}
});
