'use strict';

// Must match API_BASE in background.js (your deployed backend URL, no trailing slash)
const API_BASE = 'https://lightspeed-extension-production.up.railway.app';

function updateConfigureLink(connectionId) {
  const el = document.getElementById('configure-link');
  if (connectionId && el) {
    const url = API_BASE + '/settings?key=' + encodeURIComponent(connectionId);
    el.innerHTML = '<a href="' + url + '" target="_blank" rel="noopener">Configure which fields to export</a>';
  } else if (el) {
    el.textContent = '';
  }
}

function loadConnectionInfo(connectionId) {
  const currentEl = document.getElementById('base-url-current');
  const inputEl = document.getElementById('base-url');
  const statusEl = document.getElementById('base-url-status');
  if (!connectionId || !currentEl || !inputEl) return;
  currentEl.textContent = 'Loading…';
  inputEl.value = '';
  statusEl.textContent = '';
  fetch(API_BASE + '/api/connection-info?key=' + encodeURIComponent(connectionId))
    .then(function (r) { return r.json(); })
    .then(function (data) {
      if (data.airtable_base_url) {
        currentEl.textContent = 'Current: ' + data.airtable_base_url;
        inputEl.placeholder = data.airtable_base_url;
      } else {
        currentEl.textContent = 'No base set.';
      }
    })
    .catch(function () {
      currentEl.textContent = 'Could not load.';
    });
}

function showConnectedState(connectionId) {
  const setup = document.getElementById('setup-block');
  const connected = document.getElementById('connected-block');
  if (!setup || !connected) return;
  if (connectionId) {
    setup.hidden = true;
    connected.hidden = false;
    updateConfigureLink(connectionId);
    loadConnectionInfo(connectionId);
    loadSharedKeys();
  } else {
    setup.hidden = false;
    connected.hidden = true;
  }
}

function loadSharedKeys() {
  const sel = document.getElementById('shared-key-select');
  if (!sel) return;
  sel.innerHTML = '<option value="">Loading…</option>';
  fetch(API_BASE + '/api/shared-keys')
    .then(function (r) { return r.json(); })
    .then(function (data) {
      sel.innerHTML = '<option value="">— Choose a key —</option>';
      (data.shared_keys || []).forEach(function (k) {
        var opt = document.createElement('option');
        opt.value = k.id;
        opt.textContent = k.label || k.id;
        sel.appendChild(opt);
      });
    })
    .catch(function () {
      sel.innerHTML = '<option value="">Failed to load</option>';
    });
}

document.getElementById('shared-key-unlock').addEventListener('click', function () {
  var sel = document.getElementById('shared-key-select');
  var pwd = (document.getElementById('shared-key-password').value || '').trim();
  var status = document.getElementById('shared-key-status');
  var id = (sel && sel.value) || '';
  if (!id) {
    status.textContent = 'Choose a key from the list.';
    status.className = 'muted';
    return;
  }
  if (!pwd) {
    status.textContent = 'Enter the password for this key.';
    status.className = 'muted';
    return;
  }
  chrome.storage.sync.get(['connection_id'], function (data) {
    var connectionId = (data && data.connection_id) ? data.connection_id.trim() : '';
    if (!connectionId) {
      status.textContent = 'Save your connection key first.';
      status.className = 'muted';
      return;
    }
    status.textContent = 'Unlocking…';
    status.className = 'muted';
    fetch(API_BASE + '/api/shared-keys/' + encodeURIComponent(id) + '/unlock', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ password: pwd, connection_id: connectionId })
    })
      .then(function (r) { return r.json(); })
      .then(function (res) {
        if (res.success) {
          status.textContent = 'Unlocked. Exports will use this store key.';
          status.className = 'saved';
          document.getElementById('shared-key-password').value = '';
        } else {
          status.textContent = res.error || 'Unlock failed.';
          status.className = 'muted';
        }
      })
      .catch(function () {
        status.textContent = 'Request failed.';
        status.className = 'muted';
      });
  });
});

document.getElementById('get-started').onclick = function () {
  chrome.tabs.create({ url: API_BASE + '/connect' });
};

var reconnectEl = document.getElementById('reconnect-link');
if (reconnectEl) reconnectEl.addEventListener('click', function (e) {
  e.preventDefault();
  chrome.tabs.create({ url: API_BASE + '/connect' });
});

document.getElementById('base-url-save').addEventListener('click', function () {
  var input = document.getElementById('base-url');
  var status = document.getElementById('base-url-status');
  var url = (input && input.value || '').trim();
  if (!url) {
    status.textContent = 'Enter a base URL to change it.';
    status.className = 'muted';
    return;
  }
  chrome.storage.sync.get(['connection_id'], function (data) {
    var connectionId = (data && data.connection_id) ? data.connection_id.trim() : '';
    if (!connectionId) {
      status.textContent = 'Not connected.';
      status.className = 'muted';
      return;
    }
    status.textContent = 'Updating…';
    status.className = 'muted';
    fetch(API_BASE + '/api/connection/update-base', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ connection_id: connectionId, airtable_base_url: url })
    })
      .then(function (r) { return r.json(); })
      .then(function (res) {
        if (res.success) {
          status.textContent = 'Base updated.';
          status.className = 'saved';
          document.getElementById('base-url-current').textContent = 'Current: ' + url;
          input.value = '';
        } else {
          status.textContent = res.error || 'Update failed.';
          status.className = 'muted';
        }
      })
      .catch(function () {
        status.textContent = 'Request failed.';
        status.className = 'muted';
      });
  });
});

chrome.storage.sync.get(['connection_id'], function (data) {
  const key = (data && data.connection_id) ? data.connection_id : '';
  if (key) {
    showConnectedState(key);
  } else {
    showConnectedState('');
  }
});
