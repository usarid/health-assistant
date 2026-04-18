"""Personal Health Vault — API Layer

Sits between nginx and HAPI FHIR, providing:
  - /api/search: synonym-expanded search across FHIR resources
  - /api/timeline/metrics: dynamically discover available timeline metrics
  - /api/synonyms: expose synonym groups for UI autocomplete
  - /api/health: health check

The FHIR proxy (/fhir/*) stays in nginx for performance — no need to
route raw FHIR traffic through Python.
"""

import asyncio
import os
import time
from collections import defaultdict
from typing import Optional

import httpx
from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware

# Import shared synonym module (mounted at /app in Docker)
import sys
sys.path.insert(0, os.environ.get('PHV_LIB', '/app'))
from loinc_synonyms import SYNONYM_GROUPS, SEARCH_SYNONYMS, expand_query, expand_search_terms, tokenize, BOILERPLATE_WORDS

HAPI_BASE = os.environ.get('HAPI_BASE', 'http://hapi:8080/fhir')

app = FastAPI(
    title='Personal Health Vault API',
    version='1.0.0',
    description='Synonym-expanded search and utilities for the Personal Health Vault.',
)

# ── AI routes ──
from assistant import router as assistant_router
from narrator import router as narrator_router
from analyst import router as analyst_router
from meds import router as meds_router
from patient_profile import router as profile_router
from epic_oauth import router as epic_router
from reminders import router as reminders_router
app.include_router(assistant_router)
app.include_router(narrator_router)
app.include_router(analyst_router)
app.include_router(meds_router)
app.include_router(profile_router)
app.include_router(epic_router)
app.include_router(reminders_router)

app.add_middleware(
    CORSMiddleware,
    allow_origins=['*'],
    allow_methods=['*'],
    allow_headers=['*'],
)

# Shared async HTTP client — reused across requests
_client: Optional[httpx.AsyncClient] = None


async def get_client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(base_url=HAPI_BASE, timeout=30.0)
    return _client


@app.on_event('shutdown')
async def shutdown():
    global _client
    if _client and not _client.is_closed:
        await _client.aclose()


# ═══════════════════════════════════════════════════════════════
# Health check
# ═══════════════════════════════════════════════════════════════

@app.get('/api/health')
async def health():
    """Health check — also verifies HAPI connectivity."""
    client = await get_client()
    try:
        r = await client.get('/metadata', params={'_summary': 'true'})
        hapi_ok = r.status_code == 200
    except Exception:
        hapi_ok = False
    return {
        'status': 'ok' if hapi_ok else 'degraded',
        'hapi': 'connected' if hapi_ok else 'unreachable',
        'synonym_groups': len(SYNONYM_GROUPS),
    }


# ═══════════════════════════════════════════════════════════════
# Synonym endpoints
# ═══════════════════════════════════════════════════════════════

@app.get('/api/synonyms')
async def get_synonyms():
    """Return all synonym groups for UI display / autocomplete."""
    return {
        'validation_groups': len(SYNONYM_GROUPS),
        'search_groups': SEARCH_SYNONYMS,
        'count': len(SEARCH_SYNONYMS),
    }


@app.get('/api/synonyms/expand')
async def expand(q: str = Query(..., description='Search term to expand')):
    """Expand a search term with phrase-level synonyms."""
    terms = expand_search_terms(q)
    return {
        'original': q,
        'search_terms': terms,
        'word_expansion': sorted(expand_query(q)),
    }


# ═══════════════════════════════════════════════════════════════
# Synonym-expanded FHIR search
# ═══════════════════════════════════════════════════════════════

# FHIR search parameter mappings per resource type
SEARCH_PARAMS = {
    'Observation':         [{'code:text': None}, {'value-string': None}],
    'DiagnosticReport':    [{'code:text': None}, {'conclusion': None}],
    'Condition':           [{'code:text': None}],
    'MedicationRequest':   [{'code:text': None}],
    'Procedure':           [{'code:text': None}],
    'DocumentReference':   [{'description': None}, {'type:text': None}],
    'Immunization':        [{'vaccine-code:text': None}],
    'AllergyIntolerance':  [{'code:text': None}],
    'Communication':       [{'_text': None}, {'topic': None}, {'subject': None}],
    'Encounter':           [{'type:text': None}, {'reason-code:text': None}],
}

ALL_TYPES = list(SEARCH_PARAMS.keys())


async def search_fhir(client, resource_type: str, term: str, limit: int) -> list:
    """Search a single FHIR resource type with one term. Returns entries."""
    import logging
    log = logging.getLogger('search_fhir')
    entries = []
    params_list = SEARCH_PARAMS.get(resource_type, [{'code:text': None}])

    # FHIR sort parameter varies by resource type; not all support 'date'
    SORT_PARAM = {
        'Communication': '-sent',
        'Condition': '-recorded-date',
        'Encounter': '-date',
        'DocumentReference': '-date',
        'AllergyIntolerance': '-date',
    }
    sort = SORT_PARAM.get(resource_type, '-date')

    # Full-text search via _content (OpenSearch); omit _sort since results
    # are relevance-ranked and the API does a final sort anyway.
    try:
        r = await client.get(f'/{resource_type}', params={
            '_content': term, '_count': str(limit),
        })
        if r.status_code == 200:
            bundle = r.json()
            entries.extend(bundle.get('entry', []))
        else:
            log.warning(f'_content search {resource_type} term={term!r}: HTTP {r.status_code}')
    except Exception as e:
        log.warning(f'_content search {resource_type} term={term!r} failed: {e}')

    # Field-specific searches
    for param_template in params_list:
        for param_name in param_template:
            try:
                r = await client.get(f'/{resource_type}', params={
                    param_name: term, '_count': str(limit), '_sort': sort,
                })
                if r.status_code == 200:
                    bundle = r.json()
                    entries.extend(bundle.get('entry', []))
                else:
                    log.warning(f'{param_name} search {resource_type} term={term!r}: HTTP {r.status_code}')
            except Exception as e:
                log.warning(f'{param_name} search {resource_type} term={term!r} failed: {e}')

    return entries


