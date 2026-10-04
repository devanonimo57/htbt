import asyncio, copy, json, os, subprocess, unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
import main
import test_seven_frontend_contracts as original
REPORT=Path(os.environ['HOTBOT_REPORT']); REPORT.mkdir(parents=True,exist_ok=True)
ROOT=Path(__file__).resolve().parents[2]
class FiveActionsContracts(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        main._STATE_COMMITTED={}; main._STATE_COMMITTED_OWNER=None
        await original.SevenFrontendContracts.asyncSetUp(self)
    asyncTearDown=original.SevenFrontendContracts.asyncTearDown
    cold=original.SevenFrontendContracts.cold
    def evidence(self,name,value):
        (REPORT/(name+'.json')).write_text(json.dumps(value,ensure_ascii=False,indent=2),encoding='utf8')
    def node(self,mode,kind,name):
        result=subprocess.run(['node','--require','C:/Users/Yago/hotbot-eight-followup-report/offline-node.cjs',str(ROOT/'frontend/tests/five_actions.test.cjs'),mode,kind,str(REPORT/(name+'.json'))],capture_output=True,text=True,timeout=30)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        return result.stdout
    async def checkout(self,channel=False):
        state=copy.deepcopy(await main._read_state_from_db())
        if channel:
            state['flow_bots'][0]['plans'][0].update(delivery_dest_type='Canal ou grupo',delivery_channel_id='-100800')
            self.bot.create_chat_invite_link=AsyncMock(return_value=SimpleNamespace(invite_link='https://example.invalid/invite'))
            await main._write_state_to_db(state)
            state=await main._read_state_from_db()
        await self.manager._generate_flow_checkout(self.callback,state['flow_bots'][0],state,'plan-a',0,[])
        tx=self.created[-1][0]
        if channel:
            self.gateway_status[tx]='paid'
            response=await self.client.post('/api/webhook/syncpay',json={'id':tx,'status':'paid'})
            self.assertEqual(response.status_code,200,response.text)
            self.assertEqual(response.json()['action'],'approved',response.text)
            rec=(await self.cold())['tx_records'][tx]
            self.assertEqual(rec['status'],'paid')
            self.assertEqual(rec['delivery_channel_id'],'-100800')
            self.assertEqual(rec['bot_token'],'111:test-only')
        return tx
    async def test_removal_success_held_transport_preserves_fresh_state_and_other_purchase(self):
        tx = await self.checkout(channel=True)
        other = await self.checkout(channel=True)
        before = copy.deepcopy((await self.cold())['tx_records'][other])
        initial = (await self.client.get('/api/state')).json()
        self.evidence('success-removal-contract',{'initial':initial,'tx_id':tx})
        request = json.loads(self.node('request','remove','success-removal-contract'))
        reached,finish = asyncio.Event(),asyncio.Event()
        async def ban(**kwargs):
            self.assertFalse(main._DB_LOCK.locked())
            reached.set(); await finish.wait(); return True
        self.bot.ban_chat_member = AsyncMock(side_effect=ban)
        task = asyncio.create_task(self.client.post(request['url'],json=request['body']))
        try:
            await asyncio.wait_for(reached.wait(),5)
            live = await main._read_state_from_db()
            self.assertEqual(live['tx_records'][tx]['access_status'],'Ativo')
            detached = copy.deepcopy(live)
            detached['flow_bots'][0]['welcome']['message'] = 'FRESH DURING BAN'
            detached['memory']['leads'][0]['history'].append({'role':'user','content':'DURING BAN'})
            await asyncio.wait_for(main._write_state_to_db(detached),5)
            finish.set(); response = await asyncio.wait_for(task,5)
        finally:
            finish.set(); await asyncio.gather(task,return_exceptions=True)
        self.assertEqual(response.status_code,200,response.text)
        self.factory_mock.assert_called_with('111:test-only')
        cold = await self.cold()
        self.assertEqual(cold['tx_records'][tx]['access_status'],'Removido')
        self.assertEqual(cold['tx_records'][other],before)
        self.assertEqual(cold['flow_bots'][0]['welcome']['message'],'FRESH DURING BAN')
        self.assertIn('DURING BAN',[h['content'] for h in cold['memory']['leads'][0]['history']])
        self.assertEqual(cold['flow_bots'][0]['sales_today'],2)
        for rec in cold['tx_records'].values():
            self.assertEqual(rec['status'],'paid');self.assertTrue(rec['sales_counted']);self.assertTrue(rec['delivered'])
        self.evidence('success-removal-contract',{'initial':initial,'tx_id':tx,'action':response.json(),'http_status':200})
        self.node('removal-response','remove','success-removal-contract')

    async def test_removal_missing_identity_and_unconfirmed_ban_fail_closed(self):
        tx = await self.checkout(channel=True)
        original_state = copy.deepcopy(await self.cold())
        for case in ('bot','token','channel','wrong-origin','wrong-transport','false-ban'):
            with self.subTest(case=case):
                state = await main._read_state_from_db()
                state['flow_bots'] = copy.deepcopy(original_state['flow_bots'])
                state['tx_records'][tx] = copy.deepcopy(original_state['tx_records'][tx])
                if case=='bot': state['flow_bots'] = []
                if case=='token': state['tx_records'][tx].pop('bot_token')
                if case=='channel': state['tx_records'][tx].pop('delivery_channel_id')
                if case=='wrong-origin': state['flow_bots'][0]['token']='333:test-only'
                self.bot.id = 222 if case=='wrong-transport' else 111
                await main._write_state_to_db(state)
                self.bot.ban_chat_member = AsyncMock(return_value=False)
                response = await self.client.post('/api/flow-bots/access/update-status',json={'tx_id':tx,'status':'Removido'})
                self.assertIn(response.status_code,(409,502),response.text)
                self.assertEqual((await main._read_state_from_db())['tx_records'][tx]['access_status'],'Ativo')
                rec = (await self.cold())['tx_records'][tx]
                self.assertEqual(rec['access_status'],'Ativo');self.assertTrue(rec['sales_counted']);self.assertTrue(rec['delivered'])
                if case!='false-ban': self.bot.ban_chat_member.assert_not_awaited()
        self.bot.id=111

    async def test_successful_ban_failed_sql_commit_does_not_publish_removed(self):
        from sqlalchemy.ext.asyncio import AsyncSession
        from unittest.mock import patch
        import httpx
        tx = await self.checkout(channel=True)
        before = copy.deepcopy(await main._read_state_from_db())
        self.bot.ban_chat_member = AsyncMock(return_value=True)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app,raise_app_exceptions=False),base_url='http://test') as client:
            with patch.object(AsyncSession,'commit',side_effect=RuntimeError('offline removal commit failure')):
                response = await client.post('/api/flow-bots/access/update-status',json={'tx_id':tx,'status':'Removido'})
        self.assertEqual(response.status_code,500,response.text)
        self.assertEqual((await main._read_state_from_db())['tx_records'],before['tx_records'])
        self.assertEqual((await self.cold())['tx_records'],before['tx_records'])
        self.assertIn('application/json',response.headers.get('content-type',''))
        self.assertIn('salvar',response.json()['detail'])

    async def test_each_action_failed_commit_real_response_next_save_preserves_intent(self):
        from sqlalchemy.ext.asyncio import AsyncSession
        from unittest.mock import patch
        import httpx
        for kind in ('block','access','delete'):
            with self.subTest(kind=kind):
                tx=await self.checkout() if kind=='access' else None
                initial=(await self.client.get('/api/state')).json()
                before=copy.deepcopy(await main._read_state_from_db())
                name=kind+'-failure-contract'
                self.evidence(name,{'initial':initial,'tx_id':tx})
                request=json.loads(self.node('request',kind,name))
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app,raise_app_exceptions=False),base_url='http://test') as client:
                    with patch.object(AsyncSession,'commit',side_effect=RuntimeError('offline action commit failure')):
                        response=await client.request(request['method'],request['url'],json=request.get('body'))
                self.assertEqual(response.status_code,500,response.text)
                self.assertEqual((await main._read_state_from_db())['memory'],before['memory'])
                self.assertEqual((await self.cold())['tx_records'],before['tx_records'])
                action=response.json() if 'application/json' in response.headers.get('content-type','') else {'detail':response.text}
                self.evidence(name,{'initial':initial,'tx_id':tx,'action':action,'http_status':response.status_code,**({'raw_text':response.text} if 'application/json' not in response.headers.get('content-type','') else {})})
                self.node('failure',kind,name)
                observed=json.loads((REPORT/(kind+'-observed.json')).read_text(encoding='utf8'))
                response=await self.client.post('/api/state',json=observed['post'])
                self.assertEqual(response.status_code,200,response.text)
                cold=await self.cold()
                self.assertEqual(cold['flow_bots'][0]['welcome']['message'],'NEW EDIT DURING ACTION')
                self.assertEqual(cold['publish']['gateway_secret'],'NEW ACTION SECRET')
                self.assertEqual(cold['memory'],before['memory'])
                self.assertEqual(cold['tx_records'],before['tx_records'])

    async def test_failed_ban_preserves_active_access_and_reports_ui_error(self):
        tx=await self.checkout(channel=True)
        initial=(await self.client.get('/api/state')).json()
        self.evidence('removal-contract',{'initial':initial,'tx_id':tx})
        request=json.loads(self.node('request','remove','removal-contract'))
        self.bot.ban_chat_member=AsyncMock(side_effect=RuntimeError('fixture: Telegram rejected ban (no rights)'))
        response=await self.client.post(request['url'],json=request['body'])
        self.assertEqual(response.status_code,502,response.text)
        self.bot.ban_chat_member.assert_awaited_once_with(chat_id=-100800,user_id=7)
        cold=await self.cold()
        self.assertEqual(cold['tx_records'][tx]['access_status'],'Ativo')
        self.assertTrue(cold['tx_records'][tx]['delivered'])
        self.assertTrue(cold['tx_records'][tx]['sales_counted'])
        self.assertEqual(cold['tx_records'][tx]['status'],'paid')
        self.evidence('removal-contract',{'initial':initial,'tx_id':tx,'action':response.json(),'http_status':response.status_code})
        self.node('removal-response','remove','removal-contract')
        observed=json.loads((REPORT/'removal-ui-observed.json').read_text(encoding='utf8'))
        saved=await self.client.post('/api/state',json=observed['post'])
        self.assertEqual(saved.status_code,200,saved.text)
        self.assertEqual((await self.cold())['tx_records'][tx]['access_status'],'Ativo')
        self.assertEqual((await main._read_state_from_db())['tx_records'][tx]['access_status'],'Ativo')

    async def test_actions_preserve_new_panel_edits_real_response_and_cold_save(self):
        for kind in ('block','access','delete'):
            with self.subTest(kind=kind):
                # Restore the single customer's active placement before each action.
                state=copy.deepcopy(await main._read_state_from_db())
                state['flow_bots'][0]['welcome']['message']='ORIGINAL'
                state['flow_bots'][0]['welcome']['medias']=[{'id':'remove-m','url':'https://example.invalid/old','type':'photo'}]
                state['flow_bots'][1]['plans']=copy.deepcopy(state['flow_bots'][0]['plans'])
                await main._write_state_to_db(state)
                tx=await self.checkout() if kind=='access' else None
                # Real paid peer checkout: each action must leave B intact.
                peer_bot=SimpleNamespace(token='222:test-only',send_message=AsyncMock())
                self.manager.extra_bots[peer_bot.token]=peer_bot
                peer_callback=SimpleNamespace(from_user=self.user,bot=peer_bot,message=self.callback.message)
                current=await main._read_state_from_db()
                await self.manager._generate_flow_checkout(peer_callback,current['flow_bots'][1],current,'plan-a',0,[])
                peer_tx=self.created[-1][0];self.gateway_status[peer_tx]='paid'
                approved=await self.client.post('/api/webhook/syncpay',json={'id':peer_tx,'status':'paid'})
                self.assertEqual(approved.status_code,200,approved.text)
                self.assertEqual(approved.json()['action'],'approved',approved.text)
                before_peer=copy.deepcopy((await self.cold())['tx_records'][peer_tx])
                before_lead=copy.deepcopy(main._client_map(await main._read_state_from_db())[('222','7',False)])
                initial=(await self.client.get('/api/state')).json()
                self.evidence(kind+'-contract',{'initial':initial,'tx_id':tx})
                request=json.loads(self.node('request',kind,kind+'-contract'))
                response=await self.client.request(request['method'],request['url'],json=request.get('body'))
                self.assertEqual(response.status_code,200,response.text)
                self.evidence(kind+'-contract',{'initial':initial,'action':response.json(),'tx_id':tx})
                output=self.node('response',kind,kind+'-contract')
                observed=json.loads((REPORT/(kind+'-observed.json')).read_text(encoding='utf8'))
                self.assertEqual(observed['after'],'NEW EDIT DURING ACTION')
                save=await self.client.post('/api/state',json=observed['post'])
                self.assertEqual(save.status_code,200,save.text)
                cold=await self.cold()
                self.assertEqual(cold['flow_bots'][0]['welcome']['message'],'NEW EDIT DURING ACTION')
                self.assertEqual(cold['publish']['gateway_secret'],'NEW ACTION SECRET')
                self.assertFalse(cold['flow_bots'][0]['welcome']['medias'])
                self.assertFalse(cold['flow_bots'][1]['plans'])
                self.assertEqual(cold['tx_records'][peer_tx],before_peer)
                self.assertEqual(main._client_map(cold)[('222','7',False)],before_lead)
                self.assertTrue(cold['tx_records'][peer_tx]['delivered'])
                self.assertTrue(cold['tx_records'][peer_tx]['sales_counted'])
                self.evidence(kind+'-cold',{'typed':'NEW EDIT DURING ACTION','frontend':observed['after'],'cold_db_welcome':cold['flow_bots'][0]['welcome']['message'],'node':output})
