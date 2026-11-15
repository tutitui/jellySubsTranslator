"""Disposable Jellyfin folder discovery + automatic save test; no provider inference."""
import json,sys,time,urllib.request,urllib.error,subprocess
from pathlib import Path
root=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(root/'worker'))
import server
base='http://127.0.0.1:18096'
password=json.loads((root/'integration/test-auth.json').read_text())['password']
auth='MediaBrowser Client="FolderIntegration",Device="Test",DeviceId="folder-test",Version="1"'
token=''
def api(path,body=None):
 headers={'Authorization':auth+(', Token="'+token+'"' if token else ''),'Content-Type':'application/json'}
 with urllib.request.urlopen(urllib.request.Request(base+path,headers=headers,data=json.dumps(body).encode() if body is not None else None),timeout=30) as r:
  data=r.read();return json.loads(data) if data else None
for _ in range(45):
 try:
  with urllib.request.urlopen(base+'/health',timeout=2) as r:
   if r.read()==b'Healthy':break
 except Exception:pass
 time.sleep(1)
else:raise AssertionError('Disposable Jellyfin did not start')
token=api('/Users/AuthenticateByName',{'Username':'translator-integration','Pw':password})['AccessToken']
item=api('/Items?Recursive=true&IncludeItemTypes=Movie&Fields=MediaStreams,Path')['Items'][0]
data=api('/SubtitleTranslator/Items/'+item['Id']+'/Files')
files={f['name']:f for f in data['files']}
assert files['UnindexedSpanish.es.srt']['language']=='es'
assert files['UnindexedFrench.fr.vtt']['language']=='fr'
assert files['Bitmap.idx']['canTranslate'] is False
assert 'Outside.en.srt' not in files
print('Discovers unindexed subtitle files, detects language tags, and excludes symbolic-link escapes.')
for name,expected in [('UnindexedSpanish.es.srt','Hola, amigo.'),('UnindexedFrench.fr.vtt','Bonjour.')]:
 source=api('/SubtitleTranslator/Items/'+item['Id']+'/Files/'+files[name]['id']+'/Srt')
 assert expected in source['srt'];assert '-->' in source['srt']
print('Reads unindexed SRT and converts VTT into SRT with FFmpeg.')
try:api('/SubtitleTranslator/Items/'+item['Id']+'/Files/'+'0'*64+'/Srt');raise AssertionError('Unknown file ID accepted')
except urllib.error.HTTPError as e:assert e.code==404
# Stop only the disposable worker, seed a completed mocked translation, then test automatic publishing.
subprocess.run(['docker','stop','subtitle-translator-worker-test'],check=True,stdout=subprocess.DEVNULL)
server.DATA=root/'integration/worker';server.init()
job=server.create_job({'item_id':item['Id'],'source_file_id':files['UnindexedSpanish.es.srt']['id'],'target':'it','source':'es',
                     'srt':'1\n00:00:00,000 --> 00:00:01,000\nHola, amigo.\n','title':'Folder integration','auto_save':True})
with server.connect() as c:
 c.execute("UPDATE jobs SET state='completed',progress=?,saved=0,error='' WHERE id=?",(json.dumps({'0':'Ciao, amico.'}),job['id']));c.commit()
subprocess.run(['docker','start','subtitle-translator-worker-test'],check=True,stdout=subprocess.DEVNULL)
for _ in range(30):
 try:jobs=api('/SubtitleTranslator/Jobs')
 except urllib.error.HTTPError as e:
  if e.code==503:time.sleep(1);continue
  raise
 result=next(j for j in jobs if j['id']==job['id'])
 if result['saved']:break
 if result['error']:raise AssertionError(result['error'])
 time.sleep(1)
else:raise AssertionError('Automatic subtitle saving did not complete')
folder=root/'integration/media/Test Movie (2000)'
assert any('Ciao, amico.' in p.read_text() for p in folder.glob('*.srt'))
assert 'Hola, amigo.' in (folder/'UnindexedSpanish.es.srt').read_text()
print('Completed translation automatically saved to the correct movie folder; source subtitle preserved.')
saved_token=token;token=''
try:api('/SubtitleTranslator/Items/'+item['Id']+'/Files');raise AssertionError('Unauthenticated folder access accepted')
except urllib.error.HTTPError as e:assert e.code in (401,403)
print('Folder API authentication verified. No paid provider call made.')
