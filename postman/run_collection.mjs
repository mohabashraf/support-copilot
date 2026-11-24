// Runs the Postman collection against a live API with Node only (no npm packages): real HTTP for
// every request and every pm.sendRequest, the collection's own test scripts, the pm.* subset the
// collection uses. Usage:
//   node postman/run_collection.mjs [folder-name-prefix ...]
// Keys and baseUrl come from postman/local.postman_environment.json (written by `copilot setup`)
// or from the env vars BASE_URL, HELPDESK_KEY, AGENT_KEY (or keys built into the collection).
// COLLECTION=path runs another copy (e.g. the reviewer's keyed one). Exit code 1 if any test fails.
import fs from "node:fs";
import path from "node:path";
import { isDeepStrictEqual } from "node:util";
import { randomUUID } from "node:crypto";

const here = path.dirname(new URL(import.meta.url).pathname);
const collection = JSON.parse(fs.readFileSync(process.env.COLLECTION || path.join(here, "support-copilot.postman_collection.json"), "utf8"));
const env = {};
const envFile = path.join(here, "local.postman_environment.json");
if (fs.existsSync(envFile)) for (const v of JSON.parse(fs.readFileSync(envFile, "utf8")).values) env[v.key] = v.value;
if (process.env.BASE_URL) env.baseUrl = process.env.BASE_URL;
if (process.env.HELPDESK_KEY) env.helpdeskKey = process.env.HELPDESK_KEY;
if (process.env.AGENT_KEY) env.agentKey = process.env.AGENT_KEY;
const colVars = Object.fromEntries((collection.variable || []).map((v) => [v.key, v.value]));
const getVar = (k) => (k in env && env[k] !== "" ? env[k] : colVars[k]);
const resolve = (s) =>
  String(s ?? "").replace(/\{\{\s*([^}]+?)\s*\}\}/g, (_, k) =>
    k === "$timestamp" ? String(Math.floor(Date.now() / 1000)) : k === "$guid" ? randomUUID()
      : k === "$randomInt" ? String(Math.floor(Math.random() * 1000)) : getVar(k) ?? "");

// --- a small chai-like expect: only the forms the collection uses ---
const show = (v) => { try { return JSON.stringify(v)?.slice(0, 200); } catch { return String(v); } };
function expect(actual, msg) {
  const fail = (m) => { throw new Error(msg ? `${msg} (${m})` : m); };
  const includes = (a, x) => typeof a === "string" ? a.includes(x)
    : Array.isArray(a) ? a.some((y) => isDeepStrictEqual(y, x))
    : a && typeof a === "object" ? Object.entries(x).every(([k, v]) => isDeepStrictEqual(a[k], v)) : false;
  const chain = (neg) => ({
    eql: (x) => { if (isDeepStrictEqual(actual, x) === neg) fail(`expected ${show(actual)} ${neg ? "not " : ""}to equal ${show(x)}`); },
    include: Object.assign((x) => { if (includes(actual, x) === neg) fail(`expected ${show(actual)} ${neg ? "not " : ""}to include ${show(x)}`); },
      { members: (xs) => { if (xs.every((x) => includes(actual, x)) === neg) fail(`expected ${show(actual)} to include members ${show(xs)}`); } }),
    match: (re) => { if (re.test(String(actual)) === neg) fail(`expected ${show(actual)} ${neg ? "not " : ""}to match ${re}`); },
    have: { property: (p) => { if ((actual != null && p in Object(actual)) === neg) fail(`expected property ${p}`); },
      status: (c) => { if ((actual === c) === neg) fail(`expected status ${c}, got ${actual}`); } },
    be: {
      get true() { if ((actual === true) === neg) fail(`expected ${show(actual)} to be true`); return true; },
      get false() { if ((actual === false) === neg) fail(`expected ${show(actual)} to be false`); return true; },
      above: (n) => { if ((actual > n) === neg) fail(`expected ${show(actual)} above ${n}`); },
      a: (t) => { const ok = t === "array" ? Array.isArray(actual) : t === "null" ? actual === null : typeof actual === t; if (ok === neg) fail(`expected ${show(actual)} to be a ${t}`); },
      an: (t) => chain(neg).be.a(t),
    },
  });
  const pos = chain(false);
  pos.not = chain(true);
  return { to: pos };
}

