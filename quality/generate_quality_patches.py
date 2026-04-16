#!/usr/bin/env python3
"""Phase 2: Generate FHIR patch bundles to fix data quality issues.

Reads quality_issues_export.json and produces FHIR transaction bundles
that PATCH observations with:
  1. Computed interpretations (H/L/N) from value vs reference range
  2. Negative interpretations for "Not Detected" results
  3. Proper valueQuantity for numeric strings like "<10"
  4. Extracted values from narrative text (VALUE_SEE_COMMENTS, VALUE_MISSING)

Output: quality_patches_bundle.json  (FHIR transaction bundle with PUT operations)
"""

import json
import re
import sys
from collections import Counter
from fhir_utils import make_meta_tag

INPUT  = 'quality_issues_export.json'
OUTPUT = 'quality_patches_bundle.json'
SOURCE_TAG = make_meta_tag('quality-patch-v1', 'Automated Quality Patches v1')

stats = Counter()


def parse_comparator_value(s):
    """Parse strings like '<10', '<=0.1', '>100', '>=5.0', '0.5' into (comparator, float).
    Returns (None, None) if unparseable.
    """
    if not s:
        return None, None
    s = s.strip()
    m = re.match(r'^([<>]=?)\s*([\d.]+)$', s)
    if m:
        return m.group(1), float(m.group(2))
    try:
        return None, float(s)
    except (ValueError, TypeError):
        return None, None


def compute_interpretation(value, ref_range):
    """Compare a numeric value against reference range, return FHIR interpretation code.
    Returns: 'H', 'L', 'N', 'HH', 'LL', or None if can't determine.
    """
    if value is None or not ref_range:
        return None

    rr = ref_range[0]  # Use first reference range
    low = rr.get('low', {}).get('value')
    high = rr.get('high', {}).get('value')

    if low is None and high is None:
        # Try parsing text like "10-50" or "<10" or ">100"
        text = rr.get('text', '')
        m = re.match(r'^\s*([\d.]+)\s*[-–]\s*([\d.]+)\s*$', text)
        if m:
            low, high = float(m.group(1)), float(m.group(2))
        else:
            return None

    if low is not None and high is not None:
        if value < low:
            return 'LL' if value < low * 0.5 and low > 0 else 'L'
        elif value > high:
            return 'HH' if high > 0 and value > high * 2 else 'H'
        else:
            return 'N'
    elif low is not None:
        return 'L' if value < low else 'N'
    elif high is not None:
        return 'H' if value > high else 'N'

    return None


INTERP_CODES = {
    'H':  {'system': 'http://terminology.hl7.org/CodeSystem/v3-ObservationInterpretation', 'code': 'H',  'display': 'High'},
    'HH': {'system': 'http://terminology.hl7.org/CodeSystem/v3-ObservationInterpretation', 'code': 'HH', 'display': 'Critical high'},
    'L':  {'system': 'http://terminology.hl7.org/CodeSystem/v3-ObservationInterpretation', 'code': 'L',  'display': 'Low'},
    'LL': {'system': 'http://terminology.hl7.org/CodeSystem/v3-ObservationInterpretation', 'code': 'LL', 'display': 'Critical low'},
    'N':  {'system': 'http://terminology.hl7.org/CodeSystem/v3-ObservationInterpretation', 'code': 'N',  'display': 'Normal'},
    'NEG': {'system': 'http://terminology.hl7.org/CodeSystem/v3-ObservationInterpretation', 'code': 'NEG', 'display': 'Negative'},
    'POS': {'system': 'http://terminology.hl7.org/CodeSystem/v3-ObservationInterpretation', 'code': 'POS', 'display': 'Positive'},
}


