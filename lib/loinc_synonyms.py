"""Shared synonym table for LOINC display names.

Used by:
  - validate_loinc.py: to avoid false NAME_DIVERGENCE flags
  - Web app search: to expand queries so "CRP" finds "C-Reactive Protein" results

Each synonym group is a set of words that are considered equivalent for matching
purposes. When any word from the group appears, all other words in the group
count as a match.
"""

import re

# ══════════════════════════════════════════════════════════════
# Synonym Groups
#
# Each set contains words that are interchangeable for matching.
# Abbreviations, acronyms, and their full forms.
# ══════════════════════════════════════════════════════════════

SYNONYM_GROUPS = [
    # ── Abbreviation <-> Full name ──
    {'crp', 'reactive', 'protein'},
    {'igg', 'immunoglobulin', 'g'},
    {'igm', 'immunoglobulin', 'm'},
    {'iga', 'immunoglobulin', 'a'},
    {'ige', 'immunoglobulin', 'e'},
    {'alt', 'alanine', 'aminotransferase', 'transaminase', 'sgpt'},
    {'ast', 'aspartate', 'aminotransferase', 'sgot'},
    {'alp', 'alkaline', 'phosphatase', 'alk'},
    {'ldh', 'lactate', 'dehydrogenase', 'ld'},
    {'ldl', 'low', 'density', 'lipoprotein'},
    {'hdl', 'high', 'density', 'lipoprotein'},
    {'co2', 'carbon', 'dioxide', 'bicarbonate', 'tco2'},
    {'bun', 'urea', 'nitrogen'},
    {'creatinine', 'creat'},
    {'tsh', 'thyroid', 'stimulating', 'hormone'},
    {'pth', 'parathyroid', 'hormone', 'intact'},
    {'ace', 'angiotensin', 'converting', 'enzyme'},
    {'esr', 'sedimentation', 'rate', 'sed'},
    {'tibc', 'iron', 'binding', 'capacity', 'total', 'ibc'},
    {'hba1c', 'a1c', 'glycohemoglobin', 'glycated', 'hemoglobin'},
    {'pt', 'prothrombin', 'protime', 'time'},
    {'inr', 'international', 'normalized'},
    {'aptt', 'ptt', 'partial', 'thromboplastin'},
    {'psa', 'prostate', 'specific', 'antigen'},
    {'cea', 'carcinoembryonic'},
    {'afp', 'fetoprotein', 'alpha'},
    {'bnp', 'natriuretic', 'peptide', 'brain'},
    {'nt', 'pro', 'probnp', 'terminal'},
    {'egfr', 'gfr', 'estimated', 'glomerular', 'filtration'},
    {'ana', 'antinuclear', 'antibody', 'nuclear'},
    {'rdw', 'red', 'cell', 'distribution', 'width', 'distrib'},
    {'mcv', 'mean', 'corpuscular'},
    {'mch', 'mean', 'corpuscular'},
    {'mchc', 'mean', 'corpuscular', 'concentration'},
    {'mpv', 'mean', 'platelet'},
    {'wbc', 'white', 'leukocytes', 'leukocyte', 'wbcs'},
    {'rbc', 'red', 'erythrocytes', 'erythrocyte', 'rbcs'},
    {'hgb', 'hemoglobin'},
    {'hct', 'hematocrit'},
    {'plt', 'platelets', 'platelet'},
    {'nrbc', 'nucleated'},

    # ── Cell types ──
    {'reticulocyte', 'reticulocytes', 'retic', 'ret'},
    {'neutrophil', 'neutrophils', 'neut', 'neuts'},
    {'lymphocyte', 'lymphocytes', 'lymph', 'lymphs'},
    {'monocyte', 'monocytes', 'mono', 'monos'},
    {'eosinophil', 'eosinophils', 'eos'},
    {'basophil', 'basophils', 'baso', 'basos'},
    {'immature', 'imm', 'granulocyte', 'granulocytes', 'grans'},

    # ── Cardiac ──
    {'troponin', 'trop', 'hs'},
    {'ck', 'cpk', 'creatine', 'kinase'},
    {'b2m', 'beta', 'microglobulin'},
    {'ef', 'ejection', 'fraction', 'simpson'},
    {'lv', 'left', 'ventricular'},

    # ── Infectious disease ──
    {'lyme', 'borrelia', 'burgdorferi'},
    {'hbs', 'hepatitis', 'surface', 'hep'},
    {'hbc', 'hepatitis', 'core'},
    {'hcv', 'hepatitis', 'c'},
    {'stec', 'shiga', 'coli', 'toxin'},

    # ── Autoantibodies ──
    {'sm', 'smith'},
    {'dsdna', 'dna', 'ds'},
    {'ssa', 'ro'},
    {'ssb', 'la'},
    {'scl', 'scleroderma'},
    {'rnp', 'ribonucleoprotein'},
    {'rf', 'rheumatoid', 'factor'},

    # ── Chemistry synonyms ──
    {'lactic', 'lactate'},
    {'pyridoxal', 'phosphate', 'vitamin', 'b6'},
    {'scfa', 'short', 'chain', 'fatty', 'acids'},
    {'h2', 'hydrogen'},
    {'ch4', 'methane'},
    {'cold', 'cagg', 'agglutinin'},

    # ── Porphyrins ──
    {'porphyrin', 'porphyrins', 'uroporphyrin', 'coproporphyrin',
     'heptacarboxyporphyrin', 'hexacarboxyporphyrin', 'pentacarboxylporph'},

    # ── Abbreviation variants ──
    {'poc', 'point', 'care', 'istat'},
    {'kappa', 'k'},
    {'lambda', 'l'},
    {'abs', 'absolute'},
    {'vitamin', 'vit', 'oh', 'hydroxy', 'hydroxyvitamin'},
    {'cholesterol', 'chol'},
    {'calcium', 'ca'},
    {'sodium', 'na'},
    {'potassium', 'k'},
    {'chloride', 'cl'},
    {'phosphate', 'phosphorus', 'phos'},
    {'glucose', 'glucometer', 'fingerstick', 'meter'},
    {'antibody', 'ab', 'antibodies'},
    {'antigen', 'ag'},
    {'automated', 'auto', 'count'},
    {'quantitative', 'quant'},
    {'prot', 'protein', 'proteins'},
    {'qrs', 'qrsd'},
    {'drvvt', 'lupus', 'anticoagulant'},
    {'csf', 'cerebral', 'spinal', 'fluid'},
    {'ur', 'urine', 'urinary'},
    {'conc', 'concentration'},
    {'tot', 'total'},
    {'occult', 'hemoglobin'},
    {'duration', 'interval'},
]

