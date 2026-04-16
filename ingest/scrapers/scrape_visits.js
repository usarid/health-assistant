/**
 * MyChart Visits Scraper (v2)
 * ===========================
 * Run this while on the Visits page.
 *
 * ONLY follows MSKCC-native visit links.
 * Skips any visits from linked institutions (UCSF, Mayo, Sutter, etc.)
 * which would redirect away and break the script.
 *
 * Paste in DevTools Console and wait. Auto-downloads when done.
 */

(async function() {
  'use strict';

  const log = (msg) => console.log(`%c[Visits] ${msg}`, 'color: #4CAF50; font-weight: bold');
  const wait = (ms) => new Promise(r => setTimeout(r, ms || 500));

  log('Starting visit extraction...');

  const MSKCC_ORIGIN = window.location.origin;

  // ===== Step 1: Capture the full visits list =====
  log('\nStep 1: Capturing visits list...');

  // Scroll to load all visits
  let prevHeight = 0;
  let stableCount = 0;
  for (let i = 0; i < 50; i++) {
    window.scrollTo(0, document.body.scrollHeight);
    await wait(800);
    const newHeight = document.body.scrollHeight;
    if (newHeight === prevHeight) {
      stableCount++;
      if (stableCount >= 3) break;
    } else {
      stableCount = 0;
      log(`  Scrolling... page height: ${newHeight}`);
    }
    prevHeight = newHeight;
  }
  window.scrollTo(0, 0);
  await wait(300);

  const fullPageText = document.body.innerText;

  // ===== Step 2: Find MSKCC-only visit links =====
  log('\nStep 2: Finding MSKCC visit detail links...');

  const allLinks = Array.from(document.querySelectorAll('a[href]'));
  const seenHrefs = new Set();
  const mskccVisitLinks = [];
  const externalVisitLinks = [];

  for (const a of allLinks) {
    const href = a.href;
    if (seenHrefs.has(href)) continue;
    seenHrefs.add(href);

    const text = a.textContent.trim();
    if (text.length < 3 || text.length > 500) continue;

    // Check if it's a visit-related link
    const path = new URL(href).pathname;
    const isVisit = path.includes('Visit') || path.includes('visit')
                  || path.includes('Appointment') || path.includes('appointment')
                  || path.includes('Encounter') || path.includes('encounter');
    if (!isVisit) continue;

    const parentText = a.closest('li, tr, div[class], article, section')
      ?.textContent?.trim()?.replace(/\s+/g, ' ')?.substring(0, 500) || '';

    const entry = { href, text: text.substring(0, 300), parentText };

    if (href.startsWith(MSKCC_ORIGIN)) {
      mskccVisitLinks.push(entry);
    } else {
      externalVisitLinks.push(entry);
    }
  }

  log(`MSKCC visit links: ${mskccVisitLinks.length}`);
  log(`External visit links (skipped): ${externalVisitLinks.length}`);
  if (externalVisitLinks.length > 0) {
    log(`  External institutions: ${[...new Set(externalVisitLinks.map(l => {
      try { return new URL(l.href).hostname; } catch(e) { return '?'; }
    }))].join(', ')}`);
  }

  // ===== Step 3: Fetch MSKCC visit details =====
  log('\nStep 3: Fetching MSKCC visit details...');

  const visitDetails = [];
  const errors = [];

  for (let i = 0; i < mskccVisitLinks.length; i++) {
    const link = mskccVisitLinks[i];
    const shortText = link.text.substring(0, 50);

    if ((i + 1) % 5 === 0 || i === 0) {
      log(`  [${i + 1}/${mskccVisitLinks.length}] ${shortText}...`);
    }

    try {
      const resp = await fetch(link.href, {
        credentials: 'include',
        redirect: 'manual',
        headers: { 'Accept': 'text/html' }
      });

      if (resp.type === 'opaqueredirect' || (resp.status >= 300 && resp.status < 400)) {
        log(`    Skipped (redirect): ${shortText}`);
        continue;
      }
      if (!resp.ok) {
        errors.push({ href: link.href, status: resp.status });
        continue;
      }

      const html = await resp.text();
      if (!html.includes('MyChart') && !html.includes('mskcc')) {
        log(`    Skipped (not MSKCC content): ${shortText}`);
        continue;
      }

      const parser = new DOMParser();
      const doc = parser.parseFromString(html, 'text/html');

      const detail = {
        linkText: link.text,
        href: link.href,
        listContext: link.parentText,
        pageTitle: doc.title || '',
        fullText: doc.body?.innerText?.trim()?.substring(0, 30000) || '',
        sections: {},
        tables: [],
      };

      // Extract sections by headers
      doc.querySelectorAll('h1, h2, h3, h4').forEach(h => {
        let content = '';
        let sibling = h.nextElementSibling;
        while (sibling && !sibling.matches('h1, h2, h3, h4')) {
          content += sibling.textContent.trim() + '\n';
          sibling = sibling.nextElementSibling;
        }
        if (content.trim()) {
          detail.sections[h.textContent.trim()] = content.trim().substring(0, 5000);
        }
      });

      // Extract tables (often contain visit details, diagnoses, procedures)
      doc.querySelectorAll('table').forEach(table => {
        const rows = [];
        table.querySelectorAll('tr').forEach(tr => {
          const cells = Array.from(tr.querySelectorAll('td, th')).map(c => c.textContent.trim());
          if (cells.some(c => c.length > 0)) rows.push(cells);
        });
        if (rows.length > 0) detail.tables.push(rows);
      });

      visitDetails.push(detail);
    } catch(e) {
      errors.push({ href: link.href, error: e.message });
    }

    await wait(300);
  }

  log(`\nFetched ${visitDetails.length} visit details (${errors.length} errors/skipped)`);

  // ===== Step 4: Download =====
  const output = {
    extractedAt: new Date().toISOString(),
    source: 'MSKCC MyChart Visits',
    sourceUrl: window.location.href,
    stats: {
      mskccLinks: mskccVisitLinks.length,
      externalLinks: externalVisitLinks.length,
      detailsFetched: visitDetails.length,
      errors: errors.length,
    },
    mskccVisitLinks: mskccVisitLinks,
    externalVisitLinks: externalVisitLinks,
    visitDetails: visitDetails,
    errors: errors,
    pageText: fullPageText.substring(0, 200000),
  };

  const jsonStr = JSON.stringify(output, null, 2);
  const sizeMB = (jsonStr.length / 1048576).toFixed(1);

  console.log('%c\n=== VISIT EXTRACTION COMPLETE ===', 'color: #4CAF50; font-weight: bold; font-size: 14px');
  log(`MSKCC visits: ${mskccVisitLinks.length}`);
  log(`External visits (captured text only): ${externalVisitLinks.length}`);
  log(`Details fetched: ${visitDetails.length}`);
  log(`Output size: ${sizeMB} MB`);

  const blob = new Blob([jsonStr], { type: 'application/json' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = `mskcc_visits_${new Date().toISOString().slice(0,10)}.json`;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  URL.revokeObjectURL(url);
  log('✓ Downloaded!');

  window.__visits = output;
  log('Data also in window.__visits');

})();
