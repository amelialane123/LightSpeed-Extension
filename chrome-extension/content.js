(function () {
  'use strict';

  function isItemSearchPage() {
    const href = (window.location.href || '').toLowerCase();
    return href.includes('item.listings') || href.includes('form_name=listing');
  }

  function getCategoryFromPage() {
    const url = window.location.href;
    const parsed = new URL(url);

    // URL search params (common names)
    const paramNames = ['category', 'categoryId', 'categoryID', 'category_id'];
    for (const name of paramNames) {
      const val = parsed.searchParams.get(name);
      if (val && /^\d+$/.test(val)) return val;
    }

    // Hash-based routes, e.g. #/inventory/category/639 or #category=639
    const hash = parsed.hash || '';
    const hashMatch = hash.match(/category[\/=](\d+)/i) || hash.match(/\/(\d+)(?:\?|$)/);
    if (hashMatch) return hashMatch[1];

    // Path segments, e.g. /category/639 or /categories/639
    const pathMatch = parsed.pathname.match(/categor(?:y|ies)[\/](\d+)/i);
    if (pathMatch) return pathMatch[1];

    // DOM: look for data attributes or selected filter
    const dataEl = document.querySelector('[data-category-id], [data-categoryid]');
    if (dataEl) {
      const id = dataEl.getAttribute('data-category-id') || dataEl.getAttribute('data-categoryid');
      if (id && /^\d+$/.test(id)) return id;
    }

    // Select/dropdown that might be a category filter
    const select = document.querySelector('select[name*="category" i], select[id*="category" i]');
    if (select && select.value && /^\d+$/.test(select.value)) return select.value;

    return null;
  }

  function runExportAndOpenAirtable(btn) {
    const categoryId = getCategoryFromPage() || 'ALL';
    const origText = btn.textContent;
    btn.disabled = true;
    btn.textContent = 'Exporting…';

    chrome.runtime.sendMessage({ action: 'runExport', categoryId: categoryId }, function (res) {
      if (res && !res.ok) {
        var msg = res.error || 'Export failed';
        if (msg.includes('Reconnect') || msg.includes('reconnect')) {
          msg = "Lightspeed sign-in expired or was revoked. We've opened the reconnect page—complete the steps there to sign in again, then try exporting again.";
        } else if (res.output && res.output.trim()) {
          var out = res.output.trim();
          if (out.length > 500) out = out.slice(-500);
          msg += '\n\n' + out;
        }
        alert(msg);
      }
      btn.textContent = origText;
      btn.disabled = false;
    });
  }

  function openGallery() {
    const categoryId = getCategoryFromPage() || 'ALL';
    chrome.runtime.sendMessage({ action: 'openGallery', categoryId: categoryId }, function (res) {
      if (res && !res.ok && res.error) {
        alert(res.error);
      }
    });
  }

  function updateButton() {
    var wrap = document.getElementById('ls-airtable-export-wrap');
    if (isItemSearchPage()) {
      if (wrap) return; /* already shown */
      var categoryId = getCategoryFromPage();
      var label = categoryId ? 'Export this category to Airtable' : 'Export to Airtable (all categories)';
      var btn = document.createElement('button');
      btn.type = 'button';
      btn.id = 'ls-airtable-export-btn';
      btn.title = label;
      btn.textContent = 'Export to Airtable';
      btn.addEventListener('click', function () { runExportAndOpenAirtable(btn); });
      var galleryBtn = document.createElement('button');
      galleryBtn.type = 'button';
      galleryBtn.id = 'ls-gallery-export-btn';
      galleryBtn.title = categoryId ? 'View this category as printable gallery (PDF-friendly)' : 'View all items as printable gallery';
      galleryBtn.textContent = 'View as gallery';
      galleryBtn.addEventListener('click', function () { openGallery(); });
      var wrapper = document.createElement('div');
      wrapper.id = 'ls-airtable-export-wrap';
      wrapper.appendChild(btn);
      wrapper.appendChild(galleryBtn);
      document.body.appendChild(wrapper);
    } else {
      if (wrap) wrap.remove();
    }
  }

  /* Run on load */
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', updateButton);
  } else {
    updateButton();
  }

  /* SPA: detect URL changes (Lightspeed doesn't full-reload on navigation) */
  var lastHref = window.location.href;
  setInterval(function () {
    if (window.location.href !== lastHref) {
      lastHref = window.location.href;
      updateButton();
    }
  }, 500);
})();
