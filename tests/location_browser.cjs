'use strict';

// Offline browser checks only: every page request is fulfilled or aborted below.
// No production credentials, school API, actual geolocation, or Amap navigation.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { test: nodeTest, before, after } = require('node:test');
const { chromium } = require('/home/ubuntu/offer-cdk-integration.cy1fjW/node_modules/playwright-core');

const STATIC = path.resolve(__dirname, '../admin_static');
const START = { longitude: '119.205678', latitude: '26.064321', address: 'OFFLINE-ADDRESS-NOT-FOR-MAPS' };
const POSITION = { longitude: 116.397128, latitude: 39.916527, accuracy: 12 };
const CONVERTED = { longitude: 116.403372, latitude: 39.917931 };
const test = (name, run) => nodeTest(name, { timeout: 20000 }, run);
let browser;

before(async () => {
  browser = await chromium.launch({
    executablePath: '/snap/chromium/current/usr/lib/chromium-browser/chrome',
    headless: true,
    args: ['--no-sandbox', '--disable-dev-shm-usage', '--disable-background-networking'],
    env: {
      ...process.env,
      LD_LIBRARY_PATH: '/snap/chromium/current/usr/lib:/snap/chromium/current/usr/lib/aarch64-linux-gnu:/snap/gnome-46-2404/current/usr/lib:/snap/gnome-46-2404/current/usr/lib/aarch64-linux-gnu'
    }
  });
}, { timeout: 30000 });
after(async () => { if (browser) await browser.close(); }, { timeout: 10000 });

function deferred() {
  let resolve;
  const promise = new Promise((done) => { resolve = done; });
  return { promise, resolve };
}