# ══════════════════════════════════════════════════════════════
# Search Synonym Phrases
#
# Unlike SYNONYM_GROUPS (word-level, for validation), these are
# phrase-level equivalences for search expansion. When someone
# searches "CRP", we also search "c-reactive protein" as a phrase.
#
# Each list entry is a list of equivalent search phrases.
# The first element is the canonical short form.
# ══════════════════════════════════════════════════════════════

SEARCH_SYNONYMS = [
    # ── Common abbreviations ──
    ['crp', 'c-reactive protein'],
    ['hs-crp', 'high sensitivity c-reactive protein', 'high sensitivity crp'],
    ['igg', 'immunoglobulin g'],
    ['igm', 'immunoglobulin m'],
    ['iga', 'immunoglobulin a'],
    ['ige', 'immunoglobulin e'],
    ['alt', 'alanine aminotransferase', 'alanine transaminase', 'sgpt'],
    ['ast', 'aspartate aminotransferase', 'sgot'],
    ['alp', 'alkaline phosphatase', 'alk phos'],
    ['ldh', 'lactate dehydrogenase'],
    ['ldl', 'ldl cholesterol', 'low density lipoprotein'],
    ['hdl', 'hdl cholesterol', 'high density lipoprotein'],
    ['co2', 'carbon dioxide', 'bicarbonate', 'tco2'],
    ['bun', 'blood urea nitrogen', 'urea nitrogen'],
    ['creatinine', 'creat'],
    ['tsh', 'thyroid stimulating hormone'],
    ['pth', 'parathyroid hormone', 'intact pth'],
    ['ace', 'angiotensin converting enzyme'],
    ['esr', 'sedimentation rate', 'sed rate'],
    ['tibc', 'total iron binding capacity', 'iron binding capacity'],
    ['hba1c', 'hemoglobin a1c', 'a1c', 'glycohemoglobin'],
    ['pt', 'prothrombin time', 'protime'],
    ['inr', 'international normalized ratio'],
    ['aptt', 'ptt', 'partial thromboplastin time'],
    ['psa', 'prostate specific antigen'],
    ['cea', 'carcinoembryonic antigen'],
    ['afp', 'alpha fetoprotein'],
    ['bnp', 'brain natriuretic peptide'],
    ['nt-pro bnp', 'nt pro bnp', 'n-terminal pro-bnp', 'probnp'],
    ['egfr', 'gfr', 'estimated glomerular filtration rate'],
    ['ana', 'antinuclear antibody'],

    # ── Hematology ──
    ['rdw', 'red cell distribution width'],
    ['mcv', 'mean corpuscular volume'],
    ['mch', 'mean corpuscular hemoglobin'],
    ['mchc', 'mean corpuscular hemoglobin concentration'],
    ['mpv', 'mean platelet volume'],
    ['wbc', 'white blood cells', 'leukocytes'],
    ['rbc', 'red blood cells', 'erythrocytes'],
    ['hgb', 'hemoglobin'],
    ['hct', 'hematocrit'],
    ['plt', 'platelets', 'platelet count'],
    ['nrbc', 'nucleated red blood cells'],
    ['reticulocyte', 'retic', 'retic count'],
    ['neutrophil', 'neutrophils', 'neut'],
    ['lymphocyte', 'lymphocytes', 'lymph'],
    ['monocyte', 'monocytes', 'mono'],
    ['eosinophil', 'eosinophils', 'eos'],
    ['basophil', 'basophils', 'baso'],

    # ── Cardiac ──
    ['troponin', 'hs troponin', 'high sensitivity troponin'],
    ['ck', 'cpk', 'creatine kinase'],
    ['b2m', 'beta-2 microglobulin'],

    # ── Infectious disease ──
    ['lyme', 'borrelia burgdorferi'],
    ['hep b', 'hepatitis b'],
    ['hep c', 'hepatitis c', 'hcv'],

    # ── Autoantibodies ──
    ['dsdna', 'double stranded dna', 'anti-dsdna'],
    ['ssa', 'anti-ro', 'ro antibody'],
    ['ssb', 'anti-la', 'la antibody'],
    ['rf', 'rheumatoid factor'],

    # ── Chemistry ──
    ['lactic acid', 'lactate'],
    ['d-dimer', 'd dimer'],
    ['vitamin d', '25-oh vitamin d', '25-hydroxyvitamin d'],
    ['vitamin b12', 'cobalamin', 'b12'],
    ['free t4', 'free thyroxine'],
    ['free t3', 'free triiodothyronine'],
    ['kappa', 'kappa light chain'],
    ['lambda', 'lambda light chain'],
    ['ferritin', 'ferritin serum'],
    ['cortisol', 'cortisol serum'],
    ['testosterone', 'testosterone total'],
    ['homocysteine', 'homocyst'],
    ['uric acid', 'urate'],
    ['ammonia', 'nh3'],

    # ── Coagulation ──
    ['fibrinogen', 'fibrinogen activity'],
    ['drvvt', 'lupus anticoagulant'],

    # ── CSF / special specimens ──
    ['csf', 'cerebrospinal fluid', 'spinal fluid'],

    # ── POC / methodology ──
    ['poc', 'point of care', 'istat'],
]