def dedup_entries(entries: list) -> list:
    """Deduplicate FHIR bundle entries by resource ID."""
    seen = set()
    result = []
    for e in entries:
        res = e.get('resource', {})
        key = f"{res.get('resourceType')}/{res.get('id')}"
        if key not in seen:
            seen.add(key)
            result.append(e)
    return result


def entry_sort_key(entry):
    """Sort key for FHIR entries — most recent first."""
    r = entry.get('resource', {})
    return r.get('effectiveDateTime') or r.get('issued') or \
        r.get('recordedDate') or r.get('authoredOn') or \
        r.get('date') or r.get('sent') or \
        (r.get('period', {}) or {}).get('start', '') or ''


@app.get('/api/search')
async def search(
    q: str = Query(..., description='Search query'),
    type: Optional[str] = Query(None, description='FHIR resource type (blank = all)'),
    limit: int = Query(50, description='Max results per search term per resource type'),
    expand_synonyms: bool = Query(True, description='Expand query with synonyms'),
):
    """Synonym-expanded search across FHIR resources.

    Expands the query with known medical synonyms (e.g., "CRP" also
    searches "c-reactive protein"), fans out searches in parallel,
    and returns deduplicated, sorted results.
    """
    client = await get_client()

    # Determine search terms (phrase-level synonym expansion)
    if expand_synonyms:
        search_terms = expand_search_terms(q)
    else:
        search_terms = [q]

    # Determine resource types to search
    types_to_search = [type] if type else ALL_TYPES

    # Fan out all searches in parallel
    tasks = []
    for term in search_terms:
        for rt in types_to_search:
            tasks.append(search_fhir(client, rt, term, limit))

    results = await asyncio.gather(*tasks, return_exceptions=True)

    # Collect and dedup
    all_entries = []
    for r in results:
        if isinstance(r, list):
            all_entries.extend(r)

    deduped = dedup_entries(all_entries)
    deduped.sort(key=entry_sort_key, reverse=True)

    # Trim to reasonable max
    max_results = 200
    truncated = len(deduped) > max_results
    showing = deduped[:max_results]

    return {
        'query': q,
        'search_terms': search_terms,
        'synonym_expansion': expand_synonyms,
        'total': len(deduped),
        'showing': len(showing),
        'truncated': truncated,
        'entry': showing,
    }


# ═══════════════════════════════════════════════════════════════
# Timeline metrics discovery
# ═══════════════════════════════════════════════════════════════