async function fixture(t, { width = 390, holdSave = false, protocol = 'https:', hostname = 'admin.example.invalid', statusOverride = null, sessionUser = null } = {}) {
  const origin = `${protocol}//${hostname}`;
  const context = await browser.newContext({ viewport: { width, height: 844 }, serviceWorkers: 'block' });
  const requests = [];
  const unexpected = [];
  const errors = [];
  const saveStarted = deferred();
  const saveRelease = deferred();
  let config = {
    enabled: false,
    user: { username: 'OFFLINE-ACCOUNT-ONLY' },
    checkin: { coordinate_system: 'GCJ-02', confirmed: true, longitude: START.longitude,
      latitude: START.latitude, actual_location: START.address },
    notify: { type: 'none' }, skip_dates: [], vacation: { skip_ranges: [] }
  };
  const configResponse = () => ({ config, configured: { password: true, token: false },
    revision: 'OFFLINE-REVISION', validation: [] });
  const status = { today: '2026-09-19', timezone: 'Asia/Shanghai', enabled: false, paused: true,
    timer: { active: 'inactive', enabled: 'disabled', next_run: '' }, last_run: null,
    last_preflight: null, validation: [], history: [], busy: false, job: null, ...(statusOverride || {}) };
  const contentTypes = { 'index.html': 'text/html', 'admin.js': 'application/javascript',
    'location.js': 'application/javascript', 'admin.css': 'text/css' };

  await context.route('**/*', async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    if (url.origin !== origin || url.search || !url.pathname.startsWith('/fzu/')) {
      unexpected.push({ method: request.method(), url: request.url() });
      await route.abort();
      return;
    }
    const relative = url.pathname.slice('/fzu/'.length);
    if (!relative.startsWith('api/')) {
      const filename = relative || 'index.html';
      if (!Object.hasOwn(contentTypes, filename) || request.method() !== 'GET') {
        unexpected.push({ method: request.method(), url: request.url() });
        await route.abort();
        return;
      }
      await route.fulfill({ status: 200, contentType: `${contentTypes[filename]}; charset=utf-8`,
        headers: { 'Cache-Control': 'no-store', 'Referrer-Policy': 'no-referrer',
          'Permissions-Policy': 'geolocation=(self)',
          'Content-Security-Policy': "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; form-action 'self'; object-src 'none'" },
        body: fs.readFileSync(path.join(STATIC, filename)) });
      return;
    }
    const endpoint = relative.slice('api/'.length);
    const entry = { method: request.method(), endpoint,
      body: request.postData() === null ? null : request.postDataJSON() };
    requests.push(entry);
    let result;
    if (entry.method === 'GET' && endpoint === 'session') result = { authenticated: true, csrf: 'OFFLINE-CSRF', ...(sessionUser ? { user: sessionUser } : {}) };
    else if (entry.method === 'GET' && endpoint === 'users') result = { users: [{ id: 'admin', role: 'owner', created_at: '' }, { id: 'mate', role: 'member', created_at: '2026-09-29' }] };
    else if (entry.method === 'GET' && endpoint === 'config') result = configResponse();
    else if (entry.method === 'GET' && endpoint === 'status') result = status;
    else if (entry.method === 'POST' && endpoint === 'logout') result = { ok: true };
    else if (entry.method === 'PUT' && endpoint === 'config') {
      saveStarted.resolve();
      if (holdSave) await saveRelease.promise;
      config = { ...config, checkin: { ...entry.body.config.checkin } };
      result = { ok: true, paused: true, ...configResponse() };
    } else {
      unexpected.push(entry);
      await route.abort();
      return;
    }
    await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(result) });
  });
  await context.addInitScript(() => {
    const callbacks = [];
    window.__geo = {
      calls: [], watches: 0,
      success(index, coords) {
        callbacks[index].success({ coords: { ...coords, altitude: null, altitudeAccuracy: null,
          heading: null, speed: null }, timestamp: Date.now() });
      },
      fail(index, code) { callbacks[index].error({ code, message: 'OFFLINE-RAW-ERROR-DO-NOT-REFLECT' }); }
    };
    Object.defineProperty(navigator, 'geolocation', { configurable: true, value: {
      getCurrentPosition(success, error, options) {
        callbacks.push({ success, error });
        window.__geo.calls.push({ ...options });
      },
      watchPosition() { window.__geo.watches += 1; throw new Error('Continuous geolocation is forbidden in this test'); },
      clearWatch() {}
    } });
    window.__opens = [];
    window.__confirmations = [];
    window.__confirmAllow = true;
    window.confirm = (message) => { window.__confirmations.push(String(message)); return window.__confirmAllow; };
    window.open = (url, target, features) => {
      window.__opens.push({ url: String(url), target, features });
      return { opener: null, closed: false };
    };
  });
  const page = await context.newPage();
  page.setDefaultTimeout(5000);
  page.on('pageerror', (error) => errors.push(error.message));
  t.after(async () => {
    saveRelease.resolve();
    await context.close();
    assert.deepEqual(unexpected, [], 'all network requests must remain within the explicitly mocked routes');
    assert.deepEqual(errors, [], 'the location UI must not produce JavaScript errors');
  });
  await page.goto(`${origin}/fzu/`);
  await page.locator('#app-view:not([hidden])').waitFor();
  await page.locator('#locate-button').waitFor();
  return { page, requests, saveStarted: saveStarted.promise, releaseSave: saveRelease.resolve,
    writes: () => requests.filter((request) => request.method !== 'GET') };
}

async function values(page) {
  return page.evaluate(() => ({
    longitude: document.getElementById('longitude').value,
    latitude: document.getElementById('latitude').value,
    address: document.getElementById('actual-location').value,
    confirmed: document.getElementById('location-confirmed').checked
  }));
}

async function locate(page) {
  const index = await page.evaluate(() => window.__geo.calls.length);
  await page.locator('#locate-button').click();
  await page.waitForFunction((count) => window.__geo.calls.length > count, index);
  return index;
}

async function success(page, index, position = POSITION) {
  await page.evaluate(({ index, position }) => window.__geo.success(index, position), { index, position });
}

test('initial rendering never requests location, watches position, opens maps, or writes configuration', async (t) => {
  const { page, writes } = await fixture(t);
  assert.equal(await page.locator('#map-button').isVisible(), true);
  assert.deepEqual(await page.evaluate(() => ({ calls: window.__geo.calls, watches: window.__geo.watches,
    opens: window.__opens })), { calls: [], watches: 0, opens: [] });
  assert.deepEqual(await values(page), { ...START, confirmed: true });
  assert.deepEqual(writes(), []);
});

