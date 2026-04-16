#!/usr/bin/env python3
"""
Generate FHIR R4 transaction bundles for historical PDF-based medical records (2012-2017, 2023).

Sources:
  1. 2012 Sutter East Bay (Nov 5, 2012) - Dr Amy B Levin: CBC, CMP, Celiac, H.pylori, TSH, Amylase, Lipase, ESR, Urinalysis
  2. 2013 Sutter (Jan 24, 2013) - Dr Frank Fazzolari: Lipid Profile, CT Abdomen/Pelvis
  3. 2016 LabCorp (Apr 19, 2016) - One Medical / Tercero S: CMP, Lipids, HbA1c, PSA, CV Report
  4. 2017 LabCorp (Jul 20, 2017) - Functional Medicine SF / S Daniel: massive panel (50+ components)
  5. 2017 SIBO Center (Aug 12, 2017) - Dr Stephanie Daniel: SIBO Breath Test (triple positive)
  6. 2017 Doctor's Data (Jul 12, 2017) - Comprehensive Stool Analysis/Parasitology x3
  7. 2017 VCS APTitude Screening (Nov 5, 2017) - Visual Contrast Sensitivity test (Fail)
  8. 2023 UCSF/MarinHealth (May 24, 2023) - MR Cervical Spine
"""

import json
from fhir_utils import (make_id, make_narrative, make_meta_tag, sanitize_for_xhtml,
                        make_provenance_meta)

SCRIPT_NAME = 'create_historical_fhir.py'
SOURCE_CODE = 'historical-pdf-results'
SOURCE_DISPLAY = 'Historical PDF Test Results'
# Legacy tag kept for non-observation resources
SOURCE_TAG = make_meta_tag(SOURCE_CODE, SOURCE_DISPLAY)
OUTPUT_PREFIX = 'historical_results_fhir_batch'

# ──────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────

def obs(obs_id_prefix, order_id, date, code_text, value, unit, ref_range=None,
        interpretation=None, loinc_code=None, loinc_display=None,
        performer_name=None, identifier_system=None, value_string=None):
    """Build a single Observation entry."""
    oid = make_id(obs_id_prefix, order_id, code_text.replace(' ', '-').lower()[:40])
    r = {
        "resourceType": "Observation",
        "id": oid,
        "status": "final",
        "meta": make_provenance_meta(
            source_file=f'pdf:{obs_id_prefix}',
            source_tag_code=SOURCE_CODE,
            source_tag_display=SOURCE_DISPLAY,
            raw_name=code_text,
            order_name=order_id,
            convert_script=SCRIPT_NAME,
        ),
        "code": {
            "text": code_text
        },
        "effectiveDateTime": f"{date}T00:00:00Z",
    }
    if performer_name:
        r["performer"] = [{"display": performer_name}]
    if loinc_code:
        r["code"]["coding"] = [{"system": "http://loinc.org", "code": loinc_code, "display": loinc_display or code_text}]
    if identifier_system:
        r["identifier"] = [{"system": identifier_system, "value": order_id}]

    if value_string is not None:
        r["valueString"] = value_string
    elif value is not None:
        try:
            numeric = float(value) if not isinstance(value, (int, float)) else value
            r["valueQuantity"] = {
                "value": numeric,
                "unit": unit or "",
                "system": "http://unitsofmeasure.org",
                "code": unit or ""
            }
        except (ValueError, TypeError):
            r["valueString"] = str(value)

    if ref_range:
        r["referenceRange"] = [{"text": ref_range}]
    if interpretation:
        code_map = {"H": ("H", "High"), "L": ("L", "Low"), "N": ("N", "Normal"), "A": ("A", "Abnormal")}
        ic, id_ = code_map.get(interpretation, ("A", "Abnormal"))
        r["interpretation"] = [{"coding": [{"system": "http://terminology.hl7.org/CodeSystem/v3-ObservationInterpretation", "code": ic, "display": id_}]}]

    return {
        "resource": r,
        "request": {"method": "PUT", "url": f"Observation/{oid}"}
    }


def diagnostic_report(dr_id_prefix, order_id, date, code_text, narrative_text,
                      category_code="LAB", category_display="Laboratory",
                      performer_name=None, issued=None, conclusion=None,
                      observation_entries=None, identifier_system=None,
                      loinc_code=None, loinc_display=None):
    """Build a DiagnosticReport entry plus return all associated obs entries."""
    dr_id = make_id(dr_id_prefix, order_id, date)
    obs_refs = []
    if observation_entries:
        for e in observation_entries:
            obs_refs.append({"reference": f"Observation/{e['resource']['id']}"})

    r = {
        "resourceType": "DiagnosticReport",
        "id": dr_id,
        "status": "final",
        "meta": {"tag": [SOURCE_TAG]},
        "category": [{"coding": [{"system": "http://terminology.hl7.org/CodeSystem/v2-0074", "code": category_code, "display": category_display}]}],
        "code": {"text": code_text},
        "effectiveDateTime": f"{date}T00:00:00Z",
        "text": make_narrative(narrative_text, title=code_text),
    }
    if loinc_code:
        r["code"]["coding"] = [{"system": "http://loinc.org", "code": loinc_code, "display": loinc_display or code_text}]
    if performer_name:
        r["performer"] = [{"display": performer_name}]
    if issued:
        r["issued"] = f"{issued}T00:00:00Z"
    if conclusion:
        r["conclusion"] = conclusion
    if identifier_system:
        r["identifier"] = [{"system": identifier_system, "value": order_id}]
    if obs_refs:
        r["result"] = obs_refs

    dr_entry = {"resource": r, "request": {"method": "PUT", "url": f"DiagnosticReport/{dr_id}"}}
    entries = [dr_entry]
    if observation_entries:
        entries.extend(observation_entries)
    return entries


# ──────────────────────────────────────────────────────────────────────
# 1. 2012 Sutter East Bay - Dr Amy B Levin (Nov 5, 2012)
# ──────────────────────────────────────────────────────────────────────