# Known LOINC code metadata: category, friendly label, unit, reference range.
# Codes not listed here are still discovered — they get auto-categorized
# from their LOINC display name and use whatever unit/range the data provides.
KNOWN_METRICS = {
    # ── Vitals ──
    '8480-6':  {'cat': 'Vitals', 'label': 'BP Systolic',       'unit': 'mmHg',    'range': [90, 120],  'parentCode': '85354-9'},
    '8462-4':  {'cat': 'Vitals', 'label': 'BP Diastolic',      'unit': 'mmHg',    'range': [60, 80],   'parentCode': '85354-9'},
    '8867-4':  {'cat': 'Vitals', 'label': 'Heart Rate',        'unit': 'bpm',     'range': [60, 100]},
    '29463-7': {'cat': 'Vitals', 'label': 'Weight',            'unit': 'kg'},
    '39156-5': {'cat': 'Vitals', 'label': 'BMI',               'unit': 'kg/m²',   'range': [18.5, 25]},
    '8310-5':  {'cat': 'Vitals', 'label': 'Temperature',       'unit': '°F',      'range': [97.0, 99.0]},
    '59408-5': {'cat': 'Vitals', 'label': 'SpO2',              'unit': '%',       'range': [95, 100]},
    '9279-1':  {'cat': 'Vitals', 'label': 'Respiratory Rate',  'unit': '/min',    'range': [12, 20]},

    # ── Hematology ──
    '718-7':   {'cat': 'Hematology', 'label': 'Hemoglobin',       'unit': 'g/dL',      'range': [13.5, 17.5]},
    '4544-3':  {'cat': 'Hematology', 'label': 'Hematocrit',       'unit': '%',         'range': [38.3, 48.6]},
    '6690-2':  {'cat': 'Hematology', 'label': 'WBC',              'unit': 'x10³/µL',  'range': [4.5, 11.0]},
    '777-3':   {'cat': 'Hematology', 'label': 'Platelets',        'unit': 'x10³/µL',  'range': [150, 400]},
    '789-8':   {'cat': 'Hematology', 'label': 'RBC',              'unit': 'x10⁶/µL',  'range': [4.35, 5.65]},
    '787-2':   {'cat': 'Hematology', 'label': 'MCV',              'unit': 'fL',        'range': [80, 100]},
    '785-6':   {'cat': 'Hematology', 'label': 'MCH',              'unit': 'pg',        'range': [27, 33]},
    '788-0':   {'cat': 'Hematology', 'label': 'RDW',              'unit': '%',         'range': [11.5, 14.5]},
    '751-8':   {'cat': 'Hematology', 'label': 'Neutrophils Abs',  'unit': 'x10³/µL',  'range': [1.8, 7.7]},
    '731-0':   {'cat': 'Hematology', 'label': 'Lymphocytes Abs',  'unit': 'x10³/µL',  'range': [1.0, 4.8]},
    '742-7':   {'cat': 'Hematology', 'label': 'Monocytes Abs',    'unit': 'x10³/µL',  'range': [0.2, 0.8]},
    '711-2':   {'cat': 'Hematology', 'label': 'Eosinophils Abs',  'unit': 'x10³/µL',  'range': [0.0, 0.5]},
    '704-7':   {'cat': 'Hematology', 'label': 'Basophils Abs',    'unit': 'x10³/µL',  'range': [0.0, 0.1]},
    '786-4':   {'cat': 'Hematology', 'label': 'MCHC',             'unit': 'g/dL',      'range': [31.5, 35.7]},
    '32623-1': {'cat': 'Hematology', 'label': 'MPV',              'unit': 'fL',        'range': [7.5, 11.5]},

    # ── Chemistry ──
    '2160-0':  {'cat': 'Chemistry', 'label': 'Creatinine',     'unit': 'mg/dL',   'range': [0.7, 1.3], 'specimen': 'Serum'},
    '3094-0':  {'cat': 'Chemistry', 'label': 'BUN',            'unit': 'mg/dL',   'range': [7, 20]},
    '2345-7':  {'cat': 'Chemistry', 'label': 'Glucose',        'unit': 'mg/dL',   'range': [70, 100], 'specimen': 'Serum'},
    '17861-6': {'cat': 'Chemistry', 'label': 'Calcium',        'unit': 'mg/dL',   'range': [8.5, 10.5]},
    '2951-2':  {'cat': 'Chemistry', 'label': 'Sodium',         'unit': 'mmol/L',  'range': [136, 145]},
    '2823-3':  {'cat': 'Chemistry', 'label': 'Potassium',      'unit': 'mmol/L',  'range': [3.5, 5.1]},
    '2075-0':  {'cat': 'Chemistry', 'label': 'Chloride',       'unit': 'mmol/L',  'range': [98, 106]},
    '2028-9':  {'cat': 'Chemistry', 'label': 'CO2',            'unit': 'mmol/L',  'range': [23, 29]},
    '1751-7':  {'cat': 'Chemistry', 'label': 'Albumin',        'unit': 'g/dL',    'range': [3.5, 5.5], 'specimen': 'Serum'},
    '2885-2':  {'cat': 'Chemistry', 'label': 'Total Protein',  'unit': 'g/dL',    'range': [6.0, 8.3], 'specimen': 'Serum'},
    '3097-3':  {'cat': 'Chemistry', 'label': 'BUN/Creatinine', 'unit': '',        'range': [10, 20]},
    '33037-3': {'cat': 'Chemistry', 'label': 'Anion Gap',      'unit': 'mmol/L',  'range': [4, 12]},

    # ── Liver ──
    '1742-6':  {'cat': 'Liver', 'label': 'ALT',             'unit': 'U/L',    'range': [7, 56]},
    '1920-8':  {'cat': 'Liver', 'label': 'AST',             'unit': 'U/L',    'range': [10, 40]},
    '6768-6':  {'cat': 'Liver', 'label': 'ALP',             'unit': 'U/L',    'range': [44, 147]},
    '1975-2':  {'cat': 'Liver', 'label': 'Total Bilirubin', 'unit': 'mg/dL',  'range': [0.1, 1.2]},
    '1968-7':  {'cat': 'Liver', 'label': 'Direct Bilirubin','unit': 'mg/dL',  'range': [0.0, 0.3]},
    '2324-2':  {'cat': 'Liver', 'label': 'GGT',             'unit': 'U/L',    'range': [9, 48]},

    # ── Lipids ──
    '2093-3':  {'cat': 'Lipids', 'label': 'Total Cholesterol', 'unit': 'mg/dL', 'range': [0, 200]},
    '2085-9':  {'cat': 'Lipids', 'label': 'HDL',               'unit': 'mg/dL', 'range': [40, 60]},
    '2089-1':  {'cat': 'Lipids', 'label': 'LDL',               'unit': 'mg/dL', 'range': [0, 100]},
    '2571-8':  {'cat': 'Lipids', 'label': 'Triglycerides',     'unit': 'mg/dL', 'range': [0, 150]},

    # ── Metabolic ──
    '4548-4':  {'cat': 'Metabolic', 'label': 'HbA1c',              'unit': '%',      'range': [4.0, 5.6]},
    '33914-3': {'cat': 'Metabolic', 'label': 'eGFR',               'unit': 'mL/min', 'range': [90, 120]},
    '14749-6': {'cat': 'Metabolic', 'label': 'Glucose (fasting)',   'unit': 'mg/dL',  'range': [70, 100]},

    # ── Inflammation ──
    '1988-5':  {'cat': 'Inflammation', 'label': 'CRP',      'unit': 'mg/L',   'range': [0, 3.0]},
    '4537-7':  {'cat': 'Inflammation', 'label': 'ESR',      'unit': 'mm/hr',  'range': [0, 20]},
    '2276-4':  {'cat': 'Inflammation', 'label': 'Ferritin', 'unit': 'ng/mL',  'range': [30, 400]},

    # ── Immunology ──
    '2458-8':  {'cat': 'Immunology', 'label': 'IgA',                    'unit': 'mg/dL', 'range': [70, 400]},
    '2462-0':  {'cat': 'Immunology', 'label': 'IgG',                    'unit': 'mg/dL', 'range': [700, 1600]},
    '2472-9':  {'cat': 'Immunology', 'label': 'IgM',                    'unit': 'mg/dL', 'range': [40, 230]},
    '11050-2': {'cat': 'Immunology', 'label': 'Free Kappa Light Chain', 'unit': 'mg/L',  'range': [3.3, 19.4]},
    '11051-0': {'cat': 'Immunology', 'label': 'Free Lambda Light Chain','unit': 'mg/L',  'range': [5.7, 26.3]},
    '11052-8': {'cat': 'Immunology', 'label': 'Kappa/Lambda Ratio',     'unit': '',      'range': [0.26, 1.65]},

    # ── Cancer Markers ──
    '1952-1':  {'cat': 'Cancer Markers', 'label': 'Beta-2 Microglobulin', 'unit': 'mg/L',  'range': [0.7, 1.8]},
    '2532-0':  {'cat': 'Cancer Markers', 'label': 'LDH',                  'unit': 'U/L',   'range': [140, 280]},
    '2857-1':  {'cat': 'Cancer Markers', 'label': 'PSA',                  'unit': 'ng/mL',  'range': [0, 4.0]},
    '51435-6': {'cat': 'Cancer Markers', 'label': 'M-Protein (Serum)',    'unit': 'g/dL',   'range': [0, 0]},
    '33358-3': {'cat': 'Cancer Markers', 'label': 'M-Protein (Serum)',    'unit': 'g/dL',   'range': [0, 0]},  # alternate serum M-protein LOINC
    '56759-4': {'cat': 'Cancer Markers', 'label': 'M-Protein (Urine)',    'unit': 'g/dL',   'range': [0, 0]},
    '42484-6': {'cat': 'Cancer Markers', 'label': '% M-Protein (Urine)', 'unit': '%',      'range': None},    # urine M-protein percentage

    # ── Thyroid ──
    '3016-3':  {'cat': 'Thyroid', 'label': 'TSH',     'unit': 'mIU/L',  'range': [0.27, 4.2]},
    '3024-7':  {'cat': 'Thyroid', 'label': 'Free T4', 'unit': 'ng/dL',  'range': [0.93, 1.7]},
    '3051-0':  {'cat': 'Thyroid', 'label': 'Free T3', 'unit': 'pg/mL',  'range': [2.0, 4.4]},

    # ── Iron ──
    '2498-4':  {'cat': 'Iron', 'label': 'Iron',  'unit': 'µg/dL',  'range': [60, 170]},
    '2500-7':  {'cat': 'Iron', 'label': 'TIBC',  'unit': 'µg/dL',  'range': [250, 370]},
    '2502-3':  {'cat': 'Iron', 'label': 'Iron Saturation', 'unit': '%', 'range': [20, 50]},

    # ── Coagulation ──
    '5902-2':  {'cat': 'Coagulation', 'label': 'PT',          'unit': 's',  'range': [11.0, 13.5]},
    '6301-6':  {'cat': 'Coagulation', 'label': 'INR',         'unit': '',   'range': [0.8, 1.1]},
    '3173-2':  {'cat': 'Coagulation', 'label': 'aPTT',        'unit': 's',  'range': [25, 35]},
    '3255-7':  {'cat': 'Coagulation', 'label': 'Fibrinogen',  'unit': 'mg/dL', 'range': [200, 400]},
    '48066-5': {'cat': 'Coagulation', 'label': 'D-Dimer',     'unit': 'mg/L',  'range': [0, 0.5]},
}

