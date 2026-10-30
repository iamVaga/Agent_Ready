import io
import json
import logging
import os
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import httpx
from shared.privacy import redact, redact_text, safe_url, cart_fields, SYNTHETIC_IDENTITY
from shared import memory
from tester_agent.web import Browser, safe_request
from tester_agent.signals import read
from tester_agent.shopper import Report, Step, format_report

SECRET = "alice.private@example.net +91 98765 43210 123 Maple Street sk-secret123456789 4111 1111 1111 session_token=opaque-session-value"
FORBIDDEN = ['alice.private@example.net','98765','Maple','sk-secret123456789','4111','opaque-session-value','url-private-secret']

class PrivacyTests(unittest.IsolatedAsyncioTestCase):
    def clean(self, value):
        text = value if isinstance(value,str) else json.dumps(value)
        for token in FORBIDDEN: self.assertNotIn(token,text)

    async def test_nested_redaction_and_products(self):
        self.clean(redact({'evidence':SECRET,'cookies':{'sid':'url-private-secret'},'cvv':'4111','address':'123 Maple Street'}))
        self.clean(redact_text('Cookie: sid=url-private-secret; customer=alice.private@example.net'))
        self.assertEqual(safe_url('https://a:b@example.com/products/shoe?id=42&session=url-private-secret'),'https://example.com/products/shoe?id=42')
        self.assertEqual(redact_text('Shoes ₹2,500, HTTP 200'), 'Shoes ₹2,500, HTTP 200')

    async def test_llm_boundaries(self):
        from agentready_agent import brain
        from fixer_agent import coder
        captured=[]
        class Completions:
            async def create(self, **kwargs):
                captured.append(kwargs['messages'])
                if kwargs.get('stream'):
                    async def chunks():
                        if False: yield None
                    return chunks()
                return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='{"action":"scan","url":"https://example.com"}'))])
        client=SimpleNamespace(chat=SimpleNamespace(completions=Completions()))
        with patch.object(brain,'_llm',client): await brain.understand('Scan https://example.com '+SECRET)
        await coder._stream(client,messages=[{'role':'user','content':SECRET},{'role':'tool','content':SECRET}])
        self.assertEqual(len(captured),2)
        self.clean(captured)

    async def test_memory_persistence_and_expiry(self):
        store=memory._Local()
        with patch.object(memory,'_backend',store):
            await memory.update('agent-user',task=SECRET,session_token='url-private-secret')
            await memory.remember('agent-user','Tester','user',SECRET)
            await memory.post_task('tester','test','agent-user',task=SECRET)
            self.clean(store.kv); self.clean(store.lists)
            self.clean(await memory.summary('agent-user'))
            store.expires['facts:agent-user']=time.time()-1
            self.assertEqual(await memory.facts('agent-user'),{})

    async def test_reports(self):
        r=Report(site='https://example.com',task=SECRET,steps=[Step('Find',True,SECRET)],checkout_url='https://example.com/checkout?session=url-private-secret')
        self.clean(r.to_dict()); self.clean(format_report(r))

    async def test_customer_extraction_and_cart_forms(self):
        html='<h1>Public shoes</h1><p>'+SECRET+'</p><form action="/cart/add" method="post"><input name="product_id" value="42"><input name="quantity" value="1"><input name="csrf_token" value="ephemeral-token"><input name="email" value="alice.private@example.net"><input name="customer_name" value="Real Customer"></form>'
        signals=read(html)
        self.clean(signals.text)
        self.assertNotIn('Real Customer',str(signals.forms))
        self.assertEqual(signals.forms[0].fields[:3],[('product_id','42'),('quantity','1'),('csrf_token','ephemeral-token')])
        with self.assertRaises(ValueError): cart_fields(signals.forms[0].fields)
        self.assertEqual(cart_fields(signals.forms[0].fields[:3]),signals.forms[0].fields[:3])
        self.assertTrue(SYNTHETIC_IDENTITY['email'].endswith('.invalid'))
        data=read('<script type="application/ld+json">{"@type":"Product","name":"Public shoe","offers":{"price":"2500"},"seller":{"email":"alice.private@example.net"}}</script>')
        self.assertEqual(data.products_ld[0]['name'],'Public shoe')
        self.clean(data.jsonld)

    async def test_http_logs_and_blocked_submissions(self):
        b=Browser('https://example.com')
        seen=[]
        async def handler(request):
            seen.append(request)
            return httpx.Response(200,text='Public shoe',request=request)
        await b.client.aclose()
        b.client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            r=await b.get('https://user:pass@example.com/p?email=alice.private@example.net&session=url-private-secret')
            self.assertEqual(r.status,0)
            for url,fields in [('https://example.com/checkout',[('product_id','42')]),('https://example.com/cart',[('email','alice.private@example.net')]),('https://example.com/cart',[('cc_number','4111111111111111')])]:
                self.assertEqual((await b.post_form(url,fields)).status,0)
            self.assertEqual(len(seen),0)
            self.assertEqual((await b.post_form('https://example.com/cart/add',[('product_id','42'),('csrf_token','ephemeral-token')])).status,200)
            self.clean(b.log)
            self.assertNotIn('ephemeral-token',str(b.log))
            b.client.cookies.set('session','url-private-secret')
        finally: await b.close()
        self.assertEqual(len(b.client.cookies),0)
        self.assertEqual(b.log,[])

    async def test_redirect_privacy_policy(self):
        seen=[]
        async def handler(request):
            seen.append(str(request.url))
            return httpx.Response(307,headers={"location":"https://example.com/checkout"})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with self.assertRaises(httpx.RequestError):
                await safe_request(client,"POST","https://example.com/cart",content=b"product_id=42")
        self.assertEqual(len(seen),1)
        self.assertNotIn("url-private-secret", format_report(Report(site="https://example.com",task="shoe",checkout_url="https://example.com/checkout?token=url-private-secret")))
        with self.assertRaises(ValueError): cart_fields([("action","place_order")])
        self.assertEqual(read('<script>var customer="Real Customer";</script><h1>Shoe</h1>').script_count,1)

    async def test_managed_temporary_cleanup(self):
        from fixer_agent import fixer
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            outside=root/"existing-records"
            outside.mkdir(); (outside/"keep.txt").write_text("Keep me")
            with patch.object(fixer,"WORK_DIR",root/"managed"):
                fixer.WORK_DIR.mkdir()
                with self.assertRaises(ValueError): fixer._rmtree(outside)
                self.assertTrue((outside/"keep.txt").exists())
                jobdir=fixer.WORK_DIR/"job-unit-test"
                jobdir.mkdir()
                job=SimpleNamespace(ws=jobdir,live={"task":"private"},live_text="private",task="private")
                fixer.cleanup(job)
                self.assertFalse(jobdir.exists());self.assertEqual(job.live,{})

    async def test_logging_boundary(self):
        output=io.StringIO(); handler=logging.StreamHandler(output); logger=logging.getLogger('privacy-test');logger.addHandler(handler);logger.setLevel(logging.INFO)
        try:
            logger.info('Request https://user:pass@example.com/p?id=opaque-session-value&token=url-private-secret '+SECRET)
            self.clean(output.getvalue()); self.assertNotIn('user:pass',output.getvalue())
        finally: logger.removeHandler(handler)

if __name__ == '__main__': unittest.main()
