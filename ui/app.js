// 东南大学校园网 —— 前端逻辑
// 只负责渲染和调用 Rust 命令，所有认证逻辑都在 seu_net.py 里。

const { invoke } = window.__TAURI__.core;
const { listen } = window.__TAURI__.event;

const $ = (id) => document.getElementById(id);

const el = {
  dot: $('statusDot'),
  title: $('statusTitle'),
  sub: $('statusSub'),
  scene: $('mScene'),
  ip: $('mIp'),
  account: $('mAccount'),
  sceneButtons: $('sceneButtons'),
  btnLogin: $('btnLogin'),
  btnRefresh: $('btnRefresh'),
  btnSave: $('btnSave'),
  btnOpenDir: $('btnOpenDir'),
  log: $('logBox'),
  hint: $('saveHint'),
  ckAutoReconnect: $('ckAutoReconnect'),
  ckAutostart: $('ckAutostart'),
  autostartHint: $('autostartHint'),
};

let busy = false;
let sceneList = [];        // [{key,label,wifi,name,is_current}]

// ---------------------------------------------------------------- 状态

function setStatus(kind, title, sub) {
  el.dot.className = 'dot ' + kind;
  el.title.textContent = title;
  el.sub.textContent = sub || '';
}

function applyStatus(s) {
  if (s.config_error) {
    setStatus('bad', '配置文件有问题', String(s.config_error).slice(0, 70));
  } else if (s.connectivity) {
    setStatus('ok', '已联网', '外网可用');
  } else if (s.online) {
    setStatus('warn', '需要重新认证', '门户在线但外网不通');
  } else if (s.portal) {
    setStatus('bad', '未认证', '在校园网内，点下方按钮或场景');
  } else {
    setStatus('', '不在校园网', '连不上认证门户，可能在别的网络');
  }

  const cur = sceneList.find((x) => x.key === s.active_scene) ||
              sceneList.find((x) => x.is_current);
  el.scene.textContent = cur
    ? `${cur.label}（${cur.wifi}）`
    : (s.credentials ? '已配置' : '未配置');
  el.ip.textContent = s.ip || '—';
  el.account.textContent = s.uid || (s.credentials ? '已配置' : '未配置');

  el.btnLogin.querySelector('.label').textContent =
    s.credentials ? '立即认证' : '去填账号';
}

async function refresh() {
  if (busy) return;
  busy = true;
  try {
    const s = await invoke('get_status');
    applyStatus(s);
  } catch (err) {
    setStatus('bad', '检查失败', String(err).slice(0, 60));
  } finally {
    busy = false;
  }
  loadLog();
}

// ---------------------------------------------------------------- 场景切换

async function loadScenes() {
  try {
    const data = await invoke('get_scenes');
    sceneList = data.scenes || [];
    renderSceneButtons(data.active);
    return data;
  } catch (err) {
    console.error('读场景失败', err);
    return null;
  }
}

function renderSceneButtons(active) {
  el.sceneButtons.innerHTML = '';
  sceneList.forEach((scene) => {
    const btn = document.createElement('button');
    btn.className = 'scene-btn' + (scene.key === active ? ' current' : '');
    btn.innerHTML =
      `<span class="s-label">${escapeHtml(scene.label)}</span>` +
      `<span class="s-wifi">${escapeHtml(scene.wifi || '')}</span>`;
    btn.addEventListener('click', () => switchTo(scene.key));
    el.sceneButtons.appendChild(btn);
  });
}

// 点场景按钮 = 连对应 Wi-Fi + 用对应账号认证。
// 这是「一键切换」：用户不用自己先想办法连上那个 Wi-Fi。
async function switchTo(key) {
  if (busy) return;
  const scene = sceneList.find((x) => x.key === key);
  if (!scene) return;

  setBusy(true);
  setStatus('busy', `正在切到${scene.label}`, `连接 ${scene.wifi} 并认证…`);
  try {
    const result = await invoke('switch_scene', { key, connectWifi: true });
    if (result.ok) {
      setStatus('ok', `已连上${scene.label}`, scene.wifi);
      await loadScenes();
    } else {
      setStatus('bad', `${scene.label}没连上`, result.message || '');
    }
  } catch (err) {
    setStatus('bad', '切换失败', String(err).slice(0, 70));
  } finally {
    setBusy(false);
    loadLog();
    setTimeout(refresh, 800);
  }
}

function escapeHtml(text) {
  return String(text).replace(/[&<>"']/g, (c) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
  ));
}

// ---------------------------------------------------------------- 日志

async function loadLog() {
  try {
    const text = await invoke('read_log', { lines: 200 });
    el.log.textContent = text || '（还没有日志）';
    el.log.scrollTop = el.log.scrollHeight;
  } catch (err) {
    el.log.textContent = '读取日志失败：' + err;
  }
}