# Category display order (categories not listed here sort alphabetically after these)
CATEGORY_ORDER = [
    'Vitals', 'Fitness', 'Hematology', 'Chemistry', 'Protein Electrophoresis',
    'Liver', 'Lipids', 'Metabolic', 'Thyroid', 'Hormones',
    'Iron', 'Vitamins', 'Inflammation', 'Coagulation',
    'Cardiac', 'Immunology', 'Cancer Markers',
    'GI / Stool', 'Urinalysis',
]

# Auto-categorization heuristics based on LOINC display name keywords.
# Rules are checked in order — first match wins. More specific rules go first.
AUTO_CATEGORY_RULES = [
    # ── Fitness / wearable ──
    (['steps', 'step count', 'walking distance', 'flights climbed',
      'active energy', 'exercise time', 'stand hour', 'vo2 max',
      'running', 'cycling', 'swimming'], 'Fitness'),

    # ── Protein electrophoresis (SPEP/UPEP fractions — critical for myeloma) ──
    (['globulin', 'alpha-1-glob', 'alpha-2-glob', 'beta-1-glob', 'beta-2-glob',
      'gamma glob', 'electrophoresis', 'm spike', 'monoclonal protein',
      'viscosity'], 'Protein Electrophoresis'),

    # ── Cardiac / ECG ──
    (['troponin', 'ck ', 'creatine kinase', 'nt-probnp', 'pro-bnp', 'probnp',
      'bnp', 'natriuretic', 'ejection fraction', 'e/e\'',
      'qtc', 'qrs', 'qt interval', 'p-r interval', 'p wave',
      't wave', 'ventricular rate', 'atrial rate',
      'echocardiog', 'doppler'], 'Cardiac'),

    # ── Vitamins & nutrients ──
    (['vitamin', 'tocopherol', 'folate', 'folic acid', 'thiamine',
      'pyridox', 'cobalamin', 'b12', 'hydroxyvitamin d', '25-oh',
      'coenzyme q10', 'ascorbic'], 'Vitamins'),

    # ── Hormones / endocrine ──
    (['testosterone', 'estradiol', 'progesterone', 'cortisol',
      'dhea', 'fsh', 'shbg', 'prolactin', 'growth hormone',
      'igf-1', 'aldosterone', 'renin', 'pregnenolone',
      'metanephrine', 'normetanephrine', 'catecholamine',
      'pth', 'parathyroid', ' lh'], 'Hormones'),

    # ── GI / stool / breath test ──
    (['stool', 'fecal', 'calprotectin', 'lactoferrin',
      'butyrate', 'propionate', 'acetate', 'valerate', 'fatty acid',
      'hydrogen', 'methane', 'h2 ', 'ch4', 'breath',
      'h. pylori', 'helicobacter', 'ova and parasit',
      'elastase', 'gliadin', 'celiac'], 'GI / Stool'),

    # ── Hematology (expanded) ──
    (['leukocyte', 'erythrocyte', 'hemoglobin', 'hematocrit', 'platelet',
      'neutrophil', 'lymphocyte', 'monocyte', 'eosinophil', 'basophil',
      'reticulocyte', 'mch', 'mcv', 'rdw', 'mpv', 'nrbc',
      'nucleated rbc', 'nucleated red', 'immature granulocyte',
      'haptoglobin', 'g6pd', 'sickle', 'porphyrin'], 'Hematology'),

    # ── Lipids ──
    (['cholesterol', 'triglyceride', 'hdl', 'ldl', 'lipoprotein', 'lipid',
      'apolipoprotein'], 'Lipids'),

    # ── Liver / pancreas ──
    (['bilirubin', 'transaminase', 'aminotransferase', 'alkaline phosphatase', 'ggt',
      'amylase', 'lipase'], 'Liver'),

    # ── Thyroid ──
    (['thyroid', 'tsh', 'thyroxine', 'triiodothyronine', 'reverse t3'], 'Thyroid'),

    # ── Iron (expanded) ──
    (['iron', 'ferritin', 'transferrin', 'tibc', 'uibc',
      'iron saturation'], 'Iron'),

    # ── Coagulation ──
    (['prothrombin', 'inr', 'fibrinogen', 'aptt', 'thromboplastin', 'd-dimer',
      'coagulation', 'lupus anticoag'], 'Coagulation'),

    # ── Immunology / autoantibodies (expanded) ──
    (['immunoglobulin', 'kappa', 'lambda', 'light chain',
      'iga', 'igg', 'igm', 'ige', 'secretory ig',
      'ana ', 'anti-', 'antinuclear', 'complement c3', 'complement c4',
      'rheumatoid factor', 'igra', 'tryptase', 'interleukin',
      'cd57', 'cd4', 'cd8', 'cd19', 'cd3', 'plasma cell',
      'vegf', 'lysozyme', 'rnp ab', 'intrinsic factor',
      'blocking ab', 'parietal cell'], 'Immunology'),

    # ── Cancer markers (expanded) ──
    (['antigen', 'tumor', 'marker', 'cea', 'psa', 'afp', 'ca 19', 'ca 125',
      'm-protein', 'beta-2 microglobulin', 'ldh', 'lactate dehydrogenase'],
     'Cancer Markers'),

    # ── Inflammation ──
    (['crp', 'c-reactive', 'sedimentation', 'esr'], 'Inflammation'),

    # ── Metabolic ──
    (['glucose', 'a1c', 'gfr', 'glomerular', 'insulin', 'cystatin'], 'Metabolic'),

    # ── Vitals (expanded) ──
    (['heart rate', 'blood pressure', 'systolic', 'diastolic', 'bmi',
      'weight', 'height', 'temperature', 'spo2', 'respiratory',
      'body mass', 'body height'], 'Vitals'),

    # ── Chemistry (catch-all for common serum tests) ──
    (['sodium', 'potassium', 'chloride', 'calcium', 'phosph', 'magnesium',
      'creatinine', 'urea', 'albumin', 'total protein', 'co2', 'bicarbonate',
      'anion gap', 'osmolality', 'ammonia', 'uric acid',
      'ceruloplasmin', 'copper', 'zinc', 'homocysteine', 'methylmalonic',
      'angiotensin-1-converting', ' ace',
      'poc k', 'poc bun', 'poc cl', 'poc na', 'poc ca', 'istat',
      'lactate', 'lactic acid',
      'galactosidase', 'cystatin',
      'cerebral spinal', 'csf', 'total volume',
      'delta ala'], 'Chemistry'),

    # ── Urinalysis (last, since "urine" is broad) ──
    (['urine', 'urinalysis', 'ua ph', 'porphobilinogen'], 'Urinalysis'),

    # ── Stool (catch stragglers) ──
    (['stool ph'], 'GI / Stool'),
]


