// 东南大学校园网 —— 前端逻辑
// 只负责渲染和调用 Rust 命令，所有认证逻辑都在 seu_net.py 里。

const { invoke } = window.__TAURI__.core;
const { listen } = window.__TAURI__.event;

const $ = (id) => document.getElementById(id);

const el = {
  dot: $('statusDot'),
  title: $('statusTitle'),
  sub: $('statusSub'),
  network: $('mNetwork'),
  ip: $('mIp'),
  account: $('mAccount'),
  btnLogin: $('btnLogin'),
  btnRefresh: $('btnRefresh'),
  btnSave: $('btnSave'),
  btnOpenDir: $('btnOpenDir'),
  log: $('logBox'),
  hint: $('saveHint'),
  inAccount: $('inAccount'),
  inPassword: $('inPassword'),
  inWlanAccount: $('inWlanAccount'),
  inWlanPassword: $('inWlanPassword'),
  ckWlan: $('ckWlan'),
  wlanFields: $('wlanFields'),
  carrierGroup: $('carrierGroup'),
};

let carrier = '';        // 当前选中的运营商
let busy = false;

// ---------------------------------------------------------------- 状态

function setStatus(kind, title, sub) {
  el.dot.className = 'dot ' + kind;
  el.title.textContent = title;
  el.sub.textContent = sub || '';
}

function applyStatus(s) {
  const autostart = s.autostart && s.autostart.running;
  if (s.connectivity) {
    setStatus('ok', '已联网', '外网可用，无需操作');
  } else if (s.online) {
    setStatus('warn', '需要重新认证', '门户显示在线，但外网不通');
  } else if (s.portal) {
    setStatus('bad', '未认证', '已连接校园网，点下方按钮认证');
  } else {
    setStatus('', '不在校园网', s.online_detail || '未发现认证门户');
  }

  el.network.textContent = s.ssid_readable ? s.ssid : '未识别（不影响认证）';
  el.ip.textContent = s.ip || '—';
  el.account.textContent = s.uid || (s.credentials ? '已配置' : '未配置');

  // 账号没配的时候，直接把主按钮变成引导
  if (!s.credentials) {
    el.btnLogin.querySelector('.label').textContent = '去填账号';
  } else {
    el.btnLogin.querySelector('.label').textContent = '立即认证';
  }
}

async function refresh() {
  if (busy) return;
  try {
    const s = await invoke('get_status');
    applyStatus(s);
  } catch (err) {
    setStatus('bad', '检查失败', String(err).slice(0, 60));
  }
  loadLog();
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
  if (on) setStatus('busy', '正在处理', '稍等一下…');
}

async function doLogin() {
  if (busy) return;
  const status = await invoke('get_status').catch(() => null);
  if (status && !status.credentials) {
    switchView('viewSettings');
    el.inAccount.focus();
    return;
  }
  setBusy(true);
  try {
    const result = await invoke('login_once');
    if (result.ok) {
      setStatus('ok', '认证完成', '外网已连通');
    } else {
      const code = result.code;
      const msg = code === 2 ? '账号或密码被拒绝' : (code === 1 ? '等待超时' : '认证未成功');
      setStatus('bad', msg, '详情见日志');
    }
  } catch (err) {
    setStatus('bad', '出错了', String(err).slice(0, 60));
  } finally {
    setBusy(false);
    loadLog();
    setTimeout(refresh, 1200);
  }
}

// ---------------------------------------------------------------- 设置

async function loadConfig() {
  try {
    const cfg = await invoke('get_config');
    el.inAccount.value = cfg.account || '';
    el.inPassword.value = '';
    el.inPassword.placeholder = cfg.has_password ? '留空 = 不修改' : '请输入密码';

    // 教室账号
    const wlan = (cfg.stations || []).find((st) =>
      (st.ssids || []).some((n) => String(n).toUpperCase().includes('SEU-WLAN')));
    const separate = wlan && wlan.account && wlan.account !== cfg.account;
    el.ckWlan.checked = !!separate;
    el.inWlanAccount.value = separate ? wlan.account : '';
    el.inWlanPassword.value = '';
    el.wlanFields.classList.toggle('hidden', !separate);

    // 运营商：只有恰好一条 SEU-ISP 时才能反推
    const isp = (cfg.stations || []).filter((st) =>
      (st.ssids || []).some((n) => String(n).toUpperCase().includes('SEU-ISP')));
    carrier = '';
    if (isp.length === 1) {
      const map = { '@cmcc': '1', '@dx': '2', '@lt': '3' };
      carrier = map[isp[0].account_suffix] || '';
    }
    setCarrier(carrier);
  } catch (err) {
    el.hint.textContent = '读配置失败：' + err;
    el.hint.className = 'hint bad';
  }
}

function setCarrier(value) {
  carrier = value;
  [...el.carrierGroup.children].forEach((btn) => {
    btn.classList.toggle('on', btn.dataset.v === value);
  });
}

async function saveConfig() {
  if (busy) return;
  const account = el.inAccount.value.trim();
  if (!account) {
    el.hint.textContent = '账号不能为空';
    el.hint.className = 'hint bad';
    el.inAccount.focus();
    return;
  }
  setBusy(true);
  el.hint.textContent = '';
  el.hint.className = 'hint';
  try {
    const payload = {
      account,
      password: el.inPassword.value,
      carrier,
      wlan_account: el.ckWlan.checked ? el.inWlanAccount.value.trim() : '',
      wlan_password: el.ckWlan.checked ? el.inWlanPassword.value : '',
    };
    const result = await invoke('save_config', { payload });
    if (result.ok) {
      el.hint.textContent = '已保存';
      el.hint.className = 'hint ok';
      await loadConfig();
      refresh();
    } else {
      el.hint.textContent = result.error || '保存失败';
      el.hint.className = 'hint bad';
    }
  } catch (err) {
    el.hint.textContent = String(err);
    el.hint.className = 'hint bad';
  } finally {
    setBusy(false);
  }
}

// ---------------------------------------------------------------- 视图切换

function switchView(id) {
  document.querySelectorAll('.panel').forEach((p) => {
    p.classList.toggle('hidden', p.id !== id);
  });
  document.querySelectorAll('.tab').forEach((t) => {
    t.classList.toggle('active', t.dataset.view === id);
  });
  if (id === 'viewSettings') {
    loadConfig();
  } else {
    loadLog();
  }
}

// ---------------------------------------------------------------- 事件绑定

document.querySelectorAll('.tab').forEach((tab) => {
  tab.addEventListener('click', () => switchView(tab.dataset.view));
});

el.btnLogin.addEventListener('click', doLogin);
el.btnRefresh.addEventListener('click', refresh);
el.btnSave.addEventListener('click', saveConfig);
el.btnOpenDir.addEventListener('click', () => invoke('open_data_dir').catch(() => {}));

el.carrierGroup.addEventListener('click', (e) => {
  const btn = e.target.closest('button');
  if (btn) setCarrier(btn.dataset.v);
});

el.ckWlan.addEventListener('change', () => {
  el.wlanFields.classList.toggle('hidden', !el.ckWlan.checked);
});

// 托盘里点「立即认证」
listen('tray-login', () => doLogin());

// ---------------------------------------------------------------- 启动

refresh();
loadConfig();
setInterval(refresh, 30000);
