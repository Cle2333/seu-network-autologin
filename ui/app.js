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
  ckAutostart: $('ckAutostart'),
  autostartHint: $('autostartHint'),
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
  if (s.connectivity) {
    setStatus('ok', '已联网', '外网可用，无需操作');
  } else if (s.online) {
    setStatus('warn', '需要重新认证', '门户显示在线，但外网不通');
  } else if (s.portal) {
    setStatus('bad', '未认证', '已连接校园网，点下方按钮认证');
  } else {
    setStatus('', '不在校园网', s.online_detail || '未发现认证门户');
  }

  // 配置文件本身读不了的时候必须说出来，否则用户会一直重输正确的账号，
  // 却不知道是文件坏了。
  if (s.config_error) {
    setStatus('bad', '配置文件有问题', String(s.config_error).slice(0, 70));
  }

  el.network.textContent = s.ssid_readable ? s.ssid : '未识别（不影响认证）';
  el.ip.textContent = s.ip || '—';
  el.account.textContent = s.uid || (s.credentials ? '已配置' : '未配置');

  // 账号没配的时候，直接把主按钮变成引导
  el.btnLogin.querySelector('.label').textContent =
    s.credentials ? '立即认证' : '去填账号';
}

async function refresh() {
  if (busy) return;
  busy = true;              // 先占位，否则连点两次会并发两个 Python 进程
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

// 退出码 → 提示语。用查表而不是嵌套三元，漏掉的情况也有兜底。
const LOGIN_MESSAGES = {
  0: ['ok', '认证完成', '外网已连通'],
  1: ['bad', '等待超时', '网络还没起来，稍后会自动重试'],
  2: ['bad', '账号或密码被拒绝', '请检查账号、密码和运营商'],
  3: ['bad', '配置不完整', '去「设置」补上账号密码'],
  130: ['bad', '已取消', ''],
};

async function doLogin() {
  if (busy) return;
  busy = true;                 // 先占位再 await，否则连点会并发两次认证
  let status = null;
  try {
    status = await invoke('get_status');
  } catch {
    /* 拿不到状态就直接尝试认证 */
  }
  if (status && !status.credentials) {
    busy = false;
    switchView('viewSettings');
    el.inAccount.focus();
    return;
  }

  setBusy(true);
  try {
    const result = await invoke('login_once');
    if (result.skipped) {
      // 自启任务正在认证，这次只是排到队尾，别报「成功」
      setStatus('busy', '已有认证在进行', '等它跑完再看结果');
    } else {
      const [kind, title, sub] =
        LOGIN_MESSAGES[result.code] || ['bad', '认证未成功', '详情见日志'];
      setStatus(kind, title, sub);
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
  // 勾了「教室用另一个号」就必须填账号，否则后端会当成「和宿舍一样」，
  // 保存后勾选状态自己消失，用户以为存上了其实没有。
  if (el.ckWlan.checked && !el.inWlanAccount.value.trim()) {
    el.hint.textContent = '勾选了「教室用另一个号」，请填教室的账号';
    el.hint.className = 'hint bad';
    el.inWlanAccount.focus();
    return;
  }
  setBusy(true);
  el.hint.textContent = '';
  el.hint.className = 'hint';
  let saved = false;
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
      saved = true;
      el.hint.textContent = '已保存';
      el.hint.className = 'hint ok';
      await loadConfig();
    } else {
      el.hint.textContent = result.error || '保存失败';
      el.hint.className = 'hint bad';
    }
  } catch (err) {
    el.hint.textContent = String(err);
    el.hint.className = 'hint bad';
  } finally {
    setBusy(false);
    // refresh 开头有 if (busy) return，必须等 setBusy(false) 之后再调，
    // 否则状态卡会一直停在「正在处理」直到下一次轮询。
    if (saved) refresh();
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

// 开机自启：勾上/取消立刻生效，不需要再点保存
async function loadAutostart() {
  try {
    el.ckAutostart.checked = await invoke('autostart_state');
  } catch (err) {
    el.autostartHint.textContent = '读取自启状态失败：' + err;
    el.autostartHint.className = 'hint bad';
  }
}

el.ckAutostart.addEventListener('change', async () => {
  const want = el.ckAutostart.checked;
  el.autostartHint.textContent = want ? '正在开启…' : '正在关闭…';
  el.autostartHint.className = 'hint';
  try {
    const actual = await invoke('set_autostart', { enabled: want });
    el.ckAutostart.checked = actual;
    el.autostartHint.textContent = actual
      ? '已开启，下次登录会自动出现在菜单栏'
      : '已关闭';
    el.autostartHint.className = 'hint ok';
  } catch (err) {
    el.ckAutostart.checked = !want;     // 失败就还原勾选
    el.autostartHint.textContent = String(err);
    el.autostartHint.className = 'hint bad';
  }
});

// 托盘里点「立即认证」
listen('tray-login', () => doLogin());

// ---------------------------------------------------------------- 启动

refresh();
loadConfig();
loadAutostart();

// 由开机自启拉起时（plist / 注册表里带了 --autostart），自动认证一次。
// 手动打开程序不会走到这里——那样会变成「一开就联网」，不符合预期。
(async () => {
  try {
    if (!(await invoke('launched_by_autostart'))) return;
    // 等第一次 refresh 跑完（它占着 busy），否则 doLogin 会被 if (busy) return 直接跳过
    for (let i = 0; i < 20 && busy; i += 1) {
      await new Promise((r) => setTimeout(r, 200));
    }
    const s = await invoke('get_status').catch(() => null);
    if (s && s.connectivity) return;   // 已经能上网，不用再提交
    if (s && !s.credentials) return;   // 没配账号，认证也没意义
    await doLogin();
  } catch {
    /* 自启认证失败不该影响正常使用 */
  }
})();

// 定时刷新。窗口被隐藏（点关闭只是收起，托盘还在）时就别轮询了——
// 每次刷新都要起 Python 进程，看不见的时候纯属浪费。
let pollTimer = setInterval(refresh, 30000);
document.addEventListener('visibilitychange', () => {
  if (document.hidden) {
    clearInterval(pollTimer);
    pollTimer = null;
  } else if (!pollTimer) {
    refresh();
    pollTimer = setInterval(refresh, 30000);
  }
});
