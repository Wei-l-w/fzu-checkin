'use strict';

(() => {
  const $ = (id) => document.getElementById(id);
  const csrfHeader = 'X-CSRF-Token';
  const secretFields = [
    { path: 'user.token', key: 'token', input: 'school-token', clear: 'clear-token', saved: 'token-saved' },
    { path: 'notify.bark_url', key: 'bark_url', input: 'bark-url', clear: 'clear-bark', saved: 'bark-saved', channel: 'bark' },
    { path: 'notify.serverchan_key', key: 'serverchan_key', input: 'serverchan-key', clear: 'clear-serverchan', saved: 'serverchan-saved', channel: 'serverchan' },
    { path: 'notify.wecom_webhook', key: 'wecom_webhook', input: 'wecom-webhook', clear: 'clear-wecom', saved: 'wecom-saved', channel: 'wecom' }
  ];
  const labels = {
    confirmed: { title: '学校已确认签到', badge: '学校确认', tone: 'success', detail: '提交后已复核学校当天记录，确认签到成功。' },
    already: { title: '学校已确认签到', badge: '学校确认', tone: 'success', detail: '学校当天记录显示已签到；本次没有重复提交。' },
    ready: { title: '预检通过，尚未提交', badge: '仅只读预检', tone: 'info', detail: '已完成只读验证，没有提交签到请求。恢复定时需另行确认。' },
    pending: { title: '结果待确认', badge: '待确认', tone: 'warning', detail: '尚无充分成功证据。请到学校 App 核对，系统不会据此重复提交。' },
    failed: { title: '本次执行失败', badge: '失败', tone: 'danger', detail: '本次没有签到成功证据，请在学校 App 中核对并查看运行记录。' },
    auth_error: { title: '学校认证未通过', badge: '需要处理', tone: 'danger', detail: '请检查学校账号；如需验证码或人脸认证，请在学校官方 App 中处理。' },
    config_error: { title: '配置尚未就绪', badge: '需要配置', tone: 'warning', detail: '已保存配置缺失或无效，请完善后重新进行只读预检。' },
    paused: { title: '自动签到已暂停', badge: '未提交', tone: 'neutral', detail: '这条运行记录表示暂停，不是学校签到成功记录。' },
    skipped: { title: '按设置跳过签到', badge: '已跳过', tone: 'neutral', detail: '命中本人设置的离校或请假日期，本次没有提交签到。' },
    no_task: { title: '学校返回无需签到', badge: '今日无任务', tone: 'info', detail: '学校明确返回当天无需签到，不等同于完成了一次签到。' },
    outside_window: { title: '不在允许签到时段', badge: '未提交', tone: 'neutral', detail: '不在学校当天允许时段，本次没有提交签到。' },
    resumed: { title: '定时已恢复，尚未提交', badge: '已恢复计划', tone: 'info', detail: '恢复操作本身不会即时签到，只启动后续定时计划。' },
    busy: { title: '已有任务运行中', badge: '本次未执行', tone: 'neutral', detail: '已有实例正在运行，本次没有额外执行。' },
    notification_accepted: { title: '测试通知已获渠道受理', badge: '待核对送达', tone: 'info', detail: '渠道受理不等于手机已收到，请在设备上核对。此操作没有签到。' },
    notification_failed: { title: '测试通知未获渠道确认', badge: '通知失败', tone: 'danger', detail: '请检查通知配置及网络。此操作没有签到。' }
  };
  const unknownLabel = { title: '尚未验证', badge: '尚未验证', tone: 'neutral', detail: '没有可验证的学校签到结果，不能认定已签到。' };
  const actionNames = { preflight: '只读预检', resume: '恢复定时', pause: '暂停定时', 'notify-test': '测试通知' };
  const modeNames = { run: '定时运行', preflight: '只读预检', resume: '恢复定时', pause: '暂停定时', 'notify-test': '测试通知', config: '保存配置', 'config-save': '保存配置' };

  let authenticated = false;
  let csrf = '';
  let revision = null;
  let currentStatus = null;
  let configured = {};
  let dirty = false;
  let saving = false;
  let loggingOut = false;
  let actionRequesting = false;
  let statusRequesting = false;
  let pendingJob = null;
  let notifiedJob = null;
  let rangeCounter = 0;
  let sessionGeneration = 0;
  let locationRequestGeneration = 0;
  let locationEditVersion = 0;
  let activeLocationRequest = null;

  class ApiError extends Error {
    constructor(message, status, code) {
      super(message);
      this.status = status;
      this.code = code;
    }
  }

  function safeText(value, fallback = '') {
    return typeof value === 'string' ? value : fallback;
  }

  function showFeedback(message, tone = 'info') {
    const box = $('feedback');
    box.textContent = message;
    box.className = `notice notice-${tone}`;
    box.hidden = false;
  }

  async function api(path, options = {}) {
    const controller = new AbortController();
    const timeout = window.setTimeout(() => controller.abort(), 30000);
    const headers = { Accept: 'application/json' };
    if (options.body !== undefined) headers['Content-Type'] = 'application/json';
    if (options.method && options.method !== 'GET' && csrf) headers[csrfHeader] = csrf;
    try {
      const response = await fetch(new URL(`./api/${path}`, document.baseURI), {
        method: options.method || 'GET',
        headers,
        body: options.body === undefined ? undefined : JSON.stringify(options.body),
        credentials: 'same-origin',
        cache: 'no-store',
        redirect: 'error',
        signal: controller.signal
      });
      let data;
      try { data = await response.json(); } catch (_) {
        throw new ApiError('服务返回了无法识别的响应，请稍后刷新状态。', response.status, 'invalid_response');
      }
      if (!response.ok) {
        const error = data && typeof data.error === 'object' ? data.error : {};
        throw new ApiError(safeText(error.message, response.status === 401 ? '登录已失效，请重新登录。' : '操作未完成，请稍后重试。'), response.status, safeText(error.code));
      }
      return data;
    } catch (error) {
      if (error instanceof ApiError) throw error;
      throw new ApiError(error.name === 'AbortError' ? '请求超时。操作可能仍在服务端执行，请先刷新状态，不要重复点击。' : '暂时无法连接服务，请检查网络后刷新状态。', 0, 'network_error');
    } finally {
      window.clearTimeout(timeout);
    }
  }

  let currentUser = null;

  function showLogin(message = '') {
    sessionGeneration += 1;
    currentUser = null;
    $('current-user').hidden = true;
    $('current-user').replaceChildren();
    $('members-card').hidden = true;
    $('members-list').replaceChildren();
    $('password-form').reset();
    $('member-form').reset();
    invalidateLocationRequest(true);
    locationEditVersion += 1;
    authenticated = false;
    csrf = '';
    revision = null;
    currentStatus = null;
    configured = {};
    dirty = false;
    saving = false;
    pendingJob = null;
    notifiedJob = null;
    $('config-form').reset();
    $('login-form').reset();
    $('date-ranges').replaceChildren();
    $('config-fields').disabled = true;
    $('app-view').hidden = true;
    $('login-view').hidden = false;
    $('logout-button').hidden = true;
    $('private-label').hidden = false;
    $('job-banner').hidden = true;
    $('feedback').hidden = !message;
    if (message) showFeedback(message, 'info');
  }

  function handleError(error) {
    if (error.status === 401 && authenticated) {
      showLogin('会话已过期。未保存的敏感草稿已清除，请重新登录。');
      return;
    }
    showFeedback(error.message || '操作未完成，请稍后重试。', 'error');
  }

  function setDirty() {
    if (!authenticated || saving) return;
    dirty = true;
    updateControls();
  }

  function updateControls() {
    const busy = actionRequesting || Boolean(pendingJob && pendingJob.state === 'running') || Boolean(currentStatus && currentStatus.busy);
    const locating = activeLocationRequest !== null;
    $('save-button').disabled = !authenticated || revision === null || saving || busy || locating;
    $('save-button').textContent = saving ? '正在保存并暂停…' : '保存配置并暂停';
    $('save-button').title = locating ? '请等待定位结束或取消本次定位，再保存配置' : '';
    $('config-fields').disabled = !authenticated || revision === null || saving;
    $('locate-button').disabled = !authenticated || revision === null || saving || loggingOut || locating;
    $('locate-button').textContent = locating ? '正在获取位置…' : '手机获取当前位置';
    $('map-button').disabled = !authenticated || revision === null || saving || loggingOut || locating;
    $('cancel-locate-button').hidden = !locating;
    $('location-actions').setAttribute('aria-busy', String(locating));
    $('draft-state').textContent = dirty ? '有未保存的修改' : '已保存配置';
    $('draft-state').classList.toggle('is-dirty', dirty);
    const timerNow = currentStatus && currentStatus.timer ? currentStatus.timer : {};
    const pausedNow = !currentStatus || currentStatus.paused !== false;
    const authorizedNow = !!currentStatus && currentStatus.enabled === true;
    const runningNow = !pausedNow && authorizedNow && (timerNow.active === 'active' || timerNow.active === true) && (timerNow.enabled === 'enabled' || timerNow.enabled === true);
    const problemsNow = currentStatus && Array.isArray(currentStatus.validation) ? currentStatus.validation.filter((entry) => typeof entry === 'string').slice(0, 6) : [];
    const stateLine = $('control-state');
    stateLine.classList.remove('is-on', 'is-off');
    if (!currentStatus) stateLine.textContent = '当前状态：读取中…';
    else if (runningNow) {
      stateLine.textContent = `当前状态：定时已启用，下次运行 ${timerNow.next_run ? formatDateTime(timerNow.next_run) : '时间待确认'}。无需再点“恢复定时”。`;
      stateLine.classList.add('is-on');
    } else if (!authorizedNow) {
      stateLine.textContent = '当前状态：未授权自动签到。请在配置底部勾选“授权按计划执行自动签到”并保存。';
      stateLine.classList.add('is-off');
    } else if (pausedNow) {
      stateLine.textContent = problemsNow.length
        ? `当前状态：已暂停，不会自动签到。已保存配置还缺：${problemsNow.join('；')}。`
        : '当前状态：已暂停，不会自动签到。先“只读预检”，通过后点“恢复定时”。';
      stateLine.classList.add('is-off');
    } else {
      stateLine.textContent = '当前状态：定时未正常启用。请先点“暂停定时”，再重新预检并恢复。';
      stateLine.classList.add('is-off');
    }
    document.querySelectorAll('[data-action]').forEach((button) => {
      const action = button.dataset.action;
      const unavailable = !authenticated || revision === null || actionRequesting || saving;
      const invalid = action === 'resume' && problemsNow.length > 0;
      const redundant = action === 'resume' && runningNow;
      const noNotify = action === 'notify-test' && $('notify-type').value === 'none';
      button.disabled = unavailable || (action !== 'pause' && (busy || dirty || locating || invalid || noNotify || redundant));
      if (action === 'resume') button.textContent = redundant ? '定时已启用' : '恢复定时';
      // Segmented state indicator: fill only the button matching the current state.
      const currentOn = action === 'resume' && runningNow;
      const currentOff = action === 'pause' && !!currentStatus && !runningNow;
      button.classList.toggle('is-current', currentOn || currentOff);
      button.classList.toggle('is-current-on', currentOn);
      button.classList.toggle('is-current-off', currentOff);
      button.title = locating && action !== 'pause' ? '请等待定位结束或取消本次定位' : dirty && action !== 'pause' ? '请先保存修改，再执行此操作' : invalid ? '请先完善已保存配置并预检' : redundant ? '定时已在运行，无需恢复' : noNotify ? '请先配置并保存通知渠道' : '';
    });
    const pending = currentStatus && Array.isArray(currentStatus.validation) ? currentStatus.validation.filter((entry) => typeof entry === 'string').slice(0, 6) : [];
    $('control-help').textContent = locating ? '正在获取手机位置，仅用于更新本页草稿。定位结束或取消后才能保存、预检或恢复；不会自动签到。' : dirty
      ? '有未保存的修改：请先保存，再预检、恢复定时或测试通知。暂停定时仍可操作。'
      : pending.length ? '恢复定时暂不可用，已保存配置还缺：' + pending.join('；') + '。修正并保存后，先只读预检再恢复。'
      : '预检会连接学校服务进行查询，但不会提交签到。此页面没有即时签到按钮。';
    secretFields.forEach((field) => { $(field.input).disabled = $(field.clear).checked; });
  }

  function setNotificationView() {
    document.querySelectorAll('[data-notify]').forEach((section) => { section.hidden = section.dataset.notify !== $('notify-type').value; });
  }

  function showLocationStatus(message, tone = 'info', metadata = '', draftNote = '') {
    $('location-status').className = `notice notice-${tone} location-status`;
    $('location-status').hidden = false;
    $('location-message').textContent = message;
    $('location-meta').textContent = metadata;
    $('location-meta').hidden = !metadata;
    $('location-draft-note').textContent = draftNote;
    $('location-draft-note').hidden = !draftNote;
  }

  function invalidateLocationRequest(clearFeedback = false) {
    locationRequestGeneration += 1;
    if (activeLocationRequest && activeLocationRequest.timer !== null) {
      window.clearTimeout(activeLocationRequest.timer);
    }
    activeLocationRequest = null;
    $('cancel-locate-button').hidden = true;
    $('location-actions').setAttribute('aria-busy', 'false');
    if (clearFeedback) {
      $('location-status').hidden = true;
      $('location-message').textContent = '';
      $('location-meta').textContent = '';
      $('location-draft-note').textContent = '';
    }
  }

  function locationSnapshot() {
    return [$('longitude').value, $('latitude').value, $('actual-location').value];
  }

  function currentLocationRequest(request) {
    return activeLocationRequest === request && authenticated && !saving && !loggingOut
      && request.session === sessionGeneration && request.generation === locationRequestGeneration
      && request.editVersion === locationEditVersion
      && request.snapshot.every((value, index) => value === locationSnapshot()[index]);
  }

  function finishLocationRequest(request) {
    if (!currentLocationRequest(request)) {
      if (activeLocationRequest === request) {
        invalidateLocationRequest();
        showLocationStatus('位置草稿或登录状态已改变，旧定位结果已忽略，不会覆盖当前输入。', 'warning');
        updateControls();
      }
      return false;
    }
    invalidateLocationRequest();
    return true;
  }

  function addressEdited() {
    // Only the description changed; coordinates and the user's confirmation of them stay as saved.
    if (!authenticated || saving) return;
    showLocationStatus('真实地址已修改，坐标未变，位置确认保持不变。', 'info', '', '尚未保存；保存后生效，不会自动预检、恢复定时或签到。');
    setDirty();
  }

  function manualLocationEdited() {
    if (!authenticated || saving) return;
    const wasLocating = activeLocationRequest !== null;
    locationEditVersion += 1;
    invalidateLocationRequest();
    $('location-confirmed').checked = false;
    showLocationStatus(wasLocating
      ? '你已手动修改位置，旧定位结果将被忽略，不会覆盖草稿。请重新核对坐标与真实地址。'
      : '坐标或真实地址已修改，请重新核对位置并勾选本人确认。手填的 GCJ-02 坐标不会再做转换。',
    'info', '', '尚未保存；不会自动预检、恢复定时或签到。');
    setDirty();
  }

  function startLocationRequest() {
    if (!authenticated || revision === null || saving || $('locate-button').disabled) return;
    invalidateLocationRequest();
    if (window.location.protocol !== 'https:' || !window.isSecureContext) {
      showLocationStatus('当前页面不是受支持的 HTTPS 安全页面，无法请求手机定位。请用 iPhone Safari 打开本站 HTTPS 地址后重试，或手动填写已核对的 GCJ-02 坐标。', 'warning');
      updateControls();
      return;
    }
    if (!navigator.geolocation || typeof navigator.geolocation.getCurrentPosition !== 'function') {
      showLocationStatus('当前浏览器不支持定位。请用 iPhone Safari 打开此页面，或使用高德拾取器手动核对。现有坐标未改变。', 'warning');
      updateControls();
      return;
    }
    if (!window.FzuLocation || typeof window.FzuLocation.fromBrowserPosition !== 'function') {
      showLocationStatus('定位组件未正确加载，请刷新页面后重试。现有坐标未改变。', 'error');
      updateControls();
      return;
    }
    const request = {
      generation: locationRequestGeneration,
      session: sessionGeneration,
      editVersion: locationEditVersion,
      snapshot: locationSnapshot(),
      timer: null
    };
    activeLocationRequest = request;
    showLocationStatus('正在请求手机当前位置，请在浏览器提示中确认是否允许定位。结果只填入草稿，不会保存或签到。', 'info');
    updateControls();
    request.timer = window.setTimeout(() => {
      if (!finishLocationRequest(request)) return;
      showLocationStatus('定位等待已超时，本次结果将被忽略，现有坐标未改变。请检查 Safari 网站定位权限后重试，或手动核对。', 'warning');
      updateControls();
    }, 15000);
    try {
      navigator.geolocation.getCurrentPosition((position) => {
        if (!finishLocationRequest(request)) return;
        try {
          const result = window.FzuLocation.fromBrowserPosition(position);
          const longitude = result.longitude.toFixed(6);
          const latitude = result.latitude.toFixed(6);
          const accuracy = Math.ceil(result.accuracy);
          const metadata = `设备报告精度约 ${accuracy} 米 · 采样时间 ${formatDateTime(new Date(result.measuredAt).toISOString())}（北京时间）`;
          const lowAccuracy = result.accuracy > 100;
          $('longitude').value = longitude;
          $('latitude').value = latitude;
          $('location-confirmed').checked = false;
          locationEditVersion += 1;
          showLocationStatus(lowAccuracy
            ? `定位精度较低（约 ${accuracy} 米），请勿直接确认。结果已换算为 GCJ-02 并填入草稿；请在高德核对，必要时改善定位条件后重试。`
            : '已获取手机位置并换算为 GCJ-02，尚未保存。请通过高德核对点位，并填写真实地址后重新勾选确认。',
          lowAccuracy ? 'warning' : 'info', metadata, '尚未保存；未自动确认、授权、预检或签到。真实地址仍需本人填写。');
          setDirty();
        } catch (error) {
          showLocationStatus(safeText(error.message, '定位结果无法安全使用，请重试或手动核对。'), 'warning', '', '未自动保存；请核对页面中的现有坐标。');
        }
        updateControls();
      }, (error) => {
        if (!finishLocationRequest(request)) return;
        const message = error && error.code === 1
          ? '未获定位权限，现有坐标未改变。请在 iPhone 的网站设置及系统定位服务中允许 Safari 定位后重试。'
          : error && error.code === 3
            ? '定位超时，现有坐标未改变。可检查权限后重试，或使用高德拾取器手动核对。'
            : '暂时无法获取手机位置，现有坐标未改变。请检查系统定位服务与网络，或在信号较好的位置重试。';
        showLocationStatus(message, 'warning');
        updateControls();
      }, { enableHighAccuracy: true, timeout: 12000, maximumAge: 0 });
    } catch (_) {
      if (!finishLocationRequest(request)) return;
      showLocationStatus('浏览器阻止了定位请求，现有坐标未改变。请检查 HTTPS、Safari 网站定位权限及系统定位服务。', 'warning');
      updateControls();
    }
  }

  function openMapForDraft() {
    if (!authenticated || revision === null || saving || $('map-button').disabled) return;
    if ($('longitude').validity.badInput || $('latitude').validity.badInput) {
      showLocationStatus('经纬度输入不是有效数字。请填写完整的 GCJ-02 坐标，或将两项都留空后打开拾取器。', 'warning');
      return;
    }
    if (!window.FzuLocation || typeof window.FzuLocation.mapUrl !== 'function') {
      showLocationStatus('地图核对组件未正确加载，请刷新页面后重试。', 'error');
      return;
    }
    const longitude = $('longitude').value.trim();
    const latitude = $('latitude').value.trim();
    let url;
    try { url = window.FzuLocation.mapUrl(longitude, latitude); } catch (error) {
      showLocationStatus(safeText(error.message, '请填写完整、有效的 GCJ-02 经纬度，或将两项都留空后打开拾取器。'), 'warning');
      return;
    }
    const message = longitude === '' && latitude === ''
      ? '当前经纬度都留空，将在新页面打开高德官方坐标拾取器。\n\n游客显示可能只有 2 位小数，不能据此确认具体楼栋；可能需登录或认证后获取足够精度。\n\n本次不发送坐标，也不携带学校账号、密码或真实地址。请只选择并核对本人真实位置；不会保存或签到。确认打开？'
      : '将在新页面打开高德核对，并仅向高德发送当前草稿中的 GCJ-02 坐标。\n\n不携带学校账号、密码或真实地址。地图只能帮助核对点位，不能证明本人实时在校；不会保存或签到。确认打开？';
    if (!window.confirm(message)) return;
    window.open(url, '_blank', 'noopener,noreferrer');
  }

  function renderValidation(validation) {
    const list = $('validation-list');
    list.replaceChildren();
    const entries = Array.isArray(validation) ? validation : [];
    entries.slice(0, 30).forEach((entry) => {
      const item = document.createElement('li');
      item.textContent = typeof entry === 'string' ? entry : safeText(entry && entry.message, '配置需要检查');
      list.append(item);
    });
    $('validation-box').hidden = entries.length === 0;
  }

  function addRange(value = {}, focus = false) {
    rangeCounter += 1;
    const row = document.createElement('div');
    row.className = 'range-row';
    const name = document.createElement('label');
    name.className = 'range-name';
    name.textContent = '备注（可选）';
    const nameInput = document.createElement('input');
    nameInput.type = 'text';
    nameInput.className = 'range-name-input';
    nameInput.maxLength = 120;
    nameInput.autocomplete = 'off';
    nameInput.placeholder = '例如：本人请假';
    nameInput.value = safeText(value.name);
    nameInput.id = `range-name-${rangeCounter}`;
    name.htmlFor = nameInput.id;
    name.append(nameInput);
    const remove = document.createElement('button');
    remove.type = 'button';
    remove.className = 'button button-quiet range-remove';
    remove.textContent = '×';
    remove.setAttribute('aria-label', '移除此日期区间');
    remove.addEventListener('click', () => {
      row.remove();
      $('range-empty').hidden = $('date-ranges').children.length > 0;
      setDirty();
      $('add-range').focus();
    });
    const dates = document.createElement('div');
    dates.className = 'range-dates';
    ['start', 'end'].forEach((key) => {
      const label = document.createElement('label');
      label.textContent = key === 'start' ? '开始日期（含当天）' : '结束日期（含当天）';
      const input = document.createElement('input');
      input.type = 'date';
      input.className = `range-${key}`;
      input.value = safeText(value[key]);
      input.min = '2000-01-01';
      input.max = '2100-12-31';
      input.required = true;
      input.id = `range-${key}-${rangeCounter}`;
      label.htmlFor = input.id;
      label.append(input);
      dates.append(label);
    });
    row.append(name, remove, dates);
    $('date-ranges').append(row);
    $('range-empty').hidden = true;
    if (focus) nameInput.focus();
  }

  function fillConfig(data) {
    invalidateLocationRequest(true);
    locationEditVersion += 1;
    const config = data.config || {};
    const user = config.user || {};
    const checkin = config.checkin || {};
    const notify = config.notify || {};
    configured = data.configured || {};
    revision = data.revision === undefined ? null : data.revision;
    $('coordinate-system').value = 'GCJ-02';
    $('longitude').value = checkin.longitude === undefined || checkin.longitude === null ? '' : String(checkin.longitude);
    $('latitude').value = checkin.latitude === undefined || checkin.latitude === null ? '' : String(checkin.latitude);
    $('actual-location').value = safeText(checkin.actual_location);
    $('location-confirmed').checked = checkin.confirmed === true;
    $('service-enabled').checked = config.enabled === true;
    $('notify-type').value = ['none', 'bark', 'serverchan', 'wecom'].includes(notify.type) ? notify.type : 'none';
    $('skip-dates').value = Array.isArray(config.skip_dates) ? config.skip_dates.filter((date) => typeof date === 'string').join('\n') : '';
    const scheduleTimes = config.schedule && Array.isArray(config.schedule.times) ? config.schedule.times.filter((item) => typeof item === 'string') : [];
    $('schedule-times-input').value = scheduleTimes.join(' ');
    $('date-ranges').replaceChildren();
    const ranges = config.vacation && Array.isArray(config.vacation.skip_ranges) ? config.vacation.skip_ranges : [];
    ranges.forEach((range) => { if (range && typeof range === 'object') addRange(range); });
    $('range-empty').hidden = ranges.length > 0;
    secretFields.forEach((field) => {
      const exists = configured[field.key] === true;
      $(field.input).value = '';
      $(field.input).placeholder = exists ? '已保存；留空保留，不回显' : (field.channel ? '填写本人的通知地址或密钥' : '粘贴登录后地址栏的整段链接');
      $(field.clear).checked = false;
      $(field.saved).textContent = exists ? '已保存 · 不回显' : '未保存';
      $(field.saved).classList.toggle('is-saved', exists);
    });
    dirty = false;
    setNotificationView();
    renderValidation(data.validation);
    updateControls();
  }

  function validDate(value) {
    if (!/^\d{4}-\d{2}-\d{2}$/.test(value)) return false;
    const date = new Date(`${value}T00:00:00Z`);
    return Number.isFinite(date.getTime()) && date.toISOString().slice(0, 10) === value;
  }

  // Accept a pasted post-login URL and keep only its token query value; plain tokens pass through.
  function extractToken(value) {
    const raw = String(value || '').trim();
    const match = raw.match(/(?:^|[?&#])token=([^&#\s]+)/);
    // Without "token=", treat the paste as a token fragment: drop leading ?/= and trailing &params.
    let token = match ? match[1] : raw.replace(/^[?=]+/, '').split(/[&#]/)[0];
    try { token = decodeURIComponent(token); } catch (_) { /* keep as pasted */ }
    return token.trim();
  }

  const defaultScheduleTimes = ['21:35', '21:40', '21:50'];
  function parseScheduleTimes(text) {
    const items = String(text || '').split(/[\s,，、;；/]+/).filter(Boolean);
    if (items.length === 0) return defaultScheduleTimes.slice();
    if (items.length > 6) throw new Error('签到时间最多填写 6 个。');
    let previous = '';
    items.forEach((item) => {
      if (!/^(?:[01]\d|2[0-3]):[0-5]\d$/.test(item)) throw new Error('签到时间格式应为 HH:MM，例如 21:35；请检查「' + item.slice(0, 12) + '」。');
      if (item < '21:00' || item > '23:55') throw new Error('签到时间 ' + item + ' 超出允许范围 21:00–23:55。');
      if (item <= previous) throw new Error('签到时间需按先后顺序填写且不重复。');
      previous = item;
    });
    return items;
  }

  function buildConfigPayload() {
    const skipDates = $('skip-dates').value.split(/\r?\n/).map((line) => line.trim()).filter(Boolean);
    if (skipDates.some((date) => !validDate(date))) throw new Error('跳过日期格式有误，请一行填写一个有效的 YYYY-MM-DD 日期。');
    const ranges = Array.from($('date-ranges').children).map((row) => ({
      name: row.querySelector('.range-name-input').value.trim(),
      start: row.querySelector('.range-start').value,
      end: row.querySelector('.range-end').value
    }));
    if (ranges.some((range) => !validDate(range.start) || !validDate(range.end) || range.start > range.end)) {
      throw new Error('日期区间无效：请填写起止日期，且结束日期不能早于开始日期。');
    }
    const config = {
      enabled: $('service-enabled').checked,
      user: {},
      checkin: {
        coordinate_system: 'GCJ-02',
        confirmed: $('location-confirmed').checked,
        longitude: $('longitude').value.trim(),
        latitude: $('latitude').value.trim(),
        actual_location: $('actual-location').value.trim()
      },
      notify: { type: $('notify-type').value },
      skip_dates: [...new Set(skipDates)],
      vacation: { skip_ranges: ranges },
      schedule: { times: parseScheduleTimes($('schedule-times-input').value) }
    };
    const clearSecrets = [];
    secretFields.forEach((field) => {
      if ($(field.clear).checked || (field.channel && field.channel !== config.notify.type)) {
        clearSecrets.push(field.path);
        return;
      }
      const value = field.key === 'token' ? extractToken($(field.input).value) : $(field.input).value;
      if (value !== '') config[field.path.split('.')[0]][field.key] = value;
    });
    return { config, clear_secrets: clearSecrets, revision };
  }

  function formatDateTime(value) {
    if (!hasExplicitTimezone(value)) return '时间尚未确认';
    const date = new Date(value);
    if (!Number.isFinite(date.getTime())) return '时间未知';
    return new Intl.DateTimeFormat('zh-CN', {
      timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit',
      hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false
    }).format(date).replace(/\//g, '-');
  }

  function hasExplicitTimezone(value) {
    return typeof value === 'string' && /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:\d{2})$/.test(value);
  }

  function dateOf(result) {
    if (!result || typeof result !== 'object') return '';
    const windowDate = result.window && result.window.date;
    if (typeof windowDate === 'string' && validDate(windowDate)) return windowDate;
    if (typeof result.date === 'string' && validDate(result.date)) return result.date;
    const checked = hasExplicitTimezone(result.checked_at) ? new Date(result.checked_at) : null;
    if (!checked || !Number.isFinite(checked.getTime())) return '';
    const parts = new Intl.DateTimeFormat('en-CA', { timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit' }).formatToParts(checked);
    const part = (key) => parts.find((item) => item.type === key).value;
    return `${part('year')}-${part('month')}-${part('day')}`;
  }

  function resultTime(result) {
    const value = result && hasExplicitTimezone(result.checked_at) ? Date.parse(result.checked_at) : 0;
    return Number.isFinite(value) ? value : 0;
  }

  function resultLabel(result) {
    return result && Object.hasOwn(labels, result.status) ? labels[result.status] : unknownLabel;
  }

  function renderToday(status) {
    const allCandidates = [status.last_run, status.last_preflight].filter((result) => result && typeof result === 'object').sort((a, b) => resultTime(b) - resultTime(a));
    const changedAt = hasExplicitTimezone(status.config_changed_at) ? Date.parse(status.config_changed_at) : 0;
    const candidates = changedAt > 0 ? allCandidates.filter((result) => resultTime(result) >= changedAt) : allCandidates;
    const configUnverified = changedAt > 0 && candidates.length === 0;
    const today = safeText(status.today);
    const current = candidates.find((result) => dateOf(result) === today && validDate(today));
    const recent = current || candidates[0];
    const stale = !current && recent && dateOf(recent) && validDate(today);
    const label = current ? resultLabel(current) : unknownLabel;
    $('today-card').className = `card result-card tone-${label.tone}`;
    $('result-badge').textContent = configUnverified ? '配置已更新' : !current && recent ? '今日无有效记录' : label.badge;
    $('result-title').textContent = current ? label.title : '今日尚未验证';
    const problems = Array.isArray(status.validation) ? status.validation.filter((entry) => typeof entry === 'string').slice(0, 6) : [];
    $('result-description').textContent = configUnverified ? '配置已更新，请重新只读预检；旧记录不能代表当前配置。' : current ? (current.status === 'config_error' && problems.length ? '已保存配置还缺：' + problems.join('；') + '。修正并保存后重新只读预检。' : label.detail) : stale
      ? `最近数据属于 ${dateOf(recent)}（已过期），不能代表今天的签到情况。`
      : '还没有可核对的今日学校记录，不能认定已签到。';
    $('result-date').textContent = `数据日期：${recent ? dateOf(recent) || '未知，不能用于确认今日状态' : '暂无'}${stale ? ' · 已过期' : ''}`;
    $('result-source').textContent = `来源：${recent ? modeNames[recent.mode] || '服务运行记录' : '暂无'}`;
  }

  function renderHistory(entries) {
    const list = $('history-list');
    list.replaceChildren();
    const records = Array.isArray(entries) ? entries.filter((entry) => entry && typeof entry === 'object').slice(0, 30) : [];
    records.sort((a, b) => resultTime(b.result || b) - resultTime(a.result || a));
    records.forEach((entry) => {
      const result = entry.result && typeof entry.result === 'object' ? entry.result : entry;
      const label = resultLabel(result);
      const item = document.createElement('li');
      item.className = 'history-item';
      const dot = document.createElement('span');
      dot.className = `history-dot tone-${label.tone}`;
      dot.setAttribute('aria-hidden', 'true');
      const content = document.createElement('div');
      content.className = 'history-content';
      const top = document.createElement('div');
      top.className = 'history-top';
      const title = document.createElement('span');
      title.className = 'history-title';
      title.textContent = `${modeNames[result.mode] || modeNames[entry.action] || '服务记录'} · ${label.title}`;
      const time = document.createElement('time');
      time.className = 'history-time';
      time.textContent = formatDateTime(result.checked_at || entry.checked_at);
      if (typeof result.checked_at === 'string') time.dateTime = result.checked_at;
      top.append(title, time);
      const detail = document.createElement('p');
      detail.className = 'history-detail';
      detail.textContent = safeText(result.message, label.detail);
      content.append(top, detail);
      if (typeof result.code === 'string' || (result.evidence && typeof result.evidence.reason === 'string')) {
        const code = document.createElement('span');
        code.className = 'history-code';
        code.textContent = `诊断：${safeText(result.code, safeText(result.evidence && result.evidence.reason))}`;
        content.append(code);
      }
      item.append(dot, content);
      list.append(item);
    });
    $('history-empty').hidden = records.length > 0;
  }

  function renderJob(job) {
    const banner = $('job-banner');
    if (!job || typeof job !== 'object') {
      if (!pendingJob) banner.hidden = true;
      return;
    }
    pendingJob = job;
    if (job.state === 'running') {
      banner.hidden = false;
      banner.className = 'notice notice-info';
      banner.textContent = `${actionNames[job.action] || '请求的操作'}正在执行，页面会自动查询结果。请勿重复操作。`;
      return;
    }
    if (job.state === 'done' && job.id !== notifiedJob) {
      notifiedJob = job.id;
      const result = job.result || {};
      const label = resultLabel(result);
      const action = actionNames[job.action] || '操作';
      banner.hidden = false;
      banner.className = `notice notice-${label.tone === 'danger' ? 'error' : label.tone === 'warning' ? 'warning' : 'info'}`;
      banner.textContent = `${action}结果：${safeText(result.message, label.title)}。${job.action === 'resume' && result.status === 'resumed' ? '只启动后续定时，本次没有即时签到。' : job.action === 'notify-test' ? '请在手机上确认是否收到；本次没有签到。' : job.action === 'preflight' ? '本次为只读操作，没有提交签到。' : ''}`;
    }
  }

  function renderStatus(status) {
    currentStatus = status;
    $('today-display').textContent = `北京时间 ${safeText(status.today, '日期尚未确认')} · Asia/Shanghai`;
    $('refresh-state').textContent = '每 10 秒刷新状态';
    $('connection-dot').classList.remove('offline');
    renderToday(status);
    const timer = status.timer || {};
    const paused = status.paused !== false;
    const authorized = status.enabled === true;
    const active = timer.active === true || timer.active === 'active';
    const timerEnabled = timer.enabled === true || timer.enabled === 'enabled';
    let scheduleTitle = '定时状态尚未确认';
    let scheduleBadge = '尚未确认';
    let tone = 'neutral';
    if (paused) {
      scheduleTitle = '自动签到已暂停';
      scheduleBadge = '暂停中';
    } else if (!authorized) {
      scheduleTitle = '尚未授权自动签到';
      scheduleBadge = '未启用';
    } else if (active && timerEnabled) {
      scheduleTitle = '等待后续计划运行';
      scheduleBadge = '定时已启用';
      tone = 'info';
    } else {
      scheduleTitle = '定时未正常启用';
      scheduleBadge = '需检查';
      tone = 'warning';
    }
    $('schedule-title').textContent = scheduleTitle;
    $('schedule-badge').textContent = scheduleBadge;
    $('schedule-badge').className = `badge tone-${tone}`;
    $('next-run').textContent = paused || !authorized ? '暂停期间不执行' : timer.next_run ? formatDateTime(timer.next_run) : '尚未确认';
    $('enabled-state').textContent = authorized ? '已授权（仍受暂停与日期规则限制）' : '未授权';
    const scheduleList = Array.isArray(status.schedule_times) ? status.schedule_times.filter((item) => /^\d\d:\d\d$/.test(String(item))) : [];
    $('schedule-times').textContent = scheduleList.length ? scheduleList.join(' / ') : defaultScheduleTimes.join(' / ');
    const preflight = status.last_preflight;
    if (preflight && typeof preflight === 'object') {
      const day = dateOf(preflight);
      const expired = day && day !== status.today;
      $('preflight-summary').textContent = `${resultLabel(preflight).title} · ${formatDateTime(preflight.checked_at)}${expired ? ' · 已过期，不代表今日' : ''}`;
    } else $('preflight-summary').textContent = '尚无预检记录';
    renderValidation(status.validation);
    renderHistory(status.history);
    renderJob(status.job);
    updateControls();
  }

  async function refreshStatus() {
    if (!authenticated || document.hidden || statusRequesting) return;
    const generation = sessionGeneration;
    statusRequesting = true;
    try {
      const status = await api('status');
      if (authenticated && generation === sessionGeneration) renderStatus(status);
    } catch (error) {
      if (generation !== sessionGeneration) return;
      if (error.status === 401) handleError(error);
      else {
        $('refresh-state').textContent = '连接中断，显示上次数据';
        $('connection-dot').classList.add('offline');
      }
    } finally { statusRequesting = false; }
  }

  async function openApp(session) {
    csrf = safeText(session.csrf);
    if (!csrf) throw new ApiError('登录会话缺少安全校验信息，请刷新后重新登录。', 401, 'csrf_missing');
    authenticated = true;
    sessionGeneration += 1;
    const generation = sessionGeneration;
    const [config, status] = await Promise.all([api('config'), api('status')]);
    if (!authenticated || generation !== sessionGeneration) return;
    fillConfig(config);
    renderStatus(status);
    applyUser(session && session.user);
    $('login-form').reset();
    $('login-view').hidden = true;
    $('app-view').hidden = false;
    $('logout-button').hidden = false;
    $('private-label').hidden = true;
    $('feedback').hidden = true;
  }

  function applyUser(user) {
    const id = user && typeof user === 'object' ? safeText(user.id, '') : '';
    const role = user && typeof user === 'object' && user.role === 'owner' ? 'owner' : 'member';
    currentUser = id ? { id, role } : null;
    const label = $('current-user');
    label.replaceChildren();
    if (id) {
      const name = document.createElement('strong');
      name.textContent = id;
      label.append('当前登录：', name, role === 'owner' ? ' · 管理员' : ' · 成员');
    }
    label.hidden = !id;
    $('members-card').hidden = role !== 'owner';
    if (role === 'owner') loadMembers().catch(handleError);
  }

  function renderMembers(users) {
    const list = $('members-list');
    list.replaceChildren();
    const entries = Array.isArray(users) ? users.filter((item) => item && typeof item === 'object' && typeof item.id === 'string') : [];
    entries.forEach((item) => {
      const row = document.createElement('li');
      row.className = 'member-row';
      const name = document.createElement('span');
      name.className = 'member-name';
      name.textContent = item.id;
      const role = document.createElement('span');
      role.className = 'member-role';
      const self = currentUser && currentUser.id === item.id;
      role.textContent = (item.role === 'owner' ? '管理员' : '成员') + (self ? '（你）' : '') + (item.created_at ? ` · ${safeText(item.created_at)}` : '');
      name.append(role);
      row.append(name);
      if (item.role !== 'owner' && !self) {
        const tools = document.createElement('div');
        tools.className = 'member-tools';
        const input = document.createElement('input');
        input.type = 'password';
        input.autocomplete = 'new-password';
        input.maxLength = 1024;
        input.placeholder = '新密码（至少 10 个字符）';
        const reset = document.createElement('button');
        reset.type = 'button';
        reset.className = 'button button-small button-secondary';
        reset.textContent = '重置密码';
        reset.addEventListener('click', async () => {
          if (input.value.length < 10) { showFeedback('新密码至少 10 个字符。', 'error'); return; }
          reset.disabled = true;
          try {
            await api(`users/${encodeURIComponent(item.id)}/password`, { method: 'POST', body: { password: input.value } });
            input.value = '';
            showFeedback(`已重置 ${item.id} 的密码，其旧登录已失效。`, 'success');
          } catch (error) { handleError(error); } finally { reset.disabled = false; }
        });
        const remove = document.createElement('button');
        remove.type = 'button';
        remove.className = 'button button-small button-quiet button-danger';
        remove.textContent = '移除';
        remove.addEventListener('click', async () => {
          if (!window.confirm(`移除成员 ${item.id}？其定时会被暂停、登录停用，配置档案保留在服务器上。`)) return;
          remove.disabled = true;
          try {
            const result = await api(`users/${encodeURIComponent(item.id)}/remove`, { method: 'POST', body: { confirmed: true } });
            renderMembers(result.users);
            showFeedback(`已移除成员 ${item.id}。`, 'success');
          } catch (error) { handleError(error); remove.disabled = false; }
        });
        tools.append(input, reset, remove);
        row.append(tools);
      }
      list.append(row);
    });
  }

  async function loadMembers() {
    const result = await api('users');
    renderMembers(result.users);
  }

  $('copy-sso-link').addEventListener('click', async () => {
    const url = $('sso-link').href;
    try {
      if (!navigator.clipboard || !navigator.clipboard.writeText) throw new Error('clipboard');
      await navigator.clipboard.writeText(url);
      showFeedback('学校登录页网址已复制，请到浏览器地址栏粘贴打开。', 'success');
    } catch (_) {
      window.prompt('请长按或全选复制下面的网址：', url);
    }
  });

  $('password-form').addEventListener('submit', async (event) => {
    event.preventDefault();
    const current = $('current-password').value;
    const next = $('new-password').value;
    if (next.length < 10) { showFeedback('新密码至少 10 个字符。', 'error'); return; }
    if (next !== $('new-password-confirm').value) { showFeedback('两次输入的新密码不一致。', 'error'); return; }
    const button = $('password-button');
    button.disabled = true;
    try {
      await api('password', { method: 'PUT', body: { current_password: current, new_password: next } });
      $('password-form').reset();
      showFeedback('密码已修改，当前登录保持有效。', 'success');
    } catch (error) { handleError(error); } finally { button.disabled = false; }
  });

  $('member-form').addEventListener('submit', async (event) => {
    event.preventDefault();
    const username = $('member-username').value.trim();
    const password = $('member-password').value;
    if (!/^[a-z][a-z0-9_]{1,23}$/.test(username)) { showFeedback('用户名需为 2–24 位小写字母、数字或下划线，且以字母开头。', 'error'); return; }
    if (password.length < 10) { showFeedback('初始密码至少 10 个字符。', 'error'); return; }
    const button = $('member-button');
    button.disabled = true;
    try {
      const result = await api('users', { method: 'POST', body: { username, password } });
      $('member-form').reset();
      renderMembers(result.users);
      showFeedback(`已添加成员 ${username}，请把用户名和初始密码告诉本人。`, 'success');
    } catch (error) { handleError(error); } finally { button.disabled = false; }
  });

  $('login-form').addEventListener('submit', async (event) => {
    event.preventDefault();
    const button = $('login-button');
    button.disabled = true;
    button.textContent = '正在登录…';
    try {
      const session = await api('login', { method: 'POST', body: { username: $('admin-username').value.trim(), password: $('admin-password').value } });
      $('admin-password').value = '';
      $('admin-username').value = '';
      await openApp(session);
    } catch (error) {
      $('admin-password').value = '';
      handleError(error);
      if (authenticated) showLogin('已登录，但暂时无法加载管理数据。请刷新页面重试。');
    } finally {
      button.disabled = false;
      button.textContent = '登录管理页面';
    }
  });

  $('logout-button').addEventListener('click', async () => {
    if (dirty && !window.confirm('有未保存的修改。退出会清除当前草稿，确定退出吗？')) return;
    loggingOut = true;
    invalidateLocationRequest(true);
    updateControls();
    $('logout-button').disabled = true;
    try {
      await api('logout', { method: 'POST', body: {} });
      showLogin('已安全退出，页面中的账号和敏感草稿已清除。');
    } catch (error) { handleError(error); }
    finally { loggingOut = false; $('logout-button').disabled = false; updateControls(); }
  });

  // Narrow screens start with the run history collapsed; the list itself scrolls inside a fixed-height box.
  if (window.matchMedia && window.matchMedia('(max-width: 700px)').matches) $('records-details').removeAttribute('open');
  $('config-form').addEventListener('input', setDirty);
  $('config-form').addEventListener('change', setDirty);
  ['longitude', 'latitude'].forEach((id) => {
    $(id).addEventListener('input', manualLocationEdited);
    $(id).addEventListener('change', manualLocationEdited);
  });
  ['input', 'change'].forEach((type) => $('actual-location').addEventListener(type, addressEdited));
  $('locate-button').addEventListener('click', startLocationRequest);
  $('map-button').addEventListener('click', openMapForDraft);
  $('cancel-locate-button').addEventListener('click', () => {
    if (!activeLocationRequest) return;
    invalidateLocationRequest();
    showLocationStatus('已取消本次定位，随后返回的结果不会写入。现有坐标未改变；浏览器授权弹窗如仍显示，可自行关闭。', 'info');
    updateControls();
  });
  $('notify-type').addEventListener('change', setNotificationView);
  $('add-range').addEventListener('click', () => { addRange({}, true); setDirty(); });
  secretFields.forEach((field) => {
    $(field.clear).addEventListener('change', () => {
      if ($(field.clear).checked) $(field.input).value = '';
      updateControls();
    });
  });

  $('config-form').addEventListener('submit', async (event) => {
    event.preventDefault();
    if ($('save-button').disabled || !authenticated) return;
    let payload;
    try { payload = buildConfigPayload(); } catch (error) { showFeedback(error.message, 'error'); return; }
    if (currentStatus && currentStatus.paused === false && !window.confirm('保存配置会暂停自动签到。之后需先只读预检，再手动恢复定时。确认保存并暂停吗？')) return;
    const generation = sessionGeneration;
    invalidateLocationRequest(true);
    saving = true;
    updateControls();
    try {
      let result = await api('config', { method: 'PUT', body: payload });
      if (!authenticated || generation !== sessionGeneration) return;
      if (!result.config) result = await api('config');
      if (!authenticated || generation !== sessionGeneration) return;
      fillConfig(result);
      showFeedback('配置已保存，自动签到保持暂停。请先点击“只读预检”，核对通过后再确认“恢复定时”；本次没有签到。', 'success');
      await refreshStatus();
    } catch (error) {
      if (generation === sessionGeneration) handleError(error);
    } finally {
      payload = null;
      saving = false;
      updateControls();
    }
  });

  document.querySelectorAll('[data-action]').forEach((button) => {
    button.addEventListener('click', async () => {
      if (button.disabled) return;
      const action = button.dataset.action;
      if (action === 'resume' && !window.confirm('确认本人配置、位置和学校要求均已核对，并恢复自动签到？\n\n系统会先进行只读验证；通过后启用未来的定时计划，可能按计划提交真实签到。本次不会即时签到。离校或请假请先设置跳过日期。')) return;
      if (action === 'notify-test' && !window.confirm('向已保存的通知渠道真实发送一条测试通知？\n\n不会签到；渠道受理后仍需本人在手机上确认送达。')) return;
      const generation = sessionGeneration;
      actionRequesting = true;
      updateControls();
      try {
        const result = await api(`actions/${action}`, { method: 'POST', body: { confirmed: true } });
        if (!authenticated || generation !== sessionGeneration) return;
        if (result.job) {
          pendingJob = { ...result.job, action: result.job.action || action };
          renderJob(pendingJob);
          showFeedback(`${actionNames[action]}请求已接收，请等待下方结果。`, 'info');
        } else showFeedback(`${actionNames[action]}请求已处理，正在刷新实际状态。`, 'info');
        await refreshStatus();
      } catch (error) {
        if (generation === sessionGeneration) handleError(error);
      } finally { actionRequesting = false; updateControls(); }
    });
  });

  window.addEventListener('beforeunload', (event) => {
    if (!dirty) return;
    event.preventDefault();
    event.returnValue = '';
  });
  document.addEventListener('visibilitychange', () => { if (!document.hidden) refreshStatus(); });
  window.addEventListener('pageshow', async (event) => {
    if (!event.persisted || !authenticated) return;
    try {
      const session = await api('session');
      if (session.authenticated !== true) showLogin('请重新登录管理页面。');
      else { csrf = safeText(session.csrf); await refreshStatus(); }
    } catch (error) { handleError(error); }
  });
  window.setInterval(refreshStatus, 10000);

  (async () => {
    try {
      const session = await api('session');
      if (session.authenticated === true) await openApp(session);
      else showLogin();
    } catch (error) {
      if (error.status === 401) showLogin();
      else { showLogin(); handleError(error); }
    }
  })();
})();