def auto_categorize(loinc_display: str) -> str:
    """Guess a category from the LOINC display name."""
    dl = loinc_display.lower()
    # Pad with spaces so short keywords like 'lh' can match word boundaries
    # e.g., "LH" -> " lh ", "LH [Units/volume]..." -> " lh [units/volume]..."
    dl_padded = ' ' + dl + ' '
    for keywords, cat in AUTO_CATEGORY_RULES:
        if any(kw in dl_padded for kw in keywords):
            return cat
    return 'Other'


import re as _re

# Maximum label length in the sidebar (chars).  Anything longer gets truncated.
_MAX_LABEL_LEN = 40

# Regex to strip LOINC bracketed units like "[Mass/volume] in Serum"
_BRACKET_RE = _re.compile(r'\s*\[.*?\]\s*')
# Regex to strip methodology suffixes like "by Electrophoresis", "by Immunoassay"
_BY_METHOD_RE = _re.compile(r'\s+by\s+\w[\w\s/-]*$', _re.I)
# Regex to strip specimen from end like "in Serum or Plasma", "in 24 hour Urine"
_IN_SPECIMEN_RE = _re.compile(r'\s+in\s+(?:24\s+hour\s+)?(?:Serum|Plasma|Red\s+Blood\s+Cells|Blood|Urine|Stool|Body\s+fluid|Cerebral\s+spinal\s+fluid)(?:\s+or\s+\w+)?', _re.I)


