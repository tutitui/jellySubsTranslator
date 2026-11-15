/* Jellyfin Web 12.4 subtitle-editor extension. Contains no credentials. */
(function () {
    'use strict';
    if (window.jellySubsTranslatorUiLoaded) return;
    window.jellySubsTranslatorUiLoaded = true;
    const style = document.createElement('style'); style.textContent = '.jst-editor-actions [hidden]{display:none!important}'; document.head.appendChild(style);
    const patched = new WeakSet(), recentItems = new Map();
    let lastItem = null, scheduled = false;
    const textCodecs = new Set(['srt', 'subrip', 'ass', 'ssa', 'webvtt', 'vtt', 'mov_text', 'text', 'ttml']);
    const isText = s => s.Type === 'Subtitle' && (s.IsTextSubtitleStream === true || textCodecs.has((s.Codec || '').toLowerCase()));
    const baseName = p => (p || '').split(/[\\/]/).pop();
    function apiClient() { return window.ApiClient; }
    async function request(path, method, body, dataType) {
        const client = apiClient();
        const options = {type: method || 'GET', url: client.getUrl(path), dataType: dataType || 'json'};
        if (body !== undefined) { options.data = JSON.stringify(body); options.contentType = 'application/json'; }
        try { return await client.ajax(options); }
        catch (e) {
            let message = 'Request failed. Check the server connection.';
            try { message = (await e.json()).error || message; } catch (_) { if (e.status) message += ' HTTP ' + e.status; }
            throw new Error(message);
        }
    }
    const api = (path, method, body) => request('SubtitleTranslator/' + path, method, body);
    function element(tag, text, className) {
        const e = document.createElement(tag); if (text !== undefined) e.textContent = text; if (className) e.className = className; return e;
    }
    function button(label, fn) {
        const b = element('button', label, 'raised emby-button'); b.type = 'button';
        b.addEventListener('click', async () => { b.disabled = true; try { await fn(); } finally { if (b.isConnected) b.disabled = b.dataset.stDisabled === 'true'; } }); return b;
    }
    function trackList(item) { return item.MediaStreams || (item.MediaSources && item.MediaSources[0] && item.MediaSources[0].MediaStreams) || []; }
    function remember(item) {
        if (item && item.Id && ['Movie', 'Episode', 'Video'].includes(item.Type)) {
            lastItem = item; recentItems.delete(item.Id); recentItems.set(item.Id, item);
            if (recentItems.size > 100) recentItems.delete(recentItems.keys().next().value);
        }
    }
    function patchClient() {
        const client = apiClient(); if (!client || patched.has(client) || typeof client.getItem !== 'function') return;
        const original = client.getItem;
        client.getItem = function () { return original.apply(this, arguments).then(item => { remember(item); return item; }); };
        patched.add(client);
    }
    async function resolveItem(editor) {
        const path = editor.querySelector('.pathValue');
        const videoName = path && path.textContent.trim();
        const subtitlePaths = Array.from(editor.querySelectorAll('.subtitleList .listItemBodyText')).map(e => e.textContent.trim());
        const matches = item => !!item && (videoName && baseName(item.Path) === videoName || trackList(item).some(s => s.Path && subtitlePaths.includes(s.Path)));
        const params = new URLSearchParams(location.hash.split('?')[1] || '');
        const id = params.get('id');
        if (id) {
            const item = await apiClient().getItem(apiClient().getCurrentUserId(), id);
            if (matches(item)) return item;
        }
        const cached = Array.from(recentItems.values()).reverse().find(matches);
        if (cached) return cached;
        if (matches(lastItem)) return lastItem;
        throw new Error('Could not identify this video. Open its details page, then choose Edit subtitles again.');
    }
    function toBase64(value) {
        const bytes = new TextEncoder().encode(value); let binary = '';
        for (let i = 0; i < bytes.length; i += 8192) binary += String.fromCharCode.apply(null, bytes.subarray(i, i + 8192));
        return btoa(binary);
    }
    async function openPanel(editor, section) {
        if (section.querySelector('.jst-translation-panel')) { section.querySelector('.jst-translation-panel').scrollIntoView({block:'nearest'}); return; }
        const panel = element('section'); panel.className = 'jst-translation-panel'; panel.setAttribute('aria-label', 'Translate subtitles'); panel.style.cssText = 'margin:1em 0;padding:.5em 0';
        section.appendChild(panel);
        const status = element('p', 'Loading subtitle files…'); status.setAttribute('role', 'status'); panel.appendChild(status);
        try {
            const [item, settings, jobs] = await Promise.all([resolveItem(editor), api('Settings'), api('Jobs')]);
            if (!editor.isConnected) return;
            status.textContent = 'Choose a target language beside a subtitle, then click Translate.';
            const cfg = settings.providers[settings.provider];
            const provider = element('p', cfg.label + ' · ' + cfg.model); panel.appendChild(provider);
            const settingsLink = element('a', 'Provider, API key, and model settings', 'button-link'); settingsLink.href = '#/configurationpage?name=SubtitleTranslator'; panel.appendChild(settingsLink);
            const sourceTracks = trackList(item).filter(s => s.Type === 'Subtitle').sort((a,b) => Number(b.IsExternal)-Number(a.IsExternal));
            if (!sourceTracks.length) { status.textContent = 'No subtitle tracks found. Add or download a text subtitle first.'; return; }
            const rows = [];
            const subtitleSource = (item.MediaSources && item.MediaSources[0] && item.MediaSources[0].Id) || item.Id;
            function update(row, job) {
                row.job = job;
                if (!job) { row.status.textContent = ''; row.progress.hidden = true; row.save.hidden = true; row.resume.hidden = true; row.start.disabled = !isText(row.track); row.start.dataset.stDisabled=String(row.start.disabled); return; }
                row.status.textContent = job.state + ' · ' + job.completed + '/' + job.total + ' cues' + (job.saved ? ' · saved to library' : '') + (job.error ? '\n' + job.error : '');
                row.progress.hidden = false; row.progress.max = job.total; row.progress.value = job.completed;
                row.save.hidden = job.state !== 'completed' || job.saved;
                row.resume.hidden = !['failed','paused','cancelled'].includes(job.state);
                row.cancel.hidden = !['running','queued'].includes(job.state);
                row.start.disabled = ['queued','running','cancelling'].includes(job.state) || !isText(row.track); row.start.dataset.stDisabled=String(row.start.disabled);
            }
            async function refresh() {
                if (!panel.isConnected) return;
                try {
                    const latest = await api('Jobs');
                    rows.forEach(row => { if (row.job) { const job = latest.find(j=>j.id===row.job.id); if (job) update(row,job); } });
                } catch (e) { status.textContent = e.message; }
            }
            for (const track of sourceTracks) {
                const row = {track, job: null}; rows.push(row);
                const card = element('div'); card.className = 'jst-subtitle-row'; card.style.cssText = 'padding:1em 0;border-bottom:1px solid #555';
                const line = element('div'); line.style.cssText = 'display:flex;gap:.8em;align-items:center;flex-wrap:wrap';
                const description = element('div'); description.style.cssText = 'flex:1;min-width:180px;overflow-wrap:anywhere';
                const name = element('strong', track.IsExternal && track.Path ? baseName(track.Path) : (track.DisplayTitle || track.Language || 'Unknown language') + ' (embedded)'); description.appendChild(name);
                const detail = element('div', track.DisplayTitle || track.Title || track.Codec || 'Subtitle', 'secondaryText'); detail.style.fontSize = '.9em'; description.appendChild(detail);
                const select = element('select'); select.className = 'emby-select'; select.setAttribute('aria-label', 'Target language for ' + name.textContent); select.style.cssText = 'max-width:210px;padding:.6em;color:inherit;background:#292929;border:1px solid #666;border-radius:4px';
                for (const [code,label] of Object.entries(settings.languages)) { const o = element('option', label); o.value = code; select.appendChild(o); }
                select.value = 'pt-BR'; row.select = select;
                row.start = button('Translate', async () => {
                    try {
                        if (cfg.key_required && !cfg.has_key) throw new Error('Set your '+cfg.label+' API key in the plugin settings first.');
                        row.status.textContent = 'Loading source subtitle…';
                        const srt = await request('Videos/'+item.Id+'/'+subtitleSource+'/Subtitles/'+track.Index+'/Stream.srt','GET',undefined,'text');
                        const count = (srt.match(/\d+:\d{2}:\d{2}[,.]\d{3}\s+-->/g)||[]).length;
                        if (!count) throw new Error('Jellyfin did not return valid SRT text.');
                        if (!window.confirm('Translate '+count+' cues into '+select.selectedOptions[0].textContent+' using '+cfg.label+' / '+cfg.model+'? Cloud usage is billed to your API account.')) { row.status.textContent = 'Translation not started.'; return; }
                        const job = await api('Jobs','POST',{item_id:item.Id, stream_index:track.Index, title:item.SeriesName ? item.SeriesName+' — '+item.Name : item.Name, source:track.Language || 'auto-detect', target:select.value, srt});
                        update(row,job); status.textContent = 'Translation started. You can also manage it in the plugin dashboard.';
                    } catch (e) { row.status.textContent = e.message; }
                });
                row.status = element('p'); row.status.setAttribute('role','status'); row.status.style.whiteSpace = 'pre-wrap';
                row.progress = element('progress'); row.progress.hidden = true; row.progress.style.width = '100%';
                row.save = button('Save to movie folder', async () => {
                    try {
                        const result = await api('Jobs/'+row.job.id+'/Subtitle');
                        const latestItem = await apiClient().getItem(apiClient().getCurrentUserId(),item.Id);
                        const code = row.job.target.split('-')[0]; const aliases = [row.job.target.toLowerCase(),code,code==='pt'?'por':code];
                        if (trackList(latestItem).some(s=>s.Type==='Subtitle'&&aliases.includes((s.Language||'').toLowerCase())) && !window.confirm('A subtitle in this language already exists. Jellyfin may replace an external subtitle for that language. Save anyway?')) return;
                        await request('Videos/'+item.Id+'/Subtitles','POST',{Language:result.target,Format:'srt',IsForced:false,IsHearingImpaired:false,Data:toBase64(result.srt)},'text');
                        update(row,await api('Jobs/'+row.job.id+'/Saved','POST',{}));
                        status.textContent = 'Saved to the media library. Close and reopen Edit subtitles to refresh the file list.';
                    } catch (e) { row.status.textContent = e.message; }
                }); row.save.hidden = true;
                row.resume = button('Resume / retry', async () => { try { if (window.confirm('Resume this translation? Remaining cloud requests may incur charges.')) update(row,await api('Jobs/'+row.job.id+'/Retry','POST',{})); } catch(e) { row.status.textContent=e.message; } }); row.resume.hidden = true;
                row.cancel = button('Cancel', async()=>{try{update(row,await api('Jobs/'+row.job.id+'/Cancel','POST',{}));}catch(e){row.status.textContent=e.message;}});row.cancel.hidden=true;
                select.addEventListener('change',()=>{row.cancel.hidden=true;update(row,null);});
                line.append(description,select,row.start);card.append(line,row.status,row.progress,row.save,row.resume,row.cancel);panel.appendChild(card);
                if (!isText(track)) { row.start.disabled=true;row.status.textContent='Image subtitle — OCR is required before translation.'; }
                const existing = jobs.find(j=>j.item_id.replace(/-/g,'').toLowerCase()===item.Id.replace(/-/g,'').toLowerCase()&&j.stream_index===track.Index);
                if(existing){select.value=existing.target;update(row,existing);}
            }
            const timer = setInterval(()=>{if (!panel.isConnected)clearInterval(timer);else refresh();},5000);
            panel.scrollIntoView({block:'nearest',behavior:'smooth'});
        } catch(e) { status.textContent = e.message; }
    }
    async function enhance(editor) {
        if(editor.dataset.jstChecked) return; editor.dataset.jstChecked='loading';
        try {
            const user=await apiClient().getCurrentUser();
            if(!editor.isConnected || !user.Policy || !user.Policy.IsAdministrator) {delete editor.dataset.jstChecked;return;}
            const host=editor.querySelector('.dialogContentInner');if(!host){delete editor.dataset.jstChecked;return;}
            const section=element('section');section.className='jst-editor-actions';section.style.cssText='margin:1.5em 0;border-top:1px solid #555;padding-top:1em';
            const open=button('Translate subtitles',()=>openPanel(editor,section));open.classList.add('jst-open-translate');section.appendChild(open);host.appendChild(section);editor.dataset.jstChecked='done';
        } catch(_){delete editor.dataset.jstChecked;}
    }
    function scan(){scheduled=false;patchClient();if(!apiClient())return;document.querySelectorAll('.subtitleEditorDialog').forEach(enhance);}
    function schedule(){if(!scheduled){scheduled=true;requestAnimationFrame(scan);}}
    new MutationObserver(schedule).observe(document.documentElement,{childList:true,subtree:true});
    setInterval(patchClient,1000);schedule();
}());
