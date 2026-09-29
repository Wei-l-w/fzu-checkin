'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const { fromBrowserPosition, mapUrl, wgs84ToGcj02, PICKER_URL } = require('../admin_static/location.js');
const now = 1700000000000;
const position = (changes = {}) => ({ timestamp: now, coords: { longitude: 116.397128, latitude: 39.916527, accuracy: 12, ...changes } });

test('WGS84 is locally converted once, preserving accuracy and timestamp', () => {
  const result = fromBrowserPosition(position(), now);
  assert(Math.abs(result.longitude - 116.403372) < 0.000002);
  assert(Math.abs(result.latitude - 39.917931) < 0.000002);
  assert.equal(result.accuracy, 12);
  assert.equal(result.measuredAt, now);
});
test('non-mainland domain has no arbitrary China offset', () => {
  assert.deepEqual(wgs84ToGcj02(-0.1276, 51.5072), { longitude: -0.1276, latitude: 51.5072 });
});
test('invalid or non-numeric browser data is not a location', () => {
  for (const changes of [{ longitude: NaN }, { latitude: Infinity }, { longitude: 181 }, { latitude: 91 },
    { longitude: '116.39' }, { latitude: null }, { accuracy: -1 }, { accuracy: NaN }, { accuracy: null }]) {
    assert.throws(() => fromBrowserPosition(position(changes), now));
  }
  assert.throws(() => fromBrowserPosition({}, now));
});
test('stale, missing, and future positions are rejected', () => {
  for (const stamp of [now - 120001, now + 30001, null, undefined, '1700000000000', Infinity]) {
    assert.throws(() => fromBrowserPosition({ ...position(), timestamp: stamp }, now));
  }
});
test('low accuracy is preserved for explicit UI warning, never fabricated as precise', () => {
  assert.equal(fromBrowserPosition(position({ accuracy: 2000 }), now).accuracy, 2000);
});
test('empty pair uses official picker without sending any coordinates', () => {
  assert.equal(mapUrl('', '   '), PICKER_URL);
});
test('manual GCJ02 is used unchanged with explicit gaode and browser mode', () => {
  const url = new URL(mapUrl('116.403372', '39.917931'));
  assert.equal(url.origin, 'https://uri.amap.com');
  assert.equal(url.pathname, '/marker');
  assert.equal(url.searchParams.get('position'), '116.403372,39.917931');
  assert.equal(url.searchParams.get('coordinate'), 'gaode');
  assert.equal(url.searchParams.get('callnative'), '0');
  assert.deepEqual([...url.searchParams.keys()].sort(), ['callnative', 'coordinate', 'name', 'position']);
});
test('partial, invalid, injected and out-of-range map inputs fail closed', () => {
  for (const coords of [['', '26.1'], ['119.1', ''], ['NaN', '26.1'], [true, 26],
    ['119&key=SECRET', '26.1'], ['181', '26.1'], ['119.1', '91'], [null, null], ['1e2', '26.1']]) {
    assert.throws(() => mapUrl(...coords));
  }
});
