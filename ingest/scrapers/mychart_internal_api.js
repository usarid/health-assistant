/**
 * MyChart Internal API Scraper
 * ============================
 * Paste this into Chrome DevTools console while logged into MyChart.
 *
 * This script uses MyChart's internal AJAX APIs (the same ones the UI calls)
 * to extract Visits, Messages, Conditions, Procedures, Medications, etc.
 *
 * Run this if the FHIR probe (01) didn't find a working endpoint.
 */

(async function() {
  'use strict';

  const BASE = window.location.origin;
  const MC_API = `${BASE}/MyChart`;

  const log = (msg) => console.log(`%c[MyChart API] ${msg}`, 'color: #4CAF50; font-weight: bold');
  const warn = (msg) => console.warn(`[MyChart API] ${msg}`);

  // Get anti-forgery token from the page (Epic uses this for AJAX calls)
  function getVerificationToken() {
    const tokenEl = document.querySelector('input[name="__RequestVerificationToken"]');
    if (tokenEl) return tokenEl.value;
    // Also check meta tag
    const metaEl = document.querySelector('meta[name="__RequestVerificationToken"]');
    if (metaEl) return metaEl.content;
    // Check cookies
    const cookies = document.cookie.split(';');
    for (const c of cookies) {
      const [name, val] = c.trim().split('=');
      if (name === '__RequestVerificationToken' || name === '.EPICMYCHARTANTIFORGERY') {
        return decodeURIComponent(val);
      }
    }
    return null;
  }

  // Helper: internal API fetch
  async function myChartFetch(path, method = 'GET', body = null) {
    const token = getVerificationToken();
    const headers = {
      'Accept': 'application/json',
      'Content-Type': 'application/json',
      'X-Requested-With': 'XMLHttpRequest',
    };
    if (token) headers['__RequestVerificationToken'] = token;

    const opts = { method, headers, credentials: 'include' };
    if (body) opts.body = JSON.stringify(body);

    const resp = await fetch(`${MC_API}${path}`, opts);
    if (!resp.ok) {
      warn(`${path}: HTTP ${resp.status}`);
      return null;
    }
    const text = await resp.text();
    try { return JSON.parse(text); }
    catch { return text; }
  }

  const allData = {
    extractedAt: new Date().toISOString(),
    source: 'MSKCC MyChart (Internal API)',
    visits: [],
    messages: [],
    testResults: [],
    medications: [],
    conditions: [],
    allergies: [],
    immunizations: [],
    procedures: [],
    careTeam: [],
    healthSummary: null,
  };

  // ===== 1. VISITS =====
  log('=== Extracting Visits ===');
  // MyChart typically loads visits via these endpoints
  const visitPaths = [
    '/api/Visits',
    '/Visits/VisitHistory',
    '/api/appointments',
    '/Scheduling/VisitHistory',
    '/api/Visit/GetVisitHistory',
    '/Chart/GetVisits',
  ];

  for (const path of visitPaths) {
    log(`  Trying: ${path}`);
    try {
      const data = await myChartFetch(path);
      if (data && !data.error && (Array.isArray(data) || (data.Visits || data.visits || data.Items || data.items))) {
        const visits = Array.isArray(data) ? data : (data.Visits || data.visits || data.Items || data.items || []);
        allData.visits = visits;
        log(`  ✓ Found ${visits.length} visits via ${path}`);
        break;
      }
    } catch(e) { }
  }

  // If no API endpoint found, try the page-based approach
  if (allData.visits.length === 0) {
    log('  Trying page-based visit extraction...');
    try {
      // Load the Visits page HTML
      const resp = await fetch(`${MC_API}/Visits`, { credentials: 'include' });
      const html = await resp.text();
      const parser = new DOMParser();
      const doc = parser.parseFromString(html, 'text/html');

      // Look for visit data in the page
      const visitLinks = doc.querySelectorAll('a[href*="Visit"], a[href*="visit"], .visit-row, [data-visit-id], .appointment');
      log(`  Found ${visitLinks.length} visit elements in HTML`);

      // Also look for embedded JSON data
      const scripts = doc.querySelectorAll('script');
      for (const s of scripts) {
        const text = s.textContent;
        if (text.includes('visitData') || text.includes('appointments') || text.includes('VisitHistory')) {
          // Try to extract JSON from script
          const jsonMatch = text.match(/(?:visitData|appointments|VisitHistory)\s*[=:]\s*(\[[\s\S]*?\]);/);
          if (jsonMatch) {
            try {
              allData.visits = JSON.parse(jsonMatch[1]);
              log(`  ✓ Extracted ${allData.visits.length} visits from embedded script data`);
            } catch(e) {}
          }
        }
      }

      // Extract visit details from DOM elements
      if (allData.visits.length === 0) {
        const visitElements = doc.querySelectorAll('[class*="visit"], [class*="appointment"], tr[data-id], .list-group-item');
        const visits = [];
        visitElements.forEach(el => {
          const dateEl = el.querySelector('[class*="date"], time, .date');
          const provEl = el.querySelector('[class*="provider"], [class*="doctor"], .provider');
          const typeEl = el.querySelector('[class*="type"], [class*="reason"], .visit-type');
          const locEl = el.querySelector('[class*="location"], [class*="department"], .location');

          if (dateEl || provEl) {
            visits.push({
              date: dateEl?.textContent?.trim() || '',
              provider: provEl?.textContent?.trim() || '',
              type: typeEl?.textContent?.trim() || '',
              location: locEl?.textContent?.trim() || '',
              rawText: el.textContent.trim().substring(0, 500),
            });
          }
        });
        if (visits.length > 0) {
          allData.visits = visits;
          log(`  ✓ Extracted ${visits.length} visits from DOM`);
        }
      }
    } catch(e) {
      warn(`  Visit page extraction failed: ${e.message}`);
    }
  }

  // ===== 2. MESSAGES =====
  log('\n=== Extracting Messages ===');
  const messagePaths = [
    '/api/Messages',
    '/api/Message/GetMessages',
    '/Messaging/InboxMessages',
    '/api/Messaging/Inbox',
    '/Chart/GetMessages',
  ];

  for (const path of messagePaths) {
    log(`  Trying: ${path}`);
    try {
      const data = await myChartFetch(path);
      if (data && !data.error) {
        const msgs = Array.isArray(data) ? data : (data.Messages || data.messages || data.Items || data.items || []);
        if (msgs.length > 0 || Array.isArray(data)) {
          allData.messages = msgs;
          log(`  ✓ Found ${msgs.length} messages via ${path}`);
          break;
        }
      }
    } catch(e) { }
  }

  // Page-based message extraction
  if (allData.messages.length === 0) {
    log('  Trying page-based message extraction...');
    try {
      const resp = await fetch(`${MC_API}/Messaging`, { credentials: 'include' });
      const html = await resp.text();
      const parser = new DOMParser();
      const doc = parser.parseFromString(html, 'text/html');

      const msgElements = doc.querySelectorAll('[class*="message"], .inbox-item, tr[data-id], .list-group-item');
      const messages = [];
      msgElements.forEach(el => {
        const fromEl = el.querySelector('[class*="from"], [class*="sender"], .from');
        const subjEl = el.querySelector('[class*="subject"], .subject');
        const dateEl = el.querySelector('[class*="date"], time, .date');
        if (fromEl || subjEl) {
          messages.push({
            from: fromEl?.textContent?.trim() || '',
            subject: subjEl?.textContent?.trim() || '',
            date: dateEl?.textContent?.trim() || '',
            rawText: el.textContent.trim().substring(0, 500),
          });
        }
      });
      if (messages.length > 0) {
        allData.messages = messages;
        log(`  ✓ Extracted ${messages.length} messages from DOM`);
      }
    } catch(e) {
      warn(`  Message extraction failed: ${e.message}`);
    }
  }

  // ===== 3. TEST RESULTS =====
  log('\n=== Extracting Test Results ===');
  const testPaths = [
    '/api/TestResults',
    '/api/Results/GetResults',
    '/Chart/GetTestResults',
    '/TestResults/Results',
  ];

  for (const path of testPaths) {
    log(`  Trying: ${path}`);
    try {
      const data = await myChartFetch(path);
      if (data && !data.error) {
        const results = Array.isArray(data) ? data : (data.Results || data.results || data.TestResults || data.Items || []);
        if (results.length > 0) {
          allData.testResults = results;
          log(`  ✓ Found ${results.length} test results via ${path}`);
          break;
        }
      }
    } catch(e) { }
  }

  // ===== 4. MEDICATIONS =====
  log('\n=== Extracting Medications ===');
  const medPaths = [
    '/api/Medications',
    '/api/Medication/GetMedications',
    '/Chart/GetMedications',
    '/Medications/Current',
  ];

  for (const path of medPaths) {
    log(`  Trying: ${path}`);
    try {
      const data = await myChartFetch(path);
      if (data && !data.error) {
        const meds = Array.isArray(data) ? data : (data.Medications || data.medications || data.Items || []);
        if (meds.length > 0) {
          allData.medications = meds;
          log(`  ✓ Found ${meds.length} medications via ${path}`);
          break;
        }
      }
    } catch(e) { }
  }

  // ===== 5. HEALTH SUMMARY =====
  log('\n=== Extracting Health Summary ===');
  const summaryPaths = [
    '/api/HealthSummary',
    '/api/Chart/GetHealthSummary',
    '/Chart/HealthSummary',
    '/api/ClinicalSummary',
  ];

  for (const path of summaryPaths) {
    log(`  Trying: ${path}`);
    try {
      const data = await myChartFetch(path);
      if (data && !data.error && typeof data === 'object') {
        allData.healthSummary = data;
        log(`  ✓ Health summary found via ${path}`);

        // Extract sub-sections if available
        if (data.Conditions || data.conditions || data.Problems) {
          allData.conditions = data.Conditions || data.conditions || data.Problems || [];
          log(`    Conditions: ${allData.conditions.length}`);
        }
        if (data.Allergies || data.allergies) {
          allData.allergies = data.Allergies || data.allergies || [];
          log(`    Allergies: ${allData.allergies.length}`);
        }
        if (data.Immunizations || data.immunizations) {
          allData.immunizations = data.Immunizations || data.immunizations || [];
          log(`    Immunizations: ${allData.immunizations.length}`);
        }
        if (data.Procedures || data.procedures) {
          allData.procedures = data.Procedures || data.procedures || [];
          log(`    Procedures: ${allData.procedures.length}`);
        }
        break;
      }
    } catch(e) { }
  }

  // ===== 6. CARE TEAM =====
  log('\n=== Extracting Care Team ===');
  const teamPaths = [
    '/api/CareTeam',
    '/api/Providers/GetCareTeam',
    '/Chart/GetCareTeam',
    '/CareTeam/GetProviders',
  ];

  for (const path of teamPaths) {
    log(`  Trying: ${path}`);
    try {
      const data = await myChartFetch(path);
      if (data && !data.error) {
        const team = Array.isArray(data) ? data : (data.Providers || data.providers || data.CareTeam || data.Items || []);
        if (team.length > 0) {
          allData.careTeam = team;
          log(`  ✓ Found ${team.length} care team members via ${path}`);
          break;
        }
      }
    } catch(e) { }
  }

  // ===== NETWORK INTERCEPT APPROACH =====
  // If nothing worked above, let's try capturing XHR requests
  log('\n=== Checking for XHR patterns ===');
  if (window.performance && window.performance.getEntries) {
    const entries = window.performance.getEntries()
      .filter(e => e.initiatorType === 'xmlhttprequest' || e.initiatorType === 'fetch')
      .map(e => e.name);
    if (entries.length > 0) {
      log(`Found ${entries.length} prior XHR/fetch requests:`);
      const apiCalls = entries.filter(e => e.includes('/api/') || e.includes('/Chart/'));
      apiCalls.forEach(e => log(`  ${e}`));
      allData._discoveredEndpoints = apiCalls;
    }
  }

  // ===== SUMMARY & DOWNLOAD =====
  const counts = {
    visits: allData.visits.length,
    messages: allData.messages.length,
    testResults: allData.testResults.length,
    medications: allData.medications.length,
    conditions: allData.conditions.length,
    allergies: allData.allergies.length,
    immunizations: allData.immunizations.length,
    procedures: allData.procedures.length,
    careTeam: allData.careTeam.length,
    healthSummary: allData.healthSummary ? 1 : 0,
  };

  const totalItems = Object.values(counts).reduce((s, n) => s + n, 0);

  console.log('%c\n=== EXTRACTION SUMMARY ===', 'color: #4CAF50; font-weight: bold; font-size: 14px');
  for (const [k, v] of Object.entries(counts)) {
    const icon = v > 0 ? '✓' : '○';
    console.log(`%c  ${icon} ${k}: ${v}`, v > 0 ? 'color: #4CAF50' : 'color: #999');
  }

  if (totalItems > 0) {
    const blob = new Blob([JSON.stringify(allData, null, 2)], { type: 'application/json' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `mskcc_mychart_extract_${new Date().toISOString().slice(0,10)}.json`;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
    log(`\n✓ Downloaded JSON file with ${totalItems} items!`);
  } else {
    warn('No data extracted via internal APIs.');
    log('The API structure of this MyChart instance may be different.');
    log('Run 03_mychart_dom_scraper.js for DOM-based extraction (navigates through actual pages).');
  }

  window.__mskccData = allData;
  log('Data also stored in window.__mskccData');

})();