def create_2012_sutter_cbc():
    date = "2012-11-05"
    order = "sutter-2012-cbc"
    perf = "Sutter East Bay / Amy B Levin, MD"
    idsys = "urn:sutter:order"
    components = [
        ("WBC", 7.6, "K/uL", "3.8-10.8", None, "6690-2"),
        ("RBC", 5.35, "M/uL", "4.20-5.80", None, "789-8"),
        ("Hemoglobin", 15.7, "g/dL", "13.2-17.1", None, "718-7"),
        ("Hematocrit", 47.1, "%", "38.5-50.0", None, "4544-3"),
        ("MCV", 88, "fL", "80-100", None, "787-2"),
        ("MCH", 29.3, "pg", "27.0-33.0", None, "785-6"),
        ("MCHC", 33.3, "g/dL", "32.0-36.0", None, "786-4"),
        ("RDW", 12.7, "%", "11.0-15.0", None, "788-0"),
        ("Platelet Count", 244, "K/uL", "140-400", None, "777-3"),
        ("Neutrophils %", 59, "%", "40-74", None, "770-8"),
        ("Lymphocytes %", 30, "%", "14-46", None, "736-9"),
        ("Monocytes %", 7, "%", "4-13", None, "5905-5"),
        ("Eosinophils %", 3, "%", "0-7", None, "713-8"),
        ("Basophils %", 1, "%", "0-3", None, "706-2"),
        ("Abs Neutrophils", 4.5, "K/uL", "1.5-7.8", None, "751-8"),
        ("Abs Lymphocytes", 2.3, "K/uL", "0.7-4.5", None, "731-0"),
        ("Abs Monocytes", 0.5, "K/uL", "0.1-1.0", None, "742-7"),
        ("Abs Eosinophils", 0.3, "K/uL", "0.0-0.5", None, "711-2"),
        ("Abs Basophils", 0.1, "K/uL", "0.0-0.2", None, "704-7"),
    ]
    obs_entries = [obs("sutter2012-cbc", order, date, c[0], c[1], c[2], c[3], c[4], c[5], performer_name=perf, identifier_system=idsys) for c in components]
    return diagnostic_report("sutter2012-dr-cbc", order, date, "CBC with Differential",
        "CBC with Differential - Sutter East Bay, Nov 5, 2012. Ordered by Amy B Levin, MD. All values within normal limits.",
        performer_name=perf, identifier_system=idsys, observation_entries=obs_entries, loinc_code="58410-2", loinc_display="CBC panel with Differential")


def create_2012_sutter_cmp():
    date = "2012-11-05"
    order = "sutter-2012-cmp"
    perf = "Sutter East Bay / Amy B Levin, MD"
    idsys = "urn:sutter:order"
    components = [
        ("Sodium", 144, "mmol/L", "136-145", None, "2951-2"),
        ("Potassium", 4.6, "mmol/L", "3.5-5.3", None, "2823-3"),
        ("Chloride", 107, "mmol/L", "98-110", None, "2075-0"),
        ("CO2", 32, "mmol/L", "22-32", None, "2028-9"),
        ("Glucose", 93, "mg/dL", "65-99", None, "2345-7"),
        ("BUN", 14, "mg/dL", "7-25", None, "3094-0"),
        ("Creatinine", 1.2, "mg/dL", "0.6-1.3", None, "2160-0"),
        ("GFR", 60, "mL/min/1.73m2", ">60", None, "33914-3"),
        ("Calcium", 8.9, "mg/dL", "8.5-10.5", None, "17861-6"),
        ("Total Protein", 7.4, "g/dL", "6.0-8.3", None, "2885-2"),
        ("Albumin", 4.1, "g/dL", "3.5-5.7", None, "1751-7"),
        ("Total Bilirubin", 0.6, "mg/dL", "0.2-1.2", None, "1975-2"),
        ("Alkaline Phosphatase", 76, "U/L", "40-150", None, "6768-6"),
        ("AST", 18, "U/L", "10-40", None, "1920-8"),
        ("ALT", 25, "U/L", "9-60", None, "1742-6"),
    ]
    obs_entries = [obs("sutter2012-cmp", order, date, c[0], c[1], c[2], c[3], c[4], c[5], performer_name=perf, identifier_system=idsys) for c in components]
    return diagnostic_report("sutter2012-dr-cmp", order, date, "Comprehensive Metabolic Panel",
        "CMP - Sutter East Bay, Nov 5, 2012. Ordered by Amy B Levin, MD. All values within normal limits.",
        performer_name=perf, identifier_system=idsys, observation_entries=obs_entries, loinc_code="24323-8", loinc_display="Comprehensive metabolic panel")


def create_2012_sutter_celiac():
    date = "2012-11-05"
    order = "sutter-2012-celiac"
    perf = "Sutter East Bay / Amy B Levin, MD"
    idsys = "urn:sutter:order"
    components = [
        ("TTG Ab IgA", 0.7, "U/mL", "<5 Negative", None, "31017-7"),
        ("Gliadin DGP Ab IgA", 1.8, "U/mL", "<20 Negative", None, "56467-0"),
        ("IgA", 215, "mg/dL", "70-400", None, "2458-8"),
    ]
    obs_entries = [obs("sutter2012-celiac", order, date, c[0], c[1], c[2], c[3], c[4], c[5], performer_name=perf, identifier_system=idsys) for c in components]
    return diagnostic_report("sutter2012-dr-celiac", order, date, "Celiac Panel",
        "Celiac Panel - Sutter East Bay, Nov 5, 2012. TTG Ab IgA 0.7 (<5 Neg), Gliadin DGP Ab IgA 1.8 (<20 Neg), IgA 215 (70-400). All negative for celiac disease.",
        performer_name=perf, identifier_system=idsys, observation_entries=obs_entries, conclusion="Celiac panel negative")


def create_2012_sutter_misc():
    """H.pylori, TSH, Amylase, Lipase, ESR as individual observations under one DR."""
    date = "2012-11-05"
    order = "sutter-2012-misc"
    perf = "Sutter East Bay / Amy B Levin, MD"
    idsys = "urn:sutter:order"
    components = [
        ("H. pylori Ab", 0.4, "U/mL", "<0.75 Negative", None, "5182-1"),
        ("TSH", 3.36, "uIU/mL", "0.40-4.50", None, "3016-3"),
        ("Amylase", 66, "U/L", "25-115", None, "1798-8"),
        ("Lipase", 101, "U/L", "73-393", None, "3040-3"),
        ("ESR", 8, "mm/hr", "0-15", None, "4537-7"),
    ]
    obs_entries = [obs("sutter2012-misc", order, date, c[0], c[1], c[2], c[3], c[4], c[5], performer_name=perf, identifier_system=idsys) for c in components]
    return diagnostic_report("sutter2012-dr-misc", order, date, "Miscellaneous Labs (H.pylori, TSH, Amylase, Lipase, ESR)",
        "Miscellaneous labs - Sutter East Bay, Nov 5, 2012. H.pylori Ab <0.4 (Neg), TSH 3.36, Amylase 66, Lipase 101, ESR 8. All within normal limits.",
        performer_name=perf, identifier_system=idsys, observation_entries=obs_entries)


