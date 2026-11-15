import io
import json
import sys
import tempfile
import unittest
import urllib.error
from contextlib import closing
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'worker'))
import core
import server

SRT = '1\n00:00:01,000 --> 00:00:02,500\nHello <i>friend</i>.\n\n2\n00:00:03,000 --> 00:00:04,000\nGoodbye!\n'
BATCH = [{'id':0,'text':'Hello'}]
TEXT = json.dumps({'translations':[{'id':0,'text':'Olá'}]})

class CoreTests(unittest.TestCase):
    def test_timestamps_and_cue_numbers_preserved(self):
        cues = core.parse_srt('\ufeff' + SRT.replace('\n','\r\n'))
        result = core.compose_srt(cues, {'0':'Olá <i>amigo</i>.','1':'Adeus!'})
        out = core.parse_srt(result)
        self.assertEqual([(c.number,c.timing) for c in cues],[(c.number,c.timing) for c in out])
        self.assertEqual(len(out),2)
    def test_partial_translation_never_drops_cues(self):
        with self.assertRaises(ValueError): core.compose_srt(core.parse_srt(SRT), {'0':'Olá'})
    def test_invalid_or_image_subtitles_rejected(self):
        for text in ['{1}{2}MicroDVD', 'WEBVTT\n\n00:00:01 --> 00:00:02\nHello', '', '1\ninvalid\nHello']:
            with self.assertRaises(ValueError): core.parse_srt(text)
    def test_mismatched_duplicate_and_empty_model_output_rejected(self):
        for rows in [[{'id':1,'text':'wrong'}],[{'id':0,'text':''}],[{'id':'0','text':'wrong'}],[{'id':0,'text':'a\n\nb'}]]:
            with self.assertRaises(ValueError): core.validate_translation({'translations':rows}, BATCH)
    def test_requests_and_responses_all_providers(self):
        for provider in core.PROVIDERS:
            base = 'http://example.test/v1' if provider == 'custom' else ''
            t = core.ProviderTranslator(provider,'fake-key','test-model',base)
            req = t.build_request(BATCH,'en','pt-BR','previous dialogue')
            payload = json.loads(req.data)
            self.assertNotIn('fake-key', req.full_url)
            if provider == 'gemini':
                self.assertTrue(req.full_url.endswith(':generateContent'))
                response = {'candidates':[{'finishReason':'STOP','content':{'parts':[{'text':TEXT}]}}]}
            elif provider == 'anthropic':
                self.assertTrue(req.full_url.endswith('/messages'))
                response = {'stop_reason':'end_turn','content':[{'type':'text','text':TEXT}]}
            elif provider == 'ollama':
                self.assertFalse(payload['stream'])
                response = {'done':True,'message':{'content':TEXT}}
            else:
                self.assertTrue(req.full_url.endswith('/chat/completions'))
                response = {'choices':[{'finish_reason':'stop','message':{'content':TEXT}}]}
            self.assertEqual(t.parse_response(response,BATCH)[0], {'0':'Olá'})
    def test_retry_invalid_response_then_success(self):
        t = core.ProviderTranslator('openai','fake',attempts=2,sleep=lambda _:None)
        invalid = io.BytesIO(json.dumps({'choices':[]}).encode())
        valid = io.BytesIO(json.dumps({'choices':[{'finish_reason':'stop','message':{'content':TEXT}}]}).encode())
        with patch('urllib.request.urlopen',side_effect=[invalid,valid]) as mocked:
            self.assertEqual(t.translate_batch(BATCH,'en','pt-BR')[0],{'0':'Olá'})
            self.assertEqual(mocked.call_count,2)
    def test_no_retry_invalid_credentials_or_secret_leak(self):
        t = core.ProviderTranslator('openai','private-key',sleep=lambda _:None)
        error=urllib.error.HTTPError('https://api.openai.com',401,'private-key',{},None)
        with patch('urllib.request.urlopen',side_effect=error) as mocked:
            with self.assertRaises(core.TranslationError) as ctx:t.translate_batch(BATCH,'en','pt-BR')
            self.assertNotIn('private-key',str(ctx.exception));self.assertEqual(mocked.call_count,1)
        error.close()

class QueueTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.old=server.DATA;server.DATA=Path(self.tmp.name)
        server.init();server.save_settings({'provider':'openai','model':'test-model','api_key':'fake-private-key'})
    def tearDown(self):server.DATA=self.old;self.tmp.cleanup()
    def job(self,text=SRT):return server.create_job({'srt':text,'item_id':'11111111-1111-1111-1111-111111111111','stream_index':2,'target':'pt-BR','title':'Movie'})
    def test_keys_never_returned_to_browser(self):
        self.assertNotIn('fake-private-key',json.dumps(server.public_settings()))
        server.save_settings({'provider':'openai','model':'other','api_key':''})
        self.assertTrue(server.public_settings()['providers']['openai']['has_key'])
        server.save_settings({'provider':'openai','model':'other','clear_key':True})
        self.assertFalse(server.public_settings()['providers']['openai']['has_key'])
    def test_job_deduplication(self):
        first=self.job();self.assertEqual(first['id'],self.job()['id'])
        self.assertNotIn('input_srt',first)
    def test_resume_keeps_completed_batches_and_every_timestamp(self):
        text='\n'.join(f'{i+1}\n00:00:01,000 --> 00:00:02,000\nCue {i}\n' for i in range(23))
        job=self.job(text);calls=[]
        class Partial:
            def __init__(self,*args):pass
            def translate_batch(self,batch,*args):
                calls.append([x['id'] for x in batch])
                if len(calls)==2:raise core.TranslationError('Simulated network outage')
                return {str(x['id']):'Translated '+x['text'] for x in batch},{'input_tokens':10,'output_tokens':5}
        server.run_job(server.get_job(job['id']),Partial)
        failed=server.get_job(job['id']);self.assertEqual(failed['state'],'failed');self.assertEqual(len(json.loads(failed['progress'])),20)
        server.action(job['id'],'retry')
        server.run_job(server.get_job(job['id']),Partial)
        row=server.get_job(job['id']);self.assertEqual(row['state'],'completed')
        self.assertEqual(calls[2],[20,21,22]);self.assertEqual(len(core.parse_srt(core.compose_srt(core.parse_srt(text),json.loads(row['progress'])))),23)
    def test_cancel_and_restart_do_not_start_paid_calls(self):
        job=self.job();server.action(job['id'],'cancel');self.assertEqual(server.get_job(job['id'])['state'],'cancelled')
        server.action(job['id'],'retry');server.init();self.assertEqual(server.get_job(job['id'])['state'],'paused')
    def test_provider_settings_do_not_cross_contaminate_keys(self):
        server.save_settings({'provider':'gemini','model':'gemini-test','api_key':'google-secret'})
        self.assertEqual(server.settings()['providers']['openai']['api_key'],'fake-private-key')
        self.assertEqual(server.settings()['providers']['gemini']['api_key'],'google-secret')
    def test_model_list_no_key_uses_defaults_without_network(self):
        with patch('urllib.request.urlopen',side_effect=AssertionError('No network expected')):
            result=server.models('anthropic');self.assertFalse(result['live']);self.assertTrue(result['models'])
    def test_live_model_lists_for_each_provider(self):
        for provider in core.PROVIDERS:
            base = 'http://example.test/v1' if provider == 'custom' else ''
            server.save_settings({'provider':provider,'model':'configured-model','api_key':'test-key','base_url':base})
            if provider == 'gemini':payload={'models':[{'name':'models/gemini-test','supportedGenerationMethods':['generateContent']}]};expected='gemini-test'
            elif provider == 'ollama':payload={'models':[{'name':'local-test'}]};expected='local-test'
            else:payload={'data':[{'id':'gpt-model-test' if provider=='openai' else 'provider-model-test'}]};expected=payload['data'][0]['id']
            with patch('urllib.request.urlopen',return_value=io.BytesIO(json.dumps(payload).encode())):
                result=server.models(provider)
            self.assertTrue(result['live']);self.assertIn(expected,result['models'])

    def test_private_key_file_permissions(self):
        self.assertEqual((server.DATA/'settings.json').stat().st_mode & 0o777,0o600)

if __name__=='__main__':unittest.main()
