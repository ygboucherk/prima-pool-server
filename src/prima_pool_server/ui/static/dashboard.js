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
    return resp.json();
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
      renderWorkers(data.workers);
      renderKeys(data.keys);
      refreshUsageTab();
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

  function renderUsageChart(stats) {
    const chart = $('usage-chart');
    const empty = $('usage-chart-empty');
    chart.innerHTML = '';
    const windows = last7DayWindows();
    // stats is an array of per-window objects {model: {requests, prompt_tokens, completion_tokens}}.
    const totals = windows.map((w, i) => {
      const win = stats[i] || {};
      let prompt = 0, completion = 0;
      for (const m of Object.values(win)) {
        prompt += m.prompt_tokens || 0;
        completion += m.completion_tokens || 0;
      }
      return { begin: w[0], prompt, completion };
    });
    const max = Math.max(1, ...totals.map((t) => t.prompt + t.completion));
    const any = totals.some((t) => t.prompt + t.completion > 0);
    empty.hidden = any;
    chart.hidden = !any;

    // Vertical axis: 4 evenly-spaced ticks from 0 to max.
    const axis = document.createElement('div');
    axis.className = 'usage-axis';
    const ticks = 4;
    for (let i = 0; i <= ticks; i++) {
      const value = Math.round(max * i / ticks);
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
      inner.style.height = (total / max * 100) + '%';
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
        <td><code>${log.cluster_id}</code></td>
        <td>${log.prompt_tokens}</td>
        <td>${log.completion_tokens}</td>`;
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
        <td>${w.cluster_id ? `<code>${w.cluster_id}</code>` : '—'}</td>`;
      tbody.appendChild(tr);
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

  // ── tabs ─────────────────────────────────────────────────────────────
  function activateTab(name) {
    document.querySelectorAll('.tab-btn').forEach((b) => {
      b.classList.toggle('active', b.dataset.tab === name);
    });
    for (const id of ['overview', 'usage', 'workers', 'keys']) {
      $('tab-' + id).hidden = id !== name;
    }
    if (name === 'keys') renderKeys(overview ? overview.keys : []);
    if (name === 'usage') refreshUsageTab();
  }

  document.querySelectorAll('.tab-btn').forEach((btn) => {
    btn.addEventListener('click', () => activateTab(btn.dataset.tab));
  });

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