def make_patch_entry(obs_id, patch_fields):
    """Build a FHIR transaction entry that PATCHes an Observation.

    We use PUT with the full resource to ensure HAPI processes it cleanly.
    But since we don't have the full resource, we'll use a conditional approach:
    actually we'll build a minimal resource with just the fields we want to set.

    Better approach: use FHIR Patch (JSON Patch) operations.
    """
    operations = []
    for path, value in patch_fields.items():
        operations.append({
            'op': 'replace' if 'interpretation' in path else 'add',
            'path': f'/{path}',
            'value': value
        })

    return {
        'resource': {
            'resourceType': 'Parameters',
            'parameter': [{
                'name': 'operation',
                'part': [
                    {'name': 'type', 'valueCode': op['op']},
                    {'name': 'path', 'valueString': op['path']},
                    {'name': 'value', 'valueString': json.dumps(op['value']) if isinstance(op['value'], (dict, list)) else str(op['value'])}
                ]
            } for op in operations]
        },
        'request': {
            'method': 'PATCH',
            'url': f'Observation/{obs_id}'
        }
    }


def build_put_entry(obs):
    """Build a FHIR PUT entry with patched fields merged into original observation data.

    Since HAPI FHIR's JSON Patch can be finicky, we use GET-then-PUT approach:
    the export already has the observation data, so we build a script that
    GETs, patches in memory, and PUTs back.

    Actually, the simplest reliable approach for HAPI: generate a Python script
    that fetches each observation, patches it, and PUTs it back.

    But for maximum reliability and the user's workflow, let's generate
    individual PUT-ready resources by merging our patches into what we know.

    REVISED APPROACH: Generate a self-contained Python script that:
    1. GETs each observation from HAPI
    2. Applies patches
    3. PUTs it back
    This is the most reliable approach for HAPI FHIR.
    """
    pass  # We'll use the script-generation approach instead


def fix_interp_with_ref(obs):
    """Fix INTERP_MISSING_WITH_REF: compute interpretation from value vs reference range."""
    vq = obs.get('valueQuantity')
    if not vq or vq.get('value') is None:
        # Try valueString as numeric
        vs = obs.get('valueString')
        if vs:
            comp, val = parse_comparator_value(vs)
            if val is not None:
                return compute_interpretation(val, obs.get('referenceRange', []))
        return None

    value = vq['value']
    interp = compute_interpretation(value, obs.get('referenceRange', []))
    return interp


def fix_interp_negative(obs):
    """Fix INTERP_MISSING_NEGATIVE: set interpretation to NEG."""
    return 'NEG'


def fix_interp_positive(obs):
    """Fix INTERP_MISSING_POSITIVE: set interpretation to POS."""
    return 'POS'


def fix_numeric_string(obs):
    """Fix VALUE_NUMERIC_AS_STRING: parse '<10' etc into proper valueQuantity."""
    vs = obs.get('valueString', '')
    comp, val = parse_comparator_value(vs)
    if val is None:
        return None

    # Try to get unit from reference range or code
    unit = None
    unit_code = None
    rr = obs.get('referenceRange', [])
    if rr:
        for r in rr:
            for key in ['low', 'high']:
                if key in r and 'unit' in r[key]:
                    unit = r[key]['unit']
                    unit_code = r[key].get('code', unit)
                    break
            if unit:
                break

    result = {
        'value': val,
    }
    if comp:
        # FHIR comparator: < is 'lt', <= is 'le', > is 'gt', >= is 'ge'
        comp_map = {'<': '<', '<=': '<=', '>': '>', '>=': '>='}
        result['comparator'] = comp_map.get(comp, comp)
    if unit:
        result['unit'] = unit
        result['system'] = 'http://unitsofmeasure.org'
        result['code'] = unit_code or unit

    return result