def create_2012_sutter_urinalysis():
    date = "2012-11-05"
    order = "sutter-2012-ua"
    perf = "Sutter East Bay / Amy B Levin, MD"
    idsys = "urn:sutter:order"
    components = [
        ("UA Color", None, None, None, None, "5778-6", None, "Yellow"),
        ("UA Appearance", None, None, None, None, "5767-9", None, "Clear"),
        ("UA Specific Gravity", 1.024, None, "1.001-1.035", None, "5811-5"),
        ("UA pH", 5.0, None, "5.0-8.0", None, "5803-2"),
        ("UA Glucose", None, None, None, None, "5792-7", None, "Negative"),
        ("UA Bilirubin", None, None, None, None, "5770-3", None, "Negative"),
        ("UA Ketones", None, None, None, None, "5797-6", None, "Negative"),
        ("UA Blood", None, None, None, None, "5794-3", None, "Negative"),
        ("UA Protein", None, None, None, None, "5804-0", None, "Negative"),
        ("UA Nitrite", None, None, None, None, "5802-4", None, "Negative"),
        ("UA Leukocyte Esterase", None, None, None, None, "5799-2", None, "Negative"),
    ]
    obs_entries = []
    for c in components:
        if len(c) > 7 and c[7]:  # value_string
            obs_entries.append(obs("sutter2012-ua", order, date, c[0], None, None, c[3], c[4], c[5], performer_name=perf, identifier_system=idsys, value_string=c[7]))
        else:
            obs_entries.append(obs("sutter2012-ua", order, date, c[0], c[1], c[2], c[3], c[4], c[5], performer_name=perf, identifier_system=idsys))
    return diagnostic_report("sutter2012-dr-ua", order, date, "Urinalysis",
        "Urinalysis - Sutter East Bay, Nov 5, 2012. Normal urinalysis. Color Yellow, Clear, SG 1.024, pH 5.0, all components negative.",
        performer_name=perf, identifier_system=idsys, observation_entries=obs_entries, loinc_code="24356-8", loinc_display="Urinalysis complete panel")


# ──────────────────────────────────────────────────────────────────────
# 2. 2013 Sutter - Dr Frank Fazzolari (Jan 24, 2013)
# ──────────────────────────────────────────────────────────────────────

def create_2013_sutter_lipids():
    date = "2013-01-24"
    order = "sutter-2013-lipid"
    perf = "Sutter / Frank Anthony Fazzolari, MD"
    idsys = "urn:sutter:order"
    components = [
        ("Total Cholesterol", 205, "mg/dL", "<200", "H", "2093-3"),
        ("Triglycerides", 158, "mg/dL", "<150", "H", "2571-8"),
        ("HDL Cholesterol", 31, "mg/dL", ">40", "L", "2085-9"),
        ("LDL Cholesterol", 142, "mg/dL", "<130", "H", "13457-7"),
        ("Chol/HDL Ratio", 6.6, None, "<5.0", "H", "9830-1"),
    ]
    obs_entries = [obs("sutter2013-lipid", order, date, c[0], c[1], c[2], c[3], c[4], c[5], performer_name=perf, identifier_system=idsys) for c in components]
    return diagnostic_report("sutter2013-dr-lipid", order, date, "Lipid Panel",
        "Lipid Panel - Sutter, Jan 24, 2013. Total Chol 205 H, Trig 158 H, HDL 31 L, LDL 142 H, Chol/HDL 6.6 H. Multiple abnormalities noted.",
        performer_name=perf, identifier_system=idsys, observation_entries=obs_entries,
        conclusion="Dyslipidemia: elevated total cholesterol, triglycerides, LDL; low HDL.", loinc_code="24331-1", loinc_display="Lipid panel")


def create_2013_sutter_ct():
    date = "2013-01-24"
    order = "sutter-2013-ct-abd"
    perf = "Sutter / Recha S Bergstrom, MD"
    narrative = """CT Abdomen and Pelvis with Contrast - Sutter, Jan 24, 2013
Ordering Provider: Frank Anthony Fazzolari, MD
Radiologist: Recha S Bergstrom, MD

Indication: Intermittent dyspepsia with 20 lb weight loss.

Technique: CT abdomen and pelvis with IV contrast.

Findings:
- Tiny hepatic lesions likely representing cysts or hemangiomas
- Thoracolumbar scoliosis
- Mild degenerative change
- Otherwise unremarkable examination of abdomen and pelvis

Impression:
1. Tiny hepatic lesions most likely representing cysts/hemangiomas
2. Thoracolumbar scoliosis, mild degenerative change
3. Otherwise normal CT abdomen and pelvis"""
    return diagnostic_report("sutter2013-dr-ct", order, date, "CT Abdomen and Pelvis with Contrast",
        narrative, category_code="RAD", category_display="Radiology",
        performer_name=perf, identifier_system="urn:sutter:order",
        conclusion="Tiny hepatic lesions (cysts/hemangiomas), thoracolumbar scoliosis, mild degenerative change. Otherwise normal.",
        loinc_code="30621-7", loinc_display="CT Abdomen and Pelvis")


# ──────────────────────────────────────────────────────────────────────
# 3. 2016 LabCorp - One Medical / Tercero S (Apr 19, 2016)
# ──────────────────────────────────────────────────────────────────────

def create_2016_labcorp_cmp():
    date = "2016-04-19"
    order = "11047772990"
    perf = "LabCorp / One Medical - Tercero S"
    idsys = "urn:labcorp:specimen"
    components = [
        ("Glucose", 96, "mg/dL", "65-99", None, "2345-7"),
        ("BUN", 14, "mg/dL", "6-24", None, "3094-0"),
        ("Creatinine", 1.16, "mg/dL", "0.76-1.27", None, "2160-0"),
        ("eGFR Non-Afr American", 73, "mL/min/1.73m2", ">59", None, "48642-3"),
        ("eGFR Afr American", 84, "mL/min/1.73m2", ">59", None, "48643-1"),
        ("Sodium", 144, "mmol/L", "134-144", None, "2951-2"),
        ("Potassium", 4.5, "mmol/L", "3.5-5.2", None, "2823-3"),
        ("Chloride", 102, "mmol/L", "97-108", None, "2075-0"),
        ("CO2", 20, "mmol/L", "19-28", None, "2028-9"),
        ("Calcium", 9.4, "mg/dL", "8.7-10.2", None, "17861-6"),
        ("Total Protein", 7.4, "g/dL", "6.0-8.5", None, "2885-2"),
        ("Albumin", 4.6, "g/dL", "3.5-5.5", None, "1751-7"),
        ("Globulin", 2.8, "g/dL", "1.5-4.5", None, "10834-0"),
        ("A/G Ratio", 1.6, None, "1.2-2.2", None, "1759-0"),
        ("Total Bilirubin", 0.8, "mg/dL", "0.0-1.2", None, "1975-2"),
        ("Alkaline Phosphatase", 81, "U/L", "39-117", None, "6768-6"),
        ("AST", 28, "U/L", "0-40", None, "1920-8"),
        ("ALT", 34, "U/L", "0-44", None, "1742-6"),
    ]
    obs_entries = [obs("labcorp2016-cmp", order, date, c[0], c[1], c[2], c[3], c[4], c[5], performer_name=perf, identifier_system=idsys) for c in components]
    return diagnostic_report("labcorp2016-dr-cmp", order, date, "Comprehensive Metabolic Panel",
        "CMP - LabCorp, Apr 19, 2016. Specimen 11047772990. Ordered by One Medical / Tercero S. All values within normal limits.",
        performer_name=perf, identifier_system=idsys, observation_entries=obs_entries, loinc_code="24323-8")