test('one explicit location click converts WGS84 to a GCJ-02 draft only, retaining address and requiring new confirmation', async (t) => {
  const { page, writes } = await fixture(t);
  const index = await locate(page);
  const options = await page.evaluate(() => window.__geo.calls[0]);
  assert.equal(options.enableHighAccuracy, true);
  assert.equal(options.maximumAge, 0);
  assert(options.timeout > 0 && options.timeout <= 15000);
  await success(page, index);
  const current = await values(page);
  assert(Math.abs(Number(current.longitude) - CONVERTED.longitude) < 0.000002);
  assert(Math.abs(Number(current.latitude) - CONVERTED.latitude) < 0.000002);
  assert.equal(current.address, START.address);
  assert.equal(current.confirmed, false);
  assert.equal(await page.locator('#coordinate-system').inputValue(), 'GCJ-02');
  assert.match(await page.locator('#location-status').innerText(), /GCJ-02/);
  assert.match(await page.locator('#location-status').innerText(), /尚未保存|未保存/);
  assert.match(await page.locator('#location-meta').innerText(), /12.*米.*采样时间.*北京时间/);
  assert.match(await page.locator('#draft-state').innerText(), /未保存/);
  assert.deepEqual(writes(), []);
  assert.deepEqual(await page.evaluate(() => window.__opens), []);
});

test('reported accuracy worse than 100 metres is visibly warned about and never auto-confirmed or saved', async (t) => {
  const { page, writes } = await fixture(t);
  await success(page, await locate(page), { ...POSITION, accuracy: 250 });
  const feedback = await page.locator('#location-status').innerText();
  assert.match(feedback, /250/);
  assert.match(feedback, /精度.*低|误差.*大|精度不足/);
  assert.equal((await values(page)).confirmed, false);
  assert.deepEqual(writes(), []);
});

test('permission denial and timeout retain original coordinates and do not reflect raw browser errors', async (t) => {
  const { page, writes } = await fixture(t);
  for (const code of [1, 3]) {
    const index = await locate(page);
    await page.evaluate(({ index, code }) => window.__geo.fail(index, code), { index, code });
    const current = await values(page);
    assert.equal(current.longitude, START.longitude);
    assert.equal(current.latitude, START.latitude);
    assert.equal(current.address, START.address);
    const feedback = await page.locator('#location-status').innerText();
    assert.match(feedback, code === 1 ? /权限|拒绝|允许/ : /超时/);
    assert.doesNotMatch(feedback, /OFFLINE-RAW-ERROR/);
  }
  assert.deepEqual(writes(), []);
});

test('manual GCJ-02 entries remain exact and are never converted or located automatically', async (t) => {
  const { page, writes } = await fixture(t);
  await page.locator('#longitude').fill('119.222222');
  await page.locator('#latitude').fill('26.088888');
  assert.deepEqual(await values(page), { longitude: '119.222222', latitude: '26.088888',
    address: START.address, confirmed: false });
  assert.deepEqual(await page.evaluate(() => window.__geo.calls), []);
  assert.deepEqual(writes(), []);
});

test('manual edits invalidate an outstanding location callback and old callbacks cannot overwrite a newer request', async (t) => {
  const { page, writes } = await fixture(t);
  const first = await locate(page);
  await page.locator('#longitude').fill('119.244444');
  await page.locator('#actual-location').fill('OFFLINE-MANUALLY-REVIEWED-ADDRESS');
  const edited = await values(page);
  const second = await locate(page);
  await success(page, first);
  assert.deepEqual(await values(page), edited);
  await success(page, second);
  assert(Math.abs(Number((await values(page)).longitude) - CONVERTED.longitude) < 0.000002);
  assert.equal((await values(page)).address, edited.address);
  assert.deepEqual(writes(), []);
});

test('a pasted post-login URL in the token field is reduced to its token value before saving', async (t) => {
  const { page, writes, saveStarted } = await fixture(t);
  await page.locator('#school-token').fill(
    '  https://yzsxg.fzu.edu.cn/livecloud/project/fzu/attn/index.action?token=OFFLINE-TOKEN-0123456789&x=1#/home\n');
  await page.locator('#save-button').click();
  await saveStarted;
  assert.equal(writes().length, 1);
  assert.equal(writes()[0].method, 'PUT');
  assert.equal(writes()[0].body.config.user.token, 'OFFLINE-TOKEN-0123456789');
  await page.waitForFunction(() => !document.getElementById('save-button').disabled);
});

