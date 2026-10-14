// ToocaaLaser Connect – lets the ToocaaLaser web app (https page) reach a
// TOOCAA NOVA on the local network: HTTP requests and WebSocket relay.
// Message protocol is the same as TOOCAA's own "TOOCAA Connect" extension,
// so the web app works with either. Safety: only http:// and ws:// to
// local-network addresses are relayed; nothing on the internet.
'use strict';

const isLocal = h => h === 'localhost' || h.endsWith('.local') ||
  /^127\./.test(h) || /^10\./.test(h) || /^192\.168\./.test(h) ||
  /^172\.(1[6-9]|2\d|3[01])\./.test(h) || /^169\.254\./.test(h);
function allowed(url, protocol) {
  try { const u = new URL(url); return u.protocol === protocol && isLocal(u.hostname); } catch { return false; }
}
const toText = d => {
  if (typeof d === 'string') return d;
  const a = new Uint8Array(d); let s = '';
  for (let i = 0; i < a.length; i += 8192) s += String.fromCharCode.apply(null, a.subarray(i, i + 8192));
  return s;
};

chrome.runtime.onMessageExternal.addListener((msg, sender, reply) => {
  if (msg && msg.type === 'ping') {
    reply({ok: true, name: 'ToocaaLaser Connect', version: chrome.runtime.getManifest().version});
    return;
  }
  if (msg && msg.requestType === 'http-fetch') {
    if (!allowed(msg.url, 'http:')) { reply({success: false, error: 'blocked: only http:// to local network addresses'}); return; }
    (async () => {
      const ctrl = new AbortController(), to = setTimeout(() => ctrl.abort(), msg.timeout || 120000);
      try {
        const r = await fetch(msg.url, {...(msg.fetchOptions || {}), signal: ctrl.signal});
        const ct = r.headers.get('content-type') || '';
        const text = await r.text();
        let data = text, responseType = 'text';
        if (/json/.test(ct) || /^\s*[{[]/.test(text)) { try { data = JSON.parse(text); responseType = 'json'; } catch {} }
        reply({success: true, status: r.status, data, contentType: ct, responseType});
      } catch (e) { reply({success: false, error: String(e)}); }
      finally { clearTimeout(to); }
    })();
    return true;  // async reply
  }
});

chrome.runtime.onConnectExternal.addListener(port => {
  const sockets = new Map();
  const post = m => { try { port.postMessage(m); } catch {} };
  port.onMessage.addListener(m => {
    if (!m || typeof m !== 'object') return;
    const id = m.connectionId;
    if (m.type === 'ws-open') {
      if (!allowed(m.url, 'ws:')) return post({type: 'ws-error', connectionId: id, error: 'blocked: only ws:// to local network addresses'});
      const old = sockets.get(id); if (old) old.close();
      const ws = new WebSocket(m.url); ws.binaryType = 'arraybuffer'; sockets.set(id, ws);
      ws.onopen = () => post({type: 'ws-opened', connectionId: id});
      ws.onmessage = e => post({type: 'ws-message', connectionId: id, data: toText(e.data)});
      ws.onerror = () => post({type: 'ws-error', connectionId: id, error: 'WebSocket error'});
      ws.onclose = () => { if (sockets.get(id) === ws) sockets.delete(id); post({type: 'ws-closed', connectionId: id}); };
    } else if (m.type === 'ws-send') {
      const ws = sockets.get(id);
      if (!ws || ws.readyState !== WebSocket.OPEN) return post({type: 'ws-error', connectionId: id, error: 'WebSocket is not open'});
      ws.send(m.data);
    } else if (m.type === 'ws-close') {
      const ws = sockets.get(id); if (ws) ws.close(); sockets.delete(id);
    }
    // 'keepalive' and unknown types: receiving the message is enough to keep the worker alive
  });
  port.onDisconnect.addListener(() => { for (const ws of sockets.values()) try { ws.close(); } catch {} sockets.clear(); });
});