def create_2016_labcorp_lipids():
    date = "2016-04-19"
    order = "11047772990-lipid"
    perf = "LabCorp / One Medical - Tercero S"
    idsys = "urn:labcorp:specimen"
    components = [
        ("Total Cholesterol", 204, "mg/dL", "100-199", "H", "2093-3"),
        ("Triglycerides", 140, "mg/dL", "0-149", None, "2571-8"),
        ("HDL Cholesterol", 35, "mg/dL", ">39", "L", "2085-9"),
        ("VLDL Cholesterol", 28, "mg/dL", "5-40", None, "13458-5"),
        ("LDL Cholesterol", 141, "mg/dL", "0-99", "H", "13457-7"),
        ("Chol/HDL Ratio", 5.8, None, "0.0-5.0", "H", "9830-1"),
    ]
    obs_entries = [obs("labcorp2016-lipid", order, date, c[0], c[1], c[2], c[3], c[4], c[5], performer_name=perf, identifier_system=idsys) for c in components]
    return diagnostic_report("labcorp2016-dr-lipid", order, date, "Lipid Panel",
        "Lipid Panel - LabCorp, Apr 19, 2016. Total Chol 204 H, Trig 140, HDL 35 L, VLDL 28, LDL 141 H, Chol/HDL 5.8 H.",
        performer_name=perf, identifier_system=idsys, observation_entries=obs_entries,
        conclusion="Dyslipidemia persists: elevated total cholesterol, LDL; low HDL.", loinc_code="24331-1")


def create_2016_labcorp_hba1c_psa():
    date = "2016-04-19"
    order = "11047772990-misc"
    perf = "LabCorp / One Medical - Tercero S"
    idsys = "urn:labcorp:specimen"
    components = [
        ("Hemoglobin A1c", 5.8, "%", "4.8-5.6", "H", "4548-4"),
        ("PSA", 0.5, "ng/mL", "0.0-4.0", None, "2857-1"),
    ]
    obs_entries = [obs("labcorp2016-misc", order, date, c[0], c[1], c[2], c[3], c[4], c[5], performer_name=perf, identifier_system=idsys) for c in components]
    return diagnostic_report("labcorp2016-dr-misc", order, date, "HbA1c and PSA",
        "HbA1c 5.8% (4.8-5.6, pre-diabetes range). PSA 0.5 ng/mL (0.0-4.0, normal). Cardiovascular risk assessment: INTERMEDIATE.",
        performer_name=perf, identifier_system=idsys, observation_entries=obs_entries,
        conclusion="HbA1c 5.8% in pre-diabetes range. PSA normal. CV risk: intermediate.")


# ──────────────────────────────────────────────────────────────────────
# 4. 2017 LabCorp - Functional Medicine SF / S Daniel (Jul 20, 2017)
# ──────────────────────────────────────────────────────────────────────

def create_2017_labcorp_cbc():
    date = "2017-07-20"
    order = "201-477-7423-0"
    perf = "LabCorp / Functional Medicine SF - S Daniel"
    idsys = "urn:labcorp:specimen"
    components = [
        ("WBC", 6.5, "K/uL", "3.4-10.8", None, "6690-2"),
        ("RBC", 5.11, "M/uL", "4.14-5.80", None, "789-8"),
        ("Hemoglobin", 15.1, "g/dL", "12.6-17.7", None, "718-7"),
        ("Hematocrit", 45.1, "%", "37.5-51.0", None, "4544-3"),
        ("MCV", 88, "fL", "79-97", None, "787-2"),
        ("MCH", 29.5, "pg", "26.6-33.0", None, "785-6"),
        ("MCHC", 33.5, "g/dL", "31.5-35.7", None, "786-4"),
        ("RDW", 13.6, "%", "12.3-15.4", None, "788-0"),
        ("Platelet Count", 203, "K/uL", "150-379", None, "777-3"),
    ]
    obs_entries = [obs("labcorp2017-cbc", order, date, c[0], c[1], c[2], c[3], c[4], c[5], performer_name=perf, identifier_system=idsys) for c in components]
    return diagnostic_report("labcorp2017-dr-cbc", order, date, "CBC",
        "CBC - LabCorp, Jul 20, 2017. Specimen 201-477-7423-0. All values within normal limits.",
        performer_name=perf, identifier_system=idsys, observation_entries=obs_entries, loinc_code="58410-2")


