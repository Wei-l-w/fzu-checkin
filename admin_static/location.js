'use strict';

// Coordinates stay in the browser until the user explicitly saves or opens AMap.
// Browser Geolocation is specified as WGS-84. Manual inputs are already GCJ-02.
(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.FzuLocation = api;
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {
  const PICKER_URL = 'https://lbs.amap.com/tools/picker';

  function coordinate(value, limit) {
    const text = typeof value === 'string' ? value.trim() : null;
    if (typeof value !== 'number' && (text === null || !/^[+-]?(?:\d+(?:\.\d*)?|\.\d+)$/.test(text))) {
      throw new Error('请填写有效的十进制经纬度；经度和纬度需要成对填写。');
    }
    const number = typeof value === 'number' ? value : Number(text);
    if (!Number.isFinite(number) || Math.abs(number) > limit) {
      throw new Error('经纬度超出有效范围，请核对后再打开地图。');
    }
    return number;
  }

  function wgs84ToGcj02(longitude, latitude) {
    const lng = coordinate(longitude, 180);
    const lat = coordinate(latitude, 90);
    // Outside this mainland bounding region the local offset formula is not used.
    // This is a conversion-domain guard, NOT a campus/attendance boundary.
    if (lng < 72.004 || lng > 137.8347 || lat < 0.8293 || lat > 55.8271) {
      return { longitude: lng, latitude: lat };
    }
    const a = 6378245.0;
    const ee = 0.00669342162296594323;
    const x = lng - 105;
    const y = lat - 35;
    const shared = (20 * Math.sin(6 * x * Math.PI) + 20 * Math.sin(2 * x * Math.PI)) * 2 / 3;
    let deltaLat = -100 + 2 * x + 3 * y + 0.2 * y * y + 0.1 * x * y + 0.2 * Math.sqrt(Math.abs(x));
    deltaLat += shared;
    deltaLat += (20 * Math.sin(y * Math.PI) + 40 * Math.sin(y / 3 * Math.PI)) * 2 / 3;
    deltaLat += (160 * Math.sin(y / 12 * Math.PI) + 320 * Math.sin(y * Math.PI / 30)) * 2 / 3;
    let deltaLng = 300 + x + 2 * y + 0.1 * x * x + 0.1 * x * y + 0.1 * Math.sqrt(Math.abs(x));
    deltaLng += shared;
    deltaLng += (20 * Math.sin(x * Math.PI) + 40 * Math.sin(x / 3 * Math.PI)) * 2 / 3;
    deltaLng += (150 * Math.sin(x / 12 * Math.PI) + 300 * Math.sin(x / 30 * Math.PI)) * 2 / 3;
    const radians = lat / 180 * Math.PI;
    let magic = Math.sin(radians);
    magic = 1 - ee * magic * magic;
    const squareRoot = Math.sqrt(magic);
    deltaLat = deltaLat * 180 / ((a * (1 - ee)) / (magic * squareRoot) * Math.PI);
    deltaLng = deltaLng * 180 / (a / squareRoot * Math.cos(radians) * Math.PI);
    return { longitude: lng + deltaLng, latitude: lat + deltaLat };
  }

  function fromBrowserPosition(position, now = Date.now()) {
    const coords = position && position.coords;
    if (!coords || typeof coords.longitude !== 'number' || typeof coords.latitude !== 'number'
        || typeof coords.accuracy !== 'number' || !Number.isFinite(coords.accuracy) || coords.accuracy < 0) {
      throw new Error('手机返回的定位信息不完整，请重试或从高德地图手动取点。');
    }
    const measuredAt = position.timestamp;
    if (typeof measuredAt !== 'number' || !Number.isFinite(measuredAt) || measuredAt <= 0
        || !Number.isFinite(now) || now - measuredAt > 120000 || measuredAt - now > 30000) {
      throw new Error('定位结果已过期或设备时间异常，请重新获取当前位置。');
    }
    const converted = wgs84ToGcj02(coords.longitude, coords.latitude);
    return { longitude: Number(converted.longitude.toFixed(6)),
      latitude: Number(converted.latitude.toFixed(6)), accuracy: coords.accuracy, measuredAt };
  }

  function mapUrl(longitude, latitude) {
    const empty = (value) => typeof value === 'string' && value.trim() === '';
    if (empty(longitude) && empty(latitude)) return PICKER_URL;
    const lng = coordinate(longitude, 180);
    const lat = coordinate(latitude, 90);
    const url = new URL('https://uri.amap.com/marker');
    url.searchParams.set('position', `${lng},${lat}`);
    url.searchParams.set('name', '待核对的位置');
    // Manual and converted draft coordinates are already GCJ-02. Never convert twice.
    url.searchParams.set('coordinate', 'gaode');
    url.searchParams.set('callnative', '0');
    return url.href;
  }

  return Object.freeze({ fromBrowserPosition, mapUrl, wgs84ToGcj02, PICKER_URL });
});
