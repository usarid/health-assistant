// UCSF Visits Scraper v4 — Uses RenderedData from Epic SPA for real CSNs
// Paste this in the UCSF MyChart console while on the Visits page
// Only fetches UCSF-local visits (IsLocal === true)

(async () => {
  const log = (...args) => console.log('[UCSF v4]', ...args);

  // Get anti-forgery token
  const token = document.querySelector('input[name="__RequestVerificationToken"]')?.value;
  if (!token) { log('ERROR: No anti-forgery token found!'); return; }
  log('Token: found');

  // Get RenderedData from the PastVisitsListComponent (instance 7)
  const inst = Epic.PatientAccess.Components.__Instances[7];
  if (!inst || !inst.RenderedData) { log('ERROR: No RenderedData found!'); return; }

  const allVisits = inst.RenderedData;
  log('Total visits in RenderedData:', allVisits.length);

  // Filter to UCSF-local visits only
  const ucsfVisits = allVisits.filter(v => v.IsLocal === true);
  log('UCSF local visits:', ucsfVisits.length);

  // Extract metadata from DOM for each visit (date, type, provider)
  const visitMeta = [];
  ucsfVisits.forEach((v, idx) => {
    const origIndex = allVisits.indexOf(v);
    const li = document.querySelector(`li.pastvisit[data-index="${origIndex}"]`);
    let date = '', visitType = '', provider = '', department = '';
    if (li) {
      date = li.querySelector('.clearlabel')?.textContent?.trim() || '';
      visitType = li.querySelector('.visit-type')?.textContent?.trim() || '';
      provider = li.querySelector('.provider-name, .provider')?.textContent?.trim() || '';
      department = li.querySelector('.department, .department-name')?.textContent?.trim() || '';
    }
    visitMeta.push({
      index: origIndex,
      csn: v.Csn,
      encounterType: v.EncounterType,
      canRedirect: v.CanRedirectToApptDetails,
      hasNewPvd: v.HasNewPvdFeature,
      date, visitType, provider, department
    });
  });

  log('Sample visits:');
  visitMeta.slice(0, 3).forEach((m, i) => log(`  ${i}: ${m.date} | ${m.visitType} | ${m.provider}`));

  // Fetch visit details for each UCSF visit
  const results = [];
  let success = 0, errors = 0;

  for (let i = 0; i < visitMeta.length; i++) {
    const meta = visitMeta[i];

    try {
      const resp = await fetch('/UCSFMyChart/api/visits/past-details/GetVisitDetailsPast', {
        method: 'POST',
        headers: {
          'accept': 'application/json, text/plain, */*',
          'content-type': 'application/json',
          '__requestverificationtoken': token,
          'x-requested-with': 'XMLHttpRequest'
        },
        body: JSON.stringify({ csn: meta.csn, eorgID: '' })
      });

      if (!resp.ok) {
        log(`  HTTP ${resp.status} for visit ${i} (${meta.date} ${meta.visitType})`);
        errors++;
        results.push({ ...meta, error: `HTTP ${resp.status}`, details: null });
        continue;
      }

      const data = await resp.json();
      if (data) {
        success++;
        results.push({ ...meta, error: null, details: data });
      } else {
        errors++;
        results.push({ ...meta, error: 'null response', details: null });
      }
    } catch (e) {
      errors++;
      results.push({ ...meta, error: e.message, details: null });
    }

    // Progress logging
    if ((i + 1) % 20 === 0 || i === visitMeta.length - 1) {
      log(`[${i + 1}/${visitMeta.length}] success:${success} err:${errors}`);
    }

    // Small delay to avoid rate limiting
    if (i % 10 === 9) await new Promise(r => setTimeout(r, 500));
  }

  log('');
  log('=== DONE ===');
  log('Visits fetched:', success);
  log('Errors:', errors);

  // Store results for download
  const output = {
    scraped_at: new Date().toISOString(),
    source: 'UCSF MyChart',
    total_ucsf_visits: ucsfVisits.length,
    success_count: success,
    error_count: errors,
    visits: results
  };

  window.__ucsfVisits = output;

  // Copy to clipboard
  const json = JSON.stringify(output, null, 2);
  try {
    await navigator.clipboard.writeText(json);
    log('Results copied to clipboard! Paste into a text file.');
  } catch (e) {
    log('Clipboard failed. Use: copy(JSON.stringify(window.__ucsfVisits, null, 2))');
  }

  log('');
  log('>>> Results in window.__ucsfVisits <<<');
  log('>>> Or type: copy(JSON.stringify(window.__ucsfVisits, null, 2)) <<<');
})();