def create_2017_labcorp_cmp():
    date = "2017-07-20"
    order = "201-477-7423-0-cmp"
    perf = "LabCorp / Functional Medicine SF - S Daniel"
    idsys = "urn:labcorp:specimen"
    components = [
        ("Glucose", 84, "mg/dL", "65-99", None, "2345-7"),
        ("BUN", 15, "mg/dL", "6-24", None, "3094-0"),
        ("Creatinine", 1.21, "mg/dL", "0.76-1.27", None, "2160-0"),
        ("eGFR Non-Afr American", 68, "mL/min/1.73m2", ">59", None, "48642-3"),
        ("eGFR Afr American", 79, "mL/min/1.73m2", ">59", None, "48643-1"),
        ("Sodium", 139, "mmol/L", "134-144", None, "2951-2"),
        ("Potassium", 4.2, "mmol/L", "3.5-5.2", None, "2823-3"),
        ("Chloride", 101, "mmol/L", "97-108", None, "2075-0"),
        ("CO2", 24, "mmol/L", "19-28", None, "2028-9"),
        ("Calcium", 9.0, "mg/dL", "8.7-10.2", None, "17861-6"),
        ("Total Protein", 7.0, "g/dL", "6.0-8.5", None, "2885-2"),
        ("Albumin", 4.4, "g/dL", "3.5-5.5", None, "1751-7"),
        ("Globulin", 2.6, "g/dL", "1.5-4.5", None, "10834-0"),
        ("A/G Ratio", 1.7, None, "1.2-2.2", None, "1759-0"),
        ("Total Bilirubin", 0.5, "mg/dL", "0.0-1.2", None, "1975-2"),
        ("Alkaline Phosphatase", 75, "U/L", "39-117", None, "6768-6"),
        ("AST", 19, "U/L", "0-40", None, "1920-8"),
        ("ALT", 16, "U/L", "0-44", None, "1742-6"),
    ]
    obs_entries = [obs("labcorp2017-cmp", order, date, c[0], c[1], c[2], c[3], c[4], c[5], performer_name=perf, identifier_system=idsys) for c in components]
    return diagnostic_report("labcorp2017-dr-cmp", order, date, "Comprehensive Metabolic Panel",
        "CMP - LabCorp, Jul 20, 2017. All values within normal limits.",
        performer_name=perf, identifier_system=idsys, observation_entries=obs_entries, loinc_code="24323-8")


def create_2017_labcorp_lipids():
    date = "2017-07-20"
    order = "201-477-7423-0-lipid"
    perf = "LabCorp / Functional Medicine SF - S Daniel"
    idsys = "urn:labcorp:specimen"
    components = [
        ("Total Cholesterol", 162, "mg/dL", "100-199", None, "2093-3"),
        ("Triglycerides", 93, "mg/dL", "0-149", None, "2571-8"),
        ("HDL Cholesterol", 31, "mg/dL", ">39", "L", "2085-9"),
        ("VLDL Cholesterol", 19, "mg/dL", "5-40", None, "13458-5"),
        ("LDL Cholesterol", 112, "mg/dL", "0-99", "H", "13457-7"),
        ("LDL/HDL Ratio", 3.6, None, "0.0-3.6", None, "11054-4"),
    ]
    obs_entries = [obs("labcorp2017-lipid", order, date, c[0], c[1], c[2], c[3], c[4], c[5], performer_name=perf, identifier_system=idsys) for c in components]
    return diagnostic_report("labcorp2017-dr-lipid", order, date, "Lipid Panel",
        "Lipid Panel - LabCorp, Jul 20, 2017. Total Chol 162, Trig 93, HDL 31 L, VLDL 19, LDL 112 H, LDL/HDL 3.6. HDL remains low.",
        performer_name=perf, identifier_system=idsys, observation_entries=obs_entries,
        conclusion="HDL remains low. LDL mildly elevated. Total cholesterol improved from prior.", loinc_code="24331-1")


def create_2017_labcorp_hormones():
    date = "2017-07-20"
    order = "201-477-7423-0-horm"
    perf = "LabCorp / Functional Medicine SF - S Daniel"
    idsys = "urn:labcorp:specimen"
    components = [
        ("SHBG", 42.1, "nmol/L", "16.5-55.9", None, "13967-5"),
        ("Testosterone Total", 477, "ng/dL", "264-916", None, "2986-8"),
        ("Free Testosterone", 11.7, "pg/mL", "6.8-21.5", None, "2991-8"),
        ("DHEA-Sulfate", 173, "ug/dL", "106-464", None, "2191-5"),
        ("Estradiol", 16, "pg/mL", "7.6-42.6", None, "2243-4"),
        ("FSH", 3.0, "mIU/mL", "1.5-12.4", None, "15067-2"),
        ("LH", 2.9, "mIU/mL", "1.7-8.6", None, "10501-5"),
    ]
    obs_entries = [obs("labcorp2017-horm", order, date, c[0], c[1], c[2], c[3], c[4], c[5], performer_name=perf, identifier_system=idsys) for c in components]
    return diagnostic_report("labcorp2017-dr-horm", order, date, "Hormone Panel",
        "Hormone Panel - LabCorp, Jul 20, 2017. Testosterone 477, Free T 11.7, DHEA-S 173, Estradiol 16, FSH 3.0, LH 2.9, SHBG 42.1. All within reference ranges.",
        performer_name=perf, identifier_system=idsys, observation_entries=obs_entries,
        conclusion="All hormone levels within normal reference ranges.")


def create_2017_labcorp_thyroid():
    date = "2017-07-20"
    order = "201-477-7423-0-thyroid"
    perf = "LabCorp / Functional Medicine SF - S Daniel"
    idsys = "urn:labcorp:specimen"
    components = [
        ("TSH", 3.160, "uIU/mL", "0.450-4.500", None, "3016-3"),
        ("Free T4", 1.32, "ng/dL", "0.82-1.77", None, "3024-7"),
        ("Reverse T3", 21.3, "ng/dL", "9.2-24.1", None, "35209-0"),
    ]
    obs_entries = [obs("labcorp2017-thyroid", order, date, c[0], c[1], c[2], c[3], c[4], c[5], performer_name=perf, identifier_system=idsys) for c in components]
    return diagnostic_report("labcorp2017-dr-thyroid", order, date, "Thyroid Panel",
        "Thyroid Panel - LabCorp, Jul 20, 2017. TSH 3.160, Free T4 1.32, Reverse T3 21.3. All within normal ranges.",
        performer_name=perf, identifier_system=idsys, observation_entries=obs_entries, loinc_code="24348-5")