def extract_from_narrative(obs):
    """Fix VALUE_SEE_COMMENTS / VALUE_MISSING: try to extract structured data from narrative.

    This handles the hardest cases - reading the narrative text blob and
    extracting meaningful structured values.
    """
    narratives = obs.get('parent_narratives', {})
    if not narratives:
        return None

    codes = obs.get('code_coding', [])
    code_display = codes[0]['display'] if codes else obs.get('code_text', '')

    # Combine all parent narratives
    full_text = ' '.join(narratives.values())

    # Known patterns for common "see comments" results
    result = {}

    # Fasting status - very common, extract from "FASTING:NO" or "FASTING:YES"
    if 'fasting' in code_display.lower():
        m = re.search(r'FASTING:?\s*(YES|NO)', full_text, re.I)
        if m:
            val = m.group(1).upper()
            result['valueString'] = val
            stats['narrative_fasting_fixed'] += 1
            return result

    # COMMENT observations - usually metadata, mark as reviewed
    if code_display.upper() == 'COMMENT':
        # These are typically lab comments, not actual results
        stats['narrative_comment_skipped'] += 1
        return None

    # Cryoglobulin - look for "No cryoglobulin detected" or similar
    if 'cryoglobulin' in code_display.lower() or 'cryoglobulin' in full_text.lower():
        if re.search(r'no\s+cryoglobulin\s+detected|cryoglobulin[:\s]+negative|not\s+detected', full_text, re.I):
            result['valueString'] = 'Not Detected'
            result['interpretation'] = 'NEG'
            stats['narrative_cryoglobulin_fixed'] += 1
            return result
        # If cryoglobulin panel but can't determine result from short narrative
        if 'cryoglobulin' in code_display.lower():
            stats['narrative_cryoglobulin_insufficient'] += 1
            return None

    # Culture results - "No growth" patterns, also handle "SEE NOTE" cultures
    if 'culture' in code_display.lower():
        if re.search(r'no\s+growth|no\s+organism|negative|normal\s+flora', full_text, re.I):
            result['valueString'] = 'No Growth'
            result['interpretation'] = 'NEG'
            stats['narrative_culture_fixed'] += 1
            return result
        # Short narrative cultures - the actual result may not be in narrative
        vs = obs.get('valueString', '') or ''
        if 'SEE NOTE' in vs or 'SEE COMMENT' in vs:
            stats['narrative_culture_see_note'] += 1
            return None

    # BUN/Creat Ratio - try to find the numeric value in narrative
    if 'bun' in code_display.lower() and 'creat' in code_display.lower():
        # Look for BUN/Creatinine Ratio pattern in narrative
        m = re.search(r'BUN/Creat(?:inine)?\s+Ratio[:\s]+(\d+\.?\d*)', full_text, re.I)
        if m:
            result['valueQuantity'] = {'value': float(m.group(1))}
            stats['narrative_ratio_fixed'] += 1
            return result

    # "Results" from "Lab Unlisted 1" - SEE COMMENTS with no extractable data
    if code_display.lower() == 'results' and 'lab unlisted' in full_text.lower():
        stats['narrative_lab_unlisted_skipped'] += 1
        return None

    # QuantiFERON TB interpretation
    if 'qualitative' in code_display.lower() and 'tb' in full_text.lower():
        if re.search(r'positive', full_text, re.I):
            result['valueString'] = 'Positive'
            result['interpretation'] = 'POS'
            stats['narrative_tb_fixed'] += 1
            return result
        elif re.search(r'negative', full_text, re.I):
            result['valueString'] = 'Negative'
            result['interpretation'] = 'NEG'
            stats['narrative_tb_fixed'] += 1
            return result

    # Monoclonal protein / QMPTS interpretation
    if 'interpretation' in code_display.lower() or 'qmpts' in code_display.lower():
        # These are complex interpretation texts - extract key finding
        if re.search(r'no\s+monoclonal|no\s+m[- ]?spike|not\s+detected|no\s+abnormal', full_text, re.I):
            result['valueString'] = 'No monoclonal protein detected'
            result['interpretation'] = 'NEG'
            stats['narrative_monoclonal_fixed'] += 1
            return result
        elif re.search(r'monoclonal\s+protein|m[- ]?spike|abnormal\s+band|igg\s+(?:kappa|lambda)', full_text, re.I):
            # Has monoclonal protein - extract key info
            m = re.search(r'monoclonal\s+(?:protein|band|spike)[:\s]+([^\n.]{10,80})', full_text, re.I)
            if m:
                result['valueString'] = m.group(1).strip()[:80]
                result['interpretation'] = 'POS' if re.search(r'abnormal|detected|present', full_text, re.I) else None
                stats['narrative_monoclonal_found'] += 1
                return result

    # Calprotectin interpretation
    if 'calprotectin' in code_display.lower() and 'interp' in code_display.lower():
        m = re.search(r'calprotectin[:\s]+(\d+\.?\d*)', full_text, re.I)
        if m:
            val = float(m.group(1))
            # Standard interpretation: <50 normal, 50-200 borderline, >200 elevated
            if val < 50:
                result['valueString'] = 'Normal'
                result['interpretation'] = 'N'
            elif val <= 200:
                result['valueString'] = 'Borderline elevated'
                result['interpretation'] = 'H'
            else:
                result['valueString'] = 'Elevated'
                result['interpretation'] = 'HH'
            stats['narrative_calprotectin_fixed'] += 1
            return result

    # Cytogenetics / karyotype
    if 'cytogenetic' in code_display.lower() or 'karyotype' in code_display.lower():
        if re.search(r'normal\s+(?:male|female)\s+karyotype|46,\s*X[XY]', full_text, re.I):
            result['valueString'] = 'Normal karyotype'
            result['interpretation'] = 'N'
            stats['narrative_cytogenetics_fixed'] += 1
            return result
        elif re.search(r'abnormal|complex|translocation|deletion|trisomy', full_text, re.I):
            m = re.search(r'((?:abnormal|complex)[^\n.]{0,100})', full_text, re.I)
            if m:
                result['valueString'] = m.group(1).strip()[:100]
                stats['narrative_cytogenetics_found'] += 1
                return result

    # MSK-IMPACT / genomic results
    if 'diagnostic' in code_display.lower() and 'interpretation' in code_display.lower():
        # Extract key genomic findings
        mutations = re.findall(r'(?:mutation|variant|alteration)[:\s]+([^\n.]{5,60})', full_text, re.I)
        if mutations:
            result['valueString'] = '; '.join(m.strip() for m in mutations[:3])[:120]
            stats['narrative_genomic_found'] += 1
            return result

    # MPV (Mean Platelet Volume) - find in narrative
    if 'mpv' in code_display.lower():
        m = re.search(r'MPV[:\s]+([\d.]+)\s*(fL)?', full_text, re.I)
        if m:
            result['valueQuantity'] = {
                'value': float(m.group(1)),
                'unit': 'fL',
                'system': 'http://unitsofmeasure.org',
                'code': 'fL'
            }
            stats['narrative_mpv_fixed'] += 1
            return result

    # K/L Ratio (Kappa/Lambda)
    if 'k' in code_display.lower() and 'l' in code_display.lower() and 'ratio' in code_display.lower():
        m = re.search(r'(?:kappa|K)\s*/\s*(?:lambda|L)\s*(?:ratio)?[:\s]+([\d.]+)', full_text, re.I)
        if not m:
            m = re.search(r'(?:free\s+)?(?:kappa|K)/(?:lambda|L)[:\s]+([\d.]+)', full_text, re.I)
        if m:
            result['valueQuantity'] = {'value': float(m.group(1))}
            stats['narrative_kl_ratio_fixed'] += 1
            return result

    # Corrected Calcium
    if 'corrected' in code_display.lower() and 'calcium' in code_display.lower():
        m = re.search(r'corrected\s+calcium[:\s]+([\d.]+)\s*(mg/dL)?', full_text, re.I)
        if m:
            result['valueQuantity'] = {
                'value': float(m.group(1)),
                'unit': 'mg/dL',
                'system': 'http://unitsofmeasure.org',
                'code': 'mg/dL'
            }
            stats['narrative_corrected_calcium_fixed'] += 1
            return result

    stats['narrative_no_match'] += 1
    return None