# Precomputed: lowercase phrase -> list of synonym phrases
_SEARCH_LOOKUP = {}
for _group in SEARCH_SYNONYMS:
    for _phrase in _group:
        key = _phrase.lower()
        if key not in _SEARCH_LOOKUP:
            _SEARCH_LOOKUP[key] = []
        for _syn in _group:
            if _syn.lower() != key:
                _SEARCH_LOOKUP[key].append(_syn)


def expand_search_terms(query):
    """Expand a search query into a list of phrase-level search terms.

    Args:
        query: search string (e.g. "CRP" or "c-reactive protein")

    Returns:
        list of search phrases: [original, synonym1, synonym2, ...]
    """
    q = query.strip().lower()
    terms = [query]  # always include original

    # Direct phrase match
    if q in _SEARCH_LOOKUP:
        for syn in _SEARCH_LOOKUP[q]:
            if syn.lower() not in {t.lower() for t in terms}:
                terms.append(syn)
        return terms

    # If no direct match, try matching against group phrases with token overlap
    q_tokens = tokenize(query) - BOILERPLATE_WORDS
    if not q_tokens:
        return terms

    for group in SEARCH_SYNONYMS:
        for phrase in group:
            phrase_tokens = tokenize(phrase) - BOILERPLATE_WORDS
            # Require the query tokens to be a subset of (or equal to) a phrase's tokens
            # or vice versa, with at least one meaningful token match
            if q_tokens and phrase_tokens and q_tokens <= phrase_tokens:
                # Query is a subset of this phrase — add all synonyms from the group
                for syn in group:
                    if syn.lower() not in {t.lower() for t in terms}:
                        terms.append(syn)
                break  # found the matching group

    return terms


