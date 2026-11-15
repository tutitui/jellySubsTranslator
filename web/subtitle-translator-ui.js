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
    async function openPanel(editor, section) {
        if (section.querySelector('.jst-translation-panel')) return;
        const panel = element('section'); panel.className = 'jst-translation-panel'; panel.setAttribute('aria-label', 'Translate subtitles');
        section.appendChild(panel);
        const status = element('p', 'Loading subtitles…'); status.setAttribute('role','status'); panel.appendChild(status);
        try {
            const item = await resolveItem(editor);
            const [data, jobs] = await Promise.all([api('Items/'+item.Id+'/Files'), api('Jobs')]);
            if (!editor.isConnected) return;
            status.remove(); section.querySelector('.jst-open-translate').hidden = true;
            if (!data.files.length) { panel.appendChild(element('p','No subtitle files found in this movie folder.')); return; }
            const rows = [];
            function update(row,job) {
                row.job=job;
                const active=job && (['queued','running','cancelling'].includes(job.state) || job.state==='completed' && job.auto_save && !job.saved && !job.error);
                row.start.textContent = active ? 'Translating…' : job && job.saved ? 'Translated' : 'Translate';
                row.start.title=job && job.error || (!row.file.canTranslate ? 'Convert this subtitle to a supported text format first.' : '');
                row.start.disabled=!!active || !row.file.canTranslate; row.start.dataset.stDisabled=String(row.start.disabled);
                row.select.disabled=!!active;
            }
            for (const file of data.files) {
                const row={file,job:null}; rows.push(row);
                const line=element('div'); line.className='jst-subtitle-row';
                line.style.cssText='display:flex;gap:.8em;align-items:center;flex-wrap:wrap;padding:1em 0;border-bottom:1px solid #555';
                const label=element('div');label.style.cssText='flex:1;min-width:180px;overflow-wrap:anywhere';
                label.appendChild(element('strong',file.name));
                const language=element('div',file.languageName,'secondaryText');language.style.fontSize='.9em';label.appendChild(language);
                const select=element('select');row.select=select;select.className='emby-select';select.setAttribute('aria-label','Translate '+file.name+' into');
                select.style.cssText='max-width:210px;padding:.6em;color:inherit;background:#292929;border:1px solid #666;border-radius:4px';
                for(const [code,name] of Object.entries(data.languages)){const o=element('option',name);o.value=code;select.appendChild(o);}select.value='pt-BR';
                row.start=button('Translate',async()=>{
                    try{
                        row.start.textContent='Translating…';row.select.disabled=true;
                        const source=await api('Items/'+item.Id+'/Files/'+file.id+'/Srt');
                        let job=await api('Jobs','POST',{item_id:item.Id,source_file_id:file.id,title:item.SeriesName?item.SeriesName+' — '+item.Name:item.Name,
                            source:source.language,target:select.value,srt:source.srt,auto_save:true});
                        if(['failed','paused','cancelled'].includes(job.state))job=await api('Jobs/'+job.id+'/Retry','POST',{});
                        update(row,job);
                    }catch(e){row.start.textContent='Translate';row.start.title=e.message;row.select.disabled=false;row.start.dataset.stDisabled='false';}
                });
                select.addEventListener('change',()=>update(row,null));
                line.append(label,select,row.start);panel.appendChild(line);
                const previous=jobs.find(j=>j.item_id.replace(/-/g,'').toLowerCase()===item.Id.replace(/-/g,'').toLowerCase()&&j.source_file_id===file.id);
                if(previous){select.value=previous.target;update(row,previous);}else update(row,null);
            }
            let refreshing=false;
            const timer=setInterval(async()=>{
                if(!panel.isConnected){clearInterval(timer);return;}
                if(refreshing)return;refreshing=true;
                try{const latest=await api('Jobs');rows.forEach(row=>{if(row.job){const job=latest.find(j=>j.id===row.job.id);if(job)update(row,job);}});}catch(_){}finally{refreshing=false;}
            },3000);
            panel.scrollIntoView({block:'nearest',behavior:'smooth'});
        }catch(e){status.textContent=e.message;}
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
