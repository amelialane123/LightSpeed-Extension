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
  } else {
    setup.hidden = false;
    connected.hidden = true;
  }
}

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