def create_2017_labcorp_specialty():
    """HNK1/CD57, B12, Folate, PSA/Free, CRP, Homocysteine, VitD, CoQ10, Zinc, MMA."""
    date = "2017-07-20"
    order = "201-477-7423-0-spec"
    perf = "LabCorp / Functional Medicine SF - S Daniel"
    idsys = "urn:labcorp:specimen"
    components = [
        ("HNK1 (CD57) %CD8-/CD57+", 2.3, "%", None, None, "9246-0"),
        ("HNK1 (CD57) Absolute", 46, "cells/uL", "60-360", "L", "9246-0"),
        ("PSA Total", 0.7, "ng/mL", "0.0-4.0", None, "2857-1"),
        ("PSA Free", 0.20, "ng/mL", None, None, "10886-0"),
        ("PSA % Free", 28.6, "%", None, None, "12841-3"),
        ("Vitamin B12", 234, "pg/mL", "211-946", None, "2132-9"),
        ("Folate", 9.5, "ng/mL", ">3.0", None, "2284-8"),
        ("CRP Cardiac", 0.63, "mg/L", "0.00-3.00", None, "30522-7"),
        ("Homocysteine", 20.0, "umol/L", "0-15", "H", "13965-9"),
        ("Vitamin D 25-OH", 25.4, "ng/mL", "30-100", "L", "1989-3"),
        ("Coenzyme Q10", 0.49, "ug/mL", "0.37-2.20", None, "46099-0"),
        ("Zinc RBC", 1523, "ug/dL", "822-1571", None, "30167-0"),
        ("Methylmalonic Acid", 152, "nmol/L", "0-378", None, "43888-5"),
    ]
    obs_entries = [obs("labcorp2017-spec", order, date, c[0], c[1], c[2], c[3], c[4], c[5], performer_name=perf, identifier_system=idsys) for c in components]
    return diagnostic_report("labcorp2017-dr-spec", order, date, "Specialty Labs (CD57, Vitamins, Inflammation Markers)",
        "Specialty Labs - LabCorp, Jul 20, 2017. Notable: CD57 Abs 46 LOW (60-360), Homocysteine 20.0 HIGH (0-15), Vitamin D 25.4 LOW (30-100). B12 234, Folate 9.5, CRP 0.63, CoQ10 0.49, Zinc RBC 1523, MMA 152.",
        performer_name=perf, identifier_system=idsys, observation_entries=obs_entries,
        conclusion="Low CD57 (possible chronic infection marker), elevated homocysteine (cardiovascular risk), low vitamin D. Other specialty markers within range.")


# ──────────────────────────────────────────────────────────────────────
# 5. 2017 SIBO Center - Dr Stephanie Daniel (Aug 12, 2017)
# ──────────────────────────────────────────────────────────────────────

def create_2017_sibo():
    date = "2017-08-12"
    order = "sibo-center-2017"
    perf = "SIBO Center / Stephanie Daniel"
    idsys = "urn:sibocenter:order"

    time_points = [
        (0, 2, 3), (20, 3, 4), (40, 2, 3), (60, 1, 3),
        (80, 2, 4), (100, 16, 14), (120, 29, 18), (140, 29, 19),
        (160, 29, 19), (180, 45, 24),
    ]

    obs_entries = []
    for t, h2, ch4 in time_points:
        obs_entries.append(obs("sibo2017-h2", order, date, f"Hydrogen (H2) at {t} min", h2, "ppm",
            None, None, "43282-1", f"H2 at {t} min", performer_name=perf, identifier_system=idsys))
        obs_entries.append(obs("sibo2017-ch4", order, date, f"Methane (CH4) at {t} min", ch4, "ppm",
            None, None, "43283-9", f"CH4 at {t} min", performer_name=perf, identifier_system=idsys))

    # Summary observations
    obs_entries.append(obs("sibo2017-sum", order, date, "Greatest H2 in 120min", 29, "ppm", "<=20", "H", "43282-1", performer_name=perf, identifier_system=idsys))
    obs_entries.append(obs("sibo2017-sum", order, date, "Greatest H2 increase", 28, "ppm", "<=20", "H", "43282-1", performer_name=perf, identifier_system=idsys))
    obs_entries.append(obs("sibo2017-sum", order, date, "Greatest CH4", 18, "ppm", "<=12", "H", "43283-9", performer_name=perf, identifier_system=idsys))
    obs_entries.append(obs("sibo2017-sum", order, date, "Greatest CH4 increase", 15, "ppm", "<=12", "H", "43283-9", performer_name=perf, identifier_system=idsys))
    obs_entries.append(obs("sibo2017-sum", order, date, "Greatest combined H2+CH4", 47, "ppm", "<=15", "H", performer_name=perf, identifier_system=idsys))

    narrative = """SIBO Breath Test - SIBO Center, Aug 12, 2017
Provider: Stephanie Daniel

10 time points (Baseline through 180 min):
H2: 2, 3, 2, 1, 2, 16, 29, 29, 29, 45
CH4: 3, 4, 3, 3, 4, 14, 18, 19, 19, 24

Analysis:
- Combined baseline 5 (normal <=20)
- Greatest H2 in 120min: 29 HIGH
- Greatest H2 increase: 28 HIGH
- Greatest CH4: 18 HIGH
- Greatest CH4 increase: 15 HIGH
- Greatest combined: 47 HIGH

TRIPLE POSITIVE:
- SIBO Suspected Elevated Hydrogen: POSITIVE
- SIBO Suspected Elevated Methane: POSITIVE
- SIBO Suspected Elevated Combined: POSITIVE"""

    return diagnostic_report("sibo2017-dr", order, date, "SIBO Breath Test (Triple Positive)",
        narrative, performer_name=perf, identifier_system=idsys, observation_entries=obs_entries,
        conclusion="Triple-positive SIBO: elevated hydrogen, methane, and combined. Significant bacterial overgrowth indicated.",
        loinc_code="89101-1", loinc_display="SIBO breath test")


# ──────────────────────────────────────────────────────────────────────
# 6. 2017 Doctor's Data - CSAPx3 (Jul 12, 2017)
# ──────────────────────────────────────────────────────────────────────

