// ==UserScript==
// @name         ToocaaLaser – TOOCAA Studio sniffer
// @namespace    https://github.com/Chuck3CZ/ToocaaLaser
// @version      1.0
// @description  Logs everything TOOCAA Studio sends to / receives from the NOVA (USB Web Serial, Wi-Fi via TOOCAA Connect). Read-only: data passes through unchanged.
// @match        https://toocaastudio.com/*
// @match        https://*.toocaastudio.com/*
// @run-at       document-start
// @grant        none
// ==/UserScript==
//
// Use without Tampermonkey: in TOOCAA Studio disconnect the machine, reload
// the page, open DevTools (F12) → Console and run
//     import('https://chuck3cz.github.io/ToocaaLaser/tools/studio-sniffer.js')
// then connect the NOVA again. A panel appears bottom-right.
//
// How it works: SerialPort.prototype.readable / .writable are wrapped so the
// streams Studio gets are pass-through copies that also append to a log.
// Nothing is injected into the data and nothing is sent anywhere – the log
// stays in this tab until you press "Download".
(() => {
  'use strict';
  const W = typeof unsafeWindow !== 'undefined' ? unsafeWindow : window;
  if (W.__tlSniff) { W.__tlSniff.show(); return; }

  const T0 = performance.now();
  const log = [];            // {t, d, s}
  const uploads = [];        // captured Wi-Fi job/OTA uploads
  const listeners = new Set();
  const stats = {TX: 0, RX: 0, WS: 0};
  const SECRET = /(pass(word)?|psk|ssid|token|key)\s*[:=]\s*("?)[^",\s]+\3/gi;

  const esc = s => s.replace(/[\x00-\x08\x0b-\x1f\x7f-\x9f]/g, c => '\\x' + c.charCodeAt(0).toString(16).padStart(2, '0'));
  function redact(s) {
    let r = s.replace(SECRET, (m, k) => k + '=******');
    if (/^FF F0\b/.test(r)) r = 'FF F0 ******  (Wi-Fi credentials redacted)';
    return r;
  }
  function add(d, s) {
    const e = {t: performance.now() - T0, d, s: redact(esc(s))};
    log.push(e); if (stats[d] !== undefined) stats[d]++;
    listeners.forEach(f => f(e));
  }
  // assemble byte chunks into lines per direction
  const lineBuf = {TX: '', RX: ''}, flushTimer = {};
  function feed(d, chunk) {
    let s = typeof chunk === 'string' ? chunk : latin1(chunk);
    // real-time bytes at a line start are logged immediately (keeps timing honest)
    while (d === 'TX' && !lineBuf[d] && /^[?!~\x18\x84\x85\x90-\xa1]/.test(s)) { add(d, s[0] + '  ⟨real-time⟩'); s = s.slice(1); }
    lineBuf[d] += s;
    let i;
    while ((i = lineBuf[d].search(/\r?\n/)) >= 0) {
      const line = lineBuf[d].slice(0, i), rest = lineBuf[d].slice(i).replace(/^\r?\n/, '');
      lineBuf[d] = rest;
      emitLine(d, line);
    }
    // real-time bytes and partial lines: flush after a short pause
    clearTimeout(flushTimer[d]);
    if (lineBuf[d]) flushTimer[d] = setTimeout(() => { if (lineBuf[d]) { emitLine(d, lineBuf[d] + ' ⟨no newline⟩'); lineBuf[d] = ''; } }, 60);
  }
  function emitLine(d, line) {
    if (d === 'TX') {   // split leading real-time characters (? ! ~ 0x18 0x85…) into their own entries
      let m;
      while ((m = /^([?!~\x18\x85\x84\x90-\x9f\xa0-\xa1])/.exec(line))) { add(d, m[1] + '  ⟨real-time⟩'); line = line.slice(1); }
      if (!line || line === ' ⟨no newline⟩') return;
    }
    add(d, line);
  }
  function latin1(u8) {
    const a = u8 instanceof Uint8Array ? u8 : new Uint8Array(u8.buffer || u8);
    let s = ''; for (let i = 0; i < a.length; i += 8192) s += String.fromCharCode.apply(null, a.subarray(i, i + 8192));
    return s;
  }
  const textOf = chunk => typeof chunk === 'string' ? chunk : latin1(chunk);

  // ------------------------------------------------------------ Web Serial
  if (W.SerialPort && W.SerialPort.prototype) {
    const P = W.SerialPort.prototype;
    const rGet = Object.getOwnPropertyDescriptor(P, 'readable').get;
    const wGet = Object.getOwnPropertyDescriptor(P, 'writable').get;
    const oOpen = P.open, oClose = P.close;
    const rMap = new WeakMap(), wMap = new WeakMap(), inner = new WeakMap();

    Object.defineProperty(P, 'readable', {configurable: true, get() {
      const src = rGet.call(this); if (!src) return src;
      if (!rMap.has(src)) {
        let reader = null;
        const port = this;
        rMap.set(src, new ReadableStream({
          async pull(ctrl) {
            try {
              reader = reader || src.getReader();
              const {value, done} = await reader.read();
              if (done) { ctrl.close(); try { reader.releaseLock(); } catch {} reader = null; return; }
              feed('RX', value); ctrl.enqueue(value);
            } catch (e) { ctrl.error(e); try { reader && reader.releaseLock(); } catch {} reader = null; }
          },
          async cancel(reason) { try { reader && await reader.cancel(reason); } catch {} try { reader && reader.releaseLock(); } catch {} reader = null; },
        }));
        inner.set(port, Object.assign(inner.get(port) || {}, {releaseR: async () => { try { reader && await reader.cancel(); } catch {} try { reader && reader.releaseLock(); } catch {} reader = null; }}));
      }
      return rMap.get(src);
    }});
    Object.defineProperty(P, 'writable', {configurable: true, get() {
      const dst = wGet.call(this); if (!dst) return dst;
      if (!wMap.has(dst)) {
        let writer = null;
        const getW = () => writer || (writer = dst.getWriter());
        const rel = () => { try { writer && writer.releaseLock(); } catch {} writer = null; };
        wMap.set(dst, new WritableStream({
          async write(chunk) { feed('TX', chunk); await getW().write(chunk); },
          async close() { try { await getW().close(); } finally { rel(); } },
          async abort(r) { try { await getW().abort(r); } finally { rel(); } },
        }));
        inner.set(this, Object.assign(inner.get(this) || {}, {releaseW: rel}));
      }
      return wMap.get(dst);
    }});
    P.open = async function (opts) {
      add('NOTE', `serial open ${JSON.stringify(opts)} ${JSON.stringify(this.getInfo ? this.getInfo() : {})}`);
      return oOpen.call(this, opts);
    };
    P.close = async function () {
      const i = inner.get(this);   // free our own locks so the real close() succeeds
      if (i) { if (i.releaseR) await i.releaseR(); if (i.releaseW) i.releaseW(); }
      add('NOTE', 'serial close');
      return oClose.call(this);
    };
  }

  // ------------------------------------------------------------ Wi-Fi (TOOCAA Connect extension messaging)
  function hookRuntime() {
    const rt = W.chrome && W.chrome.runtime;
    if (!rt || rt.__tlHooked) return;
    try {
      if (rt.connect) {
        const oc = rt.connect.bind(rt);
        rt.connect = function (...a) {
          const port = oc(...a), op = port.postMessage.bind(port);
          port.postMessage = m => { onExtMsg('>', m); return op(m); };
          port.onMessage.addListener(m => onExtMsg('<', m));
          add('NOTE', 'extension port connected ' + JSON.stringify(a.slice(1)));
          return port;
        };
      }
      if (rt.sendMessage) {
        const os = rt.sendMessage.bind(rt);
        rt.sendMessage = function (...a) {
          const msg = a.find(x => x && typeof x === 'object' && x.requestType);
          if (msg) add('HTTP', `${(msg.fetchOptions && msg.fetchOptions.method) || 'GET'} ${msg.url}${msg.fetchOptions && msg.fetchOptions.body ? ' body=' + String(msg.fetchOptions.body).slice(0, 2000) : ''}`);
          const cbi = a.findIndex(x => typeof x === 'function');
          if (cbi >= 0) { const cb = a[cbi]; a[cbi] = r => { add('HTTP', '← ' + JSON.stringify(r).slice(0, 4000)); return cb(r); }; }
          return os(...a);
        };
      }
      rt.__tlHooked = true;
    } catch (e) { add('NOTE', 'could not hook chrome.runtime: ' + e); }
  }
  let curUpload = null;
  function onExtMsg(dir, m) {
    if (!m || typeof m !== 'object') return add('WS', dir + ' ' + String(m));
    if (m.type === 'ws-send') return feedWs('WS>', m.data);
    if (m.type === 'ws-message') return feedWs('WS<', m.data);
    if (m.type === 'file-chunk-transfer') {
      if (m.transferType === 'file-start-upload') curUpload = {url: m.url, method: m.method, headers: m.headers, chunks: [], t: performance.now() - T0};
      else if (m.transferType === 'file-chunk' && curUpload) curUpload.chunks[m.chunkIndex] = new Uint8Array(m.fileChunk);
      else if (m.transferType === 'file-end-upload') {
        const u = curUpload || {chunks: []}; u.url = m.url || u.url; u.method = m.method || u.method; u.headers = m.headers || u.headers;
        u.blob = new Blob(u.chunks.filter(Boolean)); uploads.push(u); curUpload = null;
        add('NOTE', `upload #${uploads.length} ${u.method || 'POST'} ${u.url} ${u.blob.size} B headers=${JSON.stringify(u.headers || {})}`);
        refreshUploads();
      }
      return;
    }
    if (m.type === 'upload-complete') return add('NOTE', 'upload reply ' + JSON.stringify(m.data || m.error).slice(0, 1000));
    add('WS', dir + ' ' + JSON.stringify(m).slice(0, 2000));
  }
  function feedWs(d, data) {
    const s = typeof data === 'string' ? data : (data && data.byteLength !== undefined ? latin1(new Uint8Array(data)) : JSON.stringify(data));
    for (const line of s.split(/\r?\n/)) if (line) add('WS', `${d} ${line}`);
  }
  hookRuntime();
  if (W.document.readyState === 'loading') W.document.addEventListener('DOMContentLoaded', hookRuntime);

  // ------------------------------------------------------------ panel
  let ui = null, hidePoll = true, paused = false;
  const isPoll = e => (e.d === 'TX' && /^(\?|FF F2)\b/.test(e.s)) || (e.d === 'RX' && /^<.*>$/.test(e.s)) || (e.d === 'WS' && /^WS[<>] (\?|<.*>$|FF F2)/.test(e.s));
  const fmt = e => `${(e.t / 1000).toFixed(3).padStart(9)}s ${e.d.padEnd(4)} ${e.s}`;
  function text() { return log.map(fmt).join('\n') + '\n'; }
  function download(name, blob) { const a = W.document.createElement('a'); a.href = URL.createObjectURL(blob); a.download = name; a.click(); setTimeout(() => URL.revokeObjectURL(a.href), 2000); }
  function stamp() { return new Date().toISOString().replace(/[:.]/g, '-').slice(0, 19); }
  function refreshUploads() {
    if (!ui) return;
    ui.up.textContent = '';
    uploads.forEach((u, i) => {
      const b = W.document.createElement('button'); b.textContent = `⬇ upload ${i + 1} (${u.blob.size} B)`;
      b.onclick = () => download(`studio-upload-${i + 1}.bin`, u.blob); ui.up.append(b);
    });
  }
  function build() {
    const d = W.document, box = d.createElement('div');
    box.innerHTML = `
<style>
#tlsn{position:fixed;right:12px;bottom:12px;z-index:2147483647;width:min(560px,calc(100vw - 24px));background:#111827;color:#e5e7eb;border:1px solid #374151;border-radius:10px;font:12px/1.4 ui-monospace,Menlo,Consolas,monospace;box-shadow:0 8px 30px rgba(0,0,0,.4)}
#tlsn header{display:flex;gap:8px;align-items:center;padding:8px 10px;border-bottom:1px solid #374151;font:600 13px system-ui,sans-serif}
#tlsn header span{flex:1}#tlsn .c{color:#9ca3af;font:12px system-ui}
#tlsn pre{margin:0;height:260px;overflow:auto;padding:8px 10px;white-space:pre-wrap;word-break:break-all}
#tlsn .row{display:flex;gap:6px;flex-wrap:wrap;padding:8px 10px;border-top:1px solid #374151;align-items:center}
#tlsn button{font:12px system-ui;background:#1f2937;color:#e5e7eb;border:1px solid #4b5563;border-radius:6px;padding:4px 8px;cursor:pointer}
#tlsn button.p{background:#f97316;border-color:#f97316;color:#111}
#tlsn input[type=text]{flex:1;min-width:120px;background:#0b1220;color:#e5e7eb;border:1px solid #4b5563;border-radius:6px;padding:4px 6px;font:12px system-ui}
#tlsn label{font:12px system-ui;color:#9ca3af}
#tlsn .TX{color:#93c5fd}#tlsn .RX{color:#e5e7eb}#tlsn .NOTE{color:#fbbf24}#tlsn .WS{color:#a7f3d0}#tlsn .HTTP{color:#f9a8d4}#tlsn .MARK{color:#111;background:#fbbf24}
#tlsn.min pre,#tlsn.min .row{display:none}
</style>
<div id="tlsn"><header><span>ToocaaLaser sniffer</span><b class="c" id="tlsnc"></b><button id="tlsnm" title="minimise">–</button></header>
<pre id="tlsnl"></pre>
<div class="row"><input type="text" id="tlsnt" placeholder="note, e.g. clicked Autofocus"><button class="p" id="tlsnk">Mark</button></div>
<div class="row"><label><input type="checkbox" id="tlsnh" checked> hide status polling</label><label><input type="checkbox" id="tlsnp"> pause view</label>
<button id="tlsnd" class="p">Download log</button><button id="tlsnx">Clear</button><span id="tlsnu"></span></div></div>`;
    d.body.append(box);
    const $ = id => d.getElementById(id);
    ui = {root: $('tlsn'), pre: $('tlsnl'), cnt: $('tlsnc'), up: $('tlsnu')};
    $('tlsnm').onclick = () => ui.root.classList.toggle('min');
    $('tlsnh').onchange = e => { hidePoll = e.target.checked; redraw(); };
    $('tlsnp').onchange = e => { paused = e.target.checked; if (!paused) redraw(); };
    const mark = () => { const v = $('tlsnt').value.trim() || 'mark'; add('MARK', '──── ' + v + ' ────'); $('tlsnt').value = ''; };
    $('tlsnk').onclick = mark; $('tlsnt').onkeydown = e => { if (e.key === 'Enter') mark(); e.stopPropagation(); };
    $('tlsnd').onclick = () => download(`studio-capture-${stamp()}.txt`, new Blob([header() + text()], {type: 'text/plain'}));
    $('tlsnx').onclick = () => { log.length = 0; Object.keys(stats).forEach(k => stats[k] = 0); redraw(); };
    redraw(); refreshUploads();
  }
  function header() {
    return `# ToocaaLaser capture of TOOCAA Studio\n# started ${new Date(Date.now() - (performance.now() - T0)).toISOString()}  page ${location.href}\n# format: <seconds> <TX|RX|WS|HTTP|NOTE|MARK> <data>; control bytes as \\xHH; Wi-Fi credentials redacted\n`;
  }
  let pending = false;
  function redraw() {
    if (!ui || paused) return;
    if (pending) return; pending = true;
    requestAnimationFrame(() => {
      pending = false;
      const rows = log.filter(e => !(hidePoll && isPoll(e))).slice(-400);
      ui.pre.textContent = '';
      const frag = W.document.createDocumentFragment();
      for (const e of rows) { const s = W.document.createElement('div'); s.className = e.d; s.textContent = fmt(e); frag.append(s); }
      ui.pre.append(frag); ui.pre.scrollTop = ui.pre.scrollHeight;
      ui.cnt.textContent = `TX ${stats.TX} · RX ${stats.RX}${stats.WS ? ' · WS ' + stats.WS : ''}`;
    });
  }
  listeners.add(redraw);
  const mount = () => { if (!ui && W.document.body) build(); };
  if (W.document.body) mount(); else W.document.addEventListener('DOMContentLoaded', mount);

  W.__tlSniff = {log, uploads, text, show: () => { mount(); ui && ui.root.classList.remove('min'); }, mark: s => add('MARK', '──── ' + s + ' ────')};
  add('NOTE', 'sniffer ready – ' + (W.SerialPort ? 'Web Serial hooked' : 'no Web Serial in this browser') + ' – connect the NOVA now');
})();
