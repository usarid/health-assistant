/**
 * Scrape Current Page
 * ===================
 * Navigate to any MyChart page (Visits, Messages, etc.), then paste this.
 * It captures everything visible on the current page.
 *
 * Usage:
 *   1. Click "Visits" in MyChart
 *   2. Wait for page to fully load
 *   3. Paste this script in DevTools console
 *   4. Repeat for Messages, Test Results, Medications, etc.
 *
 * Each run appends to window.__scrapedPages and auto-downloads.
 */

(function() {
  'use strict';

  const log = (msg) => console.log(`%c[Page Scraper] ${msg}`, 'color: #FF9800; font-weight: bold');

  // Initialize accumulator
  if (!window.__scrapedPages) window.__scrapedPages = [];

  const pageData = {
    url: window.location.href,
    title: document.title,
    scrapedAt: new Date().toISOString(),
    tables: [],
    lists: [],
    sections: [],
    links: [],
    allText: '',
    embeddedJson: [],
  };

  // 1. Extract all tables
  document.querySelectorAll('table').forEach((table, idx) => {
    const headers = Array.from(table.querySelectorAll('thead th, thead td'))
      .map(th => th.textContent.trim());
    const rows = [];
    table.querySelectorAll('tbody tr').forEach(tr => {
      const cells = Array.from(tr.querySelectorAll('td, th'))
        .map(td => td.textContent.trim());
      if (cells.some(c => c.length > 0)) {
        rows.push(cells);
      }
      // Also capture any links in the row
      const link = tr.querySelector('a[href]');
      if (link) {
        rows[rows.length - 1] = {
          cells: cells,
          href: link.href,
          linkText: link.textContent.trim(),
        };
      }
    });
    if (rows.length > 0) {
      pageData.tables.push({ headers, rows, tableIndex: idx });
      log(`Table ${idx}: ${headers.join(' | ')} — ${rows.length} rows`);
    }
  });

  // 2. Extract all lists
  document.querySelectorAll('ul, ol').forEach((list, idx) => {
    const items = Array.from(list.querySelectorAll(':scope > li'))
      .map(li => ({
        text: li.textContent.trim().replace(/\s+/g, ' ').substring(0, 500),
        href: li.querySelector('a[href]')?.href || null,
      }))
      .filter(item => item.text.length > 0);
    if (items.length > 0) {
      pageData.lists.push({ items, listIndex: idx });
    }
  });

  // 3. Extract content sections (headers + content)
  document.querySelectorAll('h1, h2, h3, h4').forEach(h => {
    let content = '';
    let sibling = h.nextElementSibling;
    while (sibling && !sibling.matches('h1, h2, h3, h4')) {
      content += sibling.textContent.trim() + '\n';
      sibling = sibling.nextElementSibling;
    }
    if (content.trim()) {
      pageData.sections.push({
        heading: h.textContent.trim(),
        level: h.tagName,
        content: content.trim().substring(0, 2000),
      });
    }
  });

  // 4. Extract all meaningful links
  document.querySelectorAll('a[href]').forEach(a => {
    const href = a.href;
    const text = a.textContent.trim();
    if (text && href && href.includes('/MyChart/') && text.length > 1) {
      pageData.links.push({ text, href });
    }
  });

  // 5. Extract any embedded JSON data from scripts
  document.querySelectorAll('script:not([src])').forEach(script => {
    const text = script.textContent;
    // Look for JSON assignments
    const patterns = [
      /window\.__INITIAL_STATE__\s*=\s*({[\s\S]*?});/,
      /window\.__DATA__\s*=\s*({[\s\S]*?});/,
      /var\s+(?:model|data|viewModel|pageData)\s*=\s*({[\s\S]*?});/,
      /JSON\.parse\('([\s\S]*?)'\)/,
    ];
    for (const pat of patterns) {
      const m = text.match(pat);
      if (m) {
        try {
          const json = JSON.parse(m[1].replace(/\\'/g, "'"));
          pageData.embeddedJson.push(json);
          log(`Found embedded JSON data in script`);
          console.log(json);
        } catch(e) {}
      }
    }
  });

  // 6. Get all text from main content area
  const mainEl = document.querySelector('main, [role="main"], .content-area, #content, .main-content')
    || document.querySelector('body');
  pageData.allText = mainEl.textContent.trim().replace(/\s{3,}/g, '\n').substring(0, 20000);

  // Store
  window.__scrapedPages.push(pageData);
  const pageCount = window.__scrapedPages.length;

  // Summary
  console.log(`%c\n=== Page ${pageCount}: ${pageData.title} ===`, 'color: #FF9800; font-weight: bold; font-size: 14px');
  log(`URL: ${pageData.url}`);
  log(`Tables: ${pageData.tables.length} (${pageData.tables.reduce((s,t) => s + (Array.isArray(t.rows[0]) ? t.rows.length : t.rows.length), 0)} total rows)`);
  log(`Lists: ${pageData.lists.length}`);
  log(`Sections: ${pageData.sections.length}`);
  log(`Links: ${pageData.links.length}`);
  log(`Embedded JSON: ${pageData.embeddedJson.length}`);
  log(`Text length: ${pageData.allText.length} chars`);

  // Auto-download accumulated data
  const blob = new Blob([JSON.stringify(window.__scrapedPages, null, 2)], { type: 'application/json' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = `mskcc_pages_scraped_${pageCount}.json`;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  URL.revokeObjectURL(url);

  log(`\n✓ Downloaded (${pageCount} pages total)`);
  log(`Navigate to another page and run this again to accumulate more data.`);
  log(`All data in: window.__scrapedPages`);

})();
