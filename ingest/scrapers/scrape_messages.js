/**
 * MyChart Messages Scraper v8 — With organizationId for cross-institution
 * ========================================================================
 * This extracts BOTH the conversation id AND organizationId (org) from
 * the href params, which is required for cross-institution messages.
 *
 * 1. Go to Messages, load all conversations
 * 2. Paste this script
 * 3. Wait for "DONE"
 * 4. Type: downloadThreads()
 */

(async function() {
  'use strict';

  const log = (m) => console.log('%c[v8] ' + m, 'color:#FF9800;font-weight:bold');
  const wait = (ms) => new Promise(r => setTimeout(r, ms));

  const API = '/MyChart/api/conversations/GetConversationDetails';

  // ── Auth tokens ──
  const tokenEl = document.querySelector('input[name="__RequestVerificationToken"]');
  const token = tokenEl ? tokenEl.value : '';
  log('Token: ' + (token ? 'found' : 'NOT FOUND'));

  // PageNonce from the captured fetch: c17d0b3c45c7458eb99344f48fe30c62
  // Search for it dynamically
  const html = document.documentElement.innerHTML;
  let nonce = '';
  const nonceMatch = html.match(/[Pp]age[Nn]once['":\s]+['"]([a-f0-9]{32})['"]/);
  if (nonceMatch) {
    nonce = nonceMatch[1];
  } else {
    // Brute force: find all 32-char hex strings
    const allHex = [...new Set(html.match(/\b[a-f0-9]{32}\b/g) || [])];
    if (allHex.length > 0) nonce = allHex[0];
  }
  log('Nonce: ' + (nonce || 'NOT FOUND'));

  // ── Extract conversation IDs with their org IDs from href params ──
  log('Extracting conversation IDs with org IDs...');

  const conversations = [];
  const seen = new Set();

  // Find all links with conversation?id=...&org=...
  document.querySelectorAll('a[href]').forEach(a => {
    const href = a.getAttribute('href') || '';
    const idMatch = href.match(/[?&]id=([^&]+)/);
    if (!idMatch) return;

    const id = decodeURIComponent(idMatch[1]);
    if (seen.has(id)) return;
    seen.add(id);

    const orgMatch = href.match(/[?&]org=([^&]+)/);
    const org = orgMatch ? decodeURIComponent(orgMatch[1]) : '';

    conversations.push({
      id: id,
      organizationId: org,
      preview: a.textContent.trim().replace(/\s+/g, ' ').substring(0, 300),
    });
  });

  log('Found ' + conversations.length + ' conversations');
  const withOrg = conversations.filter(c => c.organizationId).length;
  const withoutOrg = conversations.filter(c => !c.organizationId).length;
  log('  With org (cross-institution): ' + withOrg);
  log('  Without org (MSKCC native): ' + withoutOrg);

  if (conversations.length === 0) {
    log('ERROR: No conversations found. Make sure all conversations are loaded.');
    return;
  }

  // ── Test call ──
  log('Testing with first conversation...');

  async function fetchConv(conv) {
    const body = {
      id: conv.id,
      messageId: '',
      organizationId: conv.organizationId || '',
    };
    if (nonce) body.PageNonce = nonce;

    const resp = await fetch(API, {
      method: 'POST',
      credentials: 'include',
      headers: {
        '__requestverificationtoken': token,
        'accept': 'application/json',
        'content-type': 'application/json',
      },
      body: JSON.stringify(body),
    });

    if (!resp.ok) return null;
    return await resp.json();
  }

  // Test with a cross-institution one (has org)
  const testConv = conversations.find(c => c.organizationId) || conversations[0];
  const testResult = await fetchConv(testConv);
  const testMsgCount = testResult?.messages?.length || 0;

  if (testMsgCount > 0) {
    log('✓ Test succeeded! Got ' + testMsgCount + ' messages (org: ' + (testConv.organizationId ? 'yes' : 'no') + ')');
  } else {
    log('Test returned ' + testMsgCount + ' messages. Response: ' + JSON.stringify(testResult)?.substring(0, 200));
    log('Continuing anyway — some conversations may still work...');
  }

  // ── Fetch all ──
  log('');
  log('Fetching all ' + conversations.length + ' conversations...');

  const threads = [];
  window.__threads = threads;
  let successCount = 0;
  let nullCount = 0;
  let errorCount = 0;

  for (let i = 0; i < conversations.length; i++) {
    try {
      const data = await fetchConv(conversations[i]);
      if (data && data.messages && data.messages.length > 0) {
        threads.push({
          id: conversations[i].id,
          organizationId: conversations[i].organizationId,
          preview: conversations[i].preview,
          totalMessages: data.totalMessages || data.messages.length,
          messages: data.messages,
          users: data.users || {},
        });
        successCount++;
      } else {
        nullCount++;
      }
    } catch(e) {
      errorCount++;
    }

    if ((i + 1) % 25 === 0) {
      log('[' + (i+1) + '/' + conversations.length + '] success:' + successCount + ' null:' + nullCount + ' err:' + errorCount);
    }

    await wait(150);
  }

  log('');
  log('=== DONE ===');
  log('Threads with full content: ' + successCount + ' / ' + conversations.length);
  log('Null/empty: ' + nullCount);
  log('Errors: ' + errorCount);
  log('');
  log('>>> Type: downloadThreads() <<<');

  // ── Download ──
  window.downloadThreads = function() {
    const output = {
      extractedAt: new Date().toISOString(),
      source: 'MSKCC MyChart Messages (API v8 - with org)',
      totalThreads: threads.length,
      threads: threads,
    };
    const jsonStr = JSON.stringify(output, null, 2);
    const sizeMB = (jsonStr.length / 1048576).toFixed(1);
    log('Data: ' + threads.length + ' threads, ' + sizeMB + ' MB');

    navigator.clipboard.writeText(jsonStr).then(() => {
      log('✓ Copied to clipboard! Paste into a file and save.');
    }).catch(() => {
      try {
        const w = window.open('', '_blank');
        if (w) {
          w.document.title = 'MSKCC Messages (' + threads.length + ')';
          w.document.body.style.margin = '0';
          const pre = w.document.createElement('pre');
          pre.style.cssText = 'margin:0;padding:10px;font-size:11px;white-space:pre-wrap;word-wrap:break-word';
          pre.textContent = jsonStr;
          w.document.body.appendChild(pre);
          log('Opened in new tab. Use Cmd+A, Cmd+C to copy.');
        }
      } catch(e) {
        log('Data is in window.__threads. Use: copy(JSON.stringify({threads:window.__threads}))');
      }
    });
  };

})();
