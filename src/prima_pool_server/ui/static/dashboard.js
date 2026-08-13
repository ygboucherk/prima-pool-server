// prima-pool account dashboard.
// Static page: logs in via POST /v1/accounts/login, then fetches the
// account-scoped dashboard endpoint with the session token.
(function () {
  const statusEl = document.getElementById('connection-status');
  const $ = (id) => document.getElementById(id);
  const TOKEN_KEY = 'prima-pool.session-token';
  const ACCOUNT_KEY = 'prima-pool.account-id';
  let sessionToken = null;
  let accountId = null;

  // Persist the session in sessionStorage so a page refresh keeps the user
  // logged in for the lifetime of the tab. The token is never placed in the
  // URL or in localStorage (which would survive tab close).
  function saveSession() {
    try {
      sessionStorage.setItem(TOKEN_KEY, sessionToken);
      sessionStorage.setItem(ACCOUNT_KEY, accountId);
    } catch (e) { /* storage unavailable — session stays in-memory only */ }
  }

  function restoreSession() {
    try {
      const t = sessionStorage.getItem(TOKEN_KEY);
      const a = sessionStorage.getItem(ACCOUNT_KEY);
      if (t && a) {
        sessionToken = t;
        accountId = a;
        return true;
      }
    } catch (e) { /* ignore */ }
    return false;
  }

  function clearSession() {
    sessionToken = null;
    accountId = null;
    try {
      sessionStorage.removeItem(TOKEN_KEY);
      sessionStorage.removeItem(ACCOUNT_KEY);
    } catch (e) { /* ignore */ }
  }

  function setStatus(text, cls) {
    statusEl.textContent = text;
    statusEl.className = 'status ' + (cls || '');
  }

  async function api(path, options = {}) {
    const headers = Object.assign({ Accept: 'application/json' }, options.headers || {});
    if (sessionToken) headers.Authorization = 'Bearer ' + sessionToken;
    if (options.body) headers['Content-Type'] = 'application/json';
    const resp = await fetch(path, Object.assign({}, options, { headers }));
    if (!resp.ok) {
      let detail = 'HTTP ' + resp.status;
      try {
        const j = await resp.json();
        if (j.detail) detail = j.detail;
        else if (j.title) detail = j.title;
      } catch (e) { /* ignore */ }
      const err = new Error(detail);
      err.status = resp.status;
      throw err;
    }
    // 204 No Content (and other empty bodies) have no JSON to parse.
    if (resp.status === 204 || resp.status === 205) return null;
    const text = await resp.text();
    if (!text) return null;
    try {
      return JSON.parse(text);
    } catch (e) {
      return null;
    }
  }

  // The dashboard data is fetched once and shared by all tabs.
  let overview = null;

  async function login(username, password) {
    const session = await api('/v1/accounts/login', {
      method: 'POST',
      body: JSON.stringify({ username, password }),
    });
    sessionToken = session.session_token;
    // The session token body embeds the account_id (after the "sess_" prefix).
    accountId = session.session_token.split('.')[0].replace(/^sess_/, '');
    saveSession();
    setStatus('connected', 'ok');
    $('login').hidden = true;
    $('view').hidden = false;
    $('logout-btn').hidden = false;
    await refresh();
  }

  // Central error handler: a 401 means the loaded session is invalid or
  // expired, so drop it and return to the login form.
  function handleApiError(err) {
    setStatus('error: ' + err.message, 'err');
    if (err.status === 401) {
      clearSession();
      $('view').hidden = true;
      $('logout-btn').hidden = true;
      show('login');
    }
  }

  async function refresh() {
    if (!sessionToken || !accountId) return;
    try {
      const data = await api('/v1/accounts/' + accountId + '/dashboard');
      overview = data;
      $('stat-account').textContent = data.username || data.account_id;
      $('stat-workers').textContent = data.workers.length;
      $('stat-online').textContent = data.workers.filter((w) => w.online).length;
      $('stat-keys').textContent = data.keys.length;
      // Show the admin tab only for admins.
      $('admin-tab-btn').hidden = !data.is_admin;
      buildWorkerMemoryMap();
      renderWorkers(data.workers);
      renderKeys(data.keys);
      refreshUsageTab();
      refreshWorkersTab();
      setStatus('connected', 'ok');
    } catch (e) {
      handleApiError(e);
    }
  }

  // ── inference usage tab ──────────────────────────────────────────────
  // Fetches the account's usage stats (last 7 days, local timezone) and its
  // most recent request logs, then renders a bar chart + a logs table.

  // Start of the local day (midnight) for a Date, as a Unix timestamp (s).
  function startOfLocalDay(d) {
    const copy = new Date(d.getFullYear(), d.getMonth(), d.getDate());
    return Math.floor(copy.getTime() / 1000);
  }

  // Build the 7 daily windows [startOfDay, startOfNextDay) ending today.
  function last7DayWindows() {
    const now = new Date();
    const todayStart = startOfLocalDay(now);
    const windows = [];
    for (let i = 6; i >= 0; i--) {
      const begin = todayStart - i * 86400;
      windows.push([begin, begin + 86400]);
    }
    return windows;
  }

  function fmtDayLabel(begin) {
    const d = new Date(begin * 1000);
    return d.toLocaleDateString(undefined, { weekday: 'short', month: 'short', day: 'numeric' });
  }

  // Pick a "nice" axis step (1/2/5 × 10^n) so the tick levels are rounded,
  // evenly-spaced numbers rather than arbitrary fractions of the max.
  // The step is never fractional: for small maxima it rounds up to a whole
  // number so the levels stay clean (e.g. max=1 -> step=1, not 0.5).
  function niceStep(max, ticks) {
    const raw = max / ticks;
    const mag = Math.pow(10, Math.floor(Math.log10(raw)));
    const norm = raw / mag;
    let step;
    if (norm <= 1) step = 1;
    else if (norm <= 2) step = 2;
    else if (norm <= 5) step = 5;
    else step = 10;
    step *= mag;
    // Round fractional steps up to the next whole number.
    if (step < 1) step = 1;
    else if (step % 1 !== 0) step = Math.ceil(step);
    return step;
  }

  // Generic bar chart over the last 7 daily windows. `summarize(win)` maps a
  // per-window stats object to {prompt, completion} token totals.
  function renderBarChart(chartId, emptyId, stats, summarize) {
    const chart = $(chartId);
    const empty = $(emptyId);
    chart.innerHTML = '';
    const windows = last7DayWindows();
    const totals = windows.map((w, i) => {
      const win = stats[i] || {};
      const { prompt, completion } = summarize(win);
      return { begin: w[0], prompt, completion };
    });
    const max = Math.max(1, ...totals.map((t) => t.prompt + t.completion));
    const any = totals.some((t) => t.prompt + t.completion > 0);
    empty.hidden = any;
    chart.hidden = !any;

    // Vertical axis: evenly-spaced rounded levels from 0 (bottom) to a nice
    // ceiling (top). The step is chosen so levels are round numbers.
    const ticks = 4;
    const step = niceStep(max, ticks);
    const ceiling = step * ticks;
    const axis = document.createElement('div');
    axis.className = 'usage-axis';
    // First child sits at the TOP with space-between, so emit max..0.
    for (let i = ticks; i >= 0; i--) {
      const value = step * i;
      const tick = document.createElement('div');
      tick.className = 'usage-axis-tick';
      tick.textContent = value.toLocaleString();
      axis.appendChild(tick);
    }
    chart.appendChild(axis);

    const bars = document.createElement('div');
    bars.className = 'usage-bars';
    for (const t of totals) {
      const total = t.prompt + t.completion;
      const bar = document.createElement('div');
      bar.className = 'usage-bar';
      const inner = document.createElement('div');
      inner.className = 'usage-bar-inner';
      inner.style.height = (total / ceiling * 100) + '%';
      inner.title = `${fmtDayLabel(t.begin)}: ${total.toLocaleString()} tokens`;
      bar.appendChild(inner);
      const label = document.createElement('div');
      label.className = 'usage-bar-label';
      label.textContent = fmtDayLabel(t.begin);
      bar.appendChild(label);
      bars.appendChild(bar);
    }
    chart.appendChild(bars);
  }

  function renderUsageChart(stats) {
    // stats is an array of per-window objects {model: {requests, prompt_tokens, completion_tokens}}.
    renderBarChart('usage-chart', 'usage-chart-empty', stats, (win) => {
      let prompt = 0, completion = 0;
      for (const m of Object.values(win)) {
        prompt += m.prompt_tokens || 0;
        completion += m.completion_tokens || 0;
      }
      return { prompt, completion };
    });
  }

  function renderWorkerChart(stats) {
    // stats is an array of per-window objects {model: {total_tokens, effective_tokens}}.
    renderBarChart('worker-chart', 'worker-chart-empty', stats, (win) => {
      let prompt = 0, completion = 0;
      for (const m of Object.values(win)) {
        const eff = m.effective_tokens || [0, 0];
        prompt += eff[0] || 0;
        completion += eff[1] || 0;
      }
      return { prompt, completion };
    });
  }

  function renderUsageLogs(logs) {
    const tbody = $('usage-body');
    tbody.innerHTML = '';
    if (!logs.length) {
      tbody.innerHTML = '<tr><td colspan="5" class="empty">No requests yet</td></tr>';
      return;
    }
    for (const log of logs) {
      const tr = document.createElement('tr');
      const when = new Date(log.created_at * 1000).toLocaleString();
      tr.innerHTML = `
        <td>${when}</td>
        <td>${log.model}</td>
        <td><code class="cluster-link" data-cluster="${log.cluster_id}">${log.cluster_id}</code></td>
        <td>${log.prompt_tokens}</td>
        <td>${log.completion_tokens}</td>`;
      tr.querySelector('.cluster-link').addEventListener('click', () => openCluster(log.cluster_id));
      tbody.appendChild(tr);
    }
  }

  async function refreshUsageTab() {
    if (!sessionToken || !accountId) return;
    try {
      const [stats, logs] = await Promise.all([
        api('/v1/accounts/' + accountId + '/usage/stats', {
          method: 'POST',
          body: JSON.stringify({ windows: last7DayWindows() }),
        }),
        api('/v1/accounts/' + accountId + '/usage/logs/latest?limit=15'),
      ]);
      renderUsageChart(stats);
      renderUsageLogs(logs);
    } catch (e) {
      handleApiError(e);
    }
  }

  function renderWorkerLogs(logs) {
    const tbody = $('worker-logs-body');
    tbody.innerHTML = '';
    if (!logs.length) {
      tbody.innerHTML = '<tr><td colspan="6" class="empty">No inference yet</td></tr>';
      return;
    }
    for (const log of logs) {
      const tr = document.createElement('tr');
      const when = new Date(log.created_at * 1000).toLocaleString();
      const effP = (log.effective_prompt === null || log.effective_prompt === undefined)
        ? '—' : log.effective_prompt.toFixed(1);
      const effC = (log.effective_completion === null || log.effective_completion === undefined)
        ? '—' : log.effective_completion.toFixed(1);
      tr.innerHTML = `
        <td>${when}</td>
        <td>${log.model}</td>
        <td><code>${log.worker_id}</code></td>
        <td><code class="cluster-link" data-cluster="${log.cluster_id}">${log.cluster_id}</code></td>
        <td>${effP}</td>
        <td>${effC}</td>`;
      const link = tr.querySelector('.cluster-link');
      if (link) link.addEventListener('click', () => openCluster(log.cluster_id));
      tbody.appendChild(tr);
    }
  }

  async function refreshWorkersTab() {
    if (!sessionToken || !accountId) return;
    try {
      const [stats, logs] = await Promise.all([
        api('/v1/accounts/' + accountId + '/worker-stats', {
          method: 'POST',
          body: JSON.stringify({ windows: last7DayWindows() }),
        }),
        api('/v1/accounts/' + accountId + '/worker-logs/latest?limit=15'),
      ]);
      renderWorkerChart(stats);
      renderWorkerLogs(logs);
    } catch (e) {
      handleApiError(e);
    }
  }

  function renderWorkers(workers) {
    const tbody = $('workers-body');
    tbody.innerHTML = '';
    if (!workers.length) {
      tbody.innerHTML = '<tr><td colspan="6" class="empty">No workers yet</td></tr>';
      return;
    }
    for (const w of workers) {
      const tr = document.createElement('tr');
      const online = w.online ? '<span class="badge online">online</span>' : '<span class="badge offline">offline</span>';
      tr.innerHTML = `
        <td><code>${w.worker_id}</code></td>
        <td>${w.model}</td>
        <td><span class="badge ${w.status}">${w.status}</span></td>
        <td>${online}</td>
        <td>${(w.memory_mb / 1024).toFixed(1)} GB</td>
        <td>${w.cluster_id ? `<code class="cluster-link" data-cluster="${w.cluster_id}">${w.cluster_id}</code>` : '—'}</td>`;
      const link = tr.querySelector('.cluster-link');
      if (link) link.addEventListener('click', () => openCluster(w.cluster_id));
      tbody.appendChild(tr);
    }
  }

  // ── cluster view ─────────────────────────────────────────────────────
  // A single-cluster "drill-down" view. It reuses the public
  // /v1/clusters/{id}/info endpoint (unauthenticated) plus the account's own
  // worker list for RAM — so no cluster-scoped auth is needed, and the page
  // degrades gracefully if the cluster is gone (404 → friendly message).

  let currentClusterId = null;

  // Fetch the worker id -> memory map once per page load (from the account
  // overview). Workers the account does not own show "—".
  let workerMemoryById = null;
  function buildWorkerMemoryMap() {
    if (overview && overview.workers) {
      workerMemoryById = {};
      for (const w of overview.workers) workerMemoryById[w.worker_id] = w.memory_mb;
    }
  }

  // Enrich the public cluster info (worker ids + layer windows) with the
  // memory each member advertises, when the account owns it.
  async function fetchClusterInfo(clusterId) {
    const info = await api('/v1/clusters/' + encodeURIComponent(clusterId) + '/info');
    if (!workerMemoryById) buildWorkerMemoryMap();
    for (const m of info.members) {
      m.memory_mb = workerMemoryById ? (workerMemoryById[m.worker_id] || null) : null;
    }
    return info;
  }

  function renderClusterInfo(info) {
    const tbody = $('cluster-body');
    tbody.innerHTML = '';
    $('cluster-id').textContent = info.cluster_id;
    if (!info.members.length) {
      tbody.innerHTML = '<tr><td colspan="5" class="empty">This cluster has no members.</td></tr>';
      return;
    }
    for (let i = 0; i < info.members.length; i++) {
      const m = info.members[i];
      const tr = document.createElement('tr');
      const layers = m.layer_window === null || m.layer_window === undefined
        ? '<span class="badge unknown">unknown</span>'
        : m.layer_window;
      const mem = m.memory_mb ? (m.memory_mb / 1024).toFixed(1) + ' GB' : '—';
      const head = i === 0 ? ' <span class="badge head">head</span>' : '';
      tr.innerHTML = `
        <td>${i}${head}</td>
        <td><code>${m.worker_id}</code></td>
        <td>${info.model}</td>
        <td>${mem}</td>
        <td>${layers}</td>`;
      tbody.appendChild(tr);
    }
  }

  async function openCluster(clusterId) {
    currentClusterId = clusterId;
    activateTab('cluster');
    const tbody = $('cluster-body');
    tbody.innerHTML = '<tr><td colspan="5" class="empty">Loading…</td></tr>';
    $('cluster-id').textContent = clusterId;
    try {
      const info = await fetchClusterInfo(clusterId);
      renderClusterInfo(info);
    } catch (e) {
      // 404: the cluster is gone (dissolved) — that's a normal, non-error state.
      if (e.status === 404) {
        tbody.innerHTML = '<tr><td colspan="5" class="empty">This cluster no longer exists (it was dissolved).</td></tr>';
        return;
      }
      handleApiError(e);
    }
  }

  // Render the API keys table filtered by scope: worker keys here, user keys
  // under the inference usage tab.
  function renderKeys(keys, scope = null) {
    const tbody = $('keys-body');
    tbody.innerHTML = '';
    const list = scope ? keys.filter((k) => k.scope === scope) : keys;
    if (!list.length) {
      tbody.innerHTML = '<tr><td colspan="4" class="empty">No keys yet</td></tr>';
      return;
    }
    for (const k of list) {
      const tr = document.createElement('tr');
      tr.innerHTML = `<td><code>${k.key_id}</code></td><td>${k.name}</td><td><span class="badge ${k.scope}">${k.scope}</span></td><td><button type="button" class="revoke-btn" data-key="${k.key_id}" data-name="${k.name}">revoke</button></td>`;
      tr.querySelector('.revoke-btn').addEventListener('click', () => revokeKey(k.key_id, k.name));
      tbody.appendChild(tr);
    }
  }

  async function revokeKey(keyId, name) {
    if (!window.confirm('Revoke API key ' + name + '? This cannot be undone.')) return;
    try {
      await api('/v1/accounts/' + accountId + '/keys/' + keyId, { method: 'DELETE' });
      setStatus('key revoked', 'ok');
      await refresh();
    } catch (err) {
      handleApiError(err);
    }
  }

  async function register(username, password) {
    await api('/v1/accounts/register', {
      method: 'POST',
      body: JSON.stringify({ username, password }),
    });
    // Account created. For now, return to the login form (auto-login later).
    $('register').hidden = true;
    $('login').hidden = false;
    $('login-username').value = username;
    $('login-password').value = '';
    $('login-password').focus();
    setStatus('account created — log in to continue', 'ok');
  }

  async function createKey(name, scope) {
    const key = await api('/v1/accounts/' + accountId + '/keys', {
      method: 'POST',
      body: JSON.stringify({ name, scope }),
    });
    // Show the plaintext secret once (the server only returns it on creation).
    $('key-secret').textContent = key.api_key;
    $('key-created').hidden = false;
    await refresh();
  }

  function showAuth(view) {
    $('login').hidden = view !== 'login';
    $('register').hidden = view !== 'register';
  }

  // ── account tab ──────────────────────────────────────────────────────
  function renderAccount() {
    if (!overview) return;
    $('account-username').textContent = overview.username || '—';
    $('account-id').textContent = overview.account_id || accountId || '—';
  }

  async function changePassword(current, next) {
    await api('/v1/accounts/' + accountId + '/password', {
      method: 'POST',
      body: JSON.stringify({ current_password: current, new_password: next }),
    });
  }

  function setFormMessage(text, cls) {
    const el = $('cp-message');
    el.textContent = text;
    el.className = 'form-message ' + (cls || '');
    el.hidden = false;
  }

  // ── admin tab ───────────────────────────────────────────────────────
  // Escape user-controlled strings before injecting into innerHTML. Usernames
  // (and key names) are arbitrary user input, so this prevents stored XSS in
  // the admin accounts table.
  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, (c) => ({
      '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
    }[c]));
  }

  function badge(on) {
    return on
      ? '<span class="badge online">yes</span>'
      : '<span class="badge offline">no</span>';
  }

  function renderAdminAccounts(accounts) {
    const tbody = $('admin-accounts-body');
    tbody.innerHTML = '';
    if (!accounts.length) {
      tbody.innerHTML = '<tr><td colspan="6" class="empty">No accounts</td></tr>';
      return;
    }
    for (const a of accounts) {
      const tr = document.createElement('tr');
      tr.innerHTML = `
        <td>${escapeHtml(a.username)}</td>
        <td>${badge(a.is_admin)}</td>
        <td>${badge(a.can_work)}</td>
        <td>${badge(a.can_use)}</td>
        <td>${badge(a.banned)}</td>
        <td>
          <button type="button" class="admin-toggle" data-field="is_admin" data-account="${a.account_id}" data-val="${a.is_admin}">${a.is_admin ? 'demote' : 'promote'}</button>
          <button type="button" class="admin-toggle" data-field="can_work" data-account="${a.account_id}" data-val="${a.can_work}">${a.can_work ? 'revoke work' : 'allow work'}</button>
          <button type="button" class="admin-toggle" data-field="can_use" data-account="${a.account_id}" data-val="${a.can_use}">${a.can_use ? 'revoke use' : 'allow use'}</button>
          <button type="button" class="admin-toggle" data-field="banned" data-account="${a.account_id}" data-val="${a.banned}">${a.banned ? 'unban' : 'ban'}</button>
        </td>`;
      tr.querySelectorAll('.admin-toggle').forEach((btn) => {
        btn.addEventListener('click', () => {
          const field = btn.dataset.field;
          const next = btn.dataset.val !== 'true';
          toggleAccountPermission(btn.dataset.account, field, next);
        });
      });
      tbody.appendChild(tr);
    }
  }

  async function refreshAdminTab() {
    if (!overview || !overview.is_admin) return;
    try {
      const [perm, accounts] = await Promise.all([
        api('/v1/admin/permissions'),
        api('/v1/admin/accounts'),
      ]);
      $('admin-work-perm').textContent = perm.work_permissionless ? 'true' : 'false';
      $('admin-use-perm').textContent = perm.use_permissionless ? 'true' : 'false';
      renderAdminAccounts(accounts);
    } catch (e) {
      handleApiError(e);
    }
  }

  async function toggleAccountPermission(accountId, field, value) {
    const body = {};
    body[field] = value;
    try {
      await api('/v1/admin/accounts/' + accountId, {
        method: 'PATCH',
        body: JSON.stringify(body),
      });
      setAdminMessage('Updated.', 'ok');
      await refreshAdminTab();
    } catch (e) {
      setAdminMessage(e.message, 'err');
      handleApiError(e);
    }
  }

  function setAdminMessage(text, cls) {
    const el = $('admin-message');
    el.textContent = text;
    el.className = 'form-message ' + (cls || '');
    el.hidden = false;
  }

  // ── tabs ─────────────────────────────────────────────────────────────
  function activateTab(name) {
    document.querySelectorAll('.tab-btn').forEach((b) => {
      b.classList.toggle('active', b.dataset.tab === name);
    });
    for (const id of ['overview', 'usage', 'workers', 'keys', 'account', 'admin', 'cluster']) {
      $('tab-' + id).hidden = id !== name;
    }
    if (name === 'keys') renderKeys(overview ? overview.keys : []);
    if (name === 'usage') refreshUsageTab();
    if (name === 'workers') refreshWorkersTab();
    if (name === 'account') renderAccount();
    if (name === 'admin') refreshAdminTab();
  }

  document.querySelectorAll('.tab-btn').forEach((btn) => {
    btn.addEventListener('click', () => activateTab(btn.dataset.tab));
  });

  $('cluster-back-btn').addEventListener('click', () => activateTab('workers'));

  function show(view) { showAuth(view); }

  $('show-register').addEventListener('click', () => {
    setStatus('');
    show('register');
    $('register-username').focus();
  });

  $('show-login').addEventListener('click', () => {
    setStatus('');
    show('login');
    $('login-username').focus();
  });

  $('login-form').addEventListener('submit', async (e) => {
    e.preventDefault();
    const username = $('login-username').value.trim();
    const password = $('login-password').value;
    try {
      await login(username, password);
    } catch (err) {
      handleApiError(err);
    }
  });

  $('register-form').addEventListener('submit', async (e) => {
    e.preventDefault();
    const username = $('register-username').value.trim();
    const password = $('register-password').value;
    try {
      await register(username, password);
    } catch (err) {
      handleApiError(err);
    }
  });

  $('create-key-form').addEventListener('submit', async (e) => {
    e.preventDefault();
    const name = $('key-name').value.trim();
    const scope = $('key-scope').value;
    try {
      await createKey(name, scope);
      $('key-name').value = '';
      setStatus('key created', 'ok');
    } catch (err) {
      handleApiError(err);
    }
  });

  $('change-password-form').addEventListener('submit', async (e) => {
    e.preventDefault();
    const current = $('cp-current').value;
    const next = $('cp-new').value;
    const confirm = $('cp-confirm').value;
    if (next !== confirm) {
      setFormMessage('New passwords do not match.', 'err');
      return;
    }
    try {
      await changePassword(current, next);
      $('cp-current').value = '';
      $('cp-new').value = '';
      $('cp-confirm').value = '';
      setFormMessage('Password changed.', 'ok');
      setStatus('password changed', 'ok');
    } catch (err) {
      setFormMessage(err.message, 'err');
      handleApiError(err);
    }
  });

  $('logout-btn').addEventListener('click', () => {
    clearSession();
    $('view').hidden = true;
    $('logout-btn').hidden = true;
    $('login-password').value = '';
    show('login');
    $('login-username').focus();
    setStatus('logged out');
  });

  // Restore a persisted session (if any) so a refresh keeps the user logged in.
  if (restoreSession()) {
    $('login').hidden = true;
    $('view').hidden = false;
    $('logout-btn').hidden = false;
    activateTab('overview');
    setStatus('connected', 'ok');
    refresh();
  } else {
    $('logout-btn').hidden = true;
    setStatus('logged out');
  }
})();