def main():
    with open(INPUT) as f:
        data = json.load(f)

    print(f"Loaded {len(data['problem_observations'])} problem observations")
    print(f"Summary: {json.dumps(data['summary']['issue_counts'], indent=2)}")
    print()

    patches = []  # List of (obs_id, {field: value}) tuples

    for obs in data['problem_observations']:
        obs_id = obs['observation_id']
        issues = obs['issues']
        patch = {}

        # Skip NO_TAG (Apple Health / wearable) data for now
        tags = obs.get('source_tags', [])
        if not tags:
            stats['skipped_no_tag'] += 1
            continue

        # 1. Fix INTERP_MISSING_WITH_REF
        if 'INTERP_MISSING_WITH_REF' in issues:
            interp = fix_interp_with_ref(obs)
            if interp and interp in INTERP_CODES:
                patch['interpretation'] = [{'coding': [INTERP_CODES[interp]]}]
                stats[f'interp_{interp}'] += 1
                stats['interp_fixed'] += 1
            else:
                stats['interp_could_not_compute'] += 1

        # 2. Fix INTERP_MISSING_NEGATIVE
        if 'INTERP_MISSING_NEGATIVE' in issues:
            patch['interpretation'] = [{'coding': [INTERP_CODES['NEG']]}]
            stats['interp_neg_fixed'] += 1

        # 3. Fix INTERP_MISSING_POSITIVE
        if 'INTERP_MISSING_POSITIVE' in issues:
            patch['interpretation'] = [{'coding': [INTERP_CODES['POS']]}]
            stats['interp_pos_fixed'] += 1

        # 4. Fix VALUE_NUMERIC_AS_STRING
        if 'VALUE_NUMERIC_AS_STRING' in issues:
            vq = fix_numeric_string(obs)
            if vq:
                patch['valueQuantity'] = vq
                patch.pop('valueString', None)  # Signal to remove valueString
                patch['_remove_valueString'] = True
                stats['numeric_string_fixed'] += 1
            else:
                stats['numeric_string_failed'] += 1

        # 5. Fix VALUE_SEE_COMMENTS / VALUE_MISSING (narrative extraction)
        if 'VALUE_SEE_COMMENTS' in issues or ('VALUE_MISSING' in issues and obs.get('parent_narratives')):
            extracted = extract_from_narrative(obs)
            if extracted:
                if 'valueString' in extracted:
                    patch['valueString'] = extracted['valueString']
                if 'valueQuantity' in extracted:
                    patch['valueQuantity'] = extracted['valueQuantity']
                if 'interpretation' in extracted and extracted['interpretation']:
                    patch['interpretation'] = [{'coding': [INTERP_CODES[extracted['interpretation']]]}]
                stats['narrative_extracted'] += 1
            else:
                stats['narrative_extraction_failed'] += 1

        if patch:
            patches.append((obs_id, patch))

    print(f"\n=== PATCH STATISTICS ===")
    for key, val in sorted(stats.items()):
        print(f"  {key}: {val}")
    print(f"\nTotal patches to apply: {len(patches)}")

    # Generate the patcher script (fetches from HAPI, applies patches, PUTs back)
    generate_patcher_script(patches)

    # Also generate a summary for review
    generate_patch_review(patches, data)

    print(f"\nGenerated: apply_quality_patches.py ({len(patches)} patches)")
    print(f"Generated: quality_patches_review.json (for spot-checking)")