test('a token fragment pasted without token= keeps only the value before trailing parameters', async (t) => {
  const { page, writes, saveStarted } = await fixture(t);
  await page.locator('#school-token').fill('=OFFLINE-FRAGMENT-TOKEN-0123456789&contextPath= ');
  await page.locator('#save-button').click();
  await saveStarted;
  assert.equal(writes().length, 1);
  assert.equal(writes()[0].body.config.user.token, 'OFFLINE-FRAGMENT-TOKEN-0123456789');
  await page.waitForFunction(() => !document.getElementById('save-button').disabled);
});

test('schedule times are validated in the page and sent as schedule.times; address edits keep the confirmation', async (t) => {
  const { page, writes, saveStarted } = await fixture(t);
  await page.locator('#schedule-times-input').fill('20:30 21:40');
  await page.locator('#save-button').click();
  assert.deepEqual(writes(), []);
  await page.locator('#schedule-times-input').fill('21:10, 22:00');
  await page.locator('#actual-location').fill('OFFLINE-ADDRESS-EDITED');
  assert.equal(await page.locator('#location-confirmed').isChecked(), true);
  await page.locator('#save-button').click();
  await saveStarted;
  assert.equal(writes().length, 1);
  assert.deepEqual(writes()[0].body.config.schedule.times, ['21:10', '22:00']);
  assert.equal(writes()[0].body.config.checkin.confirmed, true);
  assert.equal(writes()[0].body.config.checkin.actual_location, 'OFFLINE-ADDRESS-EDITED');
  await page.waitForFunction(() => !document.getElementById('save-button').disabled);
});

test('the control card states whether the timer is running and makes resume redundant when it is', async (t) => {
  const unauthorized = await fixture(t);
  await unauthorized.page.waitForFunction(() => document.getElementById('control-state').textContent.includes('未授权'));
  const paused = await fixture(t, { statusOverride: { enabled: true, paused: true } });
  await paused.page.waitForFunction(() => document.getElementById('control-state').textContent.includes('已暂停'));
  assert.equal(await paused.page.locator('[data-action="resume"]').textContent(), '恢复定时');
  assert.equal(await paused.page.locator('[data-action="resume"]').isEnabled(), true);
  assert.equal(await paused.page.locator('[data-action="pause"]').textContent(), '暂停定时');
  // Paused: only the pause button is filled as the current state; preflight/resume are plain.
  assert.equal(await paused.page.evaluate(() => document.querySelector('[data-action="pause"]').classList.contains('is-current')), true);
  assert.equal(await paused.page.evaluate(() => document.querySelector('[data-action="resume"]').classList.contains('is-current')), false);
  assert.equal(await paused.page.evaluate(() => document.querySelector('[data-action="preflight"]').classList.contains('is-current')), false);
  const running = await fixture(t, { statusOverride: { enabled: true, paused: false,
    timer: { active: 'active', enabled: 'enabled', next_run: '2026-09-19T21:35:00+08:00' } } });
  await running.page.waitForFunction(() => document.getElementById('control-state').textContent.includes('定时已启用'));
  assert.equal(await running.page.locator('[data-action="resume"]').textContent(), '定时已启用');
  assert.equal(await running.page.locator('[data-action="resume"]').isDisabled(), true);
  // Running: only the resume button is filled as the current state.
  assert.equal(await running.page.evaluate(() => document.querySelector('[data-action="resume"]').classList.contains('is-current')), true);
  assert.equal(await running.page.evaluate(() => document.querySelector('[data-action="pause"]').classList.contains('is-current')), false);
  assert.equal(await running.page.locator('[data-action="pause"]').isEnabled(), true);
  assert.equal(await running.page.locator('[data-action="preflight"]').isEnabled(), true);
  assert.deepEqual(running.writes(), []);
});

