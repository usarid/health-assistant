#!/usr/bin/env python3
"""
Process scraped MyChart messages into FHIR R4 Communication resources.
"""
import json
import re
import sys
from datetime import datetime
from html.parser import HTMLParser

class HTMLTextExtractor(HTMLParser):
    def __init__(self):
        super().__init__()
        self.text = []
    def handle_data(self, data):
        self.text.append(data.strip())
    def get_text(self):
        return ' '.join(t for t in self.text if t)

def html_to_text(html):
    if not html:
        return ''
    extractor = HTMLTextExtractor()
    try:
        extractor.feed(html)
        return extractor.get_text()
    except:
        return re.sub(r'<[^>]+>', '', html)

def parse_preview(preview_text):
    """Parse a preview string like 'Subject TextSenderDateMM/DD/YYYY Body preview...'"""
    result = {'subject': '', 'sender': '', 'date': '', 'snippet': '', 'raw': preview_text}
    
    if not preview_text:
        return result
    
    # Try to extract date patterns
    date_match = re.search(r'(\d{1,2}/\d{1,2}/\d{2,4})', preview_text)
    if date_match:
        result['date'] = date_match.group(1)
    else:
        # Try "Mon DD" format
        date_match = re.search(r'((?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+\d{1,2})', preview_text)
        if date_match:
            result['date'] = date_match.group(1)
    
    # The preview format is: SubjectSenderDate snippet
    # Try to split on "You" (patient's messages) or known sender patterns
    parts = re.split(r'(You|(?:[A-Z][a-z]+ [A-Z]))', preview_text, maxsplit=1)
    if len(parts) > 1:
        result['subject'] = parts[0].strip()
    
    return result

def classify_institution(preview):
    """Classify which institution a message is from based on preview text.

    DEPRECATED: This keyword-based classifier is unreliable and outdated.
    See docs/CONCLUSIONS_LOG.md C-009 for the methodology problem (clinician
    name is not a reliable proxy for Epic instance) and tools/v2/convert_messages.py
    for the replacement, which uses authoritative organizationId from the
    raw scrape rather than text classification.

    This stub remains so existing call sites don't break. Patient-specific
    keyword lists were removed; the function now only uses generic institutional
    names available from the preview itself.
    """
    p = preview.lower()
    if 'mskcc' in p or 'sloan' in p:
        return 'MSKCC'
    if 'ucsf' in p:
        return 'UCSF'
    if 'stanford' in p:
        return 'Stanford'
    if 'sutter' in p or 'pacific internal' in p:
        return 'Sutter'
    if 'mayo' in p:
        return 'Mayo'
    if 'marinhealth' in p or 'marin health' in p:
        return 'MarinHealth'
    return 'Unknown'

def create_fhir_communication(thread, index):
    """Create a FHIR Communication resource from a thread."""
    preview = thread.get('preview', '')
    conv_id = thread.get('conversationId', f'msg-{index}')
    parsed = parse_preview(preview)
    institution = classify_institution(preview)
    
    comm = {
        'resourceType': 'Communication',
        'status': 'completed',
        'meta': {
            'source': 'https://mskmychart.mskcc.org',
            'tag': [{'system': 'http://local/institution', 'code': institution}]
        },
        'identifier': [{
            'system': 'https://mskmychart.mskcc.org/conversation',
            'value': conv_id[:100]
        }],
    }
    
    # Add subject/topic
    if parsed['subject']:
        comm['topic'] = {'text': parsed['subject'][:200]}
    
    # Handle full API response (3 threads have this)
    resp = thread.get('response')
    if resp and resp.get('messages'):
        messages = resp['messages']
        
        # Use the first message's delivery time
        first_date = messages[0].get('deliveryInstantISO', '')
        if first_date:
            comm['sent'] = first_date
        
        # Build payload from all messages in thread
        payloads = []
        for m in messages:
            body = html_to_text(m.get('body', ''))
            date = m.get('deliveryInstantISO', '')
            author = m.get('author', {})
            author_name = author.get('displayName', '')
            
            # Determine if patient or provider
            is_patient = 'wprKey' in author  # wprKey = patient, empKey = employee/provider
            sender = 'Patient' if is_patient else (author_name or 'Provider')
            
            msg_text = f"[{date}] {sender}: {body}" if body else ''
            if msg_text:
                payloads.append({'contentString': msg_text[:5000]})
        
        if payloads:
            comm['payload'] = payloads
        
        # Add participant info
        users = resp.get('users', {})
        if users:
            participants = []
            for uid, uinfo in users.items():
                name = uinfo.get('name', '')
                if name:
                    participants.append(name)
            if participants:
                comm['note'] = [{'text': f"Participants: {', '.join(participants)}"}]
    else:
        # Preview-only thread — extract what we can
        if parsed['date']:
            # Try to convert to ISO date
            for fmt in ['%m/%d/%Y', '%m/%d/%y', '%b %d']:
                try:
                    d = datetime.strptime(parsed['date'], fmt)
                    if d.year < 100:
                        d = d.replace(year=d.year + 2000)
                    if d.year == 1900:  # "Mar 18" without year
                        d = d.replace(year=2026)
                    comm['sent'] = d.strftime('%Y-%m-%dT00:00:00Z')
                    break
                except ValueError:
                    continue
        
        # Store the preview as payload
        if preview:
            comm['payload'] = [{'contentString': preview[:5000]}]
    
    return comm

def main():
    input_file = sys.argv[1] if len(sys.argv) > 1 else '/sessions/admiring-vigilant-brown/mnt/Medical/Synthesis/mskcc_messages_scraped.json'
    
    with open(input_file) as f:
        data = json.load(f)
    
    threads = data.get('threads', [])
    print(f"Processing {len(threads)} threads...")
    
    # Create FHIR Bundle
    bundle = {
        'resourceType': 'Bundle',
        'type': 'transaction',
        'entry': []
    }
    
    institution_counts = {}
    full_api_count = 0
    preview_only_count = 0
    
    for i, thread in enumerate(threads):
        comm = create_fhir_communication(thread, i)
        institution = classify_institution(thread.get('preview', ''))
        institution_counts[institution] = institution_counts.get(institution, 0) + 1
        
        if thread.get('response'):
            full_api_count += 1
        else:
            preview_only_count += 1
        
        bundle['entry'].append({
            'resource': comm,
            'request': {
                'method': 'POST',
                'url': 'Communication'
            }
        })
    
    # Save bundle
    output_file = input_file.replace('.json', '_fhir_bundle.json')
    with open(output_file, 'w') as f:
        json.dump(bundle, f, indent=2)
    
    print(f"\n=== Processing Complete ===")
    print(f"Total threads: {len(threads)}")
    print(f"Full API responses: {full_api_count}")
    print(f"Preview-only: {preview_only_count}")
    print(f"\nBy institution:")
    for inst, count in sorted(institution_counts.items(), key=lambda x: -x[1]):
        print(f"  {inst}: {count}")
    print(f"\nFHIR bundle saved to: {output_file}")
    print(f"Bundle entries: {len(bundle['entry'])}")
    
    return output_file

if __name__ == '__main__':
    main()