def create_2017_stool_analysis():
    date = "2017-07-12"
    order = "F170714-0127-1"
    perf = "Doctor's Data, Inc."
    idsys = "urn:doctorsdata:lab"

    obs_entries = []

    # Bacteriology (qualitative - use valueString)
    bact = [
        ("Bacteroides fragilis group", "3+", "Expected/Beneficial"),
        ("Bifidobacterium spp.", "NG", "Expected/Beneficial"),
        ("Escherichia coli", "NG", "Expected/Beneficial"),
        ("Lactobacillus spp.", "1+", "Expected/Beneficial"),
        ("Enterococcus spp.", "NG", "Expected/Beneficial"),
        ("Clostridium spp.", "3+", "Expected/Beneficial"),
        ("Hemolytic Escherichia coli", "2+", "Commensal (Imbalanced)"),
    ]
    for name, level, category in bact:
        obs_entries.append(obs("ddi2017-bact", order, date, f"Bacteriology: {name}", None, None,
            f"Category: {category}", None, None, None, performer_name=perf, identifier_system=idsys, value_string=level))

    # Digestion/Absorption
    obs_entries.append(obs("ddi2017-dig", order, date, "Elastase", 500, "ug/mL", ">200", None, "14720-4", performer_name=perf, identifier_system=idsys))
    obs_entries.append(obs("ddi2017-dig", order, date, "Fecal Fat Stain", None, None, "None-Mod", None, None, None, performer_name=perf, identifier_system=idsys, value_string="Moderate"))
    obs_entries.append(obs("ddi2017-dig", order, date, "Muscle Fibers", None, None, "None-Rare", None, None, None, performer_name=perf, identifier_system=idsys, value_string="None"))
    obs_entries.append(obs("ddi2017-dig", order, date, "Vegetable Fibers", None, None, "None-Few", None, None, None, performer_name=perf, identifier_system=idsys, value_string="Rare"))
    obs_entries.append(obs("ddi2017-dig", order, date, "Reducing Substances (Carbs)", None, None, "Neg", None, None, None, performer_name=perf, identifier_system=idsys, value_string="Negative"))

    # Inflammation
    obs_entries.append(obs("ddi2017-infl", order, date, "Lactoferrin", 0.5, "ug/mL", "<7.3", None, "56907-5", performer_name=perf, identifier_system=idsys))
    obs_entries.append(obs("ddi2017-infl", order, date, "Calprotectin", 10, "ug/g", "<=50", None, "38449-2", performer_name=perf, identifier_system=idsys))
    obs_entries.append(obs("ddi2017-infl", order, date, "Lysozyme", 283, "ng/mL", "<=600", None, "25459-9", performer_name=perf, identifier_system=idsys))
    obs_entries.append(obs("ddi2017-infl", order, date, "Stool WBC", None, None, "None-Rare", None, None, None, performer_name=perf, identifier_system=idsys, value_string="None"))
    obs_entries.append(obs("ddi2017-infl", order, date, "Stool Mucus", None, None, "Neg", None, None, None, performer_name=perf, identifier_system=idsys, value_string="Negative"))

    # Immunology
    obs_entries.append(obs("ddi2017-imm", order, date, "Secretory IgA", 1050, "mg/dL", "51-204", "H", "14341-9", performer_name=perf, identifier_system=idsys))

    # SCFAs
    scfa = [
        ("% Acetate", 52, "%", "40-75"),
        ("% Propionate", 18, "%", "9-29"),
        ("% Butyrate", 26, "%", "9-37"),
        ("% Valerate", 3.6, "%", "0.5-7"),
        ("Butyrate", 1.5, "mg/mL", "0.8-4.8"),
        ("Total SCFA", 5.6, "mg/mL", "4-18"),
    ]
    for name, val, unit, ref in scfa:
        obs_entries.append(obs("ddi2017-scfa", order, date, name, val, unit, ref, None, None, None, performer_name=perf, identifier_system=idsys))

    # Intestinal Health
    obs_entries.append(obs("ddi2017-ih", order, date, "Stool RBC", None, None, "None-Rare", "H", None, None, performer_name=perf, identifier_system=idsys, value_string="Moderate"))
    obs_entries.append(obs("ddi2017-ih", order, date, "Stool pH", 6.4, None, "6-7.8", None, "2753-4", performer_name=perf, identifier_system=idsys))
    obs_entries.append(obs("ddi2017-ih", order, date, "Occult Blood", None, None, "Neg", None, "2335-8", None, performer_name=perf, identifier_system=idsys, value_string="Negative"))

    # Parasitology
    obs_entries.append(obs("ddi2017-para", order, date, "Ova and Parasites x3", None, None, "None", None, None, None, performer_name=perf, identifier_system=idsys, value_string="None detected in 3 samples"))
    obs_entries.append(obs("ddi2017-para", order, date, "Giardia duodenalis", None, None, "Neg", None, None, None, performer_name=perf, identifier_system=idsys, value_string="Negative"))
    obs_entries.append(obs("ddi2017-para", order, date, "Cryptosporidium", None, None, "Neg", None, None, None, performer_name=perf, identifier_system=idsys, value_string="Negative"))
    obs_entries.append(obs("ddi2017-para", order, date, "Microscopic Yeast", None, None, "None-Rare", None, None, None, performer_name=perf, identifier_system=idsys, value_string="Rare"))

    narrative = """Comprehensive Stool Analysis / Parasitology x3 - Doctor's Data, Inc.
Lab #: F170714-0127-1. Collected: 07/12/2017. Reported: 07/31/2017.
Doctor: Stephanie Daniel, DO / Functional Medicine SF.

BACTERIOLOGY: Bacteroides fragilis 3+, Bifidobacterium NG, E.coli NG, Lactobacillus 1+, Enterococcus NG, Clostridium 3+. Commensal: Hemolytic E.coli 2+. No dysbiotic flora. No yeast isolated.

PARASITOLOGY: No ova or parasites in 3 samples. Giardia Neg. Cryptosporidium Neg. RBC: Rare/Rare/Moderate. Microscopic yeast: Rare.

DIGESTION: Elastase >500 (>200), Fat Stain Mod, Muscle fibers None, Veg fibers Rare, Carbs Neg.

INFLAMMATION: Lactoferrin <0.5 (<7.3), Calprotectin <10 (<=50), Lysozyme 283 (<=600), WBC None, Mucus Neg.

IMMUNOLOGY: Secretory IgA 1050 mg/dL (51-204) **VERY HIGH** - upregulated immune response.

SCFA: Acetate 52%, Propionate 18%, Butyrate 26% (1.5 mg/mL), Valerate 3.6%, Total 5.6 mg/mL.

INTESTINAL HEALTH: RBC Moderate (expected None-Rare) **HIGH**, pH 6.4, Occult Blood Neg.

Key Findings: Markedly elevated Secretory IgA suggesting upregulated mucosal immune response. Elevated stool RBCs. Low beneficial flora (Bifidobacterium, E.coli, Enterococcus absent). Imbalanced flora (Hemolytic E.coli 2+)."""

    return diagnostic_report("ddi2017-dr-csap", order, date, "Comprehensive Stool Analysis / Parasitology x3",
        narrative, performer_name=perf, identifier_system=idsys, observation_entries=obs_entries,
        issued="2017-07-31",
        conclusion="Markedly elevated Secretory IgA (1050, ref 51-204). Elevated stool RBCs. Low beneficial flora (absent Bifidobacterium, E.coli, Enterococcus). Hemolytic E.coli 2+ in commensal category. No parasites or dysbiotic organisms.")


# ──────────────────────────────────────────────────────────────────────
# 7. 2017 VCS APTitude Test (Nov 5, 2017)
# ──────────────────────────────────────────────────────────────────────

