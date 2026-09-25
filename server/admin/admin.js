/* VTranslate 管理后台：原生 JS，无外部依赖；CSP 严格模式（无内联脚本/样式/事件）。 */
'use strict';

const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const SKEY = 'vt_admin_session';

/* ---------- 工具 ---------- */
const esc = (v) => String(v ?? '').replace(/[&<>"']/g, (c) =>
  ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const yuan = (fen) => '¥' + ((fen || 0) / 100).toFixed(2);
const mins = (sec) => Math.round((sec || 0) / 60).toLocaleString();
const pad = (n) => String(n).padStart(2, '0');
function fmtTime(ts) {
  if (!ts) return '—';
  const d = new Date(ts * 1000);
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}
function ago(ts) {
  if (!ts) return '—';
  const s = Math.floor(Date.now() / 1000 - ts);
  if (s < 60) return `${s} 秒前`;
  if (s < 3600) return `${Math.floor(s / 60)} 分钟前`;
  if (s < 86400) return `${Math.floor(s / 3600)} 小时前`;
  return `${Math.floor(s / 86400)} 天前`;
}
function dur(sec) {
  sec = Math.max(0, Math.floor(sec));
  const h = Math.floor(sec / 3600), m = Math.floor(sec % 3600 / 60), s = sec % 60;
  return h ? `${h}:${pad(m)}:${pad(s)}` : `${m}:${pad(s)}`;
}
let toastTimer;
function toast(msg, bad = false) {
  const t = $('#toast');
  t.textContent = msg; t.classList.toggle('bad', bad); t.classList.add('show');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.classList.remove('show'), 2600);
}

/* ---------- API ---------- */
async function api(path, { method = 'GET', body, params } = {}) {
  const url = new URL('/api/admin' + path, location.origin);
  if (params) for (const [k, v] of Object.entries(params))
    if (v !== '' && v !== undefined && v !== null) url.searchParams.set(k, v);
  const headers = { 'X-Admin-Session': sessionStorage.getItem(SKEY) || '' };
  if (body !== undefined) headers['Content-Type'] = 'application/json';
  const r = await fetch(url, { method, headers, body: body === undefined ? undefined : JSON.stringify(body) });
  const d = await r.json().catch(() => ({}));
  if (r.status === 401 && path !== '/login') { logout(true); throw new Error('登录已过期'); }
  if (!r.ok || d.code !== 1) throw new Error(d.msg || d.detail || `HTTP ${r.status}`);
  return d;
}
async function act(fn, okMsg) {
  try { const r = await fn(); if (okMsg) toast(okMsg); return r; }
  catch (e) { toast(e.message, true); throw e; }
}

/* ---------- 登录 ---------- */
function showLogin() { $('#login').classList.remove('hidden'); $('#shell').classList.add('hidden'); $('#loginToken').focus(); }
function showShell() { $('#login').classList.add('hidden'); $('#shell').classList.remove('hidden'); go(location.hash.slice(1) || 'dash'); }
function logout(expired) {
  if (!expired) fetch('/api/admin/logout', { method: 'POST', headers: { 'X-Admin-Session': sessionStorage.getItem(SKEY) || '' } });
  sessionStorage.removeItem(SKEY);
  stopLive();
  if (expired) $('#loginErr').textContent = '登录已过期，请重新登录';
  showLogin();
}
$('#loginForm').addEventListener('submit', async (e) => {
  e.preventDefault();
  $('#loginErr').textContent = '';
  try {
    const d = await api('/login', { method: 'POST', body: { token: $('#loginToken').value } });
    sessionStorage.setItem(SKEY, d.session);
    $('#loginToken').value = '';
    showShell();
  } catch (err) { $('#loginErr').textContent = err.message; }
});
$('#btnLogout').onclick = () => logout(false);

/* ---------- 导航 ---------- */
const loaders = {};
function go(p) {
  if (!loaders[p]) p = 'dash';
  $$('#nav a').forEach((a) => a.classList.toggle('on', a.dataset.p === p));
  $$('.page').forEach((s) => s.classList.toggle('on', s.id === 'pg-' + p));
  if (location.hash.slice(1) !== p) history.replaceState(null, '', '#' + p);
  p === 'sessions' ? startLive() : stopLive();
  loaders[p]().catch(() => {});
}
$('#nav').addEventListener('click', (e) => { const a = e.target.closest('a[data-p]'); if (a) go(a.dataset.p); });
document.addEventListener('click', (e) => {
  const b = e.target.closest('[data-refresh]'); if (b) loaders[b.dataset.refresh]();
});

/* ---------- 分页 ---------- */
function pager(el, total, page, size, onGo) {
  const pages = Math.max(1, Math.ceil(total / size));
  el.innerHTML = `<span>共 ${total} 条 · 第 ${page}/${pages} 页</span>
    <button class="btn sm" data-pg="${page - 1}" ${page <= 1 ? 'disabled' : ''}>上一页</button>
    <button class="btn sm" data-pg="${page + 1}" ${page >= pages ? 'disabled' : ''}>下一页</button>`;
  el.onclick = (e) => { const b = e.target.closest('[data-pg]'); if (b && !b.disabled) onGo(+b.dataset.pg); };
}
function table(el, head, rows, emptyText = '暂无数据') {
  el.innerHTML = `<thead><tr>${head.map((h) => `<th class="${h[1] || ''}">${h[0]}</th>`).join('')}</tr></thead>
    <tbody>${rows.length ? rows.join('') : `<tr><td class="empty" colspan="${head.length}">${emptyText}</td></tr>`}</tbody>`;
}

/* ---------- 概览 ---------- */
function barChart(el, labels, values, fmt = (v) => v) {
  const W = 560, H = 170, L = 38, B = 20, T = 8;
  const max = Math.max(1, ...values);
  const nice = Math.pow(10, Math.floor(Math.log10(max)));
  const top = Math.ceil(max / nice) * nice;
  const bw = (W - L) / values.length;
  let g = '';
  for (let i = 0; i <= 3; i++) {
    const y = T + (H - T - B) * (1 - i / 3);
    g += `<line class="grid" x1="${L}" x2="${W}" y1="${y}" y2="${y}"/><text x="${L - 6}" y="${y + 3}" text-anchor="end">${esc(fmt(top * i / 3))}</text>`;
  }
  values.forEach((v, i) => {
    const h = (H - T - B) * v / top, x = L + i * bw + bw * 0.18;
    g += `<rect class="bar" x="${x}" y="${H - B - h}" width="${bw * 0.64}" height="${Math.max(h, 0.5)}" rx="3"><title>${esc(labels[i])}：${esc(fmt(v))}</title></rect>`;
    if (i % 2 === values.length % 2 || values.length <= 7)
      g += `<text x="${x + bw * 0.32}" y="${H - 5}" text-anchor="middle">${esc(labels[i].slice(5))}</text>`;
  });
  el.innerHTML = `<svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none">${g}</svg>`;
}
loaders.dash = async () => {
  const s = await api('/stats');
  const k = (label, v, sub, cls = '') => `<div class="kpi ${cls}"><div class="k">${label}</div><div class="v">${v}</div><div class="s">${sub}</div></div>`;
  $('#kpis').innerHTML = [
    k('今日收入', yuan(s.revenue_today_fen), `累计 ${yuan(s.revenue_total_fen)}`),
    k('今日翻译时长', mins(s.usage_today_seconds) + ' 分钟', `累计 ${mins(s.usage_total_seconds)} 分钟`, 'c2'),
    k('在线会话', s.sessions_live, `24h 活跃用户 ${s.active_24h}`, 'c4'),
    k('用户总数', s.users_total.toLocaleString(), `今日新增 ${s.users_today} · 注册 ${s.users_accounts}`, 'c3'),
    k('订阅中', s.subs_active, `已付订单 ${s.orders_paid} · 待付 ${s.orders_pending}`),
    k('余额负债', yuan(s.balance_liability_fen), '全部用户未消费余额', 'c2'),
  ].join('');
  const L = s.series.labels;
  barChart($('#chRevenue'), L, s.series.revenue_fen.map((v) => v / 100), (v) => (+v).toFixed(v >= 10 ? 0 : 1));
  $('#chUsage').classList.add('teal');
  barChart($('#chUsage'), L, s.series.usage_seconds.map((v) => Math.round(v / 60)), (v) => Math.round(v));
  $('#chUsers').classList.add('amber');
  barChart($('#chUsers'), L, s.series.new_users, (v) => Math.round(v));
  const y = s.system, flag = (ok, t, f) => `<span class="tag ${ok ? 'ok' : 'bad'}">${ok ? t : f}</span>`;
  $('#sysInfo').innerHTML = [
    ['翻译模型', `<span class="mono">${esc(y.model)}</span>`],
    ['上游', y.mock_upstream ? '<span class="tag warn">MOCK 调试</span>' : flag(y.dashscope_key, '已配置 Key', '缺少 Key')],
    ['支付', flag(y.zpay, '已配置', '未配置')],
    ['反向代理', y.trust_proxy ? '<span class="tag teal">信任 XFF</span>' : '<span class="tag">直连</span>'],
    ['热词 · 中文', `${y.terms_zh} 条`], ['热词 · 英文', `${y.terms_en} 条`],
  ].map(([a, b]) => `<div><span>${a}</span><span>${b}</span></div>`).join('');
  $('#liveBadge').textContent = s.sessions_live;
};

/* ---------- 用户 ---------- */
const uState = { page: 1, size: 20 };
function entTags(u) {
  const now = Date.now() / 1000, t = [];
  if (u.banned) t.push('<span class="tag bad">封禁</span>');
  if (u.sub_expire_at > now) t.push(`<span class="tag teal">订阅至 ${fmtTime(u.sub_expire_at).slice(0, 10)}</span>`);
  if (u.trial_seconds > 0 && u.trial_expire_at > now) t.push(`<span class="tag ok">试用 ${mins(u.trial_seconds)}m</span>`);
  return t.join(' ') || '<span class="muted">—</span>';
}
loaders.users = async (page) => {
  if (page) uState.page = page;
  const d = await api('/users', { params: { q: $('#uQ').value.trim(), kind: $('#uKind').value, page: uState.page, size: uState.size } });
  table($('#uTbl'), [['ID'], ['用户'], ['余额', 'num'], ['时长卡', 'num'], ['权益'], ['最近活跃'], ['注册时间'], ['', 'acts']],
    d.items.map((u) => `<tr>
      <td class="mono">${u.id}</td>
      <td>${u.username ? esc(u.username) : `<span class="muted">游客 · ${esc((u.device_id || '').slice(0, 8))}</span>`}${u.note ? ` <span class="tag">${esc(u.note)}</span>` : ''}</td>
      <td class="num">${yuan(u.money_cents)}</td><td class="num">${mins(u.credit_seconds)}m</td>
      <td>${entTags(u)}</td><td>${ago(u.last_active_at)}</td><td>${fmtTime(u.created_at)}</td>
      <td class="acts"><button class="btn sm" data-user="${u.id}">管理</button></td></tr>`));
  pager($('#uPager'), d.total, uState.page, uState.size, loaders.users);
};
$('#uSearch').onclick = () => loaders.users(1);
$('#uQ').addEventListener('keydown', (e) => e.key === 'Enter' && loaders.users(1));
$('#uKind').onchange = () => loaders.users(1);
document.addEventListener('click', (e) => { const b = e.target.closest('[data-user]'); if (b) openUser(+b.dataset.user); });

/* ---------- 用户详情抽屉 ---------- */
let curUser = 0;
function closeDrawer() { $('#drawer').classList.add('hidden'); $('#drawerMask').classList.add('hidden'); curUser = 0; }
$('#dClose').onclick = closeDrawer;
$('#drawerMask').onclick = closeDrawer;
document.addEventListener('keydown', (e) => e.key === 'Escape' && closeDrawer());
async function openUser(uid) {
  curUser = uid;
  const d = await act(() => api(`/users/${uid}`));
  const u = d.user, e = d.entitlement;
  $('#dTitle').textContent = u.username ? u.username : `游客 #${u.id}`;
  $('#dBody').innerHTML = `
    <dl class="dl">
      <dt>用户 ID</dt><dd class="mono">${u.id}</dd>
      <dt>设备码</dt><dd class="mono">${esc(u.device_id || '—')}</dd>
      <dt>推广码</dt><dd class="mono">${esc(u.ref_code)} · 邀请 ${d.referral.invited} 人 · 返利 ${yuan(d.referral.earned_fen)}</dd>
      <dt>余额</dt><dd>${yuan(e.money_cents)}（≈${e.money_minutes} 分钟）</dd>
      <dt>时长卡</dt><dd>${e.credit_minutes} 分钟</dd>
      <dt>免费额度</dt><dd>${mins(e.trial_seconds)} 分钟 · ${e.trial_active ? '有效至 ' + fmtTime(e.trial_expire_at) : '<span class="muted">已失效</span>'}</dd>
      <dt>订阅</dt><dd>${e.sub_active ? '有效至 ' + fmtTime(e.sub_expire_at) : '<span class="muted">无</span>'}</dd>
      <dt>状态</dt><dd>${u.banned ? '<span class="tag bad">已封禁</span>' : '<span class="tag ok">正常</span>'} · 注册 ${fmtTime(u.created_at)} · 活跃 ${ago(u.last_active_at)}</dd>
      <dt>在线会话</dt><dd>${d.sessions.length ? d.sessions.map((s) => `<span class="tag teal">${s.stream === 'mate' ? '队友' : '自翻'} → ${esc(s.target)}</span>`).join(' ') : '<span class="muted">无</span>'}</dd>
    </dl>
    <div class="box"><h3>调整权益 <span class="muted">填负数为扣减</span></h3>
      <div class="inline">
        <input id="aFen" type="number" placeholder="余额（元）" step="0.01">
        <input id="aMin" type="number" placeholder="时长卡（分钟）">
        <input id="aSub" type="number" placeholder="订阅（天）">
        <button class="btn primary sm" id="aApply">应用</button>
      </div>
      <div class="inline">
        <input id="aTrial" type="number" min="0" placeholder="免费额度（分钟）">
        <input id="aTrialDays" type="number" min="0" placeholder="有效期（天）">
        <button class="btn sm" id="aTrialSet">设定免费额度</button>
      </div>
      <div class="inline"><input id="aNote" maxlength="200" placeholder="备注" value="${esc(u.note || '')}"><button class="btn sm" id="aNoteSave">保存备注</button></div>
    </div>
    <div class="box"><h3>账号操作</h3>
      <div class="inline">
        <button class="btn sm ${u.banned ? '' : 'danger'}" id="aBan">${u.banned ? '解除封禁' : '封禁账号'}</button>
        <button class="btn sm" id="aKick">强制下线</button>
        ${u.username ? '<input id="aPwd" type="password" placeholder="新密码（≥6 位）"><button class="btn sm" id="aPwdSet">重置密码</button>' : ''}
      </div>
    </div>
    <div class="box"><h3>最近订单</h3><table class="tbl" id="dOrders"></table></div>
    <div class="box"><h3>最近用量</h3><table class="tbl" id="dUsage"></table></div>`;
  table($('#dOrders'), [['订单号'], ['商品'], ['金额', 'num'], ['状态'], ['时间']],
    d.orders.map((o) => `<tr><td class="mono">${esc(o.out_trade_no)}</td><td>${esc(o.name)}</td><td class="num">¥${esc(o.money)}</td><td>${statusTag(o.status)}</td><td>${fmtTime(o.created_at)}</td></tr>`));
  table($('#dUsage'), [['时间'], ['通道'], ['时长', 'num'], ['计费池'], ['扣费', 'num']],
    d.usage.map(usageRow));
  $('#drawer').classList.remove('hidden'); $('#drawerMask').classList.remove('hidden');

  const num = (id) => { const v = $(id).value.trim(); return v === '' ? 0 : +v; };
  const refresh = () => { openUser(uid); loaders.users(); };
  $('#aApply').onclick = async () => {
    const body = { fen: Math.round(num('#aFen') * 100), seconds: Math.round(num('#aMin') * 60), sub_days: Math.round(num('#aSub')) };
    if (!body.fen && !body.seconds && !body.sub_days) return toast('请填写要调整的数值', true);
    await act(() => api(`/users/${uid}/adjust`, { method: 'POST', body }), '已调整'); refresh();
  };
  $('#aTrialSet').onclick = async () => {
    if ($('#aTrial').value === '') return toast('请填写免费额度', true);
    const body = { trial_seconds: Math.round(num('#aTrial') * 60), trial_days: $('#aTrialDays').value === '' ? -1 : num('#aTrialDays') };
    await act(() => api(`/users/${uid}/adjust`, { method: 'POST', body }), '已设定'); refresh();
  };
  $('#aNoteSave').onclick = async () => {
    await act(() => api(`/users/${uid}/adjust`, { method: 'POST', body: { note: $('#aNote').value } }), '备注已保存'); refresh();
  };
  $('#aBan').onclick = async () => {
    if (!u.banned && !confirm('封禁后该账号立即下线且无法登录，确定？')) return;
    await act(() => api(`/users/${uid}/ban`, { method: 'POST', body: { banned: !u.banned } }), u.banned ? '已解封' : '已封禁'); refresh();
  };
  $('#aKick').onclick = async () => {
    const r = await act(() => api(`/users/${uid}/kick`, { method: 'POST' }));
    toast(`已强制下线（断开 ${r.kicked} 个会话，需重新登录）`); refresh();
  };
  if ($('#aPwdSet')) $('#aPwdSet').onclick = async () => {
    await act(() => api(`/users/${uid}/password`, { method: 'POST', body: { password: $('#aPwd').value } }), '密码已重置');
    $('#aPwd').value = '';
  };
}

/* ---------- 订单 ---------- */
const oState = { page: 1, size: 20 };
function statusTag(s) {
  return { paid: '<span class="tag ok">已支付</span>', pending: '<span class="tag warn">待支付</span>', closed: '<span class="tag">已关闭</span>' }[s] || esc(s);
}
loaders.orders = async (page) => {
  if (page) oState.page = page;
  const d = await api('/orders', { params: { q: $('#oQ').value.trim(), status: $('#oStatus').value, page: oState.page, size: oState.size } });
  table($('#oTbl'), [['订单号'], ['用户'], ['商品'], ['金额', 'num'], ['方式'], ['状态'], ['创建'], ['支付'], ['', 'acts']],
    d.items.map((o) => `<tr>
      <td class="mono">${esc(o.out_trade_no)}</td>
      <td><a data-user="${o.user_id}">${o.username ? esc(o.username) : '#' + o.user_id}</a></td>
      <td>${esc(o.name)}</td><td class="num">¥${esc(o.money)}</td>
      <td>${o.pay_type === 'wxpay' ? '微信' : '支付宝'}</td><td>${statusTag(o.status)}</td>
      <td>${fmtTime(o.created_at)}</td><td>${o.paid_at ? fmtTime(o.paid_at) : '—'}${o.trade_no === 'manual' ? ' <span class="tag">手动</span>' : ''}</td>
      <td class="acts">${o.status === 'pending' ? `<button class="btn sm" data-osync="${esc(o.out_trade_no)}">查单</button><button class="btn sm" data-ofulfill="${esc(o.out_trade_no)}">补单</button><button class="btn sm danger" data-oclose="${esc(o.out_trade_no)}">关闭</button>` : ''}</td></tr>`));
  pager($('#oPager'), d.total, oState.page, oState.size, loaders.orders);
};
$('#oSearch').onclick = () => loaders.orders(1);
$('#oQ').addEventListener('keydown', (e) => e.key === 'Enter' && loaders.orders(1));
$('#oStatus').onchange = () => loaders.orders(1);
$('#oTbl').addEventListener('click', async (e) => {
  const b = e.target.closest('button'); if (!b) return;
  if (b.dataset.osync) {
    const r = await act(() => api(`/orders/${encodeURIComponent(b.dataset.osync)}/sync`, { method: 'POST' }));
    toast(r.status === 'paid' ? '网关确认已支付，已自动发货' : `网关状态：未支付 ${r.gateway || ''}`);
  } else if (b.dataset.ofulfill) {
    if (!confirm('确认已在支付平台收到该笔款项？补单后会立即发货（含推广返利）。')) return;
    await act(() => api(`/orders/${encodeURIComponent(b.dataset.ofulfill)}/fulfill`, { method: 'POST' }), '已补单发货');
  } else if (b.dataset.oclose) {
    if (!confirm('关闭后该订单即使支付回调到达也不会发货，确定？')) return;
    await act(() => api(`/orders/${encodeURIComponent(b.dataset.oclose)}/close`, { method: 'POST' }), '订单已关闭');
  } else return;
  loaders.orders();
});

/* ---------- 实时会话 ---------- */
let liveTimer = null;
function startLive() { stopLive(); liveTimer = setInterval(() => loaders.sessions().catch(() => {}), 5000); }
function stopLive() { if (liveTimer) clearInterval(liveTimer); liveTimer = null; }
loaders.sessions = async () => {
  const d = await api('/sessions');
  $('#liveBadge').textContent = d.items.length;
  const now = Date.now() / 1000;
  table($('#sTbl'), [['用户'], ['通道'], ['目标语言'], ['播报'], ['时长', 'num'], ['已计费', 'num'], ['IP'], ['降级'], ['', 'acts']],
    d.items.map((s) => `<tr>
      <td><a data-user="${s.user_id}">${esc(s.username)}</a> <span class="muted">#${s.user_id}</span></td>
      <td>${s.stream === 'mate' ? '<span class="tag teal">队友</span>' : '<span class="tag warn">自翻</span>'}</td>
      <td>${esc(s.target)}</td><td>${s.audio ? '开' : '关'}</td>
      <td class="num">${dur(now - s.started_at)}</td><td class="num">${dur(s.billed_seconds)}</td>
      <td class="mono">${esc(s.ip)}</td><td>${(s.degraded || []).map((x) => `<span class="tag bad">${esc(x)}</span>`).join(' ') || '—'}</td>
      <td class="acts"><button class="btn sm danger" data-skick="${s.sid}">断开</button></td></tr>`), '当前没有进行中的翻译会话');
};
$('#sTbl').addEventListener('click', async (e) => {
  const b = e.target.closest('[data-skick]'); if (!b) return;
  await act(() => api(`/sessions/${b.dataset.skick}/kick`, { method: 'POST' }), '会话已断开');
  loaders.sessions();
});

/* ---------- 用量 ---------- */
const gState = { page: 1, size: 50 };
const POOL = { trial: '免费额度', card: '时长卡', money: '余额', free: '订阅' };
function usageRow(l) {
  return `<tr><td>${fmtTime(l.created_at)}</td><td>${l.stream === 'mate' ? '队友' : '自翻'}</td>
    <td class="num">${dur(l.seconds)}</td><td>${esc(POOL[l.pool] || l.pool)}</td><td class="num">${l.fen ? yuan(l.fen) : '—'}</td></tr>`;
}
loaders.usage = async (page) => {
  if (page) gState.page = page;
  const d = await api('/usage', { params: { user_id: $('#gUid').value.trim(), page: gState.page, size: gState.size } });
  table($('#gTbl'), [['时间'], ['用户'], ['通道'], ['时长', 'num'], ['计费池'], ['扣费', 'num']],
    d.items.map((l) => usageRow(l).replace('</td>', `</td><td><a data-user="${l.user_id}">${l.username ? esc(l.username) : '#' + l.user_id}</a></td>`)));
  pager($('#gPager'), d.total, gState.page, gState.size, loaders.usage);
};
$('#gSearch').onclick = () => loaders.usage(1);

/* ---------- 热词 ---------- */
let tTarget = 'zh', tItems = [];
loaders.terms = async () => {
  const d = await api('/terms', { params: { target: tTarget } });
  tItems = d.items; $('#tLimit').textContent = d.limit;
  renderTerms();
};
function renderTerms() {
  const q = $('#tQ').value.trim().toLowerCase();
  const list = q ? tItems.filter((t) => t.src.toLowerCase().includes(q) || t.dst.toLowerCase().includes(q)) : tItems;
  $('#tCount').textContent = `${tItems.length} 条${q ? ` · 匹配 ${list.length}` : ''}`;
  table($('#tTbl'), [['<input type="checkbox" id="tAll">'], ['原文'], ['译文']],
    list.map((t) => `<tr><td><input type="checkbox" data-tid="${t.id}"></td><td>${esc(t.src)}</td><td>${esc(t.dst)}</td></tr>`), '没有词条');
  $('#tDel').disabled = true;
  $('#tAll').onchange = (e) => { $$('#tTbl [data-tid]').forEach((c) => (c.checked = e.target.checked)); syncDel(); };
}
function syncDel() { $('#tDel').disabled = !$$('#tTbl [data-tid]:checked').length; }
$('#tTbl').addEventListener('change', (e) => e.target.dataset.tid && syncDel());
$('#tQ').addEventListener('input', renderTerms);
$('#tTarget').addEventListener('click', (e) => {
  const b = e.target.closest('[data-t]'); if (!b) return;
  tTarget = b.dataset.t;
  $$('#tTarget button').forEach((x) => x.classList.toggle('on', x === b));
  loaders.terms();
});
$('#tAdd').onclick = async () => {
  const pairs = {}, bad = [];
  $('#tBulk').value.split(/\r?\n/).forEach((line, i) => {
    if (!line.trim()) return;
    const k = line.indexOf('=');
    if (k <= 0 || !line.slice(k + 1).trim()) { bad.push(i + 1); return; }
    pairs[line.slice(0, k).trim()] = line.slice(k + 1).trim();
  });
  if (bad.length) return toast(`第 ${bad.join(', ')} 行格式错误（应为 原文=译文）`, true);
  if (!Object.keys(pairs).length) return toast('请输入词条', true);
  const r = await act(() => api('/terms', { method: 'POST', body: { target: tTarget, pairs } }));
  toast(`已保存 ${r.count} 条`); $('#tBulk').value = '';
  loaders.terms();
};
$('#tDel').onclick = async () => {
  const ids = $$('#tTbl [data-tid]:checked').map((c) => +c.dataset.tid);
  if (!confirm(`删除选中的 ${ids.length} 条词条？`)) return;
  await act(() => api('/terms/delete', { method: 'POST', body: { ids } }), '已删除');
  loaders.terms();
};
$('#tReset').onclick = async () => {
  if (!confirm('清空当前目标语言的所有词条并恢复为内置游戏术语，确定？')) return;
  const r = await act(() => api('/terms/reset', { method: 'POST', body: { target: tTarget } }));
  toast(`已恢复 ${r.count} 条内置词条`); loaders.terms();
};

/* ---------- 设置 ---------- */
loaders.settings = async () => {
  const d = await api('/settings');
  const f = $('#setForm');
  for (const [k, v] of Object.entries(d.values)) {
    const el = f.elements[k]; if (!el) continue;
    el.type === 'checkbox' ? (el.checked = !!v) : (el.value = v);
  }
  const KIND = { money: '余额充值', card: '时长卡', sub: '订阅' };
  table($('#prodTbl'), [['代码'], ['名称'], ['类型'], ['价格', 'num']],
    Object.entries(d.products).map(([c, p]) => `<tr><td class="mono">${esc(c)}</td><td>${esc(p.name)}</td><td>${esc(KIND[p.kind] || p.kind)}</td><td class="num">¥${esc(p.money)}</td></tr>`));
};
$('#setForm').addEventListener('submit', async (e) => {
  e.preventDefault();
  const body = {};
  for (const el of e.target.elements) {
    if (!el.name) continue;
    body[el.name] = el.type === 'checkbox' ? el.checked : el.type === 'number' ? +el.value : el.value;
  }
  await act(() => api('/settings', { method: 'PUT', body }), '设置已保存');
  $('#setMsg').textContent = '已保存 ' + fmtTime(Date.now() / 1000);
});

/* ---------- 启动 ---------- */
window.addEventListener('hashchange', () => sessionStorage.getItem(SKEY) && go(location.hash.slice(1)));
sessionStorage.getItem(SKEY) ? showShell() : showLogin();
