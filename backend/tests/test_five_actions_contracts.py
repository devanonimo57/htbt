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
    async def test_actions_preserve_new_panel_edits_real_response_and_cold_save(self):
        for kind in ('block','access','delete'):
            with self.subTest(kind=kind):
                # Restore the single customer's active placement before each action.
                state=copy.deepcopy(await main._read_state_from_db())
                state['flow_bots'][0]['welcome']['message']='ORIGINAL'
                await main._write_state_to_db(state)
                tx=await self.checkout() if kind=='access' else None
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
                self.evidence(kind+'-cold',{'typed':'NEW EDIT DURING ACTION','frontend':observed['after'],'cold_db_welcome':cold['flow_bots'][0]['welcome']['message'],'node':output})
