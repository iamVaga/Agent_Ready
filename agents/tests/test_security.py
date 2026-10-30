import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import httpx
from tester_agent.web import Browser, fresh_get, resolve_target, public_ip, safe_request
from fixer_agent import workspace

class SecurityTests(unittest.IsolatedAsyncioTestCase):
    async def test_destinations(self):
        for ip in ["127.0.0.1", "10.0.0.1", "169.254.169.254", "::1", "fc00::1", "::ffff:127.0.0.1", "2002:7f00:1::", "2001::1"]:
            self.assertFalse(public_ip(ip), ip)
        for ip in ["8.8.8.8", "1.1.1.1", "2606:4700:4700::1111"]:
            self.assertTrue(public_ip(ip), ip)
        for url in ["http://127.0.0.1", "http://[::1]", "https://example.com:22", "ftp://example.com", "http://user:pass@example.com"]:
            with self.assertRaises(httpx.RequestError):
                await resolve_target(httpx.URL(url))
        loop = asyncio.get_running_loop()
        with patch.object(loop, "getaddrinfo", return_value=[(2,1,6,"",("93.184.216.34",443))]):
            self.assertEqual(await resolve_target(httpx.URL("https://example.com")), "93.184.216.34")
        with patch.object(loop, "getaddrinfo", return_value=[(2,1,6,"",("8.8.8.8",443)),(2,1,6,"",("127.0.0.1",443))]):
            with self.assertRaises(httpx.RequestError):
                await resolve_target(httpx.URL("https://example.com"))

    async def test_all_entrypoints_block(self):
        browser = Browser("http://127.0.0.1")
        try:
            self.assertEqual((await browser.get("http://127.0.0.1")).status, 0)
            self.assertEqual((await browser.post_form("http://127.0.0.1", [("a","b")])).status, 0)
            self.assertEqual((await browser.post_form("http://127.0.0.1", [], multipart=True)).status, 0)
            self.assertEqual((await fresh_get("http://127.0.0.1")).status, 0)
        finally:
            await browser.close()

    async def test_redirect_checked(self):
        async def handler(request):
            await resolve_target(request.url)
            return httpx.Response(302, headers={"location":"http://127.0.0.1/secret"})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with self.assertRaises(httpx.RequestError):
                await safe_request(client,"GET","https://8.8.8.8")

    async def test_pinned_transport(self):
        from tester_agent.web import public_client
        seen = []
        async def handler(request):
            seen.append(request)
            return httpx.Response(200, text="public page")
        class Transport(httpx.MockTransport):
            def __init__(self, **kwargs):
                self_outer.assertFalse(kwargs["trust_env"])
                super().__init__(handler)
        self_outer = self
        with patch("tester_agent.web.resolve_target", return_value="93.184.216.34"), patch("tester_agent.web.httpx.AsyncHTTPTransport", Transport):
            async with public_client() as client:
                response = await safe_request(client, "GET", "https://example.com/shop")
                self.assertEqual(response.text, "public page")
                self.assertEqual(str(response.url), "https://example.com/shop")
        self.assertEqual(seen[0].url.host, "93.184.216.34")
        self.assertEqual(seen[0].headers["host"], "example.com")
        self.assertEqual(seen[0].extensions["sni_hostname"], "example.com")

    async def test_response_limits(self):
        from tester_agent.web import public_client
        for headers, body in [({}, b"x" * 1025), ({"content-encoding":"gzip"}, b"compressed")]:
            async def handler(request):
                return httpx.Response(200, headers=headers, content=body)
            class Transport(httpx.MockTransport):
                def __init__(self, **kwargs):
                    super().__init__(handler)
            with patch("tester_agent.web.resolve_target", return_value="93.184.216.34"), patch("tester_agent.web.httpx.AsyncHTTPTransport", Transport), patch("tester_agent.web.MAX_BODY", 1024):
                async with public_client() as client:
                    with self.assertRaises(httpx.RequestError):
                        await safe_request(client, "GET", "https://example.com")

    async def test_execution_denied_and_env_scrubbed(self):
        with patch.dict(os.environ, {"GITHUB_TOKEN":"secret", "ASI1_API_KEY":"secret", "FIXER_ISOLATED_EXECUTION":"0"}):
            self.assertNotIn("GITHUB_TOKEN", workspace.child_env())
            code, _ = await workspace._run(["never-execute"],Path('.'),1)
            self.assertEqual(code,126)

    async def test_protected_paths(self):
        from fixer_agent.coder import Session
        with tempfile.TemporaryDirectory() as tmp:
            ws=Path(tmp)
            (ws/".env").write_text("SECRET=hidden")
            session=Session(ws,workspace.Stack("static","Static"),None,None)
            for name in [".env", "sub/../.env", ".git/config", "private.key"]:
                result=await session.call("read_file",{"path":name})
                self.assertTrue(result.startswith("Error:"),result)
                self.assertNotIn("hidden",result)
            self.assertIn("disabled",await session.call("run_script",{"path":"scripts/evil.py"}))

if __name__ == "__main__": unittest.main()
