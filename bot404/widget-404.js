/* 404ai чат-виджет «Аиша» — визуал yanalitics/изумруд (Manrope, line-иконки).
   Логика/БЗ/история — на стороне speech32.ru. Вставка: <script src=".../api/widget-404.js" async></script> */
(function(){
  if (window.__w404loaded) return; window.__w404loaded = true;
  var API = 'https://bot.217-149-25-34.sslip.io', SKEY = 'sid404';
  // 3-уровневый fallback для session_id: localStorage → sessionStorage → window-level
  // (защита от incognito/disabled cookies/iframe-restrictions, иначе каждое сообщение
  // создаёт новую сессию в админке)
  function readSid(){
    try { var v = localStorage.getItem(SKEY); if (v) return v; } catch(e){}
    try { var v = sessionStorage.getItem(SKEY); if (v) return v; } catch(e){}
    return window.__sid404 || '';
  }
  function writeSid(v){
    window.__sid404 = v;
    try { localStorage.setItem(SKEY, v); } catch(e){}
    try { sessionStorage.setItem(SKEY, v); } catch(e){}
  }
  var sid = readSid();
  var busy = false, greeted = false, nudgeTimer = null;

  function loadFont(){ if (document.getElementById('w404-font')) return; var l=document.createElement('link'); l.id='w404-font'; l.rel='stylesheet'; l.href='https://fonts.googleapis.com/css2?family=Manrope:wght@400;500;600;700;800&display=swap'; (document.head||document.documentElement).appendChild(l); }

  // Дефолты для tenant 'aisha' (если config недоступен — рендерим как раньше)
  var DEFAULT_CFG = {
    brand_name: '404ai',
    bot_name: 'Аиша',
    role_subtitle: 'AI-консультант 404ai · обычно отвечает сразу',
    logo_url: null,
    primary_color: '#0B8A5B',
    accent_color: '#E0B341',
    text_color: '#1A1A1A',
    greeting: '',
    nudge_text: 'Нужна помощь? Спросите <b>Аишу</b>',
    chat_title: 'Чат с Аишей',
    footer_text: null,
    manager_email: 'ap@404ai.ru',
    position: 'br',
    font_family: 'Manrope',
    custom_css: null,
  };
  function _esc(s){ return String(s||'').replace(/[&<>"']/g,function(c){return ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'})[c];}); }
  function _darken(hex, p){ // hex #RRGGBB → темнее на p% (0.1 = на 10% темнее)
    var m = /^#?([0-9a-f]{6})$/i.exec(hex||''); if(!m) return hex||'#0A7A50';
    var n = parseInt(m[1],16), r=(n>>16)&255, g=(n>>8)&255, b=n&255;
    r=Math.max(0,Math.round(r*(1-p))); g=Math.max(0,Math.round(g*(1-p))); b=Math.max(0,Math.round(b*(1-p)));
    return '#'+((1<<24)|(r<<16)|(g<<8)|b).toString(16).slice(1);
  }
  function _wash(hex){ // светлый wash для accent
    var m = /^#?([0-9a-f]{6})$/i.exec(hex||''); if(!m) return '#E3F5ED';
    var n = parseInt(m[1],16), r=(n>>16)&255, g=(n>>8)&255, b=n&255;
    return 'rgba('+r+','+g+','+b+',0.10)';
  }

  function loadConfig(){
    return fetch(API+'/api/widget-config', { credentials:'omit' })
      .then(function(r){ return r.ok ? r.json() : null; })
      .then(function(j){ return (j && j.branding) ? Object.assign({}, DEFAULT_CFG, j.branding) : DEFAULT_CFG; })
      .catch(function(){ return DEFAULT_CFG; });
  }

  function boot(){
    loadConfig().then(doRender);
  }
  function doRender(cfg){
    loadFont();
    var host = document.createElement('div'); host.id = 'w404-host';
    (document.body||document.documentElement).appendChild(host);
    var sh = host.attachShadow ? host.attachShadow({mode:'open'}) : host;

    // вычисляем производные цвета
    var COLOR_PRIMARY = cfg.primary_color || '#0B8A5B';
    var COLOR_HOVER   = _darken(COLOR_PRIMARY, 0.10);
    var COLOR_WASH    = _wash(COLOR_PRIMARY);
    var BOT_NAME      = cfg.bot_name || 'Ассистент';
    var BRAND_NAME    = cfg.brand_name || '404ai';
    var SUBTITLE      = cfg.role_subtitle || ('AI-консультант ' + BRAND_NAME);
    var CHAT_TITLE    = cfg.chat_title    || ('Чат с ' + BOT_NAME);
    var NUDGE_HTML    = cfg.nudge_text    || ('Нужна помощь? Спросите <b>'+_esc(BOT_NAME)+'</b>');
    var POSITION      = cfg.position      || 'br';
    var POS_CSS = ({
      'br': 'right:24px;bottom:24px',
      'bl': 'left:24px;bottom:24px',
      'tr': 'right:24px;top:24px',
      'tl': 'left:24px;top:24px',
    })[POSITION] || 'right:24px;bottom:24px';

    var css = `
:host{--ai-emerald:#0B8A5B;--ai-emerald-hover:#0A7A50;--ai-emerald-wash:#E3F5ED;--ai-ink:#1A1A1A;--ai-ink-soft:#5A5A60;--ai-ink-muted:#8A8A90;--ai-bg:#F6F6F7;--ai-line:#ECECEC;--ai-line-2:#E2E2E2;--ai-shadow:0 18px 50px -24px rgba(17,17,17,.32);--ai-shadow-sm:0 8px 24px -16px rgba(17,17,17,.30);--ai-r:24px;--ai-ease:cubic-bezier(.22,.61,.36,1)}
*{box-sizing:border-box}
.aisha{position:fixed;right:24px;bottom:24px;z-index:2147483000;font-family:'Manrope',system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
.aisha-fab{position:relative;display:flex;align-items:center;justify-content:center;width:64px;height:64px;border:0;border-radius:50%;cursor:pointer;background:var(--ai-emerald);color:#fff;box-shadow:var(--ai-shadow);transition:transform .2s var(--ai-ease),background .2s ease;margin-left:auto}
.aisha-fab:hover{background:var(--ai-emerald-hover);transform:translateY(-2px)}
.aisha-fab:active{transform:translateY(0) scale(.96)}
.aisha-fab svg{width:28px;height:28px}
.aisha-fab .ic-close{display:none}
.aisha.is-open .aisha-fab .ic-chat{display:none}
.aisha.is-open .aisha-fab .ic-close{display:block}
.aisha-fab::before{content:"";position:absolute;inset:0;border-radius:50%;border:2px solid var(--ai-emerald);opacity:.6;animation:aishaPulse 2.4s var(--ai-ease) infinite}
.aisha.is-open .aisha-fab::before{display:none}
@keyframes aishaPulse{0%{transform:scale(1);opacity:.55}70%{transform:scale(1.5);opacity:0}100%{opacity:0}}
.aisha-fab .dot-online{position:absolute;top:4px;right:4px;width:13px;height:13px;border-radius:50%;background:#2BD07A;border:2.5px solid #fff}
.aisha-nudge{position:absolute;right:78px;bottom:12px;white-space:nowrap;background:#fff;color:var(--ai-ink);border:1px solid var(--ai-line);box-shadow:var(--ai-shadow-sm);border-radius:999px;padding:11px 18px;font-size:14px;font-weight:600;display:flex;align-items:center;gap:8px;transform-origin:right center;animation:nudgeIn .5s var(--ai-ease) both}
.aisha-nudge b{color:var(--ai-emerald)}
.aisha-nudge .x{margin-left:2px;width:18px;height:18px;border-radius:50%;border:0;cursor:pointer;background:var(--ai-bg);color:var(--ai-ink-muted);font-size:14px;line-height:1;display:flex;align-items:center;justify-content:center}
.aisha.is-open .aisha-nudge,.aisha-nudge.is-hidden{display:none}
@keyframes nudgeIn{from{opacity:0;transform:scale(.8) translateX(8px)}to{opacity:1;transform:none}}
.aisha-panel{position:absolute;right:0;bottom:80px;width:384px;max-width:calc(100vw - 32px);height:600px;max-height:calc(100vh - 130px);background:#fff;border:1px solid var(--ai-line);border-radius:var(--ai-r);box-shadow:var(--ai-shadow);overflow:hidden;display:flex;flex-direction:column;opacity:0;visibility:hidden;transform:translateY(12px) scale(.98);transform-origin:bottom right;transition:opacity .26s var(--ai-ease),transform .26s var(--ai-ease),visibility .26s}
.aisha.is-open .aisha-panel{opacity:1;visibility:visible;transform:none}
.aisha-head{background:var(--ai-emerald);color:#fff;padding:16px 16px 16px 18px;display:flex;align-items:center;gap:12px;flex-shrink:0}
.aisha-ava{width:44px;height:44px;border-radius:50%;background:#fff;flex-shrink:0;display:flex;align-items:center;justify-content:center;position:relative;box-shadow:0 2px 10px rgba(0,0,0,.12)}
.aisha-ava svg{width:30px;height:30px}
.aisha-ava .dot-online{position:absolute;bottom:0;right:0;width:12px;height:12px;border-radius:50%;background:#2BD07A;border:2.5px solid var(--ai-emerald)}
.aisha-id{flex:1;min-width:0}
.aisha-id .name{font-weight:800;font-size:17px;letter-spacing:-.02em;line-height:1.2}
.aisha-id .role{font-size:12.5px;opacity:.85;font-weight:500;margin-top:2px;display:flex;align-items:center;gap:6px}
.aisha-id .role .live{width:7px;height:7px;border-radius:50%;background:#9BF3C6;flex-shrink:0;animation:liveBlink 2s ease-in-out infinite}
@keyframes liveBlink{0%,100%{opacity:1}50%{opacity:.4}}
.aisha-head-btns button{width:34px;height:34px;border:0;border-radius:10px;cursor:pointer;background:transparent;color:#fff;opacity:.85;display:flex;align-items:center;justify-content:center;transition:background .15s,opacity .15s}
.aisha-head-btns button:hover{background:rgba(255,255,255,.16);opacity:1}
.aisha-head-btns svg{width:18px;height:18px}
.aisha-body{flex:1;overflow-y:auto;background:var(--ai-bg);padding:18px 16px 8px;display:flex;flex-direction:column;gap:14px;scroll-behavior:smooth}
.aisha-body::-webkit-scrollbar{width:8px}
.aisha-body::-webkit-scrollbar-thumb{background:#D8D8DC;border-radius:8px;border:2px solid var(--ai-bg)}
.aisha-day{align-self:center;font-size:11.5px;font-weight:600;color:var(--ai-ink-muted);background:#fff;border:1px solid var(--ai-line);border-radius:999px;padding:4px 12px}
.msg{display:flex;gap:9px;max-width:88%;animation:msgIn .32s var(--ai-ease) both}
@keyframes msgIn{from{opacity:0;transform:translateY(8px)}to{opacity:1;transform:none}}
.msg .m-ava{width:30px;height:30px;border-radius:50%;background:var(--ai-emerald-wash);flex-shrink:0;display:flex;align-items:center;justify-content:center;align-self:flex-end}
.msg .m-ava svg{width:20px;height:20px}
.msg .bubble{padding:11px 14px;font-size:14.5px;line-height:1.5;border-radius:16px;color:var(--ai-ink);white-space:pre-wrap}
.msg .time{font-size:11px;color:var(--ai-ink-muted);margin-top:5px;display:block}
.msg.bot{align-self:flex-start}
.msg.bot .bubble{background:#fff;border:1px solid var(--ai-line);border-bottom-left-radius:6px;box-shadow:0 6px 16px -14px rgba(17,17,17,.25)}
.msg.user{align-self:flex-end;margin-left:auto}
.msg.user .bubble{background:var(--ai-emerald);color:#fff;border-bottom-right-radius:6px}
.msg.user .time{text-align:right}
.msg.typing .bubble{display:flex;gap:5px;align-items:center;padding:14px 16px;background:#fff;border:1px solid var(--ai-line);border-bottom-left-radius:6px}
.msg.typing .bubble i{width:7px;height:7px;border-radius:50%;background:var(--ai-emerald);opacity:.5;animation:typed 1.2s ease-in-out infinite}
.msg.typing .bubble i:nth-child(2){animation-delay:.2s}
.msg.typing .bubble i:nth-child(3){animation-delay:.4s}
@keyframes typed{0%,60%,100%{transform:translateY(0);opacity:.45}30%{transform:translateY(-5px);opacity:1}}
.aisha-quick{display:flex;flex-wrap:wrap;gap:8px;padding:4px 16px 14px;background:var(--ai-bg)}
.aisha-quick button{border:1px solid var(--ai-line-2);background:#fff;color:var(--ai-ink);font-family:inherit;font-size:13px;font-weight:600;cursor:pointer;padding:8px 14px;border-radius:999px;transition:border-color .15s,color .15s,background .15s}
.aisha-quick button:hover{border-color:var(--ai-emerald);color:var(--ai-emerald);background:var(--ai-emerald-wash)}
.aisha-input{flex-shrink:0;border-top:1px solid var(--ai-line);background:#fff;padding:12px 12px 8px}
.aisha-input form{display:flex;align-items:flex-end;gap:8px}
.aisha-input textarea{flex:1;border:1px solid var(--ai-line-2);border-radius:14px;resize:none;font-family:inherit;font-size:14.5px;line-height:1.4;color:var(--ai-ink);padding:11px 14px;max-height:96px;outline:none;transition:border-color .15s,box-shadow .15s}
.aisha-input textarea::placeholder{color:var(--ai-ink-muted)}
.aisha-input textarea:focus{border-color:var(--ai-emerald);box-shadow:0 0 0 3px var(--ai-emerald-wash)}
.aisha-send{width:44px;height:44px;flex-shrink:0;border:0;border-radius:12px;cursor:pointer;background:var(--ai-emerald);color:#fff;display:flex;align-items:center;justify-content:center;transition:background .15s,transform .12s}
.aisha-send:hover{background:var(--ai-emerald-hover)}
.aisha-send:active{transform:scale(.92)}
.aisha-send:disabled{opacity:.5;cursor:default}
.aisha-send svg{width:20px;height:20px}
.aisha-consent{font-size:11px;line-height:1.4;color:var(--ai-ink-muted);text-align:center;padding:8px 8px 4px}
.aisha-consent a{color:var(--ai-ink-soft)}
@media(max-width:560px){.aisha{right:12px;bottom:12px}.aisha.is-open .aisha-fab{display:none}.aisha-panel{position:fixed;left:0;right:0;top:0;bottom:0;width:100vw;max-width:100vw;height:100dvh;max-height:100dvh;border-radius:0;border:0;transform-origin:center}.aisha-nudge{display:none}.aisha-head{padding-top:max(14px,env(safe-area-inset-top))}.aisha-input{padding-bottom:max(8px,env(safe-area-inset-bottom))}.aisha-body{padding-bottom:14px}.msg{max-width:90%}.aisha-quick{padding-bottom:16px}}
@media(prefers-reduced-motion:reduce){.aisha-fab::before,.aisha-nudge,.msg,.aisha-id .role .live,.msg.typing .bubble i{animation:none}.aisha-panel{transition:opacity .01s,visibility .01s}}
`;
    // ── Подменяем хардкод-цвета на бренд через replace (минимальное вмешательство) ──
    css = css.split('#0B8A5B').join(COLOR_PRIMARY)
             .split('#0A7A50').join(COLOR_HOVER)
             .split('#E3F5ED').join(COLOR_WASH);
    // ── Позиционирование (br/bl/tr/tl): подменяем правило .aisha{} ───────────────
    if (POSITION !== 'br') {
      css = css.replace(/\.aisha\{position:fixed;right:24px;bottom:24px;/, '.aisha{position:fixed;'+POS_CSS+';');
    }

    // Аватарка: используем кастомное лого если есть, иначе старый SVG
    var AVA_HEAD, AVA_MSG;
    if (cfg.logo_url) {
      AVA_HEAD = '<img src="'+_esc(cfg.logo_url)+'" alt="" style="width:100%;height:100%;border-radius:50%;object-fit:cover">';
      AVA_MSG  = '<img src="'+_esc(cfg.logo_url)+'" alt="" style="width:100%;height:100%;border-radius:50%;object-fit:cover">';
    } else {
      AVA_HEAD='<svg viewBox="0 0 32 32" fill="none"><circle cx="16" cy="13" r="6.2" stroke="#1A1A1A" stroke-width="1.7"/><path d="M7 26c1.4-4.2 5-6.4 9-6.4s7.6 2.2 9 6.4" stroke="#1A1A1A" stroke-width="1.7" stroke-linecap="round"/><path d="M8.2 14.4A7.8 7.8 0 0 1 23.8 14.4" stroke="'+COLOR_PRIMARY+'" stroke-width="1.7" stroke-linecap="round"/><rect x="6" y="14" width="3.4" height="5.4" rx="1.7" fill="'+COLOR_PRIMARY+'"/><rect x="22.6" y="14" width="3.4" height="5.4" rx="1.7" fill="'+COLOR_PRIMARY+'"/><path d="M22.6 18.5c0 2.4-2 3.7-4 3.7" stroke="'+COLOR_PRIMARY+'" stroke-width="1.7" stroke-linecap="round"/></svg>';
      AVA_MSG='<svg viewBox="0 0 32 32" fill="none"><circle cx="16" cy="13" r="6" stroke="#1A1A1A" stroke-width="1.6"/><path d="M7.5 25c1.3-3.9 4.7-6 8.5-6s7.2 2.1 8.5 6" stroke="#1A1A1A" stroke-width="1.6" stroke-linecap="round"/><path d="M9 14a7 7 0 0 1 14 0" stroke="'+COLOR_PRIMARY+'" stroke-width="1.6" stroke-linecap="round"/></svg>';
    }
    var markup='<div class="aisha">'
      +'<section class="aisha-panel" role="dialog" aria-label="'+_esc(CHAT_TITLE)+'">'
        +'<header class="aisha-head"><div class="aisha-ava">'+AVA_HEAD+'<span class="dot-online"></span></div>'
          +'<div class="aisha-id"><div class="name">'+_esc(BOT_NAME)+'</div><div class="role"><span class="live"></span>'+_esc(SUBTITLE)+'</div></div>'
          +'<div class="aisha-head-btns"><button class="aisha-min" type="button" aria-label="Свернуть"><svg viewBox="0 0 24 24" fill="none"><path d="M6 12h12" stroke="currentColor" stroke-width="2" stroke-linecap="round"/></svg></button></div></header>'
        +'<div class="aisha-body"><div class="aisha-day">Сегодня</div></div>'
        +'<div class="aisha-quick"><button type="button" data-q="Расскажите про речевую аналитику Echolytics">Речевая аналитика</button><button type="button" data-q="Сколько стоит автообзвон Phonex?">Цены</button><button type="button" data-q="Хочу записаться на демо">Записаться на демо</button></div>'
        +'<div class="aisha-input"><form class="aisha-form"><textarea class="aisha-text" rows="1" placeholder="Напишите сообщение…" aria-label="Сообщение"></textarea>'
          +'<button class="aisha-send" type="submit" aria-label="Отправить"><svg viewBox="0 0 24 24" fill="none"><path d="M4.5 12 19 5l-3.2 14-4-5.2L4.5 12Z" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round"/><path d="m11.8 13.8 4-4.6" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/></svg></button></form>'
          +'<div class="aisha-consent">Отправляя сообщение, вы соглашаетесь с <a href="/cookie/" target="_blank" rel="noopener">политикой конфиденциальности</a> (152-ФЗ)</div></div>'
      +'</section>'
      +'<div class="aisha-nudge is-hidden">'+NUDGE_HTML+'<button class="aisha-nudge-x" type="button" aria-label="Закрыть">&times;</button></div>'
      +'<button class="aisha-fab" type="button" aria-label="Открыть чат"><span class="dot-online"></span>'
        +'<svg class="ic-chat" viewBox="0 0 24 24" fill="none"><path d="M4 5.5h16a1.5 1.5 0 0 1 1.5 1.5v8a1.5 1.5 0 0 1-1.5 1.5H9l-4 3.5V16.5H4A1.5 1.5 0 0 1 2.5 15V7A1.5 1.5 0 0 1 4 5.5Z" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round"/><circle cx="8" cy="11" r="1.1" fill="currentColor"/><circle cx="12" cy="11" r="1.1" fill="currentColor"/><circle cx="16" cy="11" r="1.1" fill="currentColor"/></svg>'
        +'<svg class="ic-close" viewBox="0 0 24 24" fill="none"><path d="m7 7 10 10M17 7 7 17" stroke="currentColor" stroke-width="2" stroke-linecap="round"/></svg></button>'
      +'</div>';

    sh.innerHTML = '<style>'+css+'</style>'+markup;

    var q=function(s){return sh.querySelector(s);};
    var root=q('.aisha'), fab=q('.aisha-fab'), bodyEl=q('.aisha-body'), form=q('.aisha-form'),
        ta=q('.aisha-text'), sendBtn=q('.aisha-send'), nudge=q('.aisha-nudge'),
        nudgeX=q('.aisha-nudge-x'), minBtn=q('.aisha-min'), quick=q('.aisha-quick');

    function nowT(){ var d=new Date(); return ('0'+d.getHours()).slice(-2)+':'+('0'+d.getMinutes()).slice(-2); }
    function scrollDown(){ bodyEl.scrollTop=bodyEl.scrollHeight; }
    function addMsg(role,txt){ var el=document.createElement('div'); el.className='msg '+role; var ava=(role==='bot')?('<div class="m-ava">'+AVA_MSG+'</div>'):''; el.innerHTML=ava+'<div><div class="bubble"></div><span class="time">'+nowT()+'</span></div>'; el.querySelector('.bubble').textContent=txt; bodyEl.appendChild(el); scrollDown(); return el; }
    function typing(on){ var ex=q('.msg.typing'); if(on){ if(ex)return; var el=document.createElement('div'); el.className='msg bot typing'; el.innerHTML='<div class="m-ava">'+AVA_MSG+'</div><div class="bubble"><i></i><i></i><i></i></div>'; bodyEl.appendChild(el); scrollDown(); } else if(ex){ ex.remove(); } }
    function hideQuick(){ if(quick) quick.style.display='none'; }
    function hideNudge(){ if(nudge) nudge.classList.add('is-hidden'); if(nudgeTimer){clearTimeout(nudgeTimer);nudgeTimer=null;} }

    function callApi(message){
      return fetch(API+'/api/sales-chat',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({session_id:sid,message:message})})
        .then(function(r){return r.text();}).then(function(t){var j;try{j=JSON.parse(t);}catch(e){j={reply:'Секунду — попробуйте ещё раз.'};} if(j&&j.session_id){sid=j.session_id;writeSid(sid);} return j;});
    }
    function send(txt){
      if(busy) return; busy=true; if(sendBtn) sendBtn.disabled=true;
      if(txt){ addMsg('user',txt); hideQuick(); }
      typing(true);
      callApi(txt||'').then(function(j){ typing(false); addMsg('bot',(j&&(j.reply||j.error))||'…'); })
        .catch(function(){ typing(false); addMsg('bot','Связь прервалась — попробуйте ещё раз.'); })
        .then(function(){ busy=false; if(sendBtn) sendBtn.disabled=false; if(ta) ta.focus(); });
    }
    function submitText(){ var v=ta.value.trim(); if(v && !busy){ send(v); ta.value=''; ta.style.height='auto'; } }
    function loadHistory(){
      fetch(API+'/api/sales-history?sid='+encodeURIComponent(sid)).then(function(r){return r.json();}).then(function(j){
        var ms=(j&&j.messages)||[]; if(ms.length){ hideQuick(); ms.forEach(function(m){ addMsg(m.direction==='in'?'user':'bot', m.text); }); } else { send(''); }
      }).catch(function(){ send(''); });
    }
    function openChat(){ root.classList.add('is-open'); hideNudge(); if(!greeted){ greeted=true; if(sid) loadHistory(); else send(''); } setTimeout(function(){ if(ta) ta.focus(); scrollDown(); },130); }
    function closeChat(){ root.classList.remove('is-open'); }
    function toggle(){ root.classList.contains('is-open')?closeChat():openChat(); }

    fab.addEventListener('click',toggle);
    if(minBtn) minBtn.addEventListener('click',closeChat);
    nudge.addEventListener('click',openChat);
    nudgeX.addEventListener('click',function(e){ e.stopPropagation(); hideNudge(); });
    form.addEventListener('submit',function(e){ e.preventDefault(); submitText(); });
    ta.addEventListener('keydown',function(e){ if(e.key==='Enter' && !e.shiftKey){ e.preventDefault(); submitText(); } });
    ta.addEventListener('input',function(){ ta.style.height='auto'; ta.style.height=Math.min(ta.scrollHeight,96)+'px'; });
    quick.addEventListener('click',function(e){ var b=e.target.closest('button[data-q]'); if(!b||busy) return; send(b.getAttribute('data-q')); });

    nudgeTimer=setTimeout(function(){ if(!root.classList.contains('is-open')) nudge.classList.remove('is-hidden'); }, 9000);
  }
  if(document.body) boot(); else window.addEventListener('DOMContentLoaded', boot);
})();
