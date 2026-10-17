const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const {TextEncoder} = require('node:util');

const html = fs.readFileSync(path.resolve(__dirname, '..', 'tools', 'hopegoo-secret-importer.html'), 'utf8');
const script = html.match(/<script>([\s\S]*?)<\/script>/)[1];
const context = {module: {exports: {}}, TextEncoder};
vm.runInNewContext(script, context);
const {buildSecret, buildSigninSecret, DataError, QUERY_URL, SIGNIN_URL} = context.module.exports;
let checks = 0;
function check(name, run) {
  run();
  checks += 1;
  process.stdout.write('PASS ' + name + '\n');
}
function har(url, token = 'dummy-session-for-offline-tests', body = 'dummy-encoded-body==') {
  return {log: {version: '1.2', entries: [{startedDateTime: '2026-10-11T07:00:00+08:00', request: {
    method: 'POST', url, headers: [
      {name: ':method', value: 'POST'},
      {name: 'Hopegoo-Token', value: token},
      {name: 'hopegoo-encryptkey', value: 'dummy-encryption-header'},
      {name: 'Cookie', value: 'sectoken=' + token},
      {name: 'cookie', value: 'locale=zh-hk'},
      {name: 'content-type', value: 'application/json; charset=utf-8'},
      {name: 'content-length', value: String(Buffer.byteLength(body))},
      {name: 'accept-encoding', value: 'br,gzip,deflate'},
      {name: 'proxy-authorization', value: 'dummy-proxy-header'}
    ], postData: {mimeType: 'application/json', text: body}
  } }] } };
}
const goodQuery = () => har(QUERY_URL);
const goodSignin = () => har(SIGNIN_URL);
function make(query = goodQuery(), signin = goodSignin(), slot = '01') {
  return buildSecret(JSON.stringify(query), JSON.stringify(signin), slot);
}
function rejects(query, signin = goodSignin(), slot = '01') {
  assert.throws(() => make(query, signin, slot), error => error instanceof DataError);
}
check('opaque request bytes survive secret serialization', () => {
  const body = 'AA+BB/CC==\n末尾';
  const built = make(har(QUERY_URL, undefined, body), har(SIGNIN_URL, undefined, body));
  const restored = JSON.parse(built.payload);
  assert.equal(restored.query.body, body);
  assert.equal(restored.signin.body, body);
  assert.equal(built.queryBytes, Buffer.byteLength(body));
  assert.equal(restored.schema_version, 1);
  assert.equal(restored.name, '账号01');
});
check('duplicate HTTP/2 cookie headers are retained in order', () => {
  const headers = JSON.parse(make().payload).query.headers;
  assert.equal(headers.cookie, 'sectoken=dummy-session-for-offline-tests; locale=zh-hk');
  assert.equal(headers['hopegoo-token'], 'dummy-session-for-offline-tests');
  assert.equal(headers['hopegoo-encryptkey'], 'dummy-encryption-header');
});
check('transport and proxy headers cannot become stored credentials', () => {
  const headers = JSON.parse(make().payload).query.headers;
  for (const key of [':method', 'content-length', 'accept-encoding', 'proxy-authorization']) {
    assert.equal(Object.hasOwn(headers, key), false);
  }
});
check('mispaired accounts fail before any secret is produced', () => {
  rejects(goodQuery(), har(SIGNIN_URL, 'dummy-other-account'));
});
check('wrong endpoint or domain is rejected', () => {
  rejects(har(QUERY_URL.replace('app.hopegoo.com', 'untrusted.invalid')));
  rejects(goodSignin(), goodQuery());
});
check('new capture domain is preserved in both request bundles', () => {
  for (const host of ['app.reshg.com', 'apptxy.hopegoo.com']) {
    const query = har(QUERY_URL.replace('app.hopegoo.com', host));
    const signin = har(SIGNIN_URL.replace('app.hopegoo.com', host));
    const restored = JSON.parse(make(query, signin).payload);
    assert.equal(restored.query.url, query.log.entries[0].request.url);
    assert.equal(restored.signin.url, signin.log.entries[0].request.url);
  }
});
check('one HAR with repeated same-session sign-ins produces a sign-in-only account', () => {
  const signin = har(SIGNIN_URL.replace('app.hopegoo.com', 'apptxy.hopegoo.com'));
  signin.log.entries.push(signin.log.entries[0]);
  const built = buildSigninSecret(JSON.stringify(signin), '02');
  const restored = JSON.parse(built.payload);
  assert.equal(restored.schema_version, 2);
  assert.equal(restored.name, '账号02');
  assert.equal(restored.query, undefined);
  assert.equal(restored.signin.url, signin.log.entries[0].request.url);
  assert.equal(restored.signin.body, 'dummy-encoded-body==');
  assert.equal(built.records, 2);
});
check('sign-in-only import rejects mixed sessions, mixed origins and lookalike hosts', () => {
  for (const alternate of [har(SIGNIN_URL, 'dummy-different-session'),
                           har(SIGNIN_URL.replace('app.hopegoo.com', 'apptxy.hopegoo.com'))]) {
    const signin = goodSignin();
    signin.log.entries.push(alternate.log.entries[0]);
    assert.throws(() => buildSigninSecret(JSON.stringify(signin), '02'), error => error instanceof DataError);
  }
  const lookalike = har(SIGNIN_URL.replace('app.hopegoo.com', 'apptxy.hopegoo.com.untrusted.invalid'));
  assert.throws(() => buildSigninSecret(JSON.stringify(lookalike), '02'), error => error instanceof DataError);
  assert.throws(() => buildSigninSecret(JSON.stringify(goodQuery()), '02'), error => error instanceof DataError);
});
check('sign-in-only import keeps slot, size and cookie/session validation', () => {
  assert.throws(() => buildSigninSecret(JSON.stringify(goodSignin()), '11'), error => error instanceof DataError);
  assert.throws(() => buildSigninSecret(JSON.stringify(har(SIGNIN_URL, undefined, 'A'.repeat(50000))), '02'),
                error => error instanceof DataError);
  const bad = goodSignin();
  bad.log.entries[0].request.headers.push({name: 'cookie', value: 'sectoken=wrong-session'});
  assert.throws(() => buildSigninSecret(JSON.stringify(bad), '02'), error => error instanceof DataError);
});
check('mixed domains and cookie/token conflicts fail before serialization', () => {
  rejects(har(QUERY_URL.replace('app.hopegoo.com', 'app.reshg.com')));
  const query = goodQuery();
  query.log.entries[0].request.headers.push({name: 'Cookie', value: 'cnUser=sectoken=different-session'});
  rejects(query);
});
check('ambiguous multiple records are rejected', () => {
  const query = goodQuery();
  query.log.entries.push(query.log.entries[0]);
  rejects(query);
});
check('truncated HAR request bytes are rejected', () => {
  const query = goodQuery();
  query.log.entries[0].request.headers.find(x => x.name === 'content-length').value = '999';
  rejects(query);
});
check('response-only or missing body exports are rejected', () => {
  const query = goodQuery();
  delete query.log.entries[0].request.postData;
  rejects(query);
});
check('header injection is rejected', () => {
  const query = goodQuery();
  query.log.entries[0].request.headers[1].value += '\r\nInjected: true';
  rejects(query);
});
check('unknown wrapper encoding cannot change the encrypted payload', () => {
  const query = goodQuery();
  query.log.entries[0].request.postData.encoding = 'base64';
  rejects(query);
});
check('compressed request representation is rejected', () => {
  const query = goodQuery();
  query.log.entries[0].request.headers.push({name:'content-encoding', value:'gzip'});
  rejects(query);
});
check('GitHub per-secret size limit is enforced', () => {
  rejects(har(QUERY_URL, undefined, 'A'.repeat(30000)), har(SIGNIN_URL, undefined, 'B'.repeat(30000)));
});
check('all ten account slots are supported and unknown slots rejected', () => {
  assert.equal(JSON.parse(make(goodQuery(), goodSignin(), '10').payload).name, '账号10');
  rejects(goodQuery(), goodSignin(), '11');
});
check('parse errors do not echo private input text', () => {
  assert.throws(() => buildSecret('dummy-private-content-not-json', JSON.stringify(goodSignin()), '01'), error => {
    return error instanceof DataError && !error.message.includes('dummy-private-content-not-json');
  });
});
check('browser page blocks network connections and loads no remote resources', () => {
  assert.match(html, /connect-src 'none'/);
  assert.doesNotMatch(html, /<(script|link|img)\b[^>]*(src|href)\s*=/i);
  assert.doesNotMatch(script, /\b(fetch|XMLHttpRequest|WebSocket|sendBeacon|localStorage|sessionStorage)\b/);
});