def _shorten_label(loinc_display: str) -> str:
    """Create a concise sidebar label from a verbose LOINC display string.

    Examples:
      "Protein.monoclonal/Protein.total [Ratio] in 24 hour Urine by Electrophoresis"
       → "Protein.monoclonal/Protein.total, Urine"

      "Aspartate aminotransferase [Enzymatic activity/volume] in Serum or Plasma"
       → "Aspartate Aminotransferase"
    """
    label = loinc_display
    if not label:
        return label

    # 1. Strip bracketed units  [Mass/volume], [Ratio], etc.
    label = _BRACKET_RE.sub(' ', label).strip()

    # 2. Strip methodology suffix  "by Electrophoresis", "by Immunoassay"
    label = _BY_METHOD_RE.sub('', label).strip()

    # 3. Strip specimen phrase  "in Serum or Plasma", "in 24 hour Urine"
    #    (specimen is already shown as a badge, no need to repeat in label)
    label = _IN_SPECIMEN_RE.sub('', label).strip()

    # 4. Clean up leftover whitespace / trailing punctuation
    label = _re.sub(r'\s{2,}', ' ', label).strip(' ,/')

    # 5. Truncate if still too long
    if len(label) > _MAX_LABEL_LEN:
        label = label[:_MAX_LABEL_LEN - 1].rstrip(' ,/') + '\u2026'

    return label


_SPECIMEN_PATTERNS = [
    (_re.compile(r'\bin\s+(24\s+hour\s+)?urine\b', _re.I), 'Urine'),
    (_re.compile(r'\bin\s+serum\s+or\s+plasma\b', _re.I), 'Serum'),
    (_re.compile(r'\bin\s+serum\b', _re.I), 'Serum'),
    (_re.compile(r'\bin\s+plasma\b', _re.I), 'Plasma'),
    (_re.compile(r'\bin\s+blood\b', _re.I), 'Blood'),
    (_re.compile(r'\bin\s+red\s+blood\s+cells\b', _re.I), 'Blood'),
    (_re.compile(r'\bin\s+cerebral\s+spinal\s+fluid\b', _re.I), 'CSF'),
    (_re.compile(r'\bin\s+stool\b', _re.I), 'Stool'),
    (_re.compile(r'\bin\s+body\s+fluid\b', _re.I), 'Body Fluid'),
    (_re.compile(r'\burine\b', _re.I), 'Urine'),
]


def extract_specimen(loinc_display: str) -> str:
    """Extract specimen type from a LOINC display string."""
    for pattern, specimen in _SPECIMEN_PATTERNS:
        if pattern.search(loinc_display):
            return specimen
    return ''


# ── Cache for discovered metrics ──
_metrics_cache: Optional[dict] = None
_metrics_cache_time: float = 0
METRICS_CACHE_TTL = 300  # seconds


