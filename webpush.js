// Minimální Web Push bez závislostí: VAPID (RFC 8292) + šifrování aes128gcm (RFC 8291).
const crypto = require("crypto");
const https = require("https");

const b64u = (buf) => Buffer.from(buf).toString("base64").replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
const fromB64u = (s) => Buffer.from(String(s).replace(/-/g, "+").replace(/_/g, "/"), "base64");

function hmac(key, data) {
  return crypto.createHmac("sha256", key).update(data).digest();
}
// HKDF s jedním blokem výstupu (stačí pro délky do 32 B)
function hkdf(salt, ikm, info, len) {
  const prk = hmac(salt, ikm);
  return hmac(prk, Buffer.concat([info, Buffer.from([1])])).subarray(0, len);
}

/** Vygeneruje VAPID klíče: { publicKey (raw 65 B, base64url), privateKey (d, base64url) } */
function generateVapidKeys() {
  const { publicKey, privateKey } = crypto.generateKeyPairSync("ec", { namedCurve: "prime256v1" });
  const pub = publicKey.export({ format: "jwk" });
  const priv = privateKey.export({ format: "jwk" });
  const raw = Buffer.concat([Buffer.from([4]), fromB64u(pub.x), fromB64u(pub.y)]);
  return { publicKey: b64u(raw), privateKey: priv.d };
}

function vapidKeyObject(publicKey, privateKey) {
  const raw = fromB64u(publicKey);
  return crypto.createPrivateKey({
    key: { kty: "EC", crv: "P-256", d: privateKey, x: b64u(raw.subarray(1, 33)), y: b64u(raw.subarray(33, 65)) },
    format: "jwk",
  });
}

function vapidHeader(endpoint, vapid) {
  const aud = new URL(endpoint).origin;
  const header = b64u(JSON.stringify({ typ: "JWT", alg: "ES256" }));
  const payload = b64u(JSON.stringify({ aud, exp: Math.floor(Date.now() / 1000) + 12 * 3600, sub: vapid.subject }));
  const unsigned = `${header}.${payload}`;
  const sig = crypto.sign("sha256", Buffer.from(unsigned), { key: vapid.keyObject, dsaEncoding: "ieee-p1363" });
  return `vapid t=${unsigned}.${b64u(sig)}, k=${vapid.publicKey}`;
}

/** Zašifruje obsah pro odběratele (RFC 8291, aes128gcm, jeden záznam). */
function encrypt(payload, keys, salt, asEcdh) {
  const uaPublic = fromB64u(keys.p256dh);
  const authSecret = fromB64u(keys.auth);
  const ecdh = asEcdh || crypto.createECDH("prime256v1");
  if (!asEcdh) ecdh.generateKeys();
  const asPublic = ecdh.getPublicKey();
  const shared = ecdh.computeSecret(uaPublic);
  salt = salt || crypto.randomBytes(16);

  const keyInfo = Buffer.concat([Buffer.from("WebPush: info\0"), uaPublic, asPublic]);
  const ikm = hkdf(authSecret, shared, keyInfo, 32);
  const cek = hkdf(salt, ikm, Buffer.from("Content-Encoding: aes128gcm\0"), 16);
  const nonce = hkdf(salt, ikm, Buffer.from("Content-Encoding: nonce\0"), 12);

  const cipher = crypto.createCipheriv("aes-128-gcm", cek, nonce);
  const plain = Buffer.concat([Buffer.from(payload), Buffer.from([2])]); // 0x02 = poslední záznam
  const body = Buffer.concat([cipher.update(plain), cipher.final(), cipher.getAuthTag()]);

  const rs = Buffer.alloc(4);
  rs.writeUInt32BE(4096);
  return Buffer.concat([salt, rs, Buffer.from([asPublic.length]), asPublic, body]);
}

/** Odešle notifikaci. Vrací HTTP kód push služby (201 = OK, 404/410 = odběr neplatný). */
function send(subscription, payload, vapid, opts = {}) {
  return new Promise((resolve) => {
    let body;
    try {
      body = encrypt(JSON.stringify(payload), subscription.keys);
    } catch (e) {
      return resolve(400);
    }
    const u = new URL(subscription.endpoint);
    const req = https.request({
      method: "POST", hostname: u.hostname, port: u.port || 443, path: u.pathname + u.search,
      headers: {
        "Content-Type": "application/octet-stream",
        "Content-Encoding": "aes128gcm",
        "Content-Length": body.length,
        TTL: String(opts.ttl || 86400),
        Urgency: opts.urgency || "high",
        ...(opts.topic ? { Topic: opts.topic } : {}),
        Authorization: vapidHeader(subscription.endpoint, vapid),
      },
      timeout: 10000,
    }, (res) => {
      res.resume();
      res.on("end", () => resolve(res.statusCode));
    });
    req.on("error", () => resolve(0));
    req.on("timeout", () => { req.destroy(); resolve(0); });
    req.end(body);
  });
}

module.exports = { generateVapidKeys, vapidKeyObject, send, encrypt, b64u, fromB64u, hkdf };
