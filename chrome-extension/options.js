'use strict';

// Must match API_BASE in background.js (your deployed backend URL, no trailing slash)
const API_BASE = 'https://lightspeed-extension-production.up.railway.app';

document.getElementById('save').onclick = function () {
  const key = (document.getElementById('key').value || '').trim();
  const status = document.getElementById('status');
  if (!key) {
    status.textContent = 'Enter a connection key.';
    status.className = 'muted';
    return;
  }
  chrome.storage.sync.set({ connection_id: key }, function () {
    status.textContent = 'Saved. You can use the Export to Airtable button on Lightspeed.';
    status.className = 'saved';
    updateConfigureLink(key);
    showConnectedState(key);
  });
};

function updateConfigureLink(connectionId) {
  const el = document.getElementById('configure-link');
  if (connectionId) {
    const url = API_BASE + '/settings?key=' + encodeURIComponent(connectionId);
    el.innerHTML = '<a href="' + url + '" target="_blank" rel="noopener">Configure which fields to export</a>';
  } else {
    el.textContent = '';
  }
}

function showConnectedState(connectionId) {
  const setup = document.getElementById('setup-block');
  const connected = document.getElementById('connected-block');
  if (!setup || !connected) return;
  if (connectionId) {
    setup.hidden = true;
    connected.hidden = false;
    document.getElementById('key').value = connectionId;
    updateConfigureLink(connectionId);
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

chrome.storage.sync.get(['connection_id'], function (data) {
  const key = (data && data.connection_id) ? data.connection_id : '';
  if (key) {
    document.getElementById('key').value = key;
    showConnectedState(key);
  } else {
    showConnectedState('');
  }
});