// --- HTTP with pending-callback tracking (scripts poll with sendRequest + setTimeout) ---
let pending = 0;
const track = (p) => { pending++; return p.finally(() => pending--); };
async function http(url, method, headers, body) {
  const res = await fetch(url, { method, headers, body: method === "GET" ? undefined : body });
  const text = await res.text();
  return { code: res.status, status: res.statusText, text: () => text, json: () => JSON.parse(text),
    headers: { get: (h) => res.headers.get(h) } };
}

const results = [];
async function runItem(item, trail, inheritedAuth) {
  const req = item.request;
  const auth = req.auth || inheritedAuth;
  const headers = {};
  for (const h of req.header || []) if (!h.disabled) headers[h.key] = resolve(h.value);
  if (auth?.type === "bearer") headers.Authorization = `Bearer ${resolve(auth.bearer.find((b) => b.key === "token")?.value)}`;
  const tests = [];
  const pmBase = {
    info: { requestName: item.name },
    variables: { get: getVar },
    collectionVariables: { get: (k) => colVars[k], set: (k, v) => { colVars[k] = v; } },
    environment: { get: (k) => env[k], set: (k, v) => { env[k] = v; } },
    expect,
    test: (name, fn) => { try { fn(); tests.push([name, null]); } catch (e) { tests.push([name, e.message]); } },
    sendRequest: (opt, cb) => {
      const o = typeof opt === "string" ? { url: opt, method: "GET" } : opt;
      const hs = Array.isArray(o.header) ? Object.fromEntries(o.header.map((h) => [h.key, h.value])) : o.header || {};
      const body = o.body?.raw;
      track(http(resolve(o.url), o.method || "GET", hs, body).then((r) => cb(null, r), (e) => cb(e)).catch((e) => tests.push(["callback", e.message])));
    },
  };
  const timer = (fn, ms) => { track(new Promise((r) => setTimeout(r, ms)).then(fn).catch((e) => tests.push(["timer", e.message]))); };
  const run = (listen, extra) => {
    for (const ev of item.event || []) if (ev.listen === listen)
      try { new Function("pm", "console", "setTimeout", ev.script.exec.join("\n"))({ ...pmBase, ...extra }, { log() {}, warn() {}, error() {} }, timer); }
      catch (e) { tests.push([`${listen} script error`, e.message]); }
  };
  run("prerequest", {});
  while (pending > 0) await new Promise((r) => setTimeout(r, 50)); // Postman waits for async pre-request work
  let response;
  try {
    const raw = req.body?.mode === "raw" ? resolve(req.body.raw) : undefined;
    const url = resolve(typeof req.url === "string" ? req.url : req.url.raw);
    response = await http(url, req.method, headers, raw);
  } catch (e) { tests.push(["request sent", e.message]); }
  if (response) {
    const r = { ...response, to: { have: { status: (c) => { if (response.code !== c) throw new Error(`expected status ${c}, got ${response.code}: ${response.text().slice(0, 150)}`); } } } };
    run("test", { response: r });
  }
  while (pending > 0) await new Promise((r) => setTimeout(r, 50));
  const failed = tests.filter(([, e]) => e);
  results.push({ name: [...trail, item.name].join(" / "), tests: tests.length, failed });
  console.log(`${failed.length ? "FAIL" : "ok  "} ${[...trail, item.name].join(" / ")}  (${tests.length - failed.length}/${tests.length})`);
  for (const [n, e] of failed) console.log(`       ✗ ${n}: ${e}`);
}

async function walk(items, trail, auth) {
  for (const it of items) {
    if (it.item) await walk(it.item, [...trail, it.name], it.auth || auth);
    else await runItem(it, trail, auth);
  }
}

const only = process.argv.slice(2);
const top = collection.item.filter((f) => !only.length || only.some((p) => f.name.startsWith(p)));
await walk(top, [], collection.auth);
const reqFailed = results.filter((r) => r.failed.length).length;
const t = results.reduce((a, r) => a + r.tests, 0), f = results.reduce((a, r) => a + r.failed.length, 0);
console.log(`\n${results.length} requests · ${t - f}/${t} tests passed · ${reqFailed} requests with failures`);
process.exit(f ? 1 : 0);
