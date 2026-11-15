#!/usr/bin/env python3
"""Interactive/CLI replacement for subsTranslator.py. SRT only; safe resumable output."""
import argparse
import hashlib
import json
import os
import re
from pathlib import Path
from core import ProviderTranslator, atomic_json, batches, compose_srt, parse_srt

def main():
    parser = argparse.ArgumentParser(description='Translate SRT subtitles with resumable progress.')
    parser.add_argument('subtitle', nargs='?')
    parser.add_argument('--source', default='auto-detect')
    parser.add_argument('--target')
    parser.add_argument('--model')
    parser.add_argument('--provider', choices=['openai','gemini','anthropic','meta','ollama','custom'])
    parser.add_argument('--output')
    parser.add_argument('--settings', default=os.getenv('SUBTITLE_TRANSLATOR_SETTINGS',
        str(Path(__file__).resolve().parents[1] / 'runtime' / 'settings.json')))
    args = parser.parse_args()
    path = Path(args.subtitle or input('Caminho da legenda SRT: ').strip()).expanduser()
    if path.suffix.lower() != '.srt':
        parser.error('Only SRT is supported. Convert other text formats first; image subtitles need OCR.')
    target = args.target or input('Idioma de destino (ex.: pt-BR): ').strip()
    if not re.fullmatch(r'[A-Za-z]{2,3}(?:-[A-Za-z]{2,4})?', target):
        parser.error('Use a language code such as pt-BR, en, or es.')
    settings = json.loads(Path(args.settings).read_text()) if Path(args.settings).exists() else {}
    provider = args.provider or settings.get('provider', 'openai')
    cfg = settings.get('providers', {}).get(provider, {})
    from core import PROVIDERS
    model = args.model or cfg.get('model') or PROVIDERS[provider]['model']
    env_names = {'openai':'OPENAI_API_KEY','gemini':'GEMINI_API_KEY','anthropic':'ANTHROPIC_API_KEY','meta':'MODEL_API_KEY'}
    key = os.getenv(env_names.get(provider, 'TRANSLATOR_API_KEY')) or cfg.get('api_key', '')
    translator = ProviderTranslator(provider, key, model, cfg.get('base_url', ''))
    text = path.read_text(encoding='utf-8-sig'); cues = parse_srt(text)
    stem = re.sub(r'\.(?:[a-z]{2,3}(?:-[A-Za-z]{2,4})?)$', '', path.stem)
    output = Path(args.output) if args.output else path.with_name(stem + '.' + target + '.srt')
    if output.resolve() == path.resolve() or output.exists():
        parser.error('Output already exists or would overwrite the source; select a different --output.')
    signature = hashlib.sha256(json.dumps([text, args.source, target, provider, model, translator.base_url]).encode()).hexdigest()
    checkpoint = output.with_suffix(output.suffix + '.progress.json')
    state = json.loads(checkpoint.read_text()) if checkpoint.exists() else {'signature': signature, 'translations': {}}
    if state.get('signature') != signature:
        parser.error('Checkpoint belongs to different content/settings. Choose a different output path.')
    for batch in batches(cues, state['translations']):
        context = '\n'.join(c.text for c in cues[max(0, batch[0]['id']-3):batch[0]['id']])
        translated, usage = translator.translate_batch(batch, args.source, target, context)
        state['translations'].update(translated); atomic_json(checkpoint, state)
        print(f"{len(state['translations'])}/{len(cues)} cues translated", flush=True)
    content = compose_srt(cues, state['translations'])
    # Exclusive creation never overwrites source or an existing subtitle.
    with output.open('x', encoding='utf-8') as stream:
        stream.write(content)
    checkpoint.unlink(missing_ok=True)
    print('Saved:', output)

if __name__ == '__main__':
    try:
        main()
    except (Exception, KeyboardInterrupt) as exc:
        print(f'Translation stopped: {exc}. Saved progress is retained.')
        raise SystemExit(1)