// ---------------------------------------------------------------- 动作

function setBusy(on) {
  busy = on;
  el.btnLogin.disabled = on;
  el.btnRefresh.disabled = on;
  el.btnSave.disabled = on;
  el.btnLogin.querySelector('.spinner').style.display = on ? 'block' : 'none';
  document.querySelectorAll('.scene-btn').forEach((b) => { b.disabled = on; });
}

const LOGIN_MESSAGES = {
  0: ['ok', '已联网', '外网已连通'],
  1: ['bad', '没连上', '网络还没起来'],
  2: ['bad', '账号或密码被拒绝', '检查账号、密码和运营商'],
  3: ['bad', '配置不完整', '去设置里补上'],
};

async function doLogin() {
  if (busy) return;
  const status = await invoke('get_status').catch(() => null);
  if (status && !status.credentials) {
    switchView('viewSettings');
    return;
  }
  setBusy(true);
  setStatus('busy', '正在认证', '提交中…');
  try {
    const result = await invoke('login_once');
    if (result.skipped) {
      setStatus('busy', '已有认证在进行', '稍后再看');
    } else {
      const [kind, title, sub] =
        LOGIN_MESSAGES[result.code] || ['bad', '认证未成功', ''];
      setStatus(kind, title, sub);
    }
  } catch (err) {
    setStatus('bad', '出错了', String(err).slice(0, 60));
  } finally {
    setBusy(false);
    loadLog();
    setTimeout(refresh, 800);
  }
}

// ---------------------------------------------------------------- 设置

async function loadConfig() {
  try {
    const cfg = await invoke('get_config');
    const editors = document.querySelectorAll('.scene-editor');

    // 场景 1（宿舍）
    if (editors[0]) {
      editors[0].querySelector('.in-account').value = cfg.account || '';
      const pw = editors[0].querySelector('.in-password');
      pw.value = '';
      pw.placeholder = cfg.has_password ? '留空 = 不修改' : '请输入密码';
      setCarrier(editors[0].querySelector('.in-carrier'), cfg.carrier || '');
    }
    // 场景 2（教室）
    if (editors[1]) {
      const wlan = (cfg.stations || []).find((s) =>
        (s.ssids || []).some((n) => String(n).toUpperCase().includes('SEU-WLAN')));
      const separate = wlan && wlan.account && wlan.account !== cfg.account;
      editors[1].querySelector('.in-account').value = separate ? wlan.account : '';
      const pw = editors[1].querySelector('.in-password');
      pw.value = '';
      pw.placeholder = (wlan && wlan.has_password) ? '留空 = 不修改' : '和宿舍一样就留空';
    }
  } catch (err) {
    el.hint.textContent = '读配置失败：' + err;
    el.hint.className = 'hint bad';
  }
}

function setCarrier(group, value) {
  if (!group) return;
  [...group.children].forEach((btn) => {
    btn.classList.toggle('on', btn.dataset.v === String(value || ''));
  });
}

function getCarrier(group) {
  const on = group && group.querySelector('button.on');
  return on ? on.dataset.v : '';
}

async function saveConfig() {
  if (busy) return;
  const editors = document.querySelectorAll('.scene-editor');
  const account = editors[0].querySelector('.in-account').value.trim();
  if (!account) {
    el.hint.textContent = '宿舍账号不能为空';
    el.hint.className = 'hint bad';
    return;
  }
  setBusy(true);
  el.hint.textContent = '';
  el.hint.className = 'hint';
  let saved = false;
  try {
    const payload = {
      account,
      password: editors[0].querySelector('.in-password').value,
      carrier: getCarrier(editors[0].querySelector('.in-carrier')),
      wlan_account: editors[1] ? editors[1].querySelector('.in-account').value.trim() : '',
      wlan_password: editors[1] ? editors[1].querySelector('.in-password').value : '',
    };
    const result = await invoke('save_config', { payload });
    if (result.ok) {
      saved = true;
      el.hint.textContent = '已保存';
      el.hint.className = 'hint ok';
      await loadConfig();
      await loadScenes();
    } else {
      el.hint.textContent = result.error || '保存失败';
      el.hint.className = 'hint bad';
    }
  } catch (err) {
    el.hint.textContent = String(err);
    el.hint.className = 'hint bad';
  } finally {
    setBusy(false);
    if (saved) refresh();
  }
}

// ---------------------------------------------------------------- 自启

async function loadAutostart() {
  try {
    el.ckAutostart.checked = await invoke('autostart_state');
  } catch (err) {
    el.autostartHint.textContent = '读取自启状态失败：' + err;
  }
}

