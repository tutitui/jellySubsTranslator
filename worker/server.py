"""Private translation queue for the Jellyfin Subtitle Translator plugin."""
from __future__ import annotations
import hashlib
import hmac
import json
import os
import re
import sqlite3
import threading
import time
import urllib.error
import urllib.request
import uuid
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit, parse_qs
from core import (LANGUAGES, MAX_BYTES, PROVIDERS, ProviderTranslator, TranslationError,
                  atomic_json, batches, compose_srt, parse_srt)

DATA = Path(os.getenv('TRANSLATOR_DATA', '/data'))
TOKEN = os.getenv('SUBTITLE_TRANSLATOR_TOKEN', '')
SETTINGS_LOCK = threading.Lock()
WAKE = threading.Event()
STOP = threading.Event()

def connect():
    c = sqlite3.connect(DATA / 'jobs.db', timeout=30)
    c.row_factory = sqlite3.Row
    c.execute('PRAGMA foreign_keys=ON')
    return c

def init():
    DATA.mkdir(parents=True, exist_ok=True)
    with closing(connect()) as c, c:
        c.execute('PRAGMA journal_mode=WAL')
        c.execute('''CREATE TABLE IF NOT EXISTS jobs (
            id TEXT PRIMARY KEY, fingerprint TEXT UNIQUE NOT NULL, created REAL NOT NULL, updated REAL NOT NULL,
            state TEXT NOT NULL, title TEXT NOT NULL, item_id TEXT NOT NULL, stream_index INTEGER NOT NULL,
            target TEXT NOT NULL, source TEXT NOT NULL, provider TEXT NOT NULL, model TEXT NOT NULL,
            base_url TEXT NOT NULL, input_srt TEXT NOT NULL, total INTEGER NOT NULL, progress TEXT NOT NULL DEFAULT '{}',
            error TEXT NOT NULL DEFAULT '', input_tokens INTEGER NOT NULL DEFAULT 0, output_tokens INTEGER NOT NULL DEFAULT 0,
            saved INTEGER NOT NULL DEFAULT 0)''')
        columns = {r[1] for r in c.execute('PRAGMA table_info(jobs)')}
        for name, declaration in [('source_file_id', "TEXT NOT NULL DEFAULT ''"), ('auto_save', 'INTEGER NOT NULL DEFAULT 0')]:
            if name not in columns:
                c.execute(f'ALTER TABLE jobs ADD COLUMN {name} {declaration}')
        # A restart pauses work rather than silently starting paid requests again.
        c.execute("UPDATE jobs SET state='paused', error='Service restarted. Retry to resume saved progress.' WHERE state IN ('running','queued','cancelling')")
    os.chmod(DATA / 'jobs.db', 0o600)

def settings():
    with SETTINGS_LOCK:
        p = DATA / 'settings.json'
        return json.loads(p.read_text()) if p.exists() else {'provider': 'openai', 'providers': {}}

def public_settings():
    s = settings()
    result = {'provider': s.get('provider', 'openai'), 'providers': {}, 'languages': LANGUAGES}
    for key, defaults in PROVIDERS.items():
        cfg = s.get('providers', {}).get(key, {})
        result['providers'][key] = dict(defaults, model=cfg.get('model', defaults['model']),
            base_url=cfg.get('base_url', defaults['base_url']), has_key=bool(cfg.get('api_key')))
    return result

def save_settings(body):
    provider = body.get('provider')
    if provider not in PROVIDERS:
        raise ValueError('Select a supported provider.')
    model = str(body.get('model', '')).strip()
    base = str(body.get('base_url', '')).strip() or PROVIDERS[provider]['base_url']
    if not model or len(model) > 200 or re.search(r'[\r\n]', model):
        raise ValueError('Select a model.')
    ProviderTranslator(provider, model=model, base_url=base)
    key = str(body.get('api_key', '')).strip()
    if len(key) > 4096 or '\n' in key or '\r' in key:
        raise ValueError('Invalid API key.')
    with SETTINGS_LOCK:
        path = DATA / 'settings.json'
        s = json.loads(path.read_text()) if path.exists() else {'providers': {}}
        cfg = s.setdefault('providers', {}).setdefault(provider, {})
        cfg.update({'model': model, 'base_url': base})
        if key:
            cfg['api_key'] = key
        if body.get('clear_key') is True:
            cfg.pop('api_key', None)
        s['provider'] = provider
        atomic_json(path, s)
    return public_settings()