async function browserFlow() {
  const elements = Object.create(null);
  for (const id of ['slot','queryFile','signinFile','validate','copy','result','secretName','clear']) {
    elements[id] = {value: '', files: [], disabled: false, textContent: '', className: '', handlers: {},
      addEventListener(type, handler) { this.handlers[type] = handler; }};
  }
  elements.slot.value = '01';
  elements.queryFile.files = [{size: 1000, text: async () => JSON.stringify(goodQuery())}];
  elements.signinFile.files = [{size: 1000, text: async () => JSON.stringify(goodSignin())}];
  const copies = [];
  const browser = {TextEncoder, navigator: {clipboard: {writeText: async value => copies.push(value)}},
    document: {getElementById: id => elements[id]}};
  vm.runInNewContext(script, browser);
  await elements.validate.handlers.click();
  assert.equal(elements.copy.disabled, false);
  assert.match(elements.result.textContent, /校验通过/);
  assert.doesNotMatch(elements.result.textContent, /dummy-session|dummy-encryption|dummy-encoded/);
  await elements.copy.handlers.click();
  assert.equal(JSON.parse(copies[0]).name, '账号01');
  assert.match(elements.result.textContent, /HOPEGOO_ACCOUNT_01/);
  elements.clear.handlers.click();
  assert.equal(elements.copy.disabled, true);
  const before = copies.length;
  await elements.copy.handlers.click();
  assert.equal(copies.length, before);
  elements.queryFile.files = [];
  elements.slot.value = '02';
  elements.slot.handlers.change();
  await elements.validate.handlers.click();
  assert.equal(elements.copy.disabled, false);
  assert.match(elements.result.textContent, /仅签到配置/);
  await elements.copy.handlers.click();
  assert.equal(JSON.parse(copies[copies.length - 1]).schema_version, 2);
  assert.equal(JSON.parse(copies[copies.length - 1]).name, '账号02');
  assert.match(elements.result.textContent, /HOPEGOO_ACCOUNT_02/);
  checks += 1;
  process.stdout.write('PASS file selection, validation, copy and clear browser flow\n');
}
browserFlow().then(() => process.stdout.write('TOTAL ' + checks + ' checks passed; synthetic captures only\n'))
  .catch(error => { process.stderr.write(error.stack + '\n'); process.exitCode = 1; });
