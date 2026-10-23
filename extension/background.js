// ToocaaLaser Connect – lets the ToocaaLaser web app (https page) reach a
// TOOCAA NOVA on the local network: HTTP requests and WebSocket relay.
// Message protocol is the same as TOOCAA's own "TOOCAA Connect" extension,
// so the web app works with either.
//
// Safety rules (unlike TOOCAA Connect, which relays to any URL):
//  * targets: only private / loopback / link-local IPv4 *literals* – a host
//    name like "10.example.com" is refused, it could point anywhere;
//  * only the NOVA's port 8002, only http:// and ws://;
//  * redirects are not followed, no cookies or credentials are sent,
//    only GET/POST with a JSON/text body and a Content-Type header.
'use strict';

const PORTS = new Set(['8002']);
function isPrivateIPv4(h) {
  const m = /^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})$/.exec(h);
  if (!m) return false;
  const [a, b] = [+m[1], +m[2]];
  if ([m[1], m[2], m[3], m[4]].some(x => +x > 255)) return false;
  return a === 10 || a === 127 || (a === 192 && b === 168) || (a === 172 && b >= 16 && b <= 31) || (a === 169 && b === 254);
}
function allowed(url, protocol) {
  try {
    const u = new URL(url);
    return u.protocol === protocol && isPrivateIPv4(u.hostname) && PORTS.has(u.port) && !u.username && !u.password;
  } catch { return false; }
}
// rebuild fetch options from a small whitelist instead of passing them through
function safeOptions(o) {
  o = o || {};
  const method = String(o.method || 'GET').toUpperCase() === 'POST' ? 'POST' : 'GET';
  const out = {method, redirect: 'error', credentials: 'omit', cache: 'no-store', referrerPolicy: 'no-referrer'};
  const ct = o.headers && (o.headers['Content-Type'] || o.headers['content-type']);
  if (ct) out.headers = {'Content-Type': String(ct)};
  if (method === 'POST' && typeof o.body === 'string') out.body = o.body;
  return out;
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
    if (!allowed(msg.url, 'http:')) { reply({success: false, error: 'blocked: only http://<local IP>:8002'}); return; }
    (async () => {
      const ctrl = new AbortController(), to = setTimeout(() => ctrl.abort(), msg.timeout || 120000);
      try {
        const r = await fetch(msg.url, {...safeOptions(msg.fetchOptions), signal: ctrl.signal});
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
      if (!allowed(m.url, 'ws:')) return post({type: 'ws-error', connectionId: id, error: 'blocked: only ws://<local IP>:8002'});
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
