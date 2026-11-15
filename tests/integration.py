"""Runs only against the disposable localhost:18096 Jellyfin test server."""
import base64,json,secrets,urllib.request,urllib.error,time
from pathlib import Path
BASE='http://127.0.0.1:18096'
AUTH='MediaBrowser Client="SubtitleTranslatorTest", Device="Integration", DeviceId="subtitle-translator-test", Version="1.0"'
TOKEN=''
def call(path,method='GET',body=None,raw=False):
 headers={'Authorization':AUTH + (', Token="' + TOKEN + '"' if TOKEN else ''),'Content-Type':'application/json'}
 if TOKEN:headers['X-Emby-Token']=TOKEN
 req=urllib.request.Request(BASE+path,method=method,headers=headers,data=json.dumps(body).encode() if body is not None else None)
 with urllib.request.urlopen(req,timeout=30) as r:
  data=r.read()
  return data.decode() if raw else (json.loads(data) if data else None)
auth_path=Path(__file__).resolve().parents[1]/'integration/test-auth.json'
if auth_path.exists():
 credentials=json.loads(auth_path.read_text());password=credentials['password']
else:
 password=secrets.token_urlsafe(24)
 auth_path.write_text(json.dumps({'password':password}));auth_path.chmod(0o600)
 call('/Startup/Configuration','POST',{'UICulture':'en-US','MetadataCountryCode':'US','PreferredMetadataLanguage':'en'})
 call('/Startup/FirstUser')
 call('/Startup/User','POST',{'Name':'translator-integration','Password':password})
 call('/Startup/RemoteAccess','POST',{'EnableRemoteAccess':True,'EnableAutomaticPortMapping':False})
 call('/Startup/Complete','POST',{})
session=call('/Users/AuthenticateByName','POST',{'Username':'translator-integration','Pw':password});TOKEN=session['AccessToken'];uid=session['User']['Id']
print('Disposable Jellyfin admin initialized and authenticated; no credentials printed.')
settings=call('/SubtitleTranslator/Settings');assert settings['provider']=='custom';assert all('api_key' not in c for c in settings['providers'].values())
print('Plugin settings endpoint works; all provider keys are redacted.')
page=call('/web/configurationpage?name=SubtitleTranslator',raw=True);assert 'stProvider' in page and 'stModel' in page
print('Embedded provider/model dashboard loads.')
plugins=call('/Plugins');assert any(p['Name']=='Subtitle Translator' for p in plugins)
if not any(x['Name']=='Integration' for x in call('/Library/VirtualFolders')):
 call('/Library/VirtualFolders?name=Integration&collectionType=movies&refreshLibrary=true','POST',{'LibraryOptions':{'PathInfos':[{'Path':'/testmedia'}],'EnableInternetProviders':False,'EnableRealtimeMonitor':False}})
items=[]
for _ in range(20):
 items=call('/Items?Recursive=true&IncludeItemTypes=Movie&Fields=MediaStreams,MediaSources').get('Items',[])
 if items and items[0].get('MediaStreams'):break
 time.sleep(1)
assert items,'Synthetic movie was not scanned'
item=items[0]
streams=[s for s in item['MediaStreams'] if s['Type']=='Subtitle'];assert streams,'Synthetic source subtitle not discovered'
track=streams[0]
source=call('/Videos/'+item['Id']+'/'+item['Id']+'/Subtitles/'+str(track['Index'])+'/Stream.srt',raw=True)
assert 'Hello, friend.' in source
print('Movie search, subtitle-track discovery, and SRT extraction work.')
# Queue through real plugin/worker, using an unreachable test endpoint so no paid request is possible.
job=call('/SubtitleTranslator/Jobs','POST',{'item_id':item['Id'],'stream_index':track['Index'],'target':'pt-BR','source':'en','title':'Integration fixture','srt':source})
assert job['total']==2
call('/SubtitleTranslator/Jobs/'+job['id']+'/Cancel','POST',{})
# Persist a deterministic completed mock translation in the isolated worker database.
import sqlite3
root=Path(__file__).resolve().parents[1]
c=sqlite3.connect(root/'integration/worker/jobs.db');c.execute("UPDATE jobs SET state='completed',progress=? WHERE id=?",(json.dumps({'0':'Olá, amigo.','1':'Adeus.'}),job['id']));c.commit();c.close()
result=call('/SubtitleTranslator/Jobs/'+job['id']+'/Subtitle');assert 'Olá, amigo.' in result['srt']
call('/Videos/'+item['Id']+'/Subtitles','POST',{'Language':result['target'],'Format':'srt','IsForced':False,'IsHearingImpaired':False,'Data':base64.b64encode(result['srt'].encode()).decode()})
call('/SubtitleTranslator/Jobs/'+job['id']+'/Saved','POST',{})
assert call('/SubtitleTranslator/Jobs')[0]['saved']
files=list((root/'integration/media').rglob('*.srt'));assert any('Olá, amigo.' in p.read_text() for p in files)
print('Mock job queue, completion, SRT output, Jellyfin upload, and saved state work.')
# A normal authenticated viewer must also be denied.
viewer_name='translator-viewer-'+secrets.token_hex(4)
viewer_password=secrets.token_urlsafe(24)
viewer=call('/Users/New','POST',{'Name':viewer_name,'Password':viewer_password})
admin_token=TOKEN;TOKEN=''
viewer_session=call('/Users/AuthenticateByName','POST',{'Username':viewer_name,'Pw':viewer_password})
TOKEN=viewer_session['AccessToken']
try:call('/SubtitleTranslator/Settings');raise AssertionError('Normal viewer settings access was allowed')
except urllib.error.HTTPError as e:assert e.code==403
print('Normal Jellyfin viewer is denied access to translation settings.')
# Unauthenticated access to sensitive endpoints must be denied.
TOKEN=''
try:call('/SubtitleTranslator/Settings');raise AssertionError('Unauthenticated settings access was allowed')
except urllib.error.HTTPError as e:assert e.code in (401,403)
print('Unauthenticated access denied. Integration test passed without real inference.')