test('the token card links to the school login page safely and explains the steps', async (t) => {
  const { page, writes } = await fixture(t);
  const link = page.locator('#sso-link');
  const href = new URL(await link.getAttribute('href'));
  assert.equal(href.origin, 'https://sso.fzu.edu.cn');
  assert.equal(href.pathname, '/login');
  assert.equal(href.searchParams.get('service'), 'https://yzsxg.fzu.edu.cn/livecloud/project/fzu/attn/oauth2/callback.action');
  assert.equal(await link.getAttribute('target'), '_blank');
  assert.match(await link.getAttribute('rel'), /noopener/);
  assert.match(await link.getAttribute('rel'), /noreferrer/);
  assert.equal(await page.locator('.token-guide li').count(), 4);
  assert.deepEqual(writes(), []);
});

test('members see only their own account card while the owner also gets member management', async (t) => {
  const member = await fixture(t, { sessionUser: { id: 'mate', role: 'member' } });
  assert.equal(await member.page.locator('#members-card').isHidden(), true);
  assert.equal(await member.page.locator('#password-form').isVisible(), true);
  assert.equal(await member.page.locator('#current-user').textContent(), '当前登录：mate · 成员');
  assert.equal(await member.page.locator('#admin-username').count(), 1);
  assert.deepEqual(member.writes(), []);
  const owner = await fixture(t, { sessionUser: { id: 'admin', role: 'owner' } });
  await owner.page.locator('#members-card:not([hidden])').waitFor();
  await owner.page.waitForFunction(() => document.querySelectorAll('#members-list .member-row').length === 2);
  const rows = await owner.page.locator('#members-list .member-row').allTextContents();
  assert.match(rows[0], /admin管理员（你）/);
  assert.match(rows[1], /mate成员/);
  assert.equal(await owner.page.locator('#members-list .member-tools').count(), 1, 'only non-owner rows get reset/remove tools');
  assert.deepEqual(owner.writes(), []);
});

test('logout invalidates a pending location callback and leaves no location draft in the signed-out page', async (t) => {
  const { page, writes } = await fixture(t);
  const index = await locate(page);
  await page.locator('#logout-button').click();
  await page.locator('#login-view:not([hidden])').waitFor();
  await success(page, index);
  assert.deepEqual(await values(page), { longitude: '', latitude: '', address: '', confirmed: false });
  assert.equal(await page.locator('#app-view').isHidden(), true);
  assert.deepEqual(writes().map(({ method, endpoint }) => ({ method, endpoint })),
    [{ method: 'POST', endpoint: 'logout' }]);
});

test('saving is blocked during location and cancellation prevents late callbacks from rewriting a save in flight', async (t) => {
  const { page, writes, saveStarted, releaseSave } = await fixture(t, { holdSave: true });
  const index = await locate(page);
  assert.equal(await page.locator('#save-button').isDisabled(), true);
  await page.locator('#config-form').dispatchEvent('submit');
  assert.deepEqual(writes(), []);
  await page.locator('#cancel-locate-button').click();
  assert.equal(await page.locator('#save-button').isEnabled(), true);
  await page.locator('#save-button').click();
  await saveStarted;
  assert.equal(await page.locator('#locate-button').isDisabled(), true);
  const atSaveStart = await values(page);
  await success(page, index);
  assert.deepEqual(await values(page), atSaveStart);
  assert.equal(writes().length, 1);
  assert.equal(writes()[0].method, 'PUT');
  assert.equal(writes()[0].body.config.checkin.longitude, START.longitude);
  assert.equal(writes()[0].body.config.checkin.latitude, START.latitude);
  releaseSave();
  await page.waitForFunction(() => !document.getElementById('save-button').disabled);
  assert.deepEqual(await values(page), atSaveStart);
});