def models(provider):
    if provider not in PROVIDERS:
        raise ValueError('Unknown provider.')
    cfg = public_settings()['providers'][provider]
    secret = settings().get('providers', {}).get(provider, {}).get('api_key', '')
    defaults = [x for x in [cfg['model'], PROVIDERS[provider]['model']] if x]
    def fallback(message):
        return {'models': sorted(set(defaults)), 'live': False, 'message': message}
    if cfg['key_required'] and not secret:
        return fallback('Save an API key, then refresh models to load the list available to your account.')
    base = cfg['base_url'].rstrip('/')
    if not base:
        return fallback('Configure an API base URL first.')
    headers = {}
    if provider == 'gemini':
        url = base + '/models?pageSize=1000'; headers['x-goog-api-key'] = secret
    elif provider == 'anthropic':
        url = base + '/models?limit=1000'; headers.update({'x-api-key': secret, 'anthropic-version': '2023-06-01'})
    elif provider == 'ollama':
        url = base + '/api/tags'
        if secret: headers['Authorization'] = 'Bearer ' + secret
    else:
        url = base + '/models'
        if secret: headers['Authorization'] = 'Bearer ' + secret
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=15) as r:
            d = json.load(r)
        if provider == 'gemini':
            items = [x['name'].removeprefix('models/') for x in d.get('models', [])
                     if 'generateContent' in x.get('supportedGenerationMethods', [])]
        elif provider == 'ollama':
            items = [x['name'] for x in d.get('models', [])]
        else:
            items = [x['id'] for x in d.get('data', [])]
            if provider == 'openai':
                items = [x for x in items if re.match(r'^(gpt-|chatgpt-|o[134](?:-|$))', x)
                         and not any(v in x for v in ['audio', 'realtime', 'transcribe', 'tts', 'image', 'search'])]
        return {'models': sorted(set(items + defaults)), 'live': True,
                'message': 'Models loaded from provider. Choose a text/chat model.'}
    except (urllib.error.URLError, ValueError, KeyError, TypeError, TimeoutError, OSError):
        return fallback('Could not load provider models. Check the saved key, endpoint, or connectivity; custom model entry is available.')

def job_public(row):
    d = dict(row)
    progress = json.loads(d.pop('progress'))
    d.pop('input_srt'); d.pop('fingerprint')
    d['completed'] = len(progress)
    d['saved'] = bool(d['saved'])
    return d

def get_job(ident):
    with closing(connect()) as c:
        r = c.execute('SELECT * FROM jobs WHERE id=?', (ident,)).fetchone()
        if not r:
            raise KeyError('Translation job not found.')
        return r

