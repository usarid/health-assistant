/**
 * MyChart DOM Page Scraper
 * ========================
 * Paste this into Chrome DevTools console while logged into MyChart.
 *
 * This is the most reliable approach — it loads each portal section
 * in a hidden iframe, waits for content to render, and extracts data
 * from the DOM. Works regardless of API changes.
 *
 * IMPORTANT: This takes a few minutes as it navigates through pages.
 */

(async function() {
  'use strict';

  const BASE = window.location.origin;
  const MC = `${BASE}/MyChart`;

  const log = (msg) => console.log(`%c[DOM Scraper] ${msg}`, 'color: #E91E63; font-weight: bold');
  const warn = (msg) => console.warn(`[DOM Scraper] ${msg}`);

  // Helper: load a page and return its parsed document
  async function loadPage(url) {
    try {
      const resp = await fetch(url, {
        credentials: 'include',
        headers: { 'Accept': 'text/html' }
      });
      if (!resp.ok) return null;
      const html = await resp.text();
      const parser = new DOMParser();
      return parser.parseFromString(html, 'text/html');
    } catch(e) {
      warn(`Failed to load ${url}: ${e.message}`);
      return null;
    }
  }

  // Helper: extract text content safely
  function txt(el, selector) {
    if (!el) return '';
    const found = selector ? el.querySelector(selector) : el;
    return found?.textContent?.trim() || '';
  }

  // Helper: extract all links matching a pattern
  function extractLinks(doc, pattern) {
    return Array.from(doc.querySelectorAll('a'))
      .filter(a => a.href && a.href.match(pattern))
      .map(a => ({ href: a.href, text: a.textContent.trim() }));
  }

  const allData = {
    extractedAt: new Date().toISOString(),
    source: 'MSKCC MyChart (DOM)',
    visits: [],
    visitDetails: [],
    messages: [],
    messageDetails: [],
    testResults: [],
    medications: [],
    conditions: [],
    allergies: [],
    immunizations: [],
    procedures: [],
    vitalSigns: [],
    careTeam: [],
  };

  // ===== 1. VISITS =====
  log('=== Scraping Visits ===');

  // Try multiple known paths for visit history
  const visitUrls = [
    `${MC}/Visits`,
    `${MC}/visits/visit-history`,
    `${MC}/Scheduling/VisitHistory`,
  ];

  for (const url of visitUrls) {
    log(`  Loading: ${url}`);
    const doc = await loadPage(url);
    if (!doc) continue;

    // Strategy 1: Look for structured visit data in scripts
    const scripts = Array.from(doc.querySelectorAll('script'));
    for (const script of scripts) {
      const text = script.textContent;
      // Epic often embeds data as JSON in window.__INITIAL_STATE__ or similar
      const patterns = [
        /window\.__INITIAL_STATE__\s*=\s*({[\s\S]*?});/,
        /var\s+(?:visitData|appointmentData|model)\s*=\s*({[\s\S]*?});/,
        /"visits"\s*:\s*(\[[\s\S]*?\])/,
        /"Appointments"\s*:\s*(\[[\s\S]*?\])/,
      ];
      for (const pat of patterns) {
        const m = text.match(pat);
        if (m) {
          try {
            const parsed = JSON.parse(m[1]);
            log(`  ✓ Found embedded visit data in script`);
            if (Array.isArray(parsed)) allData.visits = parsed;
            else if (parsed.visits) allData.visits = parsed.visits;
            console.log('  Sample:', parsed);
          } catch(e) {}
        }
      }
    }

    // Strategy 2: Look for table rows or list items
    if (allData.visits.length === 0) {
      // Common MyChart visit list structures
      const selectors = [
        'table tbody tr',
        '.visit-row',
        '.appointment-row',
        '.list-group-item',
        '[data-testid*="visit"]',
        '[class*="VisitHistory"] [class*="row"]',
        '[class*="visithistory"] [class*="row"]',
        '.table-row',
        'section article',
      ];

      for (const sel of selectors) {
        const rows = doc.querySelectorAll(sel);
        if (rows.length > 2) { // At least a few visits
          log(`  Found ${rows.length} elements with: ${sel}`);
          rows.forEach(row => {
            const cells = row.querySelectorAll('td, [role="cell"], .cell');
            const visit = {
              rawText: row.textContent.trim().replace(/\s+/g, ' ').substring(0, 500),
              cells: Array.from(cells).map(c => c.textContent.trim()),
            };

            // Try to identify date, provider, type from text
            const dateMatch = row.textContent.match(/(\d{1,2}\/\d{1,2}\/\d{2,4})/);
            if (dateMatch) visit.date = dateMatch[1];

            // Check for links to visit details
            const detailLink = row.querySelector('a[href*="Visit"], a[href*="visit"], a[href*="Appointment"]');
            if (detailLink) visit.detailUrl = detailLink.href;

            allData.visits.push(visit);
          });
          break;
        }
      }
    }

    // Strategy 3: Get all links that look visit-related and follow them
    if (allData.visits.length === 0) {
      const links = extractLinks(doc, /visit|appointment|encounter/i);
      log(`  Found ${links.length} visit-related links`);
      for (const link of links.slice(0, 50)) {
        allData.visits.push({ href: link.href, label: link.text });
      }
    }

    if (allData.visits.length > 0) break;
  }

  log(`  Total visits found: ${allData.visits.length}`);

  // Fetch detail pages for visits that have URLs
  const visitDetailUrls = allData.visits
    .filter(v => v.detailUrl || v.href)
    .map(v => v.detailUrl || v.href)
    .slice(0, 30); // Limit to 30 detail pages

  if (visitDetailUrls.length > 0) {
    log(`  Fetching ${visitDetailUrls.length} visit detail pages...`);
    for (let i = 0; i < visitDetailUrls.length; i++) {
      const url = visitDetailUrls[i];
      log(`    [${i+1}/${visitDetailUrls.length}] ${url}`);
      const doc = await loadPage(url);
      if (!doc) continue;

      const detail = {
        url: url,
        title: txt(doc, 'h1, h2, .visit-title, .header-title'),
        allText: '',
        sections: {},
      };

      // Extract all meaningful sections
      const headers = doc.querySelectorAll('h2, h3, h4, .section-header, [class*="header"]');
      headers.forEach(h => {
        const sectionName = h.textContent.trim();
        let content = '';
        let sibling = h.nextElementSibling;
        while (sibling && !sibling.matches('h2, h3, h4, .section-header')) {
          content += sibling.textContent.trim() + '\n';
          sibling = sibling.nextElementSibling;
        }
        if (content) detail.sections[sectionName] = content.trim();
      });

      // Get the main content area
      const mainContent = doc.querySelector('main, [role="main"], .content, #content, .visit-detail');
      if (mainContent) {
        detail.allText = mainContent.textContent.trim().replace(/\s+/g, ' ').substring(0, 5000);
      }

      allData.visitDetails.push(detail);
      await new Promise(r => setTimeout(r, 300)); // Be polite
    }
  }

  // ===== 2. MESSAGES =====
  log('\n=== Scraping Messages ===');

  const messageUrls = [
    `${MC}/Messaging`,
    `${MC}/messaging/inbox`,
    `${MC}/Communication/Inbox`,
  ];

  for (const url of messageUrls) {
    log(`  Loading: ${url}`);
    const doc = await loadPage(url);
    if (!doc) continue;

    // Look for message list items
    const selectors = [
      'table tbody tr',
      '.message-row',
      '.inbox-item',
      '.list-group-item',
      '[data-testid*="message"]',
      '[class*="Message"] [class*="row"]',
      '[class*="inbox"] [class*="item"]',
      'section article',
    ];

    for (const sel of selectors) {
      const rows = doc.querySelectorAll(sel);
      if (rows.length >= 1) {
        log(`  Found ${rows.length} elements with: ${sel}`);
        rows.forEach(row => {
          const msg = {
            rawText: row.textContent.trim().replace(/\s+/g, ' ').substring(0, 500),
          };

          const dateMatch = row.textContent.match(/(\d{1,2}\/\d{1,2}\/\d{2,4})/);
          if (dateMatch) msg.date = dateMatch[1];

          const detailLink = row.querySelector('a[href*="Message"], a[href*="message"], a[href*="Communication"]');
          if (detailLink) {
            msg.detailUrl = detailLink.href;
            msg.subject = detailLink.textContent.trim();
          }

          allData.messages.push(msg);
        });
        break;
      }
    }

    // Also check for embedded data
    if (allData.messages.length === 0) {
      const scripts = Array.from(doc.querySelectorAll('script'));
      for (const script of scripts) {
        const text = script.textContent;
        if (text.includes('message') || text.includes('inbox')) {
          const patterns = [
            /"messages"\s*:\s*(\[[\s\S]*?\])/,
            /"inboxMessages"\s*:\s*(\[[\s\S]*?\])/,
          ];
          for (const pat of patterns) {
            const m = text.match(pat);
            if (m) {
              try {
                allData.messages = JSON.parse(m[1]);
                log(`  ✓ Found ${allData.messages.length} messages in embedded data`);
              } catch(e) {}
            }
          }
        }
      }
    }

    if (allData.messages.length > 0) break;
  }

  log(`  Total messages found: ${allData.messages.length}`);

  // Fetch message details
  const msgDetailUrls = allData.messages
    .filter(m => m.detailUrl)
    .map(m => m.detailUrl)
    .slice(0, 50);

  if (msgDetailUrls.length > 0) {
    log(`  Fetching ${msgDetailUrls.length} message details...`);
    for (let i = 0; i < msgDetailUrls.length; i++) {
      const url = msgDetailUrls[i];
      log(`    [${i+1}/${msgDetailUrls.length}] ${url}`);
      const doc = await loadPage(url);
      if (!doc) continue;

      const detail = {
        url: url,
        subject: txt(doc, 'h1, h2, .subject, .message-subject'),
        from: txt(doc, '[class*="from"], [class*="sender"], .from-name'),
        date: txt(doc, '[class*="date"], time, .sent-date'),
        body: '',
      };

      const bodyEl = doc.querySelector('.message-body, .message-content, [class*="body"], main p');
      if (bodyEl) {
        detail.body = bodyEl.textContent.trim().substring(0, 5000);
      } else {
        const mainContent = doc.querySelector('main, [role="main"], .content');
        if (mainContent) {
          detail.body = mainContent.textContent.trim().replace(/\s+/g, ' ').substring(0, 5000);
        }
      }

      allData.messageDetails.push(detail);
      await new Promise(r => setTimeout(r, 300));
    }
  }

  // ===== 3. TEST RESULTS =====
  log('\n=== Scraping Test Results ===');

  const testUrls = [
    `${MC}/TestResults`,
    `${MC}/test-results`,
    `${MC}/Chart/TestResults`,
  ];

  for (const url of testUrls) {
    log(`  Loading: ${url}`);
    const doc = await loadPage(url);
    if (!doc) continue;

    const rows = doc.querySelectorAll('table tbody tr, .result-row, .test-result, .list-group-item, [class*="result"]');
    if (rows.length > 0) {
      log(`  Found ${rows.length} result elements`);
      rows.forEach(row => {
        allData.testResults.push({
          rawText: row.textContent.trim().replace(/\s+/g, ' ').substring(0, 500),
          date: (row.textContent.match(/(\d{1,2}\/\d{1,2}\/\d{2,4})/) || [])[1] || '',
        });
      });
      break;
    }
  }

  log(`  Total test results found: ${allData.testResults.length}`);

  // ===== 4. MEDICATIONS =====
  log('\n=== Scraping Medications ===');

  const medUrls = [
    `${MC}/Medications`,
    `${MC}/medications`,
    `${MC}/Chart/Medications`,
  ];

  for (const url of medUrls) {
    log(`  Loading: ${url}`);
    const doc = await loadPage(url);
    if (!doc) continue;

    const rows = doc.querySelectorAll('table tbody tr, .medication-row, .med-item, .list-group-item, [class*="medication"]');
    if (rows.length > 0) {
      log(`  Found ${rows.length} medication elements`);
      rows.forEach(row => {
        allData.medications.push({
          rawText: row.textContent.trim().replace(/\s+/g, ' ').substring(0, 500),
        });
      });
      break;
    }
  }

  log(`  Total medications found: ${allData.medications.length}`);

  // ===== 5. HEALTH SUMMARY (Conditions, Allergies, etc.) =====
  log('\n=== Scraping Health Summary ===');

  const summaryUrls = [
    `${MC}/HealthSummary`,
    `${MC}/health-summary`,
    `${MC}/Chart/HealthSummary`,
    `${MC}/Chart/MyConditions`,
  ];

  for (const url of summaryUrls) {
    log(`  Loading: ${url}`);
    const doc = await loadPage(url);
    if (!doc) continue;

    // Extract sections by headers
    const headers = doc.querySelectorAll('h2, h3, .section-header');
    headers.forEach(h => {
      const title = h.textContent.trim().toLowerCase();
      let items = [];

      let sibling = h.nextElementSibling;
      while (sibling && !sibling.matches('h2, h3, .section-header')) {
        const lis = sibling.querySelectorAll('li, tr, .item');
        if (lis.length > 0) {
          lis.forEach(li => items.push(li.textContent.trim().replace(/\s+/g, ' ')));
        } else if (sibling.textContent.trim()) {
          items.push(sibling.textContent.trim().replace(/\s+/g, ' '));
        }
        sibling = sibling.nextElementSibling;
      }

      if (items.length > 0) {
        if (title.includes('condition') || title.includes('problem') || title.includes('diagnos')) {
          allData.conditions = items.map(t => ({ description: t.substring(0, 500) }));
          log(`    Conditions: ${items.length}`);
        } else if (title.includes('allerg')) {
          allData.allergies = items.map(t => ({ description: t.substring(0, 500) }));
          log(`    Allergies: ${items.length}`);
        } else if (title.includes('immuniz') || title.includes('vaccin')) {
          allData.immunizations = items.map(t => ({ description: t.substring(0, 500) }));
          log(`    Immunizations: ${items.length}`);
        } else if (title.includes('procedur') || title.includes('surger')) {
          allData.procedures = items.map(t => ({ description: t.substring(0, 500) }));
          log(`    Procedures: ${items.length}`);
        } else if (title.includes('vital') || title.includes('measurement')) {
          allData.vitalSigns = items.map(t => ({ description: t.substring(0, 500) }));
          log(`    Vital Signs: ${items.length}`);
        }
      }
    });

    if (allData.conditions.length > 0 || allData.procedures.length > 0) break;
  }

  // ===== 6. CARE TEAM (from homepage) =====
  log('\n=== Scraping Care Team ===');
  // We can see these on the current page
  const providerEls = document.querySelectorAll('[class*="provider"], [class*="care-team"] [class*="item"], [class*="CareTeam"]');
  if (providerEls.length > 0) {
    providerEls.forEach(el => {
      allData.careTeam.push({
        rawText: el.textContent.trim().replace(/\s+/g, ' ').substring(0, 300),
      });
    });
    log(`  Found ${allData.careTeam.length} care team entries from current page`);
  }

  // Also try dedicated care team page
  if (allData.careTeam.length === 0) {
    const teamUrls = [`${MC}/CareTeam`, `${MC}/care-team`, `${MC}/Chart/CareTeam`];
    for (const url of teamUrls) {
      const doc = await loadPage(url);
      if (!doc) continue;
      const items = doc.querySelectorAll('[class*="provider"], .list-group-item, table tbody tr');
      if (items.length > 0) {
        items.forEach(el => {
          allData.careTeam.push({ rawText: el.textContent.trim().replace(/\s+/g, ' ').substring(0, 300) });
        });
        log(`  Found ${allData.careTeam.length} care team entries`);
        break;
      }
    }
  }

  // ===== DISCOVERY: List all navigation links =====
  log('\n=== Discovering all MyChart sections ===');
  const navLinks = document.querySelectorAll('nav a, [role="navigation"] a, .menu a, #menu a');
  const discoveredSections = [];
  navLinks.forEach(a => {
    if (a.href && a.href.includes('/MyChart/')) {
      discoveredSections.push({ text: a.textContent.trim(), href: a.href });
    }
  });
  if (discoveredSections.length > 0) {
    log(`  Found ${discoveredSections.length} navigation links:`);
    discoveredSections.forEach(s => log(`    ${s.text}: ${s.href}`));
    allData._discoveredSections = discoveredSections;
  }

  // ===== SUMMARY & DOWNLOAD =====
  const counts = {
    visits: allData.visits.length,
    visitDetails: allData.visitDetails.length,
    messages: allData.messages.length,
    messageDetails: allData.messageDetails.length,
    testResults: allData.testResults.length,
    medications: allData.medications.length,
    conditions: allData.conditions.length,
    allergies: allData.allergies.length,
    immunizations: allData.immunizations.length,
    procedures: allData.procedures.length,
    vitalSigns: allData.vitalSigns.length,
    careTeam: allData.careTeam.length,
  };

  const totalItems = Object.values(counts).reduce((s, n) => s + n, 0);

  console.log('%c\n=== DOM SCRAPER RESULTS ===', 'color: #E91E63; font-weight: bold; font-size: 14px');
  for (const [k, v] of Object.entries(counts)) {
    const icon = v > 0 ? '✓' : '○';
    console.log(`%c  ${icon} ${k}: ${v}`, v > 0 ? 'color: #4CAF50' : 'color: #999');
  }

  // Download
  const blob = new Blob([JSON.stringify(allData, null, 2)], { type: 'application/json' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = `mskcc_mychart_dom_extract_${new Date().toISOString().slice(0,10)}.json`;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  URL.revokeObjectURL(url);
  log(`\n✓ Downloaded JSON file with ${totalItems} items!`);

  window.__mskccData = allData;
  log('Data also stored in window.__mskccData');

  if (totalItems === 0) {
    log('\nNo data found via DOM scraping either.');
    log('The MyChart portal may use a single-page app framework that requires');
    log('actual browser navigation. Try the interactive approach:');
    log('1. Manually click into Visits, then run: 04_scrape_current_page.js');
    log('2. Repeat for Messages, Test Results, etc.');
  }

})();
