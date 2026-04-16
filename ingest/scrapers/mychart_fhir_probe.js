/**
 * MyChart FHIR Endpoint Discovery & Data Extraction
 * ==================================================
 * Paste this into Chrome DevTools console while logged into MyChart.
 *
 * This script:
 * 1. Probes for Epic's patient-facing FHIR R4 endpoint
 * 2. If found, extracts all available clinical resources
 * 3. Downloads everything as a single JSON file
 *
 * Your session cookie authenticates the requests automatically.
 */

(async function() {
  'use strict';

  const BASE = window.location.origin; // e.g. https://mskmychart.mskcc.org
  const MC = '/MyChart';

  // Known Epic FHIR endpoint patterns (patient-facing)
  const FHIR_PATHS = [
    `${MC}/api/FHIR/R4`,
    `${MC}/api/epic/2021/Security/Open/EpicFhir/R4`,
    `/interconnect-prd-fhir/api/FHIR/R4`,
    `/FHIRProxy/api/FHIR/R4`,
    `/PRD-FHIR/api/FHIR/R4`,
    `${MC}/api/FHIR/STU3`,
  ];

  // FHIR Resource types we want
  const RESOURCE_TYPES = [
    'Patient',
    'Encounter',
    'Condition',
    'Procedure',
    'Observation',
    'MedicationRequest',
    'MedicationStatement',
    'AllergyIntolerance',
    'Immunization',
    'DiagnosticReport',
    'DocumentReference',
    'CarePlan',
    'CareTeam',
    'Goal',
    'Device',
  ];

  const log = (msg) => console.log(`%c[MyChart Scraper] ${msg}`, 'color: #2196F3; font-weight: bold');
  const warn = (msg) => console.warn(`[MyChart Scraper] ${msg}`);
  const err = (msg) => console.error(`[MyChart Scraper] ${msg}`);

  // Helper: fetch with session cookies
  async function fhirFetch(url) {
    const resp = await fetch(url, {
      credentials: 'include',
      headers: {
        'Accept': 'application/fhir+json, application/json',
      }
    });
    if (!resp.ok) return null;
    const text = await resp.text();
    try { return JSON.parse(text); }
    catch { return null; }
  }

  // Step 1: Probe for FHIR endpoint
  log('Probing for FHIR R4 endpoints...');
  let fhirBase = null;

  for (const path of FHIR_PATHS) {
    const url = `${BASE}${path}/metadata`;
    log(`  Trying: ${url}`);
    try {
      const meta = await fhirFetch(url);
      if (meta && (meta.resourceType === 'CapabilityStatement' || meta.fhirVersion)) {
        log(`  ✓ Found FHIR endpoint: ${path}`);
        log(`    FHIR Version: ${meta.fhirVersion || 'unknown'}`);
        if (meta.rest && meta.rest[0] && meta.rest[0].resource) {
          const supported = meta.rest[0].resource.map(r => r.type);
          log(`    Supported resources: ${supported.join(', ')}`);
        }
        fhirBase = `${BASE}${path}`;
        break;
      }
    } catch (e) {
      // Try next
    }
  }

  if (!fhirBase) {
    // Also try: look for FHIR URLs in the page source or scripts
    log('Standard paths failed. Searching page scripts for FHIR URLs...');
    const scripts = document.querySelectorAll('script[src]');
    for (const s of scripts) {
      log(`  Script: ${s.src}`);
    }

    // Try the /api/ discovery endpoint
    const apiPaths = [
      `${BASE}${MC}/api/epicmobile/version`,
      `${BASE}${MC}/.well-known/smart-configuration`,
      `${BASE}/.well-known/smart-configuration`,
    ];

    for (const url of apiPaths) {
      log(`  Trying discovery: ${url}`);
      try {
        const data = await fhirFetch(url);
        if (data) {
          log(`  ✓ Discovery response from ${url}:`);
          console.log(data);
          if (data.token_endpoint || data.authorization_endpoint) {
            // SMART config found - extract FHIR base from it
            const authUrl = data.token_endpoint || data.authorization_endpoint || '';
            log(`  Auth endpoint: ${authUrl}`);
          }
        }
      } catch(e) {}
    }

    warn('Could not find a working FHIR endpoint. Will try internal MyChart APIs instead.');
    console.log('%c=== FHIR probe complete. Proceeding to internal API scraping... ===', 'color: orange; font-weight: bold');
    console.log('%cRun the next script: 02_mychart_internal_api.js', 'color: orange; font-weight: bold');
    return;
  }

  // Step 2: Extract data from each resource type
  log(`\nExtracting data from: ${fhirBase}`);
  const allData = {
    extractedAt: new Date().toISOString(),
    source: 'MSKCC MyChart',
    fhirBase: fhirBase,
    resources: {}
  };

  for (const rt of RESOURCE_TYPES) {
    log(`Fetching ${rt}...`);
    let entries = [];
    let url = `${fhirBase}/${rt}?_count=100`;
    let pageNum = 0;

    while (url && pageNum < 20) { // Safety limit: 20 pages
      try {
        const bundle = await fhirFetch(url);
        if (!bundle || bundle.resourceType !== 'Bundle') {
          if (bundle && bundle.issue) {
            warn(`  ${rt}: ${bundle.issue[0]?.diagnostics || 'error'}`);
          }
          break;
        }

        const pageEntries = (bundle.entry || []).map(e => e.resource);
        entries = entries.concat(pageEntries);
        log(`  ${rt}: page ${pageNum + 1}, got ${pageEntries.length} (total: ${entries.length})`);

        // Follow next link for pagination
        const nextLink = (bundle.link || []).find(l => l.relation === 'next');
        url = nextLink ? nextLink.url : null;
        pageNum++;

        // Small delay to be nice
        await new Promise(r => setTimeout(r, 200));
      } catch(e) {
        warn(`  ${rt}: Error - ${e.message}`);
        break;
      }
    }

    if (entries.length > 0) {
      allData.resources[rt] = entries;
      log(`  ✓ ${rt}: ${entries.length} resources`);
    } else {
      log(`  ○ ${rt}: none found`);
    }
  }

  // Step 3: Summary
  const totalResources = Object.values(allData.resources).reduce((s, arr) => s + arr.length, 0);
  log(`\n=== Extraction Complete ===`);
  log(`Total resources: ${totalResources}`);
  for (const [rt, arr] of Object.entries(allData.resources)) {
    log(`  ${rt}: ${arr.length}`);
  }

  // Step 4: Download as JSON
  if (totalResources > 0) {
    const blob = new Blob([JSON.stringify(allData, null, 2)], { type: 'application/json' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `mskcc_mychart_fhir_extract_${new Date().toISOString().slice(0,10)}.json`;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
    log(`\n✓ Downloaded JSON file!`);
  } else {
    warn('No data extracted via FHIR. Try the internal API scraper (02_mychart_internal_api.js).');
  }

  // Also store in window for programmatic access
  window.__mskccData = allData;
  log('Data also available as window.__mskccData');

})();