# ══════════════════════════════════════════════════════════════
# Boilerplate words to strip before matching
# ══════════════════════════════════════════════════════════════

BOILERPLATE_WORDS = {
    'in', 'of', 'or', 'by', 'the', 'a', 'an',
    'serum', 'plasma', 'blood', 'volume', 'mass', 'ratio', 'fraction',
    'moles', 'units', 'enzymatic', 'activity', 'presence', 'strip',
    'test', 'light', 'microscopy', 'using', 'method', 'specimen',
    'entitic', 'respiratory', 'arterial', 'poor', 'calculated',
    'calc', 'direct', 'assay', 'immunoassay', 'sensitivity',
    's', 'p', 'b', 'wb',
}

# ══════════════════════════════════════════════════════════════
# Precomputed lookup: word -> set of all synonyms
# ══════════════════════════════════════════════════════════════

_SYNONYM_LOOKUP = {}
for _group in SYNONYM_GROUPS:
    for _word in _group:
        if _word not in _SYNONYM_LOOKUP:
            _SYNONYM_LOOKUP[_word] = set()
        _SYNONYM_LOOKUP[_word].update(_group)


def expand_synonyms(words):
    """Expand a set of words to include all known synonyms.

    Args:
        words: set of lowercase words

    Returns:
        set containing original words plus all synonyms
    """
    expanded = set(words)
    for w in words:
        if w in _SYNONYM_LOOKUP:
            expanded.update(_SYNONYM_LOOKUP[w])
    return expanded


def tokenize(text):
    """Tokenize a display name or search query into lowercase words."""
    return set(re.findall(r'[a-z0-9]+', text.lower()))


def expand_query(query):
    """Expand a search query with synonyms.

    Args:
        query: search string (e.g. "CRP" or "C-reactive protein")

    Returns:
        set of all words to search for (original + synonyms)
    """
    words = tokenize(query) - BOILERPLATE_WORDS
    return expand_synonyms(words)


def names_match(name_a, name_b):
    """Check if two display names are semantically equivalent.

    Returns True if the names share at least one meaningful word
    (after synonym expansion and boilerplate removal).
    """
    words_a = tokenize(name_a) - BOILERPLATE_WORDS
    words_b = tokenize(name_b) - BOILERPLATE_WORDS

    if not words_a or not words_b:
        return True  # Can't compare, assume OK

    expanded_a = expand_synonyms(words_a)
    expanded_b = expand_synonyms(words_b)

    overlap = (words_a & expanded_b) | (words_b & expanded_a)
    return len(overlap) > 0