def create_2017_vcs():
    date = "2017-11-05"
    order = "vcs-2017-11-05"
    perf = "survivingmold.com"

    narrative = """VCS APTitude Screening Test - survivingmold.com
Date: 11/05/2017

Visual Contrast Sensitivity test for biotoxin illness screening.
Left Eye and Right Eye tested across 5 spatial frequencies (A-E) and 9 contrast levels.

Result: FAIL

Symptoms Score: Negative (only Abdominal Pain reported as Yes)
Environmental Exposures Score: Negative
Illness Diagnoses: None reported

The VCS test is used as a screening tool for biotoxin-related illness (mold, Lyme, etc.).
A failing result may indicate neurotoxin exposure affecting visual contrast sensitivity."""

    obs_entries = []
    obs_entries.append(obs("vcs2017", order, date, "VCS Overall Result", None, None, "Pass", "A", None, None, performer_name=perf, identifier_system="urn:survivingmold:vcs", value_string="Fail"))
    obs_entries.append(obs("vcs2017", order, date, "VCS Symptoms Score", None, None, None, None, None, None, performer_name=perf, identifier_system="urn:survivingmold:vcs", value_string="Negative (Abdominal Pain only)"))

    return diagnostic_report("vcs2017-dr", order, date, "VCS APTitude Visual Contrast Sensitivity Screening",
        narrative, category_code="LAB", category_display="Laboratory",
        performer_name=perf, identifier_system="urn:survivingmold:vcs",
        conclusion="VCS test result: FAIL. Symptoms score: Negative. Environmental exposures: Negative.")


# ──────────────────────────────────────────────────────────────────────
# 8. 2023 UCSF/MarinHealth - MR Cervical Spine (May 24, 2023)
# ──────────────────────────────────────────────────────────────────────

def create_2023_cervical_mri():
    date = "2023-05-24"
    order = "MHD_10023252425"
    perf = "MarinHealth Medical Center / John B Colby, MD"

    narrative = """MR Cervical Spine without Contrast - MarinHealth Medical Center
Date: May 24, 2023. Accession: MHD_10023252425.
Ordered by: Ilkcan Cokgor. Read by: John B Colby, MD.

Reason: Cervical radicular neurologic deficit / Cervical radiculopathy.
Comparison: CT 05/23/2023.

Findings (level by level):
C2-C3: No significant disc abnormality or canal/foraminal narrowing.
C3-C4: Mild disc bulge and uncovertebral hypertrophy. Mild bilateral foraminal narrowing.
C4-C5: Mild disc bulge and uncovertebral hypertrophy. Mild right foraminal narrowing.
C5-C6: Disc-osteophyte complex with disc space narrowing. Cord flattening without signal abnormality. Mild left foraminal narrowing.
C6-C7: Disc-osteophyte complex, partial effacement of ventral and dorsal CSF space. Moderate bilateral foraminal narrowing.
C7-T1: No significant disc abnormality or canal/foraminal narrowing.

Impression:
1. Multilevel spondylosis with canal narrowing at C6-7 and multilevel foraminal narrowing (up to moderate at C6-7).
2. Suggestion of chronic central cord signal abnormality at C6-7 suggesting chronic spondylotic myelopathy."""

    return diagnostic_report("ucsf2023-dr-cspine", order, date, "MR Cervical Spine without Contrast",
        narrative, category_code="RAD", category_display="Radiology",
        performer_name=perf, identifier_system="urn:marinhealth:accession",
        conclusion="Multilevel spondylosis with canal narrowing at C6-7 and multilevel foraminal narrowing (up to moderate at C6-7). Suggestion of chronic spondylotic myelopathy at C6-7.",
        loinc_code="24969-8", loinc_display="MR Cervical spine")


# ──────────────────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────────────────

def main():
    all_entries = []

    # 2012 Sutter
    all_entries.extend(create_2012_sutter_cbc())
    all_entries.extend(create_2012_sutter_cmp())
    all_entries.extend(create_2012_sutter_celiac())
    all_entries.extend(create_2012_sutter_misc())
    all_entries.extend(create_2012_sutter_urinalysis())

    # 2013 Sutter
    all_entries.extend(create_2013_sutter_lipids())
    all_entries.extend(create_2013_sutter_ct())

    # 2016 LabCorp
    all_entries.extend(create_2016_labcorp_cmp())
    all_entries.extend(create_2016_labcorp_lipids())
    all_entries.extend(create_2016_labcorp_hba1c_psa())

    # 2017 LabCorp
    all_entries.extend(create_2017_labcorp_cbc())
    all_entries.extend(create_2017_labcorp_cmp())
    all_entries.extend(create_2017_labcorp_lipids())
    all_entries.extend(create_2017_labcorp_hormones())
    all_entries.extend(create_2017_labcorp_thyroid())
    all_entries.extend(create_2017_labcorp_specialty())

    # 2017 SIBO
    all_entries.extend(create_2017_sibo())

    # 2017 Stool Analysis
    all_entries.extend(create_2017_stool_analysis())

    # 2017 VCS
    all_entries.extend(create_2017_vcs())

    # 2023 Cervical MRI
    all_entries.extend(create_2023_cervical_mri())

    # Count resource types
    dr_count = sum(1 for e in all_entries if e['resource']['resourceType'] == 'DiagnosticReport')
    obs_count = sum(1 for e in all_entries if e['resource']['resourceType'] == 'Observation')

    # Write bundle
    bundle = {
        "resourceType": "Bundle",
        "type": "transaction",
        "entry": all_entries
    }

    outfile = f"{OUTPUT_PREFIX}_1.json"
    with open(outfile, 'w') as f:
        json.dump(bundle, f, indent=2)

    print(f"✓ Bundle written to {outfile}")
    print(f"\nBundle Summary:")
    print(f"  Total entries: {len(all_entries)}")
    print(f"  DiagnosticReports: {dr_count}")
    print(f"  Observations: {obs_count}")
    print(f"\nReports by date:")
    print(f"  2012-11-05: CBC, CMP, Celiac Panel, Misc Labs, Urinalysis (Sutter)")
    print(f"  2013-01-24: Lipid Panel, CT Abdomen/Pelvis (Sutter)")
    print(f"  2016-04-19: CMP, Lipid Panel, HbA1c/PSA (LabCorp)")
    print(f"  2017-07-12: Comprehensive Stool Analysis/Parasitology x3 (Doctor's Data)")
    print(f"  2017-07-20: CBC, CMP, Lipids, Hormones, Thyroid, Specialty Labs (LabCorp)")
    print(f"  2017-08-12: SIBO Breath Test - Triple Positive (SIBO Center)")
    print(f"  2017-11-05: VCS APTitude Screening - FAIL (survivingmold.com)")
    print(f"  2023-05-24: MR Cervical Spine (MarinHealth/UCSF)")


if __name__ == '__main__':
    main()
