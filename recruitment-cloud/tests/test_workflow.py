import os, sys, tempfile, unittest, json
from datetime import datetime
from unittest.mock import patch
sys.path.insert(0,os.path.dirname(os.path.dirname(__file__)))
os.environ.update(HR_PASSWORD='test-only-long-password',SESSION_SECRET='test-only-session-secret')
import app as module

class Workflow(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory(); os.environ['LOCAL_DB']=self.tmp.name+'/test.sqlite'
  os.environ.pop('DATABASE_URL',None); os.environ.pop('VERCEL',None); os.environ.pop('GMAIL_APP_PASSWORD',None)
  self.client=module.app.test_client(); d=self.client.post('/api/login',json={'password':'test-only-long-password'}).get_json(); self.headers={'X-CSRF-Token':d['csrf']}
  self.client.get('/api/state')
  self.job=self.post('/api/jobs',{'title':'Purchase Executive','description':'Supplier sourcing, quotation comparison and Excel required.'}).get_json()['id']
 def tearDown(self): self.tmp.cleanup()
 def post(self,path,data):return self.client.post(path,json=data,headers=self.headers)
 def add(self,**kw):
  d={'job_id':self.job,'name':'Test Candidate','email':'candidate@example.com','phone':'+91 9876543210','resume_text':'Two years in procurement. Supplier sourcing, Excel and quotations.'};d.update(kw);return self.post('/api/candidates',d)
 def test_auth_and_csrf(self):
  self.assertEqual(module.app.test_client().get('/api/state').status_code,401)
  self.assertEqual(self.client.post('/api/jobs',json={}).status_code,403)
  self.assertEqual(module.app.test_client().get('/api/cron').status_code,401)
 def test_duplicate_identifiers_and_ack(self):
  self.assertFalse(self.add().get_json()['duplicate'])
  self.assertTrue(self.add(email=' CANDIDATE@EXAMPLE.COM ').get_json()['duplicate'])
  self.assertTrue(self.add(email='other@example.com').get_json()['duplicate'])
  state=self.client.get('/api/state').get_json();self.assertEqual(len(state['candidates']),1);self.assertEqual(len(state['outbox']),1);self.assertEqual(state['outbox'][0]['kind'],'acknowledgement')
 def test_approval_idempotent(self):
  id=self.add().get_json()['id']; r=self.post(f'/api/candidates/{id}/approve',{}).get_json();self.assertIn('due_at',r)
  self.assertTrue(self.post(f'/api/candidates/{id}/approve',{}).get_json()['already_approved'])
  state=self.client.get('/api/state').get_json();self.assertEqual(len(state['outbox']),2);self.assertEqual(state['candidates'][0]['status'],'Shortlisted')
 def test_weekends_and_ist_boundary(self):
  cases={'2026-10-09T07:00:00+00:00':'2026-10-12T03:30:00+00:00','2026-10-10T07:00:00+00:00':'2026-10-12T03:30:00+00:00','2026-10-11T07:00:00+00:00':'2026-10-12T03:30:00+00:00','2026-10-05T07:00:00+00:00':'2026-10-06T03:30:00+00:00','2026-10-08T20:00:00+00:00':'2026-10-12T03:30:00+00:00'}
  for source,expected in cases.items(): self.assertEqual(module.next_workday(datetime.fromisoformat(source)),expected)
 def test_send_once_and_uncertain_not_retried(self):
  self.add();os.environ['GMAIL_APP_PASSWORD']='test-only'
  with patch('app.smtplib.SMTP_SSL') as smtp:
   self.assertEqual(module.dispatch()['sent'],1);self.assertEqual(module.dispatch()['sent'],0);self.assertEqual(smtp.return_value.__enter__.return_value.send_message.call_count,1)
  self.add(email='second@example.com',phone='9998887776',resume_text='A different resume text for a second candidate.')
  with patch('app.smtplib.SMTP_SSL',side_effect=TimeoutError): self.assertEqual(module.dispatch()['sent'],0)
  with patch('app.smtplib.SMTP_SSL') as smtp: self.assertEqual(module.dispatch()['sent'],0);smtp.assert_not_called()
 def test_future_shortlist_does_not_send(self):
  id=self.add().get_json()['id'];self.post(f'/api/candidates/{id}/approve',{});os.environ['GMAIL_APP_PASSWORD']='test-only'
  with patch('app.smtplib.SMTP_SSL'):self.assertEqual(module.dispatch()['sent'],1)
  state=self.client.get('/api/state').get_json();self.assertEqual([o for o in state['outbox'] if o['kind']=='shortlist'][0]['status'],'queued')
 def test_ai_rank_never_shortlists(self):
  id=self.add().get_json()['id']
  assessment={'criteria':[{'name':k,'points':p,'evidence':'Test evidence'} for k,p in zip(['Skills','Experience','Responsibilities','Qualifications'],[35,20,15,5])],'summary':'Evidence-based match','strengths':['Excel'],'gaps':['Qualification missing']}
  with patch('app.ai_json',return_value=assessment): self.assertEqual(self.post(f'/api/candidates/{id}/rank',{}).get_json()['score'],75)
  c=self.client.get('/api/state').get_json()['candidates'][0];self.assertEqual(c['status'],'Reviewed');self.assertIsNone(c['approved_at'])
 def test_csv_full_export_and_html(self):
  self.add(name='=FORMULA()');result=self.client.get('/api/export');self.assertIn(b"'=FORMULA()",result.data);self.assertIn(b'resume_text',result.data)
  self.assertEqual(self.client.get('/').status_code,200);self.assertIn('nosniff',self.client.get('/').headers['X-Content-Type-Options'])

if __name__=='__main__':unittest.main()
