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
      $('stat-account').textContent = data.username || data.account_id;
      $('stat-workers').textContent = data.workers.length;
      $('stat-online').textContent = data.workers.filter((w) => w.online).length;
      $('stat-keys').textContent = data.keys.length;
      renderWorkers(data.workers);
      renderKeys(data.keys);
      setStatus('connected', 'ok');
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

  function renderKeys(keys) {
    const tbody = $('keys-body');
    tbody.innerHTML = '';
    if (!keys.length) {
      tbody.innerHTML = '<tr><td colspan="3" class="empty">No API keys</td></tr>';
      return;
    }
    for (const k of keys) {
      const tr = document.createElement('tr');
      tr.innerHTML = `<td><code>${k.key_id}</code></td><td>${k.name}</td><td><span class="badge ${k.scope}">${k.scope}</span></td>`;
      tbody.appendChild(tr);
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

  function show(view) {
    $('login').hidden = view !== 'login';
    $('register').hidden = view !== 'register';
  }

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
    setStatus('connected', 'ok');
    refresh();
  } else {
    $('logout-btn').hidden = true;
    setStatus('logged out');
  }
})();