def create_job(body):
    text = body.get('srt', '')
    cues = parse_srt(text)
    item = str(uuid.UUID(str(body.get('item_id', ''))))
    target = body.get('target')
    if target not in LANGUAGES:
        raise ValueError('Select a target language.')
    source = str(body.get('source') or 'auto-detect')[:80]
    source_file_id = str(body.get('source_file_id') or '')
    if source_file_id and not re.fullmatch(r'[0-9a-f]{64}', source_file_id):
        raise ValueError('Invalid subtitle file identifier.')
    stream = body.get('stream_index', 0 if source_file_id else None)
    if type(stream) is not int or stream < 0:
        raise ValueError('Select a subtitle track.')
    title = str(body.get('title', 'Subtitle'))[:300]
    s = settings(); provider = s.get('provider', 'openai')
    cfg = s.get('providers', {}).get(provider, {})
    if PROVIDERS[provider]['key_required'] and not cfg.get('api_key'):
        raise ValueError('Configure this provider API key in Settings first.')
    model = cfg.get('model') or PROVIDERS[provider]['model']
    base = cfg.get('base_url') or PROVIDERS[provider]['base_url']
    ProviderTranslator(provider, cfg.get('api_key', ''), model, base)
    fingerprint = hashlib.sha256(json.dumps([text, item, source_file_id or stream, target, source, provider, model, base]).encode()).hexdigest()
    now = time.time(); ident = str(uuid.uuid4())
    with closing(connect()) as c, c:
        found = c.execute('SELECT * FROM jobs WHERE fingerprint=?', (fingerprint,)).fetchone()
        if found:
            if body.get('auto_save') is True:
                c.execute("UPDATE jobs SET auto_save=1,error=CASE WHEN state='completed' THEN '' ELSE error END WHERE id=?", (found['id'],))
                found = c.execute('SELECT * FROM jobs WHERE id=?', (found['id'],)).fetchone()
            return job_public(found)
        if c.execute("SELECT count(*) FROM jobs WHERE state IN ('queued','running')").fetchone()[0] >= 10:
            raise ValueError('Queue is full (10 active jobs). Wait for a job to finish.')
        c.execute('''INSERT INTO jobs (id,fingerprint,created,updated,state,title,item_id,stream_index,target,source,
            provider,model,base_url,input_srt,total,source_file_id,auto_save) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
            (ident, fingerprint, now, now, 'queued', title, item, stream, target, source, provider, model, base, text, len(cues), source_file_id, int(body.get('auto_save') is True)))
    WAKE.set()
    return job_public(get_job(ident))

def action(ident, name):
    row = get_job(ident)
    with closing(connect()) as c, c:
        if name == 'cancel':
            c.execute("UPDATE jobs SET state=CASE WHEN state='running' THEN 'cancelling' ELSE 'cancelled' END, updated=? WHERE id=? AND state IN ('queued','running')", (time.time(), ident))
        elif name == 'retry':
            c.execute("UPDATE jobs SET state='queued',error='',updated=? WHERE id=? AND state IN ('failed','cancelled','paused')", (time.time(), ident)); WAKE.set()
        elif name == 'save-error':
            c.execute("UPDATE jobs SET error='Could not save the translated subtitle. Check media-folder permissions, then click Translate to retry saving.',updated=? WHERE id=? AND state='completed'", (time.time(), ident))
        elif name == 'saved':
            c.execute("UPDATE jobs SET saved=1,error='',updated=? WHERE id=? AND state='completed'", (time.time(), ident))
        else:
            raise ValueError('Unknown job action.')
    return job_public(get_job(ident))

def run_job(row, factory=ProviderTranslator):
    ident = row['id']
    try:
        cfg = settings().get('providers', {}).get(row['provider'], {})
        translator = factory(row['provider'], cfg.get('api_key', ''), row['model'], row['base_url'])
        cues = parse_srt(row['input_srt']); progress = json.loads(row['progress'])
        for batch in batches(cues, progress):
            if STOP.is_set() or get_job(ident)['state'] == 'cancelling':
                with closing(connect()) as c, c:
                    c.execute("UPDATE jobs SET state='cancelled',updated=? WHERE id=?", (time.time(), ident))
                return
            start = batch[0]['id']
            context = '\n'.join(x.text for x in cues[max(0, start-3):start])
            translated, usage = translator.translate_batch(batch, row['source'], row['target'], context)
            progress.update(translated)
            with closing(connect()) as c, c:
                c.execute('UPDATE jobs SET progress=?,updated=?,input_tokens=input_tokens+?,output_tokens=output_tokens+? WHERE id=?',
                    (json.dumps(progress), time.time(), usage.get('input_tokens', 0), usage.get('output_tokens', 0), ident))
        compose_srt(cues, progress)  # Validate every cue before declaring the result complete.
        with closing(connect()) as c, c:
            c.execute("UPDATE jobs SET state=CASE WHEN state='cancelling' THEN 'cancelled' ELSE 'completed' END,error='',updated=? WHERE id=?", (time.time(), ident))
    except Exception as exc:
        message = str(exc) if isinstance(exc, (TranslationError, ValueError)) else 'Translation failed. Saved progress is retained; retry to resume.'
        with closing(connect()) as c, c:
            c.execute("UPDATE jobs SET state='failed',error=?,updated=? WHERE id=?", (message[:500], time.time(), ident))

def worker():
    while not STOP.is_set():
        with closing(connect()) as c, c:
            row = c.execute("SELECT * FROM jobs WHERE state='queued' ORDER BY created LIMIT 1").fetchone()
            if row:
                c.execute("UPDATE jobs SET state='running',updated=? WHERE id=?", (time.time(), row['id']))
        if row:
            run_job(row)
        else:
            WAKE.wait(2); WAKE.clear()

class Handler(BaseHTTPRequestHandler):
    server_version = 'SubtitleTranslator/1.0'
    def log_message(self, *args):
        pass  # No payloads, keys, subtitle contents, or authorization headers in access logs.
    def send_json(self, status, value):
        data = json.dumps(value, ensure_ascii=False).encode()
        self.send_response(status); self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(data))); self.send_header('Cache-Control', 'no-store'); self.end_headers(); self.wfile.write(data)
    def handle_api(self):
        route = urlsplit(self.path).path
        if route == '/health' and self.command == 'GET':
            self.send_json(200, {'ok': True}); return
        auth = self.headers.get('Authorization', '')
        if not TOKEN or not hmac.compare_digest(auth, 'Bearer ' + TOKEN):
            self.send_json(401, {'error': 'Unauthorized'}); return
        try:
            body = {}
            if self.command in ('POST', 'PUT'):
                length = int(self.headers.get('Content-Length', '0'))
                if length < 0 or length > MAX_BYTES * 2:
                    raise ValueError('Request is too large.')
                body = json.loads(self.rfile.read(length) or '{}')
                if not isinstance(body, dict): raise ValueError('Expected a JSON object.')
            if route == '/settings' and self.command == 'GET': result = public_settings()
            elif route == '/settings' and self.command == 'PUT': result = save_settings(body)
            elif route == '/models' and self.command == 'GET': result = models(parse_qs(urlsplit(self.path).query).get('provider', ['openai'])[0])
            elif route == '/jobs' and self.command == 'POST': result = create_job(body)
            elif route == '/jobs' and self.command == 'GET':
                with closing(connect()) as c:
                    result = [job_public(r) for r in c.execute('SELECT * FROM jobs ORDER BY created DESC LIMIT 100')]
            elif re.fullmatch(r'/jobs/[0-9a-f-]{36}/(cancel|retry|saved|save-error|subtitle)', route):
                _, _, ident, name = route.split('/')
                if name == 'subtitle' and self.command == 'GET':
                    row = get_job(ident)
                    if row['state'] != 'completed': raise ValueError('Translation is not complete.')
                    result = {'srt': compose_srt(parse_srt(row['input_srt']), json.loads(row['progress'])),
                              'target': row['target'], 'item_id': row['item_id']}
                elif name != 'subtitle' and self.command == 'POST': result = action(ident, name)
                else: raise KeyError('Endpoint not found.')
            else: raise KeyError('Endpoint not found.')
            self.send_json(200, result)
        except (ValueError, TypeError) as exc: self.send_json(400, {'error': str(exc)[:500]})
        except KeyError: self.send_json(404, {'error': 'Not found'})
        except Exception: self.send_json(500, {'error': 'Service error. Check disk space and retry.'})
    do_GET = do_POST = do_PUT = handle_api

def main():
    if not TOKEN:
        raise SystemExit('SUBTITLE_TRANSLATOR_TOKEN must be configured.')
    os.umask(0o077); init()
    threading.Thread(target=worker, daemon=True).start()
    ThreadingHTTPServer(('0.0.0.0', 8788), Handler).serve_forever()

if __name__ == '__main__': main()