def generate_patcher_script(patches):
    """Generate a Python script that applies patches to HAPI FHIR server."""

    # Group patches by type for reporting
    script = '''#!/usr/bin/env python3
"""Apply quality patches to HAPI FHIR server.

Auto-generated by generate_quality_patches.py
Fetches each Observation, applies patches, and PUTs back.

Usage: python3 apply_quality_patches.py [--dry-run]
"""

import json
import sys
import requests

HAPI_BASE = 'http://localhost:8080/fhir'
DRY_RUN = '--dry-run' in sys.argv

PATCHES_JSON = """'''

    script += json.dumps(patches, indent=2)

    script += '''"""

PATCHES = json.loads(PATCHES_JSON)

def apply_patches():
    success = 0
    failed = 0
    skipped = 0

    for obs_id, patch in PATCHES:
        # GET current observation
        url = f'{HAPI_BASE}/Observation/{obs_id}'
        resp = requests.get(url)
        if resp.status_code != 200:
            print(f'  SKIP {obs_id}: GET returned {resp.status_code}')
            skipped += 1
            continue

        obs = resp.json()

        # Apply patches
        remove_value_string = patch.pop('_remove_valueString', False)

        for key, value in patch.items():
            obs[key] = value

        if remove_value_string and 'valueString' in obs and 'valueQuantity' in patch:
            del obs['valueString']

        # Add source tag
        if 'meta' not in obs:
            obs['meta'] = {}
        if 'tag' not in obs['meta']:
            obs['meta']['tag'] = []

        # Check if tag already present
        tag_codes = [t.get('code') for t in obs['meta']['tag']]
        if 'quality-patch-v1' not in tag_codes:
            obs['meta']['tag'].append({
                'system': 'http://example.org/source',
                'code': 'quality-patch-v1',
                'display': 'Automated Quality Patches v1'
            })

        if DRY_RUN:
            print(f'  DRY-RUN would patch {obs_id}: {list(patch.keys())}')
            success += 1
            continue

        # PUT back
        resp = requests.put(
            url,
            json=obs,
            headers={'Content-Type': 'application/fhir+json'}
        )
        if resp.status_code in (200, 201):
            success += 1
        else:
            print(f'  FAIL {obs_id}: PUT returned {resp.status_code}')
            try:
                err = resp.json()
                if 'issue' in err:
                    print(f'    {err["issue"][0].get("diagnostics", "")}')
            except:
                pass
            failed += 1

    prefix = '[DRY-RUN] ' if DRY_RUN else ''
    print(f'\\n{prefix}Results: {success} patched, {failed} failed, {skipped} skipped')


if __name__ == '__main__':
    total = len(PATCHES)
    print(f'Applying {total} quality patches to HAPI FHIR...')
    if DRY_RUN:
        print('(DRY RUN - no changes will be made)')
    apply_patches()
'''

    with open('apply_quality_patches.py', 'w') as f:
        f.write(script)


