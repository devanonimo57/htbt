"""Execute shipped downsell functions in Node VM; persist their ASGI payload."""
import asyncio
import copy
import json
import os
from pathlib import Path
import subprocess
import unittest
from unittest.mock import AsyncMock, patch
import httpx
import main
import telegram_service
import test_checkout_webhook_integration as fixture

ROOT=Path(__file__).resolve().parents[2]
REPORT=Path(os.environ.get('HOTBOT_REPORT', str(Path(__file__).resolve().parent / 'report')))
REPORT.mkdir(parents=True, exist_ok=True)

class GeminiDownsellFrontend(unittest.IsolatedAsyncioTestCase):
    async def run_ui(self, mode):
        result=subprocess.run(['node',str(Path(__file__).with_name('gemini_downsell_ui.test.cjs')),
            str(ROOT/'frontend/public/index.html'),mode,str(REPORT/(mode+'-payload.json'))],capture_output=True,text=True,timeout=15)
        (REPORT/(mode+'-node.log')).write_text(result.stdout+result.stderr,encoding='utf8')
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        return json.loads((REPORT/(mode+'-payload.json')).read_text())

    async def test_unified_list_legacy_migration_real_reorder_audience_loop_accordion(self):
        await self.run_ui('unified')

    async def test_rapid_add_duplicate_have_unique_immutable_ids(self):
        await self.run_ui('ids')

    async def test_cycle_rest_and_card_options_payload_saved_through_real_asgi_and_cold_sql(self):
        bot=await self.run_ui('rest')
        import tempfile,uuid
        original_temp=tempfile.TemporaryDirectory
        main._DB_LOCK=asyncio.Lock();main._STATE_COMMITTED={};main._STATE_COMMITTED_OWNER=None
        with patch.object(fixture,'create_async_engine',main.create_application_engine), patch.object(
            fixture.tempfile,'TemporaryDirectory',lambda:original_temp(prefix='downsell-ui-'+uuid.uuid4().hex+'-',ignore_cleanup_errors=True)):
            await fixture.CheckoutWebhookIntegration.asyncSetUp(self)
        try:
            state=copy.deepcopy(await main._read_state_from_db())
            state['flow_bots'][0]['downsell_general']=bot['downsell_general']
            main.app.dependency_overrides[main.verify_admin_auth]=lambda:None
            with patch.object(self.manager,'configure_and_start',new=AsyncMock(return_value={'status':'offline'})),patch.object(self.manager,'warm_all_state_medias',new=AsyncMock(return_value=0)):
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app),base_url='http://test') as client:
                    response=await client.post('/api/state',json=state)
                    self.assertEqual(response.status_code,200,response.text)
            main._STATE_CACHE=None
            saved=(await main._read_state_from_db())['flow_bots'][0]['downsell_general']
            self.assertEqual(saved['loop_rest_minutes'],7)
            self.assertEqual([s['id'] for s in saved['sequences']],['p','a'])
            self.assertEqual(saved['sequences'][0]['trigger'],'pix')
            self.assertEqual(saved['sequences'][1]['trigger'],'start')
            self.assertEqual(saved['sequences'][1]['target_audience'],'all')
            self.assertTrue(saved['sequences'][0]['_collapsed'])
            self.assertTrue(saved['sequences'][0]['loop_enabled'])
            (REPORT/'cold-frontend-downsell.json').write_text(json.dumps(saved,indent=2),encoding='utf8')
        finally:
            main.app.dependency_overrides.clear()
            await fixture.CheckoutWebhookIntegration.asyncTearDown(self)