test('Amap opens only by confirmed click, uses draft coordinates and a fixed title, and carries no account, password or address', async (t) => {
  const { page, writes } = await fixture(t);
  await page.locator('#longitude').fill('119.222222');
  await page.locator('#latitude').fill('26.088888');
  await page.locator('#school-token').fill('OFFLINE-PRIVATE-TOKEN');
  await page.locator('#actual-location').fill('OFFLINE-PRIVATE-ADDRESS-A');
  await page.evaluate(() => { window.__confirmAllow = false; });
  await page.locator('#map-button').click();
  assert.deepEqual(await page.evaluate(() => window.__opens), []);
  await page.evaluate(() => { window.__confirmAllow = true; });
  await page.locator('#map-button').click();
  await page.locator('#actual-location').fill('OFFLINE-PRIVATE-ADDRESS-B');
  await page.locator('#school-token').fill('OFFLINE-OTHER-TOKEN');
  await page.locator('#map-button').click();
  const opens = await page.evaluate(() => window.__opens);
  assert.equal(opens.length, 2);
  assert.equal(opens[0].url, opens[1].url, 'changing private fields must not change the map URL or its fixed title');
  const url = new URL(opens[0].url);
  assert.equal(url.origin, 'https://uri.amap.com');
  assert.equal(url.pathname, '/marker');
  assert.equal(url.searchParams.get('position'), '119.222222,26.088888');
  assert.equal(url.searchParams.get('name'), '待核对的位置');
  assert.equal(url.searchParams.get('coordinate'), 'gaode');
  assert.equal(url.searchParams.get('callnative'), '0');
  assert.deepEqual([...url.searchParams.keys()].sort(), ['callnative', 'coordinate', 'name', 'position']);
  assert.doesNotMatch(decodeURIComponent(url.href), /OFFLINE-|ACCOUNT|PASSWORD|ADDRESS|username|password|token|actual_location/);
  for (const opened of opens) {
    assert.equal(opened.target, '_blank');
    const features = opened.features.split(',').map((value) => value.trim());
    assert(features.includes('noopener'));
    assert(features.includes('noreferrer'));
  }
  await page.locator('#longitude').fill('');
  await page.locator('#map-button').click();
  assert.equal(await page.evaluate(() => window.__opens.length), 2, 'a half-empty coordinate pair must not open a misleading marker');
  await page.locator('#latitude').fill('');
  await page.locator('#map-button').click();
  const picker = await page.evaluate(() => window.__opens.at(-1));
  assert.equal(picker.url, 'https://lbs.amap.com/tools/picker');
  assert.equal(picker.target, '_blank');
  assert.equal(picker.features, 'noopener,noreferrer');
  assert.match(await page.evaluate(() => window.__confirmations.at(-1)), /2\s*位/);
  assert.match((await page.evaluate(() => window.__confirmations)).join('\n'), /高德/);
  assert.deepEqual(await page.evaluate(() => window.__geo.calls), []);
  assert.deepEqual(writes(), []);
});

test('320px and 390px layouts remain within the viewport before and after long accuracy feedback', async (t) => {
  const { page } = await fixture(t, { width: 320 });
  for (const phase of ['initial', 'accuracy-warning']) {
    if (phase === 'accuracy-warning') await success(page, await locate(page), { ...POSITION, accuracy: 9999 });
    for (const width of [320, 390]) {
      await page.setViewportSize({ width, height: 844 });
      const bounds = await page.evaluate(() => ({ viewport: innerWidth,
        document: document.documentElement.scrollWidth, body: document.body.scrollWidth,
        buttons: ['locate-button', 'map-button'].map((id) => {
          const rectangle = document.getElementById(id).getBoundingClientRect();
          return { id, left: rectangle.left, right: rectangle.right };
        }) }));
      assert(bounds.document <= width + 1 && bounds.body <= width + 1, `${phase}: ${JSON.stringify(bounds)}`);
      for (const button of bounds.buttons) assert(button.left >= 0 && button.right <= width + 1,
        `${phase}: ${JSON.stringify(button)}`);
    }
  }
});

test('a non-HTTPS page refuses location even when the geolocation API is present', async (t) => {
  for (const hostname of ['admin.example.invalid', 'localhost']) {
    const { page, writes } = await fixture(t, { protocol: 'http:', hostname });
    if (hostname === 'localhost') assert.equal(await page.evaluate(() => window.isSecureContext), true);
    await page.locator('#locate-button').click();
    assert.deepEqual(await page.evaluate(() => window.__geo.calls), []);
    assert.match(await page.locator('#location-status').innerText(), /HTTPS|安全/);
    assert.equal((await values(page)).longitude, START.longitude);
    assert.deepEqual(writes(), []);
  }
});
