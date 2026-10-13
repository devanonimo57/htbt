import unittest
import copy
from unittest.mock import AsyncMock, patch
import httpx
import main

class RedirectLinksTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await main.init_db()
        main.app.dependency_overrides[main.verify_admin_auth] = lambda: None

    async def asyncTearDown(self):
        main.app.dependency_overrides.pop(main.verify_admin_auth, None)

    async def test_get_redirect_links(self):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://test") as client:
            resp = await client.get("/api/redirect-links")
            self.assertEqual(resp.status_code, 200)
            data = resp.json()
            self.assertEqual(data["status"], "ok")
            self.assertIsInstance(data["links"], list)

    async def test_create_and_duplicate_and_delete_redirect_link(self):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://test") as client:
            new_payload = {
                "name": "Link de Teste Auditado",
                "is_active": True,
                "public_domain": "phantoms.group",
                "slug": "teste-unitario-123",
                "destination_type": "bot",
                "selected_flows": ["flow_1"],
                "distribution_order": "aleatoria",
                "ad_platform": "meta",
                "protection_level": "v2",
                "blocked_action": "safe_page",
                "block_ad_bots": True,
                "anti_clone": True,
                "fingerprint_check": True,
                "filter_location": True,
                "allowed_countries": ["BR"],
                "check_language": True
            }
            # Criar
            resp = await client.post("/api/redirect-links", json=new_payload)
            self.assertEqual(resp.status_code, 200)
            res_data = resp.json()
            created_id = res_data["link"]["id"]
            self.assertTrue(created_id.startswith("link_"))

            # Duplicar
            resp_dup = await client.post(f"/api/redirect-links/{created_id}/duplicate")
            self.assertEqual(resp_dup.status_code, 200)
            dup_data = resp_dup.json()
            self.assertIn("Cópia", dup_data["link"]["name"])
            dup_id = dup_data["link"]["id"]

            # Excluir ambos
            del_1 = await client.delete(f"/api/redirect-links/{created_id}")
            self.assertEqual(del_1.status_code, 200)
            del_2 = await client.delete(f"/api/redirect-links/{dup_id}")
            self.assertEqual(del_2.status_code, 200)

    async def test_public_redirect_crawler_blocked(self):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://test") as client:
            # Criar link para teste de crawler
            payload = {
                "name": "Link Teste Crawler",
                "slug": "crawler-test-slug",
                "is_active": True,
                "protection_level": "v2",
                "block_ad_bots": True,
                "blocked_action": "safe_page"
            }
            await client.post("/api/redirect-links", json=payload)

            # Requisitar com User-Agent de crawler do facebook
            resp = await client.get("/l/crawler-test-slug", headers={"User-Agent": "facebookexternalhit/1.1 (+http://www.facebook.com/externalhit_uatext.php)"})
            self.assertEqual(resp.status_code, 200)
            self.assertIn("Página Segura", resp.text)
            self.assertIn("Artigos em Destaque", resp.text)

            # Limpar
            await client.delete("/api/redirect-links/crawler-test-slug")

    async def test_public_redirect_approved_traffic(self):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://test") as client:
            # Criar link para teste de tráfego aprovado
            payload = {
                "name": "Link Teste Aprovado",
                "slug": "approved-test-slug",
                "is_active": True,
                "protection_level": "v2",
                "destination_type": "channel",
                "channel_invite_url": "https://t.me/+vip_exclusivo"
            }
            await client.post("/api/redirect-links", json=payload)

            headers = {
                "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 16_5 like Mac OS X) AppleWebKit/605.1.15",
                "Referer": "https://www.instagram.com/",
                "CF-IPCountry": "BR",
                "Accept-Language": "pt-BR,pt;q=0.9"
            }
            resp = await client.get("/l/approved-test-slug", headers=headers, follow_redirects=False)
            self.assertEqual(resp.status_code, 307)
            self.assertEqual(resp.headers["location"], "https://t.me/+vip_exclusivo")

            # Limpar
            await client.delete("/api/redirect-links/approved-test-slug")


if __name__ == "__main__":
    unittest.main()
