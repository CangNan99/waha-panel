"""Standalone commerce administration page.

The page deliberately contains no backend calls at import time.  ``commerce_page``
returns a self-contained HTML document so ``app.py`` can expose it from a route
without coupling the page to a template engine.
"""

__all__ = ["commerce_page"]


def commerce_page():
    """Return the WooCommerce and PayPal operations console HTML."""
    return r'''<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>订单与支付管理 | WAHA</title>
  <style>
    :root {
      color-scheme: dark;
      --bg: #08111f;
      --surface: #101c2d;
      --surface-2: #14243a;
      --surface-3: #1a2d45;
      --line: #29405c;
      --text: #edf5ff;
      --muted: #9db0c7;
      --faint: #71869f;
      --accent: #43d17a;
      --accent-ink: #061c12;
      --warn: #f6c65b;
      --danger: #ff7d7d;
      --danger-bg: #3b1f2b;
      --radius: 7px;
      --shadow: 0 12px 30px rgba(0, 0, 0, .18);
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      background: var(--bg);
      color: var(--text);
      font: 15px/1.5 "Segoe UI", "Microsoft YaHei", sans-serif;
    }
    button, input, select, textarea { font: inherit; }
    button, a, input, select, textarea { outline-offset: 3px; }
    :focus-visible { outline: 2px solid var(--accent); }
    button {
      min-height: 40px;
      border: 1px solid var(--line);
      border-radius: var(--radius);
      background: var(--surface-3);
      color: var(--text);
      padding: 8px 13px;
      cursor: pointer;
      transition: background .2s ease, border-color .2s ease, transform .2s ease;
    }
    button:hover:not(:disabled) { background: #24405f; border-color: #4e6d91; }
    button:active:not(:disabled) { transform: translateY(1px); }
    button:disabled { cursor: wait; opacity: .55; }
    button.primary { background: var(--accent); border-color: var(--accent); color: var(--accent-ink); font-weight: 700; }
    button.primary:hover:not(:disabled) { background: #69e697; border-color: #69e697; }
    button.quiet { background: transparent; }
    button.danger { color: var(--danger); border-color: #6e3545; background: transparent; }
    a { color: var(--text); text-decoration: none; }
    .shell { width: min(1440px, calc(100% - 32px)); margin: 0 auto; padding: 28px 0 56px; }
    .topbar { display: flex; align-items: flex-start; justify-content: space-between; gap: 24px; margin-bottom: 24px; }
    .eyebrow { color: var(--accent); font: 600 12px/1.2 "Cascadia Mono", Consolas, monospace; letter-spacing: .04em; text-transform: uppercase; }
    h1, h2, h3, p { margin: 0; }
    h1 { margin-top: 7px; font-size: clamp(25px, 3vw, 36px); line-height: 1.15; letter-spacing: 0; }
    h2 { font-size: 18px; line-height: 1.25; }
    h3 { font-size: 15px; }
    .subhead { max-width: 680px; margin-top: 9px; color: var(--muted); }
    .top-actions, .actions, .status-actions { display: flex; align-items: center; flex-wrap: wrap; gap: 8px; }
    .status-actions { justify-content: flex-end; }
    .status-pill, .tag { display: inline-flex; align-items: center; gap: 7px; border: 1px solid var(--line); border-radius: 999px; padding: 6px 10px; color: var(--muted); font-size: 13px; white-space: nowrap; }
    .status-pill::before { content: ""; width: 8px; height: 8px; border-radius: 50%; background: var(--faint); }
    .status-pill.ok { color: #a9f4c3; border-color: #286344; background: rgba(67,209,122,.08); }
    .status-pill.ok::before { background: var(--accent); }
    .status-pill.bad { color: #ffb6b6; border-color: #713a4b; background: rgba(255,125,125,.08); }
    .status-pill.bad::before { background: var(--danger); }
    .status-pill.busy::before { background: var(--warn); }
    .summary { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 10px; margin-bottom: 14px; }
    .metric { min-height: 96px; border: 1px solid var(--line); border-radius: var(--radius); background: var(--surface); padding: 14px; box-shadow: var(--shadow); }
    .metric-label { color: var(--muted); font-size: 12px; }
    .metric-value { margin-top: 8px; font-size: 24px; font-weight: 700; }
    .workspace { display: grid; grid-template-columns: minmax(0, 1.1fr) minmax(340px, .9fr); gap: 14px; align-items: start; }
    .stack { display: grid; gap: 14px; }
    .panel { border: 1px solid var(--line); border-radius: var(--radius); background: var(--surface); box-shadow: var(--shadow); overflow: hidden; }
    .panel-head { display: flex; justify-content: space-between; align-items: flex-start; gap: 16px; padding: 16px 18px; border-bottom: 1px solid var(--line); }
    .panel-head p { margin-top: 4px; color: var(--muted); font-size: 13px; }
    .panel-body { padding: 16px 18px; }
    .form-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 13px; }
    .field { display: grid; gap: 6px; }
    .field.full { grid-column: 1 / -1; }
    label { color: var(--muted); font-size: 13px; }
    input, select, textarea { width: 100%; border: 1px solid var(--line); border-radius: 5px; background: #0b1727; color: var(--text); padding: 9px 10px; }
    input::placeholder, textarea::placeholder { color: var(--faint); }
    textarea { min-height: 84px; resize: vertical; }
    .hint { color: var(--faint); font-size: 12px; }
    .credential-state { min-height: 40px; display: flex; align-items: center; padding: 8px 10px; border: 1px dashed #45617d; border-radius: 5px; color: var(--muted); }
    .form-footer { display: flex; justify-content: space-between; align-items: center; gap: 12px; margin-top: 15px; }
    .notice { min-height: 22px; color: var(--muted); font-size: 13px; }
    .notice.error { color: var(--danger); }
    .notice.success { color: #a9f4c3; }
    .table-wrap { overflow-x: auto; }
    table { width: 100%; border-collapse: collapse; min-width: 620px; }
    th, td { padding: 11px 14px; border-bottom: 1px solid #1f334b; text-align: left; vertical-align: top; }
    th { color: var(--muted); font-size: 12px; font-weight: 600; white-space: nowrap; }
    td { color: #d9e5f2; font-size: 13px; }
    tr:last-child td { border-bottom: 0; }
    tr[data-ticket-id] { cursor: pointer; transition: background .2s ease; }
    tr[data-ticket-id]:hover, tr[data-ticket-id].selected { background: #162941; }
    .tag { padding: 3px 8px; font-size: 12px; }
    .tag.good { color: #a9f4c3; border-color: #286344; }
    .tag.warn { color: #ffe09a; border-color: #80652b; }
    .tag.bad { color: #ffb6b6; border-color: #713a4b; }
    .empty { padding: 28px 18px; color: var(--faint); text-align: center; }
    .detail { display: grid; gap: 13px; }
    .detail-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 8px 14px; }
    .detail-item { padding: 9px 0; border-bottom: 1px solid #1f334b; }
    .detail-item strong { display: block; margin-top: 2px; overflow-wrap: anywhere; }
    .detail-item span { color: var(--muted); font-size: 12px; }
    .reply-box { display: grid; gap: 8px; padding-top: 4px; }
    .lock-row { display: grid; grid-template-columns: 1fr auto; align-items: center; gap: 14px; padding: 12px 0; border-bottom: 1px solid #1f334b; }
    .lock-row:last-child { border-bottom: 0; }
    .lock-meta { color: var(--muted); font-size: 12px; }
    .audit-list { display: grid; gap: 0; max-height: 310px; overflow: auto; }
    .audit-item { display: grid; grid-template-columns: 140px 1fr auto; gap: 12px; padding: 10px 0; border-bottom: 1px solid #1f334b; font-size: 13px; }
    .audit-item:last-child { border-bottom: 0; }
    .audit-time, .audit-kind { color: var(--muted); }
    .audit-kind { font-family: "Cascadia Mono", Consolas, monospace; font-size: 12px; }
    .sr-only { position: absolute; width: 1px; height: 1px; padding: 0; margin: -1px; overflow: hidden; clip: rect(0,0,0,0); white-space: nowrap; border: 0; }
    @media (max-width: 980px) {
      .workspace { grid-template-columns: 1fr; }
      .summary { grid-template-columns: repeat(2, minmax(0, 1fr)); }
    }
    @media (max-width: 620px) {
      .shell { width: min(100% - 20px, 1440px); padding-top: 18px; }
      .topbar { display: grid; gap: 14px; }
      .status-actions { justify-content: flex-start; }
      .summary, .form-grid, .detail-grid { grid-template-columns: 1fr; }
      .field.full { grid-column: auto; }
      .form-footer { align-items: flex-start; flex-direction: column; }
      .audit-item { grid-template-columns: 1fr; gap: 3px; }
    }
    @media (prefers-reduced-motion: reduce) {
      *, *::before, *::after { scroll-behavior: auto !important; transition-duration: .01ms !important; }
    }
  </style>
</head>
<body>
  <main class="shell">
    <header class="topbar">
      <div>
        <div class="eyebrow">Commerce operations / WAHA</div>
        <h1>订单与支付管理</h1>
        <p class="subhead">按需核对 WooCommerce 订单与 PayPal 支付，人工工单和查询审计集中在同一处。</p>
      </div>
      <div class="status-actions">
        <span class="status-pill busy" id="connectionStatus">连接状态读取中</span>
        <button type="button" id="testConnection">测试连接</button>
        <a class="button quiet" href="/">返回面板</a>
      </div>
    </header>

    <section class="summary" aria-label="业务概览">
      <div class="metric"><div class="metric-label">待处理工单</div><div class="metric-value" id="openTicketCount">--</div></div>
      <div class="metric"><div class="metric-label">已锁定邮箱</div><div class="metric-value" id="lockedEmailCount">--</div></div>
      <div class="metric"><div class="metric-label">今日查询</div><div class="metric-value" id="todayQueryCount">--</div></div>
      <div class="metric"><div class="metric-label">最近查询</div><div class="metric-value" id="lastQueryAt">--</div></div>
    </section>

    <div class="workspace">
      <div class="stack">
        <section class="panel" aria-labelledby="configTitle">
          <div class="panel-head"><div><h2 id="configTitle">连接与凭据</h2><p>普通配置可回显；敏感凭据只支持替换，不读取旧值。</p></div><span class="tag" id="configState">配置读取中</span></div>
          <div class="panel-body">
            <form id="commerceConfigForm">
              <div class="form-grid">
                <div class="field"><label for="commerceEnabled">订单查询</label><label><input id="commerceEnabled" name="commerce_enabled" type="checkbox"> 启用 AI 按需查询订单与支付</label><span class="hint">关闭时普通自动回复不会访问订单系统。</span></div>
                <div class="field"><label for="wooUrl">WooCommerce 地址</label><input id="wooUrl" name="woocommerce_url" type="url" placeholder="https://store.example.com" autocomplete="url"><span class="hint">仅填写站点基础地址，不填写密钥。</span></div>
                <div class="field"><label for="paypalEnvironment">PayPal 环境</label><select id="paypalEnvironment" name="paypal_environment"><option value="sandbox">Sandbox 测试</option><option value="live">Live 生产</option></select></div>
                <div class="field"><label for="adminWhatsapp">管理员 WhatsApp 号码</label><input id="adminWhatsapp" name="admin_whatsapp" type="tel" placeholder="包含国家/地区码，例如 8613812345678" autocomplete="tel"></div>
                <div class="field"><label for="requestTimeout">查询超时（秒）</label><input id="requestTimeout" name="request_timeout_seconds" type="number" min="3" max="30" step="1" placeholder="10"></div>
                <div class="field"><label for="wordpressHost">WordPress 数据库主机</label><input id="wordpressHost" name="wordpress_host" type="text" placeholder="数据库容器名或 127.0.0.1"></div>
                <div class="field"><label for="wordpressPort">WordPress 数据库端口</label><input id="wordpressPort" name="wordpress_port" type="number" min="1" max="65535" step="1" placeholder="3306"></div>
                <div class="field"><label for="wordpressDatabase">WordPress 数据库名</label><input id="wordpressDatabase" name="wordpress_database" type="text"></div>
                <div class="field"><label for="wordpressUser">WordPress 只读用户</label><input id="wordpressUser" name="wordpress_user" type="text" autocomplete="username"></div>
                <div class="field"><label for="wordpressPrefix">WordPress 表前缀</label><input id="wordpressPrefix" name="wordpress_prefix" type="text" placeholder="wp_"></div>
                <div class="field"><label for="wordpressPassword">WordPress 只读密码</label><input id="wordpressPassword" name="wordpress_password" type="password" placeholder="留空表示不替换" autocomplete="new-password"><span class="credential-state" id="wordpressPasswordState">读取中</span></div>
                <div class="field"><label for="wooKey">WooCommerce Consumer Key</label><input id="wooKey" name="woocommerce_consumer_key" type="password" placeholder="留空表示不替换" autocomplete="new-password"><span class="credential-state" id="wooKeyState">读取中</span></div>
                <div class="field"><label for="wooSecret">WooCommerce Consumer Secret</label><input id="wooSecret" name="woocommerce_consumer_secret" type="password" placeholder="留空表示不替换" autocomplete="new-password"><span class="credential-state" id="wooSecretState">读取中</span></div>
                <div class="field"><label for="paypalClientId">PayPal Client ID</label><input id="paypalClientId" name="paypal_client_id" type="password" placeholder="留空表示不替换" autocomplete="new-password"><span class="credential-state" id="paypalClientIdState">读取中</span></div>
                <div class="field"><label for="paypalClientSecret">PayPal Client Secret</label><input id="paypalClientSecret" name="paypal_client_secret" type="password" placeholder="留空表示不替换" autocomplete="new-password"><span class="credential-state" id="paypalClientSecretState">读取中</span></div>
              </div>
              <div class="form-footer"><div class="notice" id="configNotice" role="status" aria-live="polite"></div><div class="actions"><button type="button" class="quiet" id="clearConfigForm">清空待替换字段</button><button type="submit" class="primary" id="saveConfig">保存配置</button></div></div>
            </form>
          </div>
        </section>

        <section class="panel" aria-labelledby="ticketsTitle">
          <div class="panel-head"><div><h2 id="ticketsTitle">人工工单</h2><p>查询异常、状态冲突或客户身份未能自动核对时，在这里处理。</p></div><div class="actions"><select id="ticketFilter" aria-label="筛选工单"><option value="open">待处理</option><option value="all">全部</option><option value="resolved">已解决</option></select><button type="button" id="refreshTickets">刷新</button></div></div>
          <div class="table-wrap"><table><thead><tr><th>工单</th><th>客户</th><th>原因</th><th>状态</th><th>更新时间</th></tr></thead><tbody id="ticketRows"><tr><td colspan="5" class="empty">工单读取中</td></tr></tbody></table></div>
        </section>

        <section class="panel" aria-labelledby="auditTitle">
          <div class="panel-head"><div><h2 id="auditTitle">查询审计</h2><p>仅显示必要操作信息，不记录密钥、完整支付凭据或 AI 上下文。</p></div><button type="button" id="refreshAudit">刷新</button></div>
          <div class="panel-body"><div class="audit-list" id="auditList"><div class="empty">审计记录读取中</div></div></div>
        </section>
      </div>

      <aside class="stack">
        <section class="panel" aria-labelledby="detailTitle">
          <div class="panel-head"><div><h2 id="detailTitle">工单详情</h2><p id="detailHint">选择左侧工单查看已授权的订单资料。</p></div><span class="tag" id="detailState">未选择</span></div>
          <div class="panel-body"><div id="ticketDetail" class="detail"><div class="empty">尚未选择工单</div></div></div>
        </section>

        <section class="panel" aria-labelledby="locksTitle">
          <div class="panel-head"><div><h2 id="locksTitle">邮箱验证锁定</h2><p>连续错误 3 次后锁定；只能由管理员手动解锁。</p></div><button type="button" id="refreshLocks">刷新</button></div>
          <div class="panel-body"><div id="lockList"><div class="empty">锁定记录读取中</div></div></div>
        </section>
      </aside>
    </div>
  </main>

  <script>
    'use strict';
    const API = '/api/commerce';
    const $ = (id) => document.getElementById(id);
    const esc = (value) => String(value ?? '').replace(/[&<>"']/g, (char) => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
    const text = (value, fallback = '--') => value === null || value === undefined || value === '' ? fallback : String(value);
    let selectedTicketId = null;

    async function request(path, options = {}) {
      const response = await fetch(API + path, {cache: 'no-store', ...options});
      const contentType = response.headers.get('content-type') || '';
      const payload = contentType.includes('application/json') ? await response.json() : {};
      if (!response.ok) throw new Error(payload.message || '请求失败');
      return payload;
    }

    function setNotice(id, message, kind = '') {
      $(id).textContent = message || '';
      $(id).className = 'notice' + (kind ? ' ' + kind : '');
    }

    function statusTag(value) {
      const normalized = String(value || '').toLowerCase();
      const good = ['resolved', 'completed', 'paid', 'connected', 'ok', 'unlocked'].includes(normalized);
      const bad = ['failed', 'error', 'locked', 'refunded', 'cancelled'].includes(normalized);
      return `<span class="tag ${good ? 'good' : bad ? 'bad' : 'warn'}">${esc(text(value, '未知'))}</span>`;
    }

    function renderStatus(data) {
      const connected = data.connected === true || data.status === 'connected' || data.ok === true;
      $('connectionStatus').className = 'status-pill ' + (connected ? 'ok' : 'bad');
      $('connectionStatus').textContent = connected ? '服务已连接' : '服务未连接';
      const stats = data.stats || data.summary || {};
      $('openTicketCount').textContent = text(stats.open_tickets ?? data.open_tickets, '0');
      $('lockedEmailCount').textContent = text(stats.locked_emails ?? data.locked_emails, '0');
      $('todayQueryCount').textContent = text(stats.today_queries ?? data.today_queries, '0');
      $('lastQueryAt').textContent = text(stats.last_query_at ?? data.last_query_at, '暂无');
    }

    async function loadStatus() {
      try { renderStatus(await request('/status')); }
      catch (error) { $('connectionStatus').className = 'status-pill bad'; $('connectionStatus').textContent = '状态读取失败'; setNotice('configNotice', error.message, 'error'); }
    }

    async function testConnection() {
      const button = $('testConnection'); button.disabled = true; button.textContent = '测试中...';
      try { const result = await request('/test-connection', {method: 'POST'}); renderStatus(result); setNotice('configNotice', result.message || '连接测试完成', result.ok === false ? 'error' : 'success'); }
      catch (error) { setNotice('configNotice', error.message, 'error'); }
      finally { button.disabled = false; button.textContent = '测试连接'; }
    }

    function fillConfig(data) {
      $('commerceEnabled').checked = data.enabled === true;
      $('wooUrl').value = data.woocommerce_url || '';
      $('paypalEnvironment').value = data.paypal_environment || 'sandbox';
      $('adminWhatsapp').value = data.admin_whatsapp || '';
      $('requestTimeout').value = data.request_timeout_seconds || '';
      $('wordpressHost').value = data.wordpress_host || '';
      $('wordpressPort').value = data.wordpress_port || 3306;
      $('wordpressDatabase').value = data.wordpress_database || '';
      $('wordpressUser').value = data.wordpress_user || '';
      $('wordpressPrefix').value = data.wordpress_prefix || 'wp_';
      const credentials = data.credentials || {};
      [['wooKeyState', 'woocommerce_consumer_key'], ['wooSecretState', 'woocommerce_consumer_secret'], ['paypalClientIdState', 'paypal_client_id'], ['paypalClientSecretState', 'paypal_client_secret'], ['wordpressPasswordState', 'wordpress_password']].forEach(([id, key]) => {
        $(id).textContent = credentials[key] ? '已配置（隐藏）' : '未配置';
      });
      $('configState').textContent = data.configured ? '配置完整' : '待配置';
      $('configState').className = 'tag ' + (data.configured ? 'good' : 'warn');
    }

    async function loadConfig() {
      try { fillConfig(await request('/config')); }
      catch (error) { $('configState').textContent = '读取失败'; setNotice('configNotice', error.message, 'error'); }
    }

    function configPayload() {
      const body = {enabled:$('commerceEnabled').checked,woocommerce_url:$('wooUrl').value.trim(),paypal_environment:$('paypalEnvironment').value,admin_whatsapp:$('adminWhatsapp').value.trim(),request_timeout_seconds:Number($('requestTimeout').value || 10),wordpress_host:$('wordpressHost').value.trim(),wordpress_port:Number($('wordpressPort').value || 3306),wordpress_database:$('wordpressDatabase').value.trim(),wordpress_user:$('wordpressUser').value.trim(),wordpress_prefix:$('wordpressPrefix').value.trim() || 'wp_'};
      [['wooKey', 'woocommerce_consumer_key'], ['wooSecret', 'woocommerce_consumer_secret'], ['paypalClientId', 'paypal_client_id'], ['paypalClientSecret', 'paypal_client_secret'], ['wordpressPassword', 'wordpress_password']].forEach(([id, key]) => { if ($(id).value) body[key] = $(id).value; });
      return body;
    }

    async function saveConfig(event) {
      event.preventDefault(); const button = $('saveConfig'); button.disabled = true; setNotice('configNotice', '');
      try { const result = await request('/config', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(configPayload())}); fillConfig(result); ['wooKey','wooSecret','paypalClientId','paypalClientSecret','wordpressPassword'].forEach((id) => $(id).value = ''); setNotice('configNotice', '配置已保存，凭据仍保持隐藏。', 'success'); }
      catch (error) { setNotice('configNotice', error.message, 'error'); }
      finally { button.disabled = false; }
    }

    function renderTickets(data) {
      const tickets = data.items || data.tickets || [];
      $('ticketRows').innerHTML = tickets.length ? tickets.map((ticket) => `<tr data-ticket-id="${esc(ticket.id)}" class="${String(ticket.id) === String(selectedTicketId) ? 'selected' : ''}"><td>#${esc(ticket.id)}</td><td>${esc(text(ticket.customer_phone || ticket.customer_id))}</td><td>${esc(text(ticket.reason, '需人工核对'))}</td><td>${statusTag(ticket.status)}</td><td>${esc(text(ticket.updated_at || ticket.created_at))}</td></tr>`).join('') : '<tr><td colspan="5" class="empty">暂无工单</td></tr>';
      document.querySelectorAll('[data-ticket-id]').forEach((row) => row.addEventListener('click', () => loadTicket(row.dataset.ticketId)));
    }

    async function loadTickets() {
      $('ticketRows').innerHTML = '<tr><td colspan="5" class="empty">工单读取中</td></tr>';
      try { const filter = encodeURIComponent($('ticketFilter').value); renderTickets(await request('/tickets?status=' + filter)); }
      catch (error) { $('ticketRows').innerHTML = `<tr><td colspan="5" class="empty">${esc(error.message)}</td></tr>`; }
    }

    function detailItem(label, value) { return `<div class="detail-item"><span>${esc(label)}</span><strong>${esc(text(value))}</strong></div>`; }

    function renderTicketDetail(data) {
      const ticket = data.ticket || data;
      selectedTicketId = ticket.id;
      $('detailState').innerHTML = statusTag(ticket.status);
      $('detailHint').textContent = '订单完整资料仅应由后端在身份验证通过后返回。';
      const order = ticket.order || data.order;
      const payment = ticket.payment || data.payment;
      const fields = [detailItem('工单编号', ticket.id), detailItem('客户 WhatsApp', ticket.customer_phone), detailItem('验证方式', ticket.verification_method), detailItem('身份状态', ticket.verification_status), detailItem('订单号', order && order.number), detailItem('订单状态', order && order.status), detailItem('支付状态', payment && payment.status), detailItem('PayPal 交易号', payment && (payment.transaction_id || payment.transactionId)), detailItem('客户邮箱', order && order.billing_email), detailItem('客户电话', order && order.billing_phone), detailItem('收货地址', order && order.shipping_address), detailItem('总额', order && order.total)];
      $('ticketDetail').innerHTML = `<div class="detail-grid">${fields.join('')}</div><div class="actions"><button type="button" data-action="recheck">重查订单</button><button type="button" data-action="confirm" class="primary">确认已处理</button></div><div class="reply-box"><label for="ticketReply">答复客户</label><textarea id="ticketReply" placeholder="填写发送给客户的处理结果"></textarea><button type="button" data-action="reply">发送答复</button><div class="notice" id="detailNotice" role="status" aria-live="polite"></div></div>`;
      document.querySelectorAll('[data-action]').forEach((button) => button.addEventListener('click', () => handleTicketAction(button.dataset.action)));
    }

    async function loadTicket(id) {
      try { renderTicketDetail(await request('/tickets/' + encodeURIComponent(id))); loadTickets(); }
      catch (error) { $('ticketDetail').innerHTML = `<div class="empty">${esc(error.message)}</div>`; }
    }

    async function handleTicketAction(action) {
      if (!selectedTicketId) return;
      const detailNotice = $('detailNotice'); if (detailNotice) setNotice('detailNotice', '处理中...');
      const path = '/tickets/' + encodeURIComponent(selectedTicketId) + '/' + action;
      const options = {method: 'POST', headers: {'Content-Type': 'application/json'}};
      if (action === 'reply') options.body = JSON.stringify({message: $('ticketReply').value});
      try { const result = await request(path, options); if (result.ticket) renderTicketDetail(result); else if (detailNotice) setNotice('detailNotice', result.message || '操作完成', 'success'); await Promise.all([loadStatus(), loadTickets(), loadAudit()]); }
      catch (error) { if (detailNotice) setNotice('detailNotice', error.message, 'error'); }
    }

    function renderLocks(data) {
      const locks = data.items || data.locks || [];
      $('lockList').innerHTML = locks.length ? locks.map((lock) => `<div class="lock-row"><div><strong>${esc(text(lock.email, '邮箱已隐藏'))}</strong><div class="lock-meta">WhatsApp：${esc(text(lock.customer_phone, '已隐藏'))} · 错误次数：${esc(text(lock.failed_attempts, '3'))} · ${esc(text(lock.locked_at))}</div></div><button type="button" data-unlock="${esc(lock.id)}">解锁</button></div>`).join('') : '<div class="empty">暂无锁定邮箱</div>';
      document.querySelectorAll('[data-unlock]').forEach((button) => button.addEventListener('click', () => unlockEmail(button.dataset.unlock, button)));
    }

    async function loadLocks() { try { renderLocks(await request('/email-locks')); } catch (error) { $('lockList').innerHTML = `<div class="empty">${esc(error.message)}</div>`; } }
    async function unlockEmail(id, button) { button.disabled = true; try { await request('/email-locks/' + encodeURIComponent(id) + '/unlock', {method: 'POST'}); await Promise.all([loadLocks(), loadStatus(), loadAudit()]); } catch (error) { setNotice('configNotice', error.message, 'error'); } finally { button.disabled = false; } }

    function renderAudit(data) {
      const items = data.items || data.audit || [];
      $('auditList').innerHTML = items.length ? items.map((item) => `<div class="audit-item"><span class="audit-time">${esc(text(item.created_at || item.time))}</span><span><strong>${esc(text(item.action || item.event))}</strong><br><span class="audit-kind">${esc(text(item.result || item.status))}</span></span><span class="audit-kind">${esc(text(item.actor, 'system'))}</span></div>`).join('') : '<div class="empty">暂无查询审计</div>';
    }
    async function loadAudit() { try { renderAudit(await request('/audit?limit=50')); } catch (error) { $('auditList').innerHTML = `<div class="empty">${esc(error.message)}</div>`; } }

    $('testConnection').addEventListener('click', testConnection);
    $('commerceConfigForm').addEventListener('submit', saveConfig);
    $('clearConfigForm').addEventListener('click', () => { ['wooKey','wooSecret','paypalClientId','paypalClientSecret','wordpressPassword'].forEach((id) => $(id).value = ''); setNotice('configNotice', '待替换字段已清空。'); });
    $('ticketFilter').addEventListener('change', loadTickets);
    $('refreshTickets').addEventListener('click', loadTickets);
    $('refreshLocks').addEventListener('click', loadLocks);
    $('refreshAudit').addEventListener('click', loadAudit);
    Promise.all([loadStatus(), loadConfig(), loadTickets(), loadLocks(), loadAudit()]);
  </script>
</body>
</html>'''
