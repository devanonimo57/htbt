"""Feed a real ASGI rejection into shipped JS and replay its reconciliation."""
import asyncio
import json
from pathlib import Path
import unittest

import main
import test_remarketing_interruption as fixtures


class RemarketingFrontendRejection(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.RemarketingInterruption.asyncSetUp
    asyncTearDown = fixtures.RemarketingInterruption.asyncTearDown
    _close = fixtures.RemarketingInterruption._close
    persisted = fixtures.RemarketingInterruption.persisted

    async def test_rejected_frontend_proposal_reconciles_confirmed_run(self):
        state = await main._read_state_from_db()
        state['remarketing_campaigns'][0].update(run_id='old-id')
        await main._write_state_to_db(state)
        await main._claim_remarketing_recipient(main._remarketing_claim_key(state['remarketing_campaigns'][0]), '111:41')
        main._STATE_CACHE = None
        response = await self.client.post('/api/flow-bots/remarketing/send', json={
            'campaign_id':'camp', 'run_id':'proposed-id', 'start_new_run':True})
        self.assertEqual(response.status_code, 409)
        script = Path(__file__).resolve().parents[2] / 'frontend/tests/remarketing_run.test.cjs'
        process = await asyncio.create_subprocess_exec('node', str(script), '--asgi-rejection', str(response.status_code),
                                                      stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        out, err = await process.communicate()
        self.assertEqual(process.returncode, 0, err.decode())
        requests = json.loads(out)
        self.assertEqual(requests[0]['payload']['run_id'], 'proposed-id')
        reconcile = await self.client.post(requests[1]['url'], json=requests[1]['payload'])
        self.assertEqual(reconcile.status_code, 200, reconcile.text)
        self.release_second.set()
        resume = await self.client.post(requests[2]['url'], json=requests[2]['payload'])
        self.assertEqual(resume.status_code, 200, resume.text)
        saved = await self.persisted()
        self.assertEqual(saved['remarketing_campaigns'][0]['run_id'], 'old-id')
        self.assertEqual(saved['remarketing_campaigns'][0]['deliveries']['111:41'], 'sent')
        self.assertEqual(self.sent, [42,43])
