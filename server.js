const http = require('http');
const https = require('https');

const PORT = Number(process.env.PORT) || 4173;
const UPSTREAM_HOST = 'www.puffco.app';

const HOP_BY_HOP = new Set([
  'connection',
  'keep-alive',
  'proxy-authenticate',
  'proxy-authorization',
  'te',
  'trailers',
  'transfer-encoding',
  'upgrade',
  'host',
]);

// HSTS from the upstream host must not be stored for localhost, or the
// browser will refuse this http site on the next load.
const STRIP_RESPONSE = new Set([
  'strict-transport-security',
  'expect-ct',
  'content-security-policy',
  'content-security-policy-report-only',
]);

function rewriteSetCookie(cookie) {
  return cookie
    .replace(/;\s*Domain=[^;]*/gi, '')
    .replace(/;\s*Secure/gi, '')
    .replace(/;\s*Partitioned/gi, '')
    .replace(/;\s*SameSite=None/gi, '; SameSite=Lax');
}

function rewriteLocation(location) {
  return location.replace(/^https:\/\/(?:www\.)?puffco\.app(?=\/|$)/i, '');
}

function proxy(clientReq, clientRes) {
  const path = clientReq.url || '/';
  if (!path.startsWith('/')) {
    clientRes.writeHead(400, { 'content-type': 'text/plain; charset=utf-8' });
    clientRes.end('Bad request');
    return;
  }

  const headers = {};
  for (const [name, value] of Object.entries(clientReq.headers)) {
    if (HOP_BY_HOP.has(name.toLowerCase())) continue;
    headers[name] = value;
  }
  headers.host = UPSTREAM_HOST;

  const upstreamReq = https.request(
    {
      hostname: UPSTREAM_HOST,
      port: 443,
      method: clientReq.method,
      path,
      headers,
    },
    (upstreamRes) => {
      const out = {};
      for (const [name, value] of Object.entries(upstreamRes.headers)) {
        const lower = name.toLowerCase();
        if (HOP_BY_HOP.has(lower) || STRIP_RESPONSE.has(lower)) continue;
        if (lower === 'set-cookie') {
          const cookies = Array.isArray(value) ? value : [value];
          out['set-cookie'] = cookies.map(rewriteSetCookie);
          continue;
        }
        if (lower === 'location' && typeof value === 'string') {
          out.location = rewriteLocation(value);
          continue;
        }
        out[name] = value;
      }
      clientRes.writeHead(upstreamRes.statusCode || 502, out);
      upstreamRes.pipe(clientRes);
    },
  );

  upstreamReq.on('error', (error) => {
    if (clientRes.headersSent) {
      clientRes.end();
      return;
    }
    clientRes.writeHead(502, { 'content-type': 'text/plain; charset=utf-8' });
    clientRes.end(`Could not reach ${UPSTREAM_HOST}: ${error.message}`);
  });

  clientReq.pipe(upstreamReq);
}

const server = http.createServer(proxy);

server.listen(PORT, '127.0.0.1', () => {
  console.log(`Puffco desktop site at http://127.0.0.1:${PORT}/`);
});
