"""Offline ASGI contract replay of payloads emitted by shipped HTML Node VM.
Green assertions below demonstrate defects; they do not certify correctness.
"""
import asyncio, copy, json, subprocess, unittest, os, tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
import httpx
import main, telegram_service
import test_checkout_webhook_integration as fixture

ROOT=Path(__file__).resolve().parents[2]
SCRIPT=ROOT/'frontend/tests/seven_round2.test.cjs'
REPORT=Path(os.environ.get('HOTBOT_REPORT', tempfile.mkdtemp(prefix='hotbot-contract-')))
REPORT.mkdir(parents=True,exist_ok=True)
class SevenFrontendContracts(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        main._DB_LOCK=asyncio.Lock()
        await fixture.CheckoutWebhookIntegration.asyncSetUp(self)
        self.manager._schedule_flow_downsell=lambda *a,**kw:None
        state=copy.deepcopy(await main._read_state_from_db())
        a=state['flow_bots'][0]
        a.update(cache_channel_id='-1001',welcome={'message':'ORIGINAL','medias':[]},
                 downsell_general={'enabled':True,'sequences':[{'id':'general-1','message':'ORIGINAL GENERAL','medias':[]}]},
                 downsell_pix={'enabled':True,'sequences':[{'id':'pix-1','message':'ORIGINAL PIX','medias':[]}]})
        b=copy.deepcopy(a);b.update(id='flow-b',bot_id='222',token='222:test-only',cache_channel_id='-1002')
        state['flow_bots'].append(b)
        await self.manager._get_or_create_lead(state,self.user,bot_token=self.bot.token)
        await main._write_state_to_db(state)
        main.app.dependency_overrides[main.verify_admin_auth]=lambda:None
        self.addCleanup(main.app.dependency_overrides.clear)
        self.client=httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app),base_url='http://test')
        self.bot.get_me=AsyncMock(return_value=SimpleNamespace(id=111,username='fixture'))
        self.bot.get_chat=AsyncMock(return_value=SimpleNamespace(id=-1001,title='Fixture Channel',username='fixture'))
        self.bot.session=SimpleNamespace(close=AsyncMock())
        self.factory=patch.object(telegram_service,'_create_bot_instance',return_value=self.bot)
        self.factory_mock=self.factory.start();self.addCleanup(self.factory.stop)
        self.config=patch.object(self.manager,'configure_and_start',new=AsyncMock(return_value='isolated'))
        self.config.start();self.addCleanup(self.config.stop)
        self.warming=patch.object(self.manager,'warm_all_state_medias',new=AsyncMock(return_value=0))
        self.warming.start();self.addCleanup(self.warming.stop)
    async def asyncTearDown(self):
        await self.client.aclose()
        await fixture.CheckoutWebhookIntegration.asyncTearDown(self)
    async def cold(self):
        main._STATE_CACHE=None
        return await main._read_state_from_db()
    def evidence(self,name,value):
        (REPORT/(name+'.json')).write_text(json.dumps(value,ensure_ascii=False,indent=2),encoding='utf8')
    async def test_failed_save_downsell_single_and_all_emit_no_delivery(self):
        self.node('downsell-failure')
        outputs=json.loads((REPORT/'downsell-failure-result.json').read_text())
        for output in outputs:
            self.assertFalse(output['requests'])
            if output.get('post'):
                self.assertEqual(output['post']['flow_bots'][0]['downsell_general']['sequences'][0]['message'],'NEW UNSAVED DOWNSELL')
        self.bot.send_message.assert_not_awaited()
        saved=await self.cold()
        self.assertEqual(saved['flow_bots'][0]['downsell_general']['sequences'][0]['message'],'ORIGINAL GENERAL')
    async def test_downsell_track_switch_real_caller_sends_clicked_general(self):
        self.node('downsell-track')
        output=json.loads((REPORT/'downsell-track-result.json').read_text())
        response=await self.client.post('/api/flow-bots/downsell/test-send',json=output['request'])
        self.assertEqual(response.status_code,200,response.text)
        texts=[c.kwargs['text'] for c in self.bot.send_message.await_args_list]
        self.assertIn('ORIGINAL GENERAL',texts)
        self.assertNotIn('ORIGINAL PIX',texts)
    async def test_downsell_preview_saved_text_stable_ids_and_refresh_controls(self):
        self.node('preview-controls')
        outputs=json.loads((REPORT/'preview-controls-result.json').read_text())
        for output in outputs:
            response=await self.client.post('/api/state',json=output['post'])
            self.assertEqual(response.status_code,200,response.text)
            response=await self.client.post('/api/flow-bots/downsell/test-send',json=output['request'])
            self.assertEqual(response.status_code,200,response.text)
        texts=[c.kwargs['text'] for c in self.bot.send_message.await_args_list]
        self.assertEqual(texts.count('NEW SAVED DOWNSELL'),2)
        self.assertNotIn('ORIGINAL GENERAL',texts)
    async def test_cache_switch_real_caller_does_not_contact_or_modify_b(self):
        self.node('cache-switch')
        output=json.loads((REPORT/'cache-switch-result.json').read_text())
        self.assertNotIn('request',output)
        saved=await self.cold()
        self.assertEqual(saved['flow_bots'][1]['cache_channel_id'],'-1002')
        self.factory_mock.assert_not_called()
    async def test_verify_channel_rejects_unknown_or_wrong_origin_before_transport(self):
        response=await self.client.post('/api/flow-bots/verify-channel',json={
            'bot_id':'missing','token':'***configured***','channel_id':'-1001'})
        self.assertEqual(response.status_code,404,response.text)
        response=await self.client.post('/api/flow-bots/verify-channel',json={
            'bot_id':'flow-a','origin_bot_numeric_id':'222','channel_id':'-1001'})
        self.assertEqual(response.status_code,409,response.text)
        self.factory_mock.assert_not_called()

    async def test_verify_channel_held_transport_cannot_restore_replaced_bot_config(self):
        reached,finish=asyncio.Event(),asyncio.Event()
        async def get_chat(**kwargs):
            reached.set()
            await finish.wait()
            return SimpleNamespace(id=-1001,title='Fixture',username='fixture')
        self.bot.get_chat=AsyncMock(side_effect=get_chat)
        task=asyncio.create_task(self.client.post('/api/flow-bots/verify-channel',json={
            'bot_id':'flow-a','origin_bot_numeric_id':'111','channel_id':'-1001'}))
        try:
            await asyncio.wait_for(reached.wait(),5)
            fresh=copy.deepcopy(await main._read_state_from_db())
            fresh['flow_bots'][0].update(token='333:test-only',bot_id='333',cache_channel_id='-1009')
            fresh['flow_bots'][0]['welcome']['message']='NEW SERVER CONFIG'
            await main._write_state_to_db(fresh)
            finish.set()
            response=await asyncio.wait_for(task,5)
            self.assertIn(response.status_code,(409,400),response.text)
        finally:
            finish.set()
            await asyncio.gather(task,return_exceptions=True)
        saved=await self.cold()
        self.assertEqual(saved['flow_bots'][0]['token'],'333:test-only')
        self.assertEqual(saved['flow_bots'][0]['cache_channel_id'],'-1009')
        self.assertEqual(saved['flow_bots'][0]['welcome']['message'],'NEW SERVER CONFIG')
        self.assertEqual(saved['flow_bots'][1]['cache_channel_id'],'-1002')

    async def test_cache_frontend_guards_and_real_response_preserve_edit_delete_secret_source(self):
        self.node('cache-controls')
        state=copy.deepcopy(await main._read_state_from_db())
        state['flow_bots'][0]['welcome']['medias']=[{'id':'m1','type':'photo','url':'https://example.invalid/old-source'}]
        await main._write_state_to_db(state)
        initial=(await self.client.get('/api/state')).json()
        self.evidence('cache-contract',{'initial':initial})
        self.node('cache-contract',REPORT/'cache-contract.json')
        produced=json.loads((REPORT/'cache-contract-result.json').read_text())
        response=await self.client.post('/api/state',json=produced['firstPost'])
        self.assertEqual(response.status_code,200,response.text)
        async def warm(st):
            st['flow_bots'][0]['welcome']['medias'][0]['telegram_file_id']='old-source-cache'
            return 1
        with patch.object(self.manager,'warm_all_state_medias',new=warm):
            response=await self.client.post('/api/flow-bots/verify-channel',json=produced['request'])
        self.assertEqual(response.status_code,200,response.text)
        self.assertEqual(response.json()['status'],'ok')
        self.evidence('cache-contract',{'initial':initial,'action':response.json()})
        self.node('cache-contract',REPORT/'cache-contract.json')
        output=json.loads((REPORT/'cache-contract-result.json').read_text())
        response=await self.client.post('/api/state',json=output['post'])
        self.assertEqual(response.status_code,200,response.text)
        saved=await self.cold()
        self.assertEqual(saved['flow_bots'][0]['welcome']['message'],'NEW CACHE WELCOME')
        self.assertEqual(saved['flow_bots'][0]['welcome']['medias'][0]['url'],'https://example.invalid/new-source')
        self.assertNotIn('telegram_file_id',saved['flow_bots'][0]['welcome']['medias'][0])
        self.assertEqual(saved['publish']['gateway_secret'],'new-cache-secret')
        self.assertFalse(saved['flow_bots'][1]['plans'])
        self.assertEqual(saved['flow_bots'][1]['cache_channel_id'],'-1002')

    async def test_cache_warming_source_replacement_preserves_new_config_and_skips_old_id(self):
        state=copy.deepcopy(await main._read_state_from_db())
        state['flow_bots'][0]['welcome']['medias']=[{'id':'m1','type':'photo','url':'https://example.invalid/old'}]
        await main._write_state_to_db(state)
        async def warm(st):
            fresh=copy.deepcopy(await main._read_state_from_db())
            fresh['flow_bots'][0]['welcome']['medias'][0]['url']='https://example.invalid/new'
            fresh['flow_bots'][0]['welcome']['message']='NEW SERVER WELCOME'
            await main._write_state_to_db(fresh)
            st['flow_bots'][0]['welcome']['medias'][0]['telegram_file_id']='old-source-id'
            return 1
        with patch.object(self.manager,'warm_all_state_medias',new=warm):
            response=await self.client.post('/api/flow-bots/verify-channel',json={
                'bot_id':'flow-a','origin_bot_numeric_id':'111','channel_id':'-1001'})
        self.assertEqual(response.status_code,200,response.text)
        saved=await self.cold()
        self.assertEqual(saved['flow_bots'][0]['welcome']['message'],'NEW SERVER WELCOME')
        media=saved['flow_bots'][0]['welcome']['medias'][0]
        self.assertEqual(media['url'],'https://example.invalid/new')
        self.assertNotIn('telegram_file_id',media)

    async def test_cache_config_save_does_not_transfer_ids_between_bot_origins(self):
        state=copy.deepcopy(await main._read_state_from_db())
        state['flow_bots'][0]['welcome']['medias']=[{'id':'m1','type':'photo','url':'https://example.invalid/shared-source','telegram_file_id':'old-bot-id'}]
        await main._write_state_to_db(state)
        edited=(await self.client.get('/api/state')).json()
        edited['flow_bots'][0].update(token='333:test-only',bot_id='333')
        edited['flow_bots'][0]['welcome']['medias'][0].pop('telegram_file_id')
        response=await self.client.post('/api/state',json=edited)
        self.assertEqual(response.status_code,200,response.text)
        saved=await self.cold()
        self.assertNotIn('telegram_file_id',saved['flow_bots'][0]['welcome']['medias'][0])

    def node(self, mode, contract=None):
        args=['node',str(SCRIPT),mode]+([str(contract)] if contract else [])
        result=subprocess.run(args,capture_output=True,text=True,timeout=30,env={**os.environ,'HOTBOT_REPORT':str(REPORT)})
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        return result
    async def test_crm_real_response_erases_new_frontend_edit_and_next_save_persists_old_value(self):
        initial=(await self.client.get('/api/state')).json()
        self.evidence('crm-initial', initial)
        producer=self.node('crm-request', REPORT/'crm-initial.json')
        request=json.loads(producer.stdout)
        response=await self.client.post('/api/flow-bots/leads/action',json=request)
        self.assertEqual(response.status_code,200);self.assertEqual(response.json()['status'],'ok')
        self.evidence('crm-contract',{'initial':initial,'action':response.json()})
        run=self.node('crm-response', REPORT/'crm-contract.json')
        (REPORT/'node-crm-contract.log').write_text(run.stdout+run.stderr,encoding='utf8')
        self.assertEqual(run.returncode,0,run.stdout+run.stderr)
        output=json.loads((REPORT/'crm-node-result.json').read_text())
        self.assertEqual(output['after'],'NEW UNSAVED WELCOME')
        saved=await self.client.post('/api/state',json=output['post']);self.assertEqual(saved.status_code,200)
        cold=await self.cold();self.assertEqual(cold['flow_bots'][0]['welcome']['message'],'NEW UNSAVED WELCOME')
        self.assertEqual(cold['memory']['leads'][0]['history'][-1]['content'],'AUDIT CRM MESSAGE')
        self.evidence('crm-observed',{'new_typed_message':'NEW UNSAVED WELCOME','after_frontend':output['after'],'cold_db_welcome':cold['flow_bots'][0]['welcome']['message'],'crm_history_preserved':True})