async def _discover_metrics(client: httpx.AsyncClient) -> list:
    """Scan all Observations in HAPI to discover available numeric metrics.

    Returns a list of categories, each with a metrics list:
    [
      { name: "Hematology", metrics: [
        { code: "718-7", label: "Hemoglobin", unit: "g/dL", range: [13.5, 17.5], count: 42 },
        ...
      ]},
      ...
    ]
    """
    # Paginate through all observations, collecting LOINC code stats.
    # We request only the fields we need (_elements) to minimize payload.
    by_code = defaultdict(lambda: {
        'count': 0, 'units': defaultdict(int),
        'display': '', 'range_low': None, 'range_high': None,
        'date_min': '', 'date_max': '',
    })

    url = '/Observation'
    params = {
        '_count': '500',
        '_elements': 'code,valueQuantity,referenceRange,effectiveDateTime,issued',
        '_sort': '-_lastUpdated',
    }

    pages = 0
    max_pages = 200  # safety limit (~100K observations)

    while url and pages < max_pages:
        try:
            r = await client.get(url, params=params)
            if r.status_code != 200:
                break
            bundle = r.json()
        except Exception:
            break

        for entry in bundle.get('entry', []):
            res = entry.get('resource', {})
            codings = res.get('code', {}).get('coding', [])
            loinc_code = None
            loinc_display = ''
            for c in codings:
                if c.get('system') == 'http://loinc.org':
                    loinc_code = c.get('code')
                    loinc_display = c.get('display', '')
                    break
            if not loinc_code:
                continue

            # Observation date
            obs_date = (res.get('effectiveDateTime') or res.get('issued') or '')[:10]

            # Only count observations that have numeric values (timeline-plottable)
            vq = res.get('valueQuantity')
            has_numeric = vq and vq.get('value') is not None

            # Also check components (e.g., BP)
            if not has_numeric:
                for comp in res.get('component', []):
                    cvq = comp.get('valueQuantity')
                    if cvq and cvq.get('value') is not None:
                        # Register the component code too
                        comp_code = None
                        comp_display = ''
                        for cc in comp.get('code', {}).get('coding', []):
                            if cc.get('system') == 'http://loinc.org':
                                comp_code = cc.get('code')
                                comp_display = cc.get('display', '')
                                break
                        if comp_code:
                            rec = by_code[comp_code]
                            rec['count'] += 1
                            if comp_display and not rec['display']:
                                rec['display'] = comp_display
                            unit = cvq.get('unit', '')
                            if unit:
                                rec['units'][unit] += 1
                            if obs_date:
                                if not rec['date_min'] or obs_date < rec['date_min']:
                                    rec['date_min'] = obs_date
                                if not rec['date_max'] or obs_date > rec['date_max']:
                                    rec['date_max'] = obs_date
                        has_numeric = True

            if not has_numeric:
                continue

            rec = by_code[loinc_code]
            rec['count'] += 1
            if loinc_display and not rec['display']:
                rec['display'] = loinc_display

            if vq and vq.get('value') is not None:
                unit = vq.get('unit', '')
                if unit:
                    rec['units'][unit] += 1

            # Track date range
            if obs_date:
                if not rec['date_min'] or obs_date < rec['date_min']:
                    rec['date_min'] = obs_date
                if not rec['date_max'] or obs_date > rec['date_max']:
                    rec['date_max'] = obs_date

            # Extract reference range if available
            for rr in res.get('referenceRange', []):
                low = rr.get('low', {}).get('value')
                high = rr.get('high', {}).get('value')
                if low is not None and rec['range_low'] is None:
                    rec['range_low'] = low
                if high is not None and rec['range_high'] is None:
                    rec['range_high'] = high

        # Follow next page
        url = None
        params = None
        for link in bundle.get('link', []):
            if link.get('relation') == 'next':
                url = link['url']
                break
        pages += 1

    # Build categorized output
    categories = defaultdict(list)
    # Track known-label -> metric, so we can merge duplicates
    _known_label_map = {}  # (cat, label) -> metric dict

    for code, rec in by_code.items():
        known = KNOWN_METRICS.get(code)
        loinc_display = rec['display']

        if known:
            cat = known['cat']
            label = known['label']
            unit = known.get('unit', '')
            ref_range = known.get('range')
            parent_code = known.get('parentCode')
        else:
            cat = auto_categorize(loinc_display)
            label = _shorten_label(loinc_display)
            # Most common unit from data
            unit = max(rec['units'], key=rec['units'].get) if rec['units'] else ''
            ref_range = None
            if rec['range_low'] is not None and rec['range_high'] is not None:
                ref_range = [rec['range_low'], rec['range_high']]
            parent_code = None

        # Extract specimen type from the LOINC display, fall back to KNOWN_METRICS
        specimen = extract_specimen(loinc_display)
        if not specimen and known and known.get('specimen'):
            specimen = known['specimen']

        # Merge duplicates: if a KNOWN_METRICS label already exists in this
        # category, fold this code's count into it instead of creating a
        # separate entry.  This handles labs using alternate LOINC codes for
        # the same logical test (e.g., two AST codes).
        merge_key = (cat, label)
        if merge_key in _known_label_map:
            existing = _known_label_map[merge_key]
            existing['count'] += rec['count']
            # Widen date range
            if rec['date_min']:
                if not existing['dateRange']:
                    existing['dateRange'] = [rec['date_min'], rec['date_max']]
                else:
                    if rec['date_min'] < existing['dateRange'][0]:
                        existing['dateRange'][0] = rec['date_min']
                    if rec['date_max'] > existing['dateRange'][1]:
                        existing['dateRange'][1] = rec['date_max']
            # Keep the code with more data as primary
            # (already set — the first one added usually has more data
            #  since by_code is unordered; we sort by count later anyway)
            continue

        metric = {
            'code': code,
            'label': label,
            'unit': unit,
            'count': rec['count'],
            'description': loinc_display,
            'specimen': specimen,
            'dateRange': [rec['date_min'], rec['date_max']] if rec['date_min'] else None,
        }
        if ref_range:
            metric['range'] = ref_range
        if parent_code:
            metric['parentCode'] = parent_code

        categories[cat].append(metric)
        _known_label_map[merge_key] = metric

    # Sort metrics within each category by count (most data first)
    for cat in categories:
        categories[cat].sort(key=lambda m: -m['count'])

    # Build ordered category list
    result = []
    seen_cats = set()
    for cat_name in CATEGORY_ORDER:
        if cat_name in categories:
            result.append({'name': cat_name, 'metrics': categories[cat_name]})
            seen_cats.add(cat_name)

    # Add remaining categories alphabetically
    for cat_name in sorted(categories.keys()):
        if cat_name not in seen_cats:
            result.append({'name': cat_name, 'metrics': categories[cat_name]})

    return result


@app.get('/api/timeline/metrics')
async def timeline_metrics(
    refresh: bool = Query(False, description='Force cache refresh'),
):
    """Discover available timeline metrics from the FHIR server.

    Scans all numeric Observations, groups by LOINC code, categorizes,
    and returns a structured catalog with counts. Results are cached
    for 5 minutes.
    """
    global _metrics_cache, _metrics_cache_time

    if not refresh and _metrics_cache and (time.time() - _metrics_cache_time) < METRICS_CACHE_TTL:
        return _metrics_cache

    client = await get_client()
    categories = await _discover_metrics(client)

    total_metrics = sum(len(c['metrics']) for c in categories)
    total_datapoints = sum(m['count'] for c in categories for m in c['metrics'])

    _metrics_cache = {
        'categories': categories,
        'total_metrics': total_metrics,
        'total_datapoints': total_datapoints,
        'cached_at': time.time(),
    }
    _metrics_cache_time = time.time()

    return _metrics_cache


# ═══════════════════════════════════════════════════════════════
# Provenance query
# ═══════════════════════════════════════════════════════════════

