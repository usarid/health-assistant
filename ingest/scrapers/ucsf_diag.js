/**
 * UCSF Visit Diagnostics — find where CSNs are stored
 */
(function() {
  const log = (m) => console.log('%c[diag] ' + m, 'color:#FF5722;font-weight:bold');

  // 1. Check all links
  log('=== All links with href ===');
  const links = document.querySelectorAll('a[href]');
  const visitLinks = [];
  links.forEach(a => {
    const href = a.getAttribute('href') || '';
    const text = a.textContent.trim().substring(0, 80);
    if (href.includes('isit') || href.includes('AVS') || href.includes('csn') || href.includes('past') || href.includes('detail') || href.includes('Summary')) {
      visitLinks.push({ href, text });
      log('  ' + text + ' → ' + href);
    }
  });
  log('Visit-related links: ' + visitLinks.length);

  // 2. Check data attributes on card-like elements
  log('\n=== Elements with data-* attributes ===');
  document.querySelectorAll('[data-csn], [data-id], [data-visit], [data-contactid], [data-key]').forEach(el => {
    const attrs = Array.from(el.attributes).filter(a => a.name.startsWith('data-')).map(a => a.name + '=' + a.value.substring(0, 60));
    log('  ' + el.tagName + ': ' + attrs.join(', '));
  });

  // 3. Search HTML for WP-24 patterns (CSN format)
  log('\n=== WP-24 patterns in HTML ===');
  const html = document.documentElement.innerHTML;
  const wpMatches = [...new Set(html.match(/WP-24[A-Za-z0-9_\-+=\/%.]{20,}/g) || [])];
  log('Unique WP-24 patterns: ' + wpMatches.length);
  wpMatches.slice(0, 5).forEach(id => log('  ' + id.substring(0, 80)));

  // 4. Check for Angular router links
  log('\n=== Router links (routerLink, ng-href, [href]) ===');
  document.querySelectorAll('[routerlink], [ng-href], [routerLink]').forEach(el => {
    const rl = el.getAttribute('routerlink') || el.getAttribute('routerLink') || el.getAttribute('ng-href') || '';
    if (rl) log('  ' + el.tagName + ': ' + rl);
  });

  // 5. Check onclick handlers
  log('\n=== Elements with onclick/click handlers near visit cards ===');
  document.querySelectorAll('[onclick], [ng-click], [(click)]').forEach(el => {
    const handler = el.getAttribute('onclick') || el.getAttribute('ng-click') || el.getAttribute('(click)') || '';
    if (handler.length > 5) log('  ' + el.tagName + ': ' + handler.substring(0, 100));
  });

  // 6. Look at the "View After Visit Summary" links specifically
  log('\n=== "View After Visit Summary" links ===');
  document.querySelectorAll('a').forEach(a => {
    if (a.textContent.includes('Visit Summary') || a.textContent.includes('AVS')) {
      log('  text: ' + a.textContent.trim());
      log('  href: ' + (a.getAttribute('href') || 'none'));
      log('  all attrs: ' + Array.from(a.attributes).map(at => at.name + '=' + at.value.substring(0, 80)).join(', '));
      const parent = a.closest('[class]');
      if (parent) log('  parent class: ' + parent.className.substring(0, 100));
    }
  });

  // 7. Dump all unique href patterns
  log('\n=== All unique href patterns ===');
  const hrefPatterns = new Set();
  document.querySelectorAll('a[href]').forEach(a => {
    const href = a.getAttribute('href') || '';
    const pattern = href.replace(/WP-[A-Za-z0-9_\-+=\/%.]+/g, '<ID>').replace(/[a-f0-9]{32}/g, '<HEX>');
    hrefPatterns.add(pattern);
  });
  [...hrefPatterns].filter(p => p.includes('isit') || p.includes('past') || p.includes('csn') || p.includes('AVS') || p.includes('ummary') || p.includes('detail')).forEach(p => log('  ' + p));
})();