el.ckAutostart.addEventListener('change', async () => {
  const want = el.ckAutostart.checked;
  el.autostartHint.textContent = want ? '正在开启…' : '正在关闭…';
  try {
    const actual = await invoke('set_autostart', { enabled: want });
    el.ckAutostart.checked = actual;
    el.autostartHint.textContent = actual
      ? '已开启，下次登录会自动打开并认证一次'
      : '已关闭';
  } catch (err) {
    el.ckAutostart.checked = !want;
    el.autostartHint.textContent = String(err);
  }
});

// 自动重连开关存 localStorage：这是界面行为，不需要进 config.json
const RECONNECT_KEY = 'seu.autoReconnect';
let autoReconnect = localStorage.getItem(RECONNECT_KEY) !== '0';
el.ckAutoReconnect.checked = autoReconnect;
el.ckAutoReconnect.addEventListener('change', () => {
  autoReconnect = el.ckAutoReconnect.checked;
  localStorage.setItem(RECONNECT_KEY, autoReconnect ? '1' : '0');
});

// ---------------------------------------------------------------- 轮询 + 自动重连
//
// 这是整个软件的核心：校园网会周期性把人踢下线，没人盯着时必须自己连回去。
// 窗口收起（隐藏）时也继续跑——掉线时人很可能正关着窗口。

let pollTimer = null;

async function pollOnce() {
  if (busy) return;
  busy = true;
  let s = null;
  try {
    s = await invoke('get_status');
    applyStatus(s);
  } catch {
    /* 单次失败不改界面 */
  } finally {
    busy = false;
  }
  loadLog();
  if (!s || !autoReconnect) return;
  if (!s.credentials || s.connectivity) return;
  if (!s.portal) return;          // 不在校园网，不折腾
  await autoReconnectNow(s);
}

// 掉线恢复：先试当前场景，不行再试另一个（换地方时靠这个自动切过去）
async function autoReconnectNow(s) {
  if (busy) return;
  const ordered = sceneList.length
    ? [...sceneList].sort((a, b) =>
        (b.key === s.active_scene) - (a.key === s.active_scene))
    : [];

  for (const scene of ordered) {
    setBusy(true);
    setStatus('busy', '掉线了，正在重连', `尝试${scene.label}…`);
    try {
      const r = await invoke('switch_scene', { key: scene.key, connectWifi: true });
      if (r.ok) {
        setStatus('ok', '已自动重连', `${scene.label}（${scene.wifi}）`);
        await loadScenes();
        return;
      }
    } catch {
      /* 试下一个 */
    } finally {
      setBusy(false);
    }
  }
  setStatus('bad', '重连失败', '两个场景都没连上，稍后再试');
}

function startPolling(interval) {
  if (pollTimer) return;
  pollOnce();
  pollTimer = setInterval(pollOnce, interval);
}

function stopPolling() {
  if (pollTimer) {
    clearInterval(pollTimer);
    pollTimer = null;
  }
}

document.addEventListener('visibilitychange', () => {
  stopPolling();
  // 窗口隐藏时放慢到 60 秒，但不停止——关着窗口掉线也得能自己连回来
  startPolling(document.hidden ? 60000 : 30000);
});

// ---------------------------------------------------------------- 视图切换

function switchView(id) {
  document.querySelectorAll('.panel').forEach((p) => {
    p.classList.toggle('hidden', p.id !== id);
  });
  document.querySelectorAll('.tab').forEach((t) => {
    t.classList.toggle('active', t.dataset.view === id);
  });
  if (id === 'viewSettings') loadConfig();
  else loadLog();
}

document.querySelectorAll('.tab').forEach((tab) => {
  tab.addEventListener('click', () => switchView(tab.dataset.view));
});

el.btnLogin.addEventListener('click', doLogin);
el.btnRefresh.addEventListener('click', refresh);
el.btnSave.addEventListener('click', saveConfig);
el.btnOpenDir.addEventListener('click', () => invoke('open_data_dir').catch(() => {}));

document.querySelectorAll('.in-carrier').forEach((group) => {
  group.addEventListener('click', (e) => {
    const btn = e.target.closest('button');
    if (!btn) return;
    [...group.children].forEach((b) => b.classList.remove('on'));
    btn.classList.add('on');
  });
});

listen('tray-login', () => doLogin());

// ---------------------------------------------------------------- 启动

loadScenes();
refresh();
loadConfig();
loadAutostart();

// 开机自启拉起时自动认证一次
(async () => {
  try {
    if (!(await invoke('launched_by_autostart'))) return;
    for (let i = 0; i < 20 && busy; i += 1) {
      await new Promise((r) => setTimeout(r, 200));
    }
    const s = await invoke('get_status').catch(() => null);
    if (s && s.connectivity) return;
    if (s && !s.credentials) return;
    await doLogin();
  } catch {
    /* 自启认证失败不影响正常使用 */
  }
})();

startPolling(30000);