def generate_patch_review(patches, data):
    """Generate a review file showing what each patch does for spot-checking."""
    review = []
    obs_map = {o['observation_id']: o for o in data['problem_observations']}

    for obs_id, patch in patches[:50]:  # First 50 for review
        obs = obs_map.get(obs_id, {})
        codes = obs.get('code_coding', [])
        display = codes[0]['display'] if codes else obs.get('code_text', '?')

        entry = {
            'obs_id': obs_id,
            'test_name': display,
            'date': obs.get('effectiveDateTime', '?')[:10],
            'original_issues': obs.get('issues', []),
            'original_value': None,
            'patch': {}
        }

        if obs.get('valueQuantity'):
            entry['original_value'] = f"{obs['valueQuantity'].get('value')} {obs['valueQuantity'].get('unit', '')}"
        elif obs.get('valueString'):
            entry['original_value'] = obs['valueString']

        # Describe the patch in human terms
        for key, value in patch.items():
            if key == '_remove_valueString':
                continue
            if key == 'interpretation' and isinstance(value, list):
                code = value[0]['coding'][0]['code']
                entry['patch']['interpretation'] = code
            elif key == 'valueQuantity':
                entry['patch']['valueQuantity'] = value
            elif key == 'valueString':
                entry['patch']['valueString'] = value

        if obs.get('referenceRange'):
            rr = obs['referenceRange'][0]
            lo = rr.get('low', {}).get('value', '')
            hi = rr.get('high', {}).get('value', '')
            entry['reference_range'] = f'{lo}-{hi}'

        review.append(entry)

    with open('quality_patches_review.json', 'w') as f:
        json.dump(review, f, indent=2)


if __name__ == '__main__':
    main()