@app.get('/api/provenance/{obs_id}')
async def get_provenance(obs_id: str):
    """Return provenance metadata for a single Observation.

    Extracts meta.source, meta.tag (PHV tags), identifier, and code info
    so the UI or debugging tools can trace where a data point came from.
    """
    client = await get_client()
    try:
        r = await client.get(f'/Observation/{obs_id}')
        if r.status_code != 200:
            return {'error': f'Observation {obs_id} not found', 'status': r.status_code}
        obs = r.json()
    except Exception as e:
        return {'error': str(e)}

    meta = obs.get('meta', {})
    tags = meta.get('tag', [])

    # Parse structured PHV tags
    phv_tags = {}
    source_tags = []
    for t in tags:
        code = t.get('code', '')
        system = t.get('system', '')
        if system == 'urn:phv:tag':
            # Parse "key:value" format
            if ':' in code:
                key, _, val = code.partition(':')
                phv_tags.setdefault(key, []).append(val)
            else:
                phv_tags.setdefault('other', []).append(code)
        elif system == 'http://example.org/source':
            source_tags.append(code)

    # Code info
    codings = obs.get('code', {}).get('coding', [])
    loinc = None
    for c in codings:
        if c.get('system') == 'http://loinc.org':
            loinc = {'code': c.get('code'), 'display': c.get('display')}
            break

    return {
        'id': obs_id,
        'source': meta.get('source', ''),
        'source_tags': source_tags,
        'raw_name': phv_tags.get('raw-name', [None])[0],
        'order': phv_tags.get('order', [None])[0],
        'pipeline_version': phv_tags.get('pipeline', [None])[0],
        'mapper_version': phv_tags.get('mapper', [None])[0],
        'convert_script': phv_tags.get('script', [None])[0],
        'loinc': loinc,
        'code_text': obs.get('code', {}).get('text', ''),
        'effective_date': obs.get('effectiveDateTime', ''),
        'identifiers': obs.get('identifier', []),
        'all_phv_tags': phv_tags,
    }


@app.get('/api/provenance/by-raw-name/{raw_name:path}')
async def find_by_raw_name(raw_name: str, limit: int = Query(50)):
    """Find all observations whose original test name contains the search term.

    Uses HAPI's code:text search (substring match) so you don't need the
    exact full name.  E.g., 'M-Protein (monoclonal)' will match both
    'M-Protein (monoclonal), Serum' and '% M-Protein (monoclonal), Urine'.
    """
    client = await get_client()

    # Use code:text for substring matching — much more practical than exact tag match
    try:
        r = await client.get('/Observation', params={
            'code:text': raw_name,
            '_count': str(limit),
            '_elements': 'id,code,effectiveDateTime,meta',
            '_sort': '-date',
        })
        if r.status_code != 200:
            return {'error': f'HAPI returned {r.status_code}', 'raw_name': raw_name}
        bundle = r.json()
    except Exception as e:
        return {'error': str(e)}

    entries = []
    for entry in bundle.get('entry', []):
        res = entry.get('resource', {})
        codings = res.get('code', {}).get('coding', [])
        loinc = None
        loinc_display = None
        for c in codings:
            if c.get('system') == 'http://loinc.org':
                loinc = c.get('code')
                loinc_display = c.get('display')
                break

        # Extract raw-name from PHV tags
        phv_raw = None
        for t in res.get('meta', {}).get('tag', []):
            if t.get('system') == 'urn:phv:tag' and t.get('code', '').startswith('raw-name:'):
                phv_raw = t['code'][len('raw-name:'):]
                break

        entries.append({
            'id': res.get('id'),
            'raw_name': phv_raw or res.get('code', {}).get('text', ''),
            'loinc': loinc,
            'loinc_display': loinc_display,
            'date': res.get('effectiveDateTime', ''),
            'source': res.get('meta', {}).get('source', ''),
        })

    return {
        'query': raw_name,
        'total': bundle.get('total', len(entries)),
        'entries': entries,
    }


@app.get('/api/provenance/by-source/{source_file}')
async def find_by_source(source_file: str, limit: int = Query(100)):
    """Find all observations from a given source file.

    Useful for re-processing a specific data source.
    """
    client = await get_client()
    try:
        r = await client.get('/Observation', params={
            '_source': f'file:{source_file}',
            '_count': str(limit),
            '_elements': 'id,code,effectiveDateTime',
            '_sort': '-date',
        })
        if r.status_code != 200:
            return {'error': f'HAPI returned {r.status_code}'}
        bundle = r.json()
    except Exception as e:
        return {'error': str(e)}

    return {
        'source_file': source_file,
        'total': bundle.get('total', 0),
        'entries': [
            {
                'id': e['resource']['id'],
                'code_text': e['resource'].get('code', {}).get('text', ''),
                'date': e['resource'].get('effectiveDateTime', ''),
            }
            for e in bundle.get('entry', [])
        ],
    }


@app.get('/api/provenance/by-mapper/{version}')
async def find_by_mapper(version: str, limit: int = Query(50)):
    """Find all observations processed by a specific mapper version.

    Useful for finding resources that need re-processing after a mapper update.
    """
    client = await get_client()
    tag = f'urn:phv:tag|mapper:v{version}'
    try:
        r = await client.get('/Observation', params={
            '_tag': tag,
            '_count': str(limit),
            '_elements': 'id,code,effectiveDateTime',
            '_sort': '-date',
            '_summary': 'count',
        })
        if r.status_code != 200:
            return {'error': f'HAPI returned {r.status_code}'}
        bundle = r.json()
    except Exception as e:
        return {'error': str(e)}

    return {
        'mapper_version': f'v{version}',
        'total': bundle.get('total', 0),
    }
