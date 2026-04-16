// UCSF Clinical Notes Scraper v1 — Fetches note content via LoadReportContent
// Requires window.__ucsfVisits from the v4 visits scraper to still be in memory
// Paste this in the UCSF MyChart console

(async () => {
  const log = (...args) => console.log('[UCSF Notes]', ...args);

  // Check for visit data
  if (!window.__ucsfVisits) {
    log('ERROR: window.__ucsfVisits not found! Run the visits scraper (v4) first.');
    return;
  }

  const token = document.querySelector('input[name="__RequestVerificationToken"]')?.value;
  if (!token) { log('ERROR: No anti-forgery token found!'); return; }
  log('Token: found');

  // Filter to visits with shareable notes
  const visitsWithNotes = window.__ucsfVisits.visits.filter(v =>
    v.details &&
    v.details.notesInfo &&
    v.details.notesInfo.isAtLeastOneNoteShareable === true &&
    v.details.notesInfo.notesReport &&
    v.details.notesInfo.notesReport.reportID
  );

  log('Visits with shareable notes:', visitsWithNotes.length);

  if (visitsWithNotes.length === 0) {
    log('No visits with shareable notes found.');
    return;
  }

  // Generate a random nonce (hex string)
  const genNonce = () => {
    const bytes = new Uint8Array(16);
    crypto.getRandomValues(bytes);
    return Array.from(bytes).map(b => b.toString(16).padStart(2, '0')).join('');
  };

  const results = [];
  let success = 0, errors = 0, empty = 0;
  let eidCounter = 1;

  for (let i = 0; i < visitsWithNotes.length; i++) {
    const visit = visitsWithNotes[i];
    const noteInfo = visit.details.notesInfo.notesReport;
    const csn = visit.details.csn || visit.csn;

    try {
      const resp = await fetch('/UCSFMyChart/api/report-content/LoadReportContent', {
        method: 'POST',
        headers: {
          'accept': 'application/json',
          'content-type': 'application/json',
          '__requestverificationtoken': token,
          'x-requested-with': 'XMLHttpRequest'
        },
        body: JSON.stringify({
          reportMnemonic: noteInfo.reportMnemonic,
          reportID: noteInfo.reportID,
          csn: csn,
          isFullReportPage: false,
          uniqueClass: 'EID-' + (eidCounter++),
          nonce: genNonce()
        })
      });

      if (!resp.ok) {
        log(`  HTTP ${resp.status} for visit ${i} (${visit.date} ${visit.visitType})`);
        errors++;
        results.push({
          visitIndex: visit.index,
          date: visit.date,
          visitType: visit.visitType,
          provider: visit.details.visitSummaryInfo?.provider || '',
          department: visit.details.visitSummaryInfo?.department || '',
          error: `HTTP ${resp.status}`,
          noteContent: null
        });
        continue;
      }

      const data = await resp.json();
      if (data) {
        // Check if there's actual content
        const hasContent = data.reportHtml || data.html || data.content || data.ReportHtml || data.Html;
        if (hasContent) {
          success++;
        } else {
          empty++;
        }
        results.push({
          visitIndex: visit.index,
          date: visit.date,
          visitType: visit.visitType,
          provider: visit.details.visitSummaryInfo?.provider || '',
          department: visit.details.visitSummaryInfo?.department || '',
          encounterDate: visit.details.visitSummaryInfo?.encounterDate || '',
          error: null,
          noteContent: data
        });
      } else {
        empty++;
        results.push({
          visitIndex: visit.index,
          date: visit.date,
          visitType: visit.visitType,
          provider: visit.details.visitSummaryInfo?.provider || '',
          department: visit.details.visitSummaryInfo?.department || '',
          error: 'null response',
          noteContent: null
        });
      }
    } catch (e) {
      errors++;
      results.push({
        visitIndex: visit.index,
        date: visit.date,
        visitType: visit.visitType,
        error: e.message,
        noteContent: null
      });
    }

    // Progress
    if ((i + 1) % 10 === 0 || i === visitsWithNotes.length - 1) {
      log(`[${i + 1}/${visitsWithNotes.length}] success:${success} empty:${empty} err:${errors}`);
    }

    // Rate limiting delay
    if (i % 5 === 4) await new Promise(r => setTimeout(r, 500));
  }

  log('');
  log('=== DONE ===');
  log('Notes with content:', success);
  log('Empty responses:', empty);
  log('Errors:', errors);

  const output = {
    scraped_at: new Date().toISOString(),
    source: 'UCSF MyChart Clinical Notes',
    total_with_notes: visitsWithNotes.length,
    success_count: success,
    empty_count: empty,
    error_count: errors,
    notes: results
  };

  window.__ucsfNotes = output;

  // Copy to clipboard
  const json = JSON.stringify(output, null, 2);
  try {
    await navigator.clipboard.writeText(json);
    log('Results copied to clipboard! Paste into a text file.');
  } catch (e) {
    log('Clipboard failed. Use: copy(JSON.stringify(window.__ucsfNotes, null, 2))');
  }

  log('');
  log('>>> Results in window.__ucsfNotes <<<');
})();
