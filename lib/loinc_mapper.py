#!/usr/bin/env python3
"""LOINC Code Mapper — assigns LOINC codes to observations missing them.

Maintains a comprehensive display-name-to-LOINC lookup table and applies it
to all observations in HAPI that lack LOINC coding.

Can be run:
  - Retroactively: scan all existing observations and backfill
  - As part of ingestion: call assign_loinc(observation) before loading

Usage:
  python3 loinc_mapper.py --dry-run       # Preview what would be updated
  python3 loinc_mapper.py                 # Apply LOINC codes
  python3 loinc_mapper.py --stats         # Show coverage statistics only
"""

import json
import re
import sys
import requests
from collections import Counter
from fhir_utils import add_provenance_tag, PHV_TAG_SYSTEM

HAPI_BASE = 'http://localhost:8080/fhir'
DRY_RUN = '--dry-run' in sys.argv
STATS_ONLY = '--stats' in sys.argv

MAPPER_VERSION = '2'  # Bump when the lookup table or logic changes materially

# ══════════════════════════════════════════════════════════════════
# LOINC Lookup Table
#
# Maps display names (case-insensitive) to LOINC codes.
# Multiple display names can map to the same LOINC code.
# Format: 'display name': ('loinc_code', 'standard_display')
# ══════════════════════════════════════════════════════════════════

LOINC_MAP = {
    # ── Hematology / CBC ──
    'wbc':                          ('6690-2', 'Leukocytes [#/volume] in Blood'),
    'white blood cells':            ('6690-2', 'Leukocytes [#/volume] in Blood'),
    'white blood cell count':       ('6690-2', 'Leukocytes [#/volume] in Blood'),
    'wbc count':                    ('6690-2', 'Leukocytes [#/volume] in Blood'),
    'rbc':                          ('789-8', 'Erythrocytes [#/volume] in Blood'),
    'red blood cells':              ('789-8', 'Erythrocytes [#/volume] in Blood'),
    'red blood cell count':         ('789-8', 'Erythrocytes [#/volume] in Blood'),
    'rbc count':                    ('789-8', 'Erythrocytes [#/volume] in Blood'),
    'hemoglobin':                   ('718-7', 'Hemoglobin [Mass/volume] in Blood'),
    'hgb':                          ('718-7', 'Hemoglobin [Mass/volume] in Blood'),
    'hematocrit':                   ('4544-3', 'Hematocrit [Volume Fraction] of Blood'),
    'hct':                          ('4544-3', 'Hematocrit [Volume Fraction] of Blood'),
    'platelet count':               ('777-3', 'Platelets [#/volume] in Blood'),
    'platelets':                    ('777-3', 'Platelets [#/volume] in Blood'),
    'plt':                          ('777-3', 'Platelets [#/volume] in Blood'),
    'mcv':                          ('787-2', 'MCV [Entitic volume]'),
    'mean corpuscular volume':      ('787-2', 'MCV [Entitic volume]'),
    'mch':                          ('785-6', 'MCH [Entitic mass]'),
    'mean corpuscular hemoglobin':  ('785-6', 'MCH [Entitic mass]'),
    'mchc':                         ('786-4', 'MCHC [Mass/volume]'),
    'rdw':                          ('788-0', 'RDW [Ratio]'),
    'red cell distribution width':  ('788-0', 'RDW [Ratio]'),
    'rdw-std dev':                  ('21000-5', 'RDW [Entitic volume] by Automated count'),
    'mpv':                          ('32623-1', 'MPV [Entitic volume] in Blood'),
    'mean platelet volume':         ('32623-1', 'MPV [Entitic volume] in Blood'),
    'neutrophil, absolute':         ('751-8', 'Neutrophils [#/volume] in Blood'),
    'neutrophil, abs':              ('751-8', 'Neutrophils [#/volume] in Blood'),
    'absolute neutrophils':         ('751-8', 'Neutrophils [#/volume] in Blood'),
    'neutrophils abs':              ('751-8', 'Neutrophils [#/volume] in Blood'),
    'abs neutrophils':              ('751-8', 'Neutrophils [#/volume] in Blood'),
    'lymphocyte, absolute':         ('731-0', 'Lymphocytes [#/volume] in Blood'),
    'lymphocyte, abs':              ('731-0', 'Lymphocytes [#/volume] in Blood'),
    'absolute lymphocytes':         ('731-0', 'Lymphocytes [#/volume] in Blood'),
    'lymphocytes abs':              ('731-0', 'Lymphocytes [#/volume] in Blood'),
    'abs lymphocytes':              ('731-0', 'Lymphocytes [#/volume] in Blood'),
    'monocyte, absolute':           ('742-7', 'Monocytes [#/volume] in Blood'),
    'monocyte, abs':                ('742-7', 'Monocytes [#/volume] in Blood'),
    'absolute monocytes':           ('742-7', 'Monocytes [#/volume] in Blood'),
    'monocytes abs':                ('742-7', 'Monocytes [#/volume] in Blood'),
    'eosinophil, absolute':         ('711-2', 'Eosinophils [#/volume] in Blood'),
    'eosinophil, abs':              ('711-2', 'Eosinophils [#/volume] in Blood'),
    'absolute eosinophils':         ('711-2', 'Eosinophils [#/volume] in Blood'),
    'eosinophils abs':              ('711-2', 'Eosinophils [#/volume] in Blood'),
    'basophil, absolute':           ('704-7', 'Basophils [#/volume] in Blood'),
    'basophil, abs':                ('704-7', 'Basophils [#/volume] in Blood'),
    'basophils abs':                ('704-7', 'Basophils [#/volume] in Blood'),
    'absolute basophils':           ('704-7', 'Basophils [#/volume] in Blood'),
    'abs basophils':                ('704-7', 'Basophils [#/volume] in Blood'),
    'abs monocytes':                ('742-7', 'Monocytes [#/volume] in Blood'),
    'abs eosinophils':              ('711-2', 'Eosinophils [#/volume] in Blood'),
    'abs imm granulocytes':         ('53115-2', 'Immature granulocytes [#/volume] in Blood'),
    'basophils':                    ('706-2', 'Basophils/100 leukocytes in Blood'),
    'basos, abs':                   ('704-7', 'Basophils [#/volume] in Blood'),
    'basos, %':                     ('706-2', 'Basophils/100 leukocytes in Blood'),
    'imm. granulocyte, abs':        ('53115-2', 'Immature granulocytes [#/volume] in Blood'),
    'neutrophil %':                 ('770-8', 'Neutrophils/100 leukocytes in Blood'),
    'neutrophils':                  ('770-8', 'Neutrophils/100 leukocytes in Blood'),
    'neuts, abs':                   ('751-8', 'Neutrophils [#/volume] in Blood'),
    'lymphocyte %':                 ('736-9', 'Lymphocytes/100 leukocytes in Blood'),
    'lymphocytes':                  ('736-9', 'Lymphocytes/100 leukocytes in Blood'),
    'lymphs, %':                    ('736-9', 'Lymphocytes/100 leukocytes in Blood'),
    'lymph, abs':                   ('731-0', 'Lymphocytes [#/volume] in Blood'),
    'monocyte %':                   ('5905-5', 'Monocytes/100 leukocytes in Blood'),
    'monocytes':                    ('5905-5', 'Monocytes/100 leukocytes in Blood'),
    'monos, abs':                   ('742-7', 'Monocytes [#/volume] in Blood'),
    'eosinophil %':                 ('713-8', 'Eosinophils/100 leukocytes in Blood'),
    'eosinophils':                  ('713-8', 'Eosinophils/100 leukocytes in Blood'),
    'eos, %':                       ('713-8', 'Eosinophils/100 leukocytes in Blood'),
    'eos, abs':                     ('711-2', 'Eosinophils [#/volume] in Blood'),
    'basophil %':                   ('706-2', 'Basophils/100 leukocytes in Blood'),
    'imm. granulocyte, %':          ('71695-1', 'Immature granulocytes/100 leukocytes in Blood'),
    'nrbc, abs':                    ('58413-6', 'Nucleated RBC [#/volume] in Blood'),
    'absolute nucleated red blood cells': ('58413-6', 'Nucleated RBC [#/volume] in Blood'),
    'nucleated rbcs':               ('58413-6', 'Nucleated RBC [#/volume] in Blood'),
    'nrbc, %':                      ('19048-8', 'Nucleated RBC/100 leukocytes in Blood'),

    # ── Chemistry / CMP ──
    'sodium':                       ('2951-2', 'Sodium [Moles/volume] in Serum or Plasma'),
    'sodium, ser/plas':             ('2951-2', 'Sodium [Moles/volume] in Serum or Plasma'),
    'potassium':                    ('2823-3', 'Potassium [Moles/volume] in Serum or Plasma'),
    'potassium, ser/plas':          ('2823-3', 'Potassium [Moles/volume] in Serum or Plasma'),
    'chloride':                     ('2075-0', 'Chloride [Moles/volume] in Serum or Plasma'),
    'chloride, ser/plas':           ('2075-0', 'Chloride [Moles/volume] in Serum or Plasma'),
    'co2':                          ('2028-9', 'CO2 [Moles/volume] in Serum or Plasma'),
    'co2 (bicarbonate)':            ('2028-9', 'CO2 [Moles/volume] in Serum or Plasma'),
    'bicarbonate':                  ('2028-9', 'CO2 [Moles/volume] in Serum or Plasma'),
    'bun':                          ('3094-0', 'BUN [Mass/volume] in Serum or Plasma'),
    'bun, ser/plas':                ('3094-0', 'BUN [Mass/volume] in Serum or Plasma'),
    'blood urea nitrogen':          ('3094-0', 'BUN [Mass/volume] in Serum or Plasma'),
    'urea nitrogen':                ('3094-0', 'BUN [Mass/volume] in Serum or Plasma'),
    'bun/creat ratio':              ('3097-3', 'BUN/Creatinine [Mass ratio] in Serum or Plasma'),
    'creatinine':                   ('2160-0', 'Creatinine [Mass/volume] in Serum or Plasma'),
    'creatinine, ser/plas':         ('2160-0', 'Creatinine [Mass/volume] in Serum or Plasma'),
    'glucose':                      ('2345-7', 'Glucose [Mass/volume] in Serum or Plasma'),
    'glucose, ser/plas':            ('2345-7', 'Glucose [Mass/volume] in Serum or Plasma'),
    'glucose, plasma':              ('2345-7', 'Glucose [Mass/volume] in Serum or Plasma'),
    'fasting glucose':              ('1558-6', 'Fasting glucose [Mass/volume] in Serum or Plasma'),
    'calcium':                      ('17861-6', 'Calcium [Mass/volume] in Serum or Plasma'),
    'calcium, ser/plas':            ('17861-6', 'Calcium [Mass/volume] in Serum or Plasma'),
    'normalized calcium':           ('29265-6', 'Calcium.ionized adjusted to pH 7.4 [Moles/volume] in Serum or Plasma'),
    'corrected calcium':            ('29265-6', 'Calcium.ionized adjusted to pH 7.4 [Moles/volume] in Serum or Plasma'),
    'total protein':                ('2885-2', 'Protein [Mass/volume] in Serum or Plasma'),
    'protein, total':               ('2885-2', 'Protein [Mass/volume] in Serum or Plasma'),
    'protein, total, ser/plas':     ('2885-2', 'Protein [Mass/volume] in Serum or Plasma'),
    'total protein, ser/plas':      ('2885-2', 'Protein [Mass/volume] in Serum or Plasma'),
    'albumin':                      ('1751-7', 'Albumin [Mass/volume] in Serum or Plasma'),
    'albumin, ser/plas':            ('1751-7', 'Albumin [Mass/volume] in Serum or Plasma'),
    'globulin':                     ('10834-0', 'Globulin [Mass/volume] in Serum or Plasma'),
    'albumin/globulin ratio':       ('1759-0', 'Albumin/Globulin [Mass ratio] in Serum or Plasma'),
    'anion gap':                    ('33037-3', 'Anion gap in Serum or Plasma'),
    'magnesium':                    ('19123-9', 'Magnesium [Mass/volume] in Serum or Plasma'),
    'magnesium, ser/plas':          ('19123-9', 'Magnesium [Mass/volume] in Serum or Plasma'),
    'phosphorus':                   ('2777-1', 'Phosphate [Mass/volume] in Serum or Plasma'),
    'uric acid':                    ('3084-1', 'Urate [Mass/volume] in Serum or Plasma'),

    # ── Liver ──
    'alt':                          ('1742-6', 'ALT [Enzymatic activity/volume] in Serum or Plasma'),
    'alt (sgpt)':                   ('1742-6', 'ALT [Enzymatic activity/volume] in Serum or Plasma'),
    'alt (sgpt), ser/plas':         ('1742-6', 'ALT [Enzymatic activity/volume] in Serum or Plasma'),
    'alanine aminotransferase':     ('1742-6', 'ALT [Enzymatic activity/volume] in Serum or Plasma'),
    'ast':                          ('1920-8', 'AST [Enzymatic activity/volume] in Serum or Plasma'),
    'ast (sgot)':                   ('1920-8', 'AST [Enzymatic activity/volume] in Serum or Plasma'),
    'ast (sgot), ser/plas':         ('1920-8', 'AST [Enzymatic activity/volume] in Serum or Plasma'),
    'aspartate aminotransferase':   ('1920-8', 'AST [Enzymatic activity/volume] in Serum or Plasma'),
    'alkaline phosphatase':         ('6768-6', 'ALP [Enzymatic activity/volume] in Serum or Plasma'),
    'alk p\'tase, total, ser/plas': ('6768-6', 'ALP [Enzymatic activity/volume] in Serum or Plasma'),
    'alk p\'tase, total':           ('6768-6', 'ALP [Enzymatic activity/volume] in Serum or Plasma'),
    'alk phos':                     ('6768-6', 'ALP [Enzymatic activity/volume] in Serum or Plasma'),
    'total bilirubin':              ('1975-2', 'Bilirubin.total [Mass/volume] in Serum or Plasma'),
    'total bilirubin, ser/plas':    ('1975-2', 'Bilirubin.total [Mass/volume] in Serum or Plasma'),
    'bilirubin, total':             ('1975-2', 'Bilirubin.total [Mass/volume] in Serum or Plasma'),
    'direct bilirubin':             ('1968-7', 'Bilirubin.direct [Mass/volume] in Serum or Plasma'),
    'ggt':                          ('2324-2', 'GGT [Enzymatic activity/volume] in Serum or Plasma'),
    'lipase':                       ('3040-3', 'Lipase [Enzymatic activity/volume] in Serum or Plasma'),

    # ── Lipids ──
    'total cholesterol':            ('2093-3', 'Cholesterol [Mass/volume] in Serum or Plasma'),
    'cholesterol, total':           ('2093-3', 'Cholesterol [Mass/volume] in Serum or Plasma'),
    'hdl':                          ('2085-9', 'HDL Cholesterol [Mass/volume] in Serum or Plasma'),
    'hdl cholesterol':              ('2085-9', 'HDL Cholesterol [Mass/volume] in Serum or Plasma'),
    'ldl':                          ('2089-1', 'LDL Cholesterol Calc [Mass/volume] in Serum or Plasma'),
    'ldl cholesterol':              ('2089-1', 'LDL Cholesterol Calc [Mass/volume] in Serum or Plasma'),
    'ldl cholesterol, calculated':  ('2089-1', 'LDL Cholesterol Calc [Mass/volume] in Serum or Plasma'),
    'triglycerides':                ('2571-8', 'Triglyceride [Mass/volume] in Serum or Plasma'),
    'triglyceride':                 ('2571-8', 'Triglyceride [Mass/volume] in Serum or Plasma'),

    # ── Metabolic ──
    'hba1c':                        ('4548-4', 'HbA1c/Hemoglobin.total in Blood'),
    'hemoglobin a1c':               ('4548-4', 'HbA1c/Hemoglobin.total in Blood'),
    'a1c':                          ('4548-4', 'HbA1c/Hemoglobin.total in Blood'),
    'egfr':                         ('33914-3', 'eGFR CKD-EPI'),
    'egfr refit without race (2021)': ('33914-3', 'eGFR CKD-EPI'),
    'egfr-other legacy':            ('33914-3', 'eGFR CKD-EPI'),
    'egfr-african american legacy': ('33914-3', 'eGFR CKD-EPI'),
    'egfr (non-race based)':        ('33914-3', 'eGFR CKD-EPI'),
    'egfr, non-african descent':    ('33914-3', 'eGFR CKD-EPI'),
    'egfr, african descent':        ('33914-3', 'eGFR CKD-EPI'),

    # ── Iron Studies ──
    'ferritin':                     ('2276-4', 'Ferritin [Mass/volume] in Serum or Plasma'),
    'ferritin, s':                  ('2276-4', 'Ferritin [Mass/volume] in Serum or Plasma'),
    'ferritin, serum':              ('2276-4', 'Ferritin [Mass/volume] in Serum or Plasma'),
    'iron':                         ('2498-4', 'Iron [Mass/volume] in Serum or Plasma'),
    'iron, ser/plas':               ('2498-4', 'Iron [Mass/volume] in Serum or Plasma'),
    'iron, total':                  ('2498-4', 'Iron [Mass/volume] in Serum or Plasma'),
    'tibc':                         ('2500-7', 'TIBC [Mass/volume] in Serum or Plasma'),
    'iron binding capacity':        ('2500-7', 'TIBC [Mass/volume] in Serum or Plasma'),
    'transferrin sat':              ('2502-3', 'Transferrin saturation in Serum or Plasma'),
    'transferrin saturation':       ('2502-3', 'Transferrin saturation in Serum or Plasma'),

    # ── Inflammation ──
    'c-reactive protein':           ('1988-5', 'CRP [Mass/volume] in Serum or Plasma'),
    'crp':                          ('1988-5', 'CRP [Mass/volume] in Serum or Plasma'),
    'esr':                          ('4537-7', 'ESR [Velocity] in Blood'),
    'sed rate':                     ('4537-7', 'ESR [Velocity] in Blood'),

    # ── Thyroid ──
    'tsh':                          ('3016-3', 'TSH [Units/volume] in Serum or Plasma'),
    'thyroid stimulating hormone':  ('3016-3', 'TSH [Units/volume] in Serum or Plasma'),
    'free t4':                      ('3024-7', 'Free T4 [Mass/volume] in Serum or Plasma'),
    'free thyroxine':               ('3024-7', 'Free T4 [Mass/volume] in Serum or Plasma'),
    't4, free':                     ('3024-7', 'Free T4 [Mass/volume] in Serum or Plasma'),
    'free t3':                      ('3051-0', 'Free T3 [Mass/volume] in Serum or Plasma'),

    # ── Immunology ──
    'iga':                          ('2458-8', 'IgA [Mass/volume] in Serum'),
    'iga, serum':                   ('2458-8', 'IgA [Mass/volume] in Serum'),
    'igg':                          ('2462-0', 'IgG [Mass/volume] in Serum'),
    'igg, serum':                   ('2462-0', 'IgG [Mass/volume] in Serum'),
    'igm':                          ('2472-9', 'IgM [Mass/volume] in Serum'),
    'igm, serum':                   ('2472-9', 'IgM [Mass/volume] in Serum'),
    'free kappa light chain':       ('11050-2', 'Free Kappa light chains [Mass/volume] in Serum'),
    'free kappa light chains':      ('11050-2', 'Free Kappa light chains [Mass/volume] in Serum'),
    'free kappa lt chains,s':       ('11050-2', 'Free Kappa light chains [Mass/volume] in Serum'),
    'free lambda light chain':      ('11051-0', 'Free Lambda light chains [Mass/volume] in Serum'),
    'free lambda light chains':     ('11051-0', 'Free Lambda light chains [Mass/volume] in Serum'),
    'free lambda lt chains,s':      ('11051-0', 'Free Lambda light chains [Mass/volume] in Serum'),
    'free kappa/lambda ratio':      ('11052-8', 'Kappa/Lambda free light chain ratio in Serum'),
    'kappa/lambda ratio':           ('11052-8', 'Kappa/Lambda free light chain ratio in Serum'),
    'kappa/lambda ratio,s':         ('11052-8', 'Kappa/Lambda free light chain ratio in Serum'),
    'k l ratio':                    ('11052-8', 'Kappa/Lambda free light chain ratio in Serum'),

    # ── SPEP / Myeloma ──
    'm-spike':                      ('51435-6', 'M-protein [Mass/volume] in Serum or Plasma by Electrophoresis'),
    'monoclonal protein':           ('51435-6', 'M-protein [Mass/volume] in Serum or Plasma by Electrophoresis'),
    'gamma globulin':               ('2871-2', 'Gamma globulin [Mass/volume] in Serum or Plasma by Electrophoresis'),
    'alpha-1-globulin':             ('2865-4', 'Alpha-1-globulin [Mass/volume] in Serum or Plasma by Electrophoresis'),
    'beta 2 globulin':              ('2870-4', 'Beta-2-globulin [Mass/volume] in Serum or Plasma by Electrophoresis'),
    'beta 1 globulin':              ('2868-8', 'Beta-1-globulin [Mass/volume] in Serum or Plasma by Electrophoresis'),
    'alpha-2-globulin':             ('2867-0', 'Alpha-2-globulin [Mass/volume] in Serum or Plasma by Electrophoresis'),
    'abnormal protein band 1':     ('51435-6', 'M-protein [Mass/volume] in Serum or Plasma by Electrophoresis'),

    # ── Cancer Markers ──
    'beta-2 microglobulin':         ('1952-1', 'Beta-2-microglobulin [Mass/volume] in Serum or Plasma'),
    'beta 2 microglobulin':         ('1952-1', 'Beta-2-microglobulin [Mass/volume] in Serum or Plasma'),
    'ldh':                          ('2532-0', 'LDH [Enzymatic activity/volume] in Serum or Plasma'),
    'lactate dehydrogenase':        ('2532-0', 'LDH [Enzymatic activity/volume] in Serum or Plasma'),
    'psa':                          ('2857-1', 'PSA [Mass/volume] in Serum or Plasma'),
    'prostate specific antigen':    ('2857-1', 'PSA [Mass/volume] in Serum or Plasma'),

    # ── Coagulation ──
    'pt':                           ('5902-2', 'Prothrombin time (PT) in Blood'),
    'prothrombin time':             ('5902-2', 'Prothrombin time (PT) in Blood'),
    'inr':                          ('6301-6', 'INR in Blood'),
    'ptt':                          ('3173-2', 'aPTT in Blood'),
    'aptt':                         ('3173-2', 'aPTT in Blood'),
    'fibrinogen':                   ('3255-7', 'Fibrinogen [Mass/volume] in Platelet poor plasma'),

    # ── Urinalysis ──
    'albumin, urine':               ('1754-1', 'Albumin [Mass/volume] in Urine'),
    'creatinine, urine':            ('2161-8', 'Creatinine [Mass/volume] in Urine'),
    'alb/creat ratio':              ('9318-7', 'Albumin/Creatinine [Mass ratio] in Urine'),
    'protein/creat ratio':          ('2890-2', 'Protein/Creatinine [Mass ratio] in Urine'),
    'alb/creat. ratio':            ('9318-7', 'Albumin/Creatinine [Mass ratio] in Urine'),
    'protein, urine':               ('2888-6', 'Protein [Mass/volume] in Urine'),
    'protein, total, urine':        ('2888-6', 'Protein [Mass/volume] in Urine'),
    'specific gravity':             ('5811-5', 'Specific gravity of Urine'),
    'specific gravity, urine':      ('5811-5', 'Specific gravity of Urine'),
    'ph':                           ('2756-5', 'pH of Urine'),
    'ph, urine':                    ('2756-5', 'pH of Urine'),

    'leukocyte esterase':           ('5799-2', 'Leukocyte esterase [Presence] in Urine'),
    'leukocyte esterase, urine':    ('5799-2', 'Leukocyte esterase [Presence] in Urine'),
    'bilirubin':                    ('5770-3', 'Bilirubin [Presence] in Urine by Test strip'),
    'ketones':                      ('2514-8', 'Ketones [Presence] in Urine'),
    'ketone, urine':                ('2514-8', 'Ketones [Presence] in Urine'),
    'bacteria':                     ('630-4', 'Bacteria [Presence] in Urine sediment'),
    'epithelial cells':             ('5787-7', 'Epithelial cells [#/area] in Urine sediment'),
    'hyaline cast':                 ('5796-0', 'Hyaline casts [#/area] in Urine sediment'),
    'occult blood':                 ('2106-3', 'Occult blood [Presence] in Urine by Test strip'),
    'urine color':                  ('5778-6', 'Color of Urine'),
    'color, urine':                 ('5778-6', 'Color of Urine'),
    'urine appearance':             ('5767-9', 'Appearance of Urine'),
    'bacteria, urine':              ('630-4', 'Bacteria [Presence] in Urine sediment'),
    'wbc, urine':                   ('5821-4', 'Leukocytes [#/area] in Urine sediment'),
    'wbcs':                         ('5821-4', 'Leukocytes [#/area] in Urine sediment'),
    'rbc, urine':                   ('13945-1', 'Erythrocytes [#/area] in Urine sediment'),
    'rbcs':                         ('13945-1', 'Erythrocytes [#/area] in Urine sediment'),
    'blood, urine':                 ('5794-3', 'Hemoglobin [Presence] in Urine by Test strip'),
    'clarity, urine':               ('32167-9', 'Clarity of Urine'),
    'nitrite':                      ('5802-4', 'Nitrite [Presence] in Urine by Test strip'),
    'nitrite, urine':               ('5802-4', 'Nitrite [Presence] in Urine by Test strip'),
    'nitrites':                     ('5802-4', 'Nitrite [Presence] in Urine by Test strip'),
    'glucose, urine':               ('5792-7', 'Glucose [Mass/volume] in Urine by Test strip'),
    'protein':                      ('5804-0', 'Protein [Mass/volume] in Urine by Test strip'),

    # ── Vitamins ──
    'vitamin d':                    ('1989-3', '25-Hydroxyvitamin D [Mass/volume] in Serum or Plasma'),
    'vitamin d, 25-oh':             ('1989-3', '25-Hydroxyvitamin D [Mass/volume] in Serum or Plasma'),
    '25-hydroxy vitamin d':         ('1989-3', '25-Hydroxyvitamin D [Mass/volume] in Serum or Plasma'),
    'vitamin b12':                  ('2132-9', 'Vitamin B12 [Mass/volume] in Serum or Plasma'),
    'folate':                       ('2284-8', 'Folate [Mass/volume] in Serum or Plasma'),

    # ── ECG ──
    'p-r interval':                 ('8625-6', 'P-R interval'),

    # ── Vitals ──
    'heart rate':                   ('8867-4', 'Heart rate'),
    'heart rate, resting':          ('40443-4', 'Heart rate resting'),
    'oxygen saturation':            ('59408-5', 'SpO2'),
    'respiratory rate':             ('9279-1', 'Respiratory rate'),
    'body temperature':             ('8310-5', 'Body temperature'),
    'weight':                       ('29463-7', 'Body weight'),
    'body mass index':              ('39156-5', 'BMI'),
    'number of steps':              ('55423-8', 'Number of steps'),
    'sleep duration':               ('93832-4', 'Sleep duration'),

    # ── Infectious Disease ──
    'hhv-6 dna':                    ('49349-4', 'HHV-6 DNA'),
    'ebv dna':                      ('32585-2', 'EBV DNA'),
    'cmv dna':                      ('49539-0', 'CMV DNA'),
    'adenovirus dna':               ('49340-3', 'Adenovirus DNA'),
    'bk virus dna':                 ('72613-3', 'BK virus DNA'),

    # ── Blood Bank ──
    'antibody screen':              ('890-4', 'Antibody screen in Serum or Plasma'),

    # ── Hematology / CBC — additional variants ──
    'rdw-cv':                       ('788-0', 'RDW [Ratio]'),
    'red cell dist width':          ('788-0', 'RDW [Ratio]'),
    'immature granulocytes':        ('53115-2', 'Immature granulocytes [#/volume] in Blood'),
    'immature granulocyte':         ('53115-2', 'Immature granulocytes [#/volume] in Blood'),
    'immature grans':               ('53115-2', 'Immature granulocytes [#/volume] in Blood'),
    'imm granulocyte %':            ('71695-1', 'Immature granulocytes/100 leukocytes in Blood'),
    'immature granulocytes %':      ('71695-1', 'Immature granulocytes/100 leukocytes in Blood'),
    'abs. neutrophil':              ('751-8', 'Neutrophils [#/volume] in Blood'),
    'abs. neutrophils':             ('751-8', 'Neutrophils [#/volume] in Blood'),
    'abs. lymphocyte':              ('731-0', 'Lymphocytes [#/volume] in Blood'),
    'abs. lymphocytes':             ('731-0', 'Lymphocytes [#/volume] in Blood'),
    'abs. monocyte':                ('742-7', 'Monocytes [#/volume] in Blood'),
    'abs. monocytes':               ('742-7', 'Monocytes [#/volume] in Blood'),
    'abs. eosinophil':              ('711-2', 'Eosinophils [#/volume] in Blood'),
    'abs. eosinophils':             ('711-2', 'Eosinophils [#/volume] in Blood'),
    'abs. basophil':                ('704-7', 'Basophils [#/volume] in Blood'),
    'abs. basophils':               ('704-7', 'Basophils [#/volume] in Blood'),
    'abs. imm granulocytes':        ('53115-2', 'Immature granulocytes [#/volume] in Blood'),
    'abs. immature granulocytes':   ('53115-2', 'Immature granulocytes [#/volume] in Blood'),
    'neut %':                       ('770-8', 'Neutrophils/100 leukocytes in Blood'),
    'neuts, %':                     ('770-8', 'Neutrophils/100 leukocytes in Blood'),
    'mono %':                       ('5905-5', 'Monocytes/100 leukocytes in Blood'),
    'monos, %':                     ('5905-5', 'Monocytes/100 leukocytes in Blood'),
    'baso %':                       ('706-2', 'Basophils/100 leukocytes in Blood'),
    'lymph %':                      ('736-9', 'Lymphocytes/100 leukocytes in Blood'),
    'eos %':                        ('713-8', 'Eosinophils/100 leukocytes in Blood'),
    '% neutrophils':                ('770-8', 'Neutrophils/100 leukocytes in Blood'),
    '% lymphocytes':                ('736-9', 'Lymphocytes/100 leukocytes in Blood'),
    '% monocytes':                  ('5905-5', 'Monocytes/100 leukocytes in Blood'),
    '% eosinophils':                ('713-8', 'Eosinophils/100 leukocytes in Blood'),
    '% basophils':                  ('706-2', 'Basophils/100 leukocytes in Blood'),
    'neutrophil abs':               ('751-8', 'Neutrophils [#/volume] in Blood'),
    'lymphocyte abs':               ('731-0', 'Lymphocytes [#/volume] in Blood'),
    'monocyte abs':                 ('742-7', 'Monocytes [#/volume] in Blood'),
    'eosinophil abs':               ('711-2', 'Eosinophils [#/volume] in Blood'),
    'basophil abs':                 ('704-7', 'Basophils [#/volume] in Blood'),
    'neut abs':                     ('751-8', 'Neutrophils [#/volume] in Blood'),
    'lymph abs':                    ('731-0', 'Lymphocytes [#/volume] in Blood'),
    'mono abs':                     ('742-7', 'Monocytes [#/volume] in Blood'),
    'eos abs':                      ('711-2', 'Eosinophils [#/volume] in Blood'),
    'baso abs':                     ('704-7', 'Basophils [#/volume] in Blood'),
    'reticulocyte count':           ('17849-1', 'Reticulocytes [#/volume] in Blood'),
    'reticulocytes':                ('17849-1', 'Reticulocytes [#/volume] in Blood'),
    'reticulocyte %':               ('4679-7', 'Reticulocytes/100 erythrocytes in Blood'),
    'reticulocyte count, auto':     ('17849-1', 'Reticulocytes [#/volume] in Blood'),
    'retic count':                  ('17849-1', 'Reticulocytes [#/volume] in Blood'),
    'retic %':                      ('4679-7', 'Reticulocytes/100 erythrocytes in Blood'),
    'abs reticulocyte count':       ('60474-4', 'Reticulocytes [#/volume] in Blood by Automated count'),
    'reticulocyte count, absolute': ('60474-4', 'Reticulocytes [#/volume] in Blood by Automated count'),
    'reticulocyte hemoglobin':      ('76768-1', 'Reticulocyte hemoglobin content [Mass/volume]'),
    'platelet est':                 ('777-3', 'Platelets [#/volume] in Blood'),
    'platelet estimate':            ('777-3', 'Platelets [#/volume] in Blood'),

    # ── Chemistry / CMP — additional variants ──
    'sodium, serum':                ('2951-2', 'Sodium [Moles/volume] in Serum or Plasma'),
    'sodium, plasma':               ('2951-2', 'Sodium [Moles/volume] in Serum or Plasma'),
    'potassium, serum':             ('2823-3', 'Potassium [Moles/volume] in Serum or Plasma'),
    'potassium, plasma':            ('2823-3', 'Potassium [Moles/volume] in Serum or Plasma'),
    'chloride, serum':              ('2075-0', 'Chloride [Moles/volume] in Serum or Plasma'),
    'chloride, plasma':             ('2075-0', 'Chloride [Moles/volume] in Serum or Plasma'),
    'calcium, serum':               ('17861-6', 'Calcium [Mass/volume] in Serum or Plasma'),
    'calcium, plasma':              ('17861-6', 'Calcium [Mass/volume] in Serum or Plasma'),
    'carbon dioxide':               ('2028-9', 'CO2 [Moles/volume] in Serum or Plasma'),
    'carbon dioxide, total':        ('2028-9', 'CO2 [Moles/volume] in Serum or Plasma'),
    'carbon dioxide total':         ('2028-9', 'CO2 [Moles/volume] in Serum or Plasma'),
    'co2, total':                   ('2028-9', 'CO2 [Moles/volume] in Serum or Plasma'),
    'total co2':                    ('2028-9', 'CO2 [Moles/volume] in Serum or Plasma'),
    'tco2':                         ('2028-9', 'CO2 [Moles/volume] in Serum or Plasma'),
    'glucose, fasting':             ('1558-6', 'Fasting glucose [Mass/volume] in Serum or Plasma'),
    'glucose fasting':              ('1558-6', 'Fasting glucose [Mass/volume] in Serum or Plasma'),
    'glucose, non-fasting':         ('2345-7', 'Glucose [Mass/volume] in Serum or Plasma'),
    'glucose non-fasting':          ('2345-7', 'Glucose [Mass/volume] in Serum or Plasma'),
    'glucose, random':              ('2345-7', 'Glucose [Mass/volume] in Serum or Plasma'),
    'blood urea nitrogen (bun)':    ('3094-0', 'BUN [Mass/volume] in Serum or Plasma'),
    'creatinine, serum':            ('2160-0', 'Creatinine [Mass/volume] in Serum or Plasma'),
    'creatinine, blood':            ('2160-0', 'Creatinine [Mass/volume] in Serum or Plasma'),
    'bun/creatinine ratio':         ('3097-3', 'BUN/Creatinine [Mass ratio] in Serum or Plasma'),
    'bun/creat':                    ('3097-3', 'BUN/Creatinine [Mass ratio] in Serum or Plasma'),
    'globulin, total':              ('10834-0', 'Globulin [Mass/volume] in Serum or Plasma'),
    'globulin total':               ('10834-0', 'Globulin [Mass/volume] in Serum or Plasma'),
    'total globulin':               ('10834-0', 'Globulin [Mass/volume] in Serum or Plasma'),
    'a/g ratio':                    ('1759-0', 'Albumin/Globulin [Mass ratio] in Serum or Plasma'),
    'ag ratio':                     ('1759-0', 'Albumin/Globulin [Mass ratio] in Serum or Plasma'),
    'alb/glob ratio':               ('1759-0', 'Albumin/Globulin [Mass ratio] in Serum or Plasma'),
    'ionized calcium':              ('1994-3', 'Ionized calcium [Moles/volume] in Serum or Plasma'),
    'calcium, ionized':             ('1994-3', 'Ionized calcium [Moles/volume] in Serum or Plasma'),
    'ica':                          ('1994-3', 'Ionized calcium [Moles/volume] in Serum or Plasma'),
    'osmolality':                   ('2692-2', 'Osmolality of Serum or Plasma'),
    'osmolality, serum':            ('2692-2', 'Osmolality of Serum or Plasma'),
    'phosphate':                    ('2777-1', 'Phosphate [Mass/volume] in Serum or Plasma'),
    'phos':                         ('2777-1', 'Phosphate [Mass/volume] in Serum or Plasma'),

    # ── Lipids — additional variants ──
    'cholesterol':                  ('2093-3', 'Cholesterol [Mass/volume] in Serum or Plasma'),
    'ldl calculated':               ('2089-1', 'LDL Cholesterol Calc [Mass/volume] in Serum or Plasma'),
    'ldl, calculated':              ('2089-1', 'LDL Cholesterol Calc [Mass/volume] in Serum or Plasma'),
    'ldl (calculated)':             ('2089-1', 'LDL Cholesterol Calc [Mass/volume] in Serum or Plasma'),
    'ldl (measured)':               ('18262-6', 'LDL Cholesterol [Mass/volume] in Serum or Plasma by Direct assay'),
    'ldl measured':                 ('18262-6', 'LDL Cholesterol [Mass/volume] in Serum or Plasma by Direct assay'),
    'ldl, direct':                  ('18262-6', 'LDL Cholesterol [Mass/volume] in Serum or Plasma by Direct assay'),
    'cholesterol/hdl ratio':        ('9830-1', 'Cholesterol.total/Cholesterol.in HDL [Mass ratio] in Serum or Plasma'),
    'cholesterol to hdl ratio':     ('9830-1', 'Cholesterol.total/Cholesterol.in HDL [Mass ratio] in Serum or Plasma'),
    'chol/hdl ratio':               ('9830-1', 'Cholesterol.total/Cholesterol.in HDL [Mass ratio] in Serum or Plasma'),
    'chol/hdl':                     ('9830-1', 'Cholesterol.total/Cholesterol.in HDL [Mass ratio] in Serum or Plasma'),
    'total chol/hdl ratio':         ('9830-1', 'Cholesterol.total/Cholesterol.in HDL [Mass ratio] in Serum or Plasma'),
    'non-hdl cholesterol':          ('43396-1', 'Cholesterol non HDL [Mass/volume] in Serum or Plasma'),
    'non hdl cholesterol':          ('43396-1', 'Cholesterol non HDL [Mass/volume] in Serum or Plasma'),
    'vldl':                         ('13458-5', 'VLDL Cholesterol [Mass/volume] in Serum or Plasma by calculation'),
    'vldl cholesterol':             ('13458-5', 'VLDL Cholesterol [Mass/volume] in Serum or Plasma by calculation'),
    'lipoprotein(a)':               ('10835-7', 'Lipoprotein(a) [Mass/volume] in Serum or Plasma'),
    'lipoprotein (a)':              ('10835-7', 'Lipoprotein(a) [Mass/volume] in Serum or Plasma'),
    'lp(a)':                        ('10835-7', 'Lipoprotein(a) [Mass/volume] in Serum or Plasma'),
    'apolipoprotein b':             ('1884-6', 'Apolipoprotein B [Mass/volume] in Serum or Plasma'),
    'apo b':                        ('1884-6', 'Apolipoprotein B [Mass/volume] in Serum or Plasma'),
    'apolipoprotein a1':            ('1869-7', 'Apolipoprotein A-I [Mass/volume] in Serum or Plasma'),
    'apo a1':                       ('1869-7', 'Apolipoprotein A-I [Mass/volume] in Serum or Plasma'),

    # ── Metabolic — additional variants ──
    'hemoglobin a1c (hba1c)':       ('4548-4', 'HbA1c/Hemoglobin.total in Blood'),
    'hgb a1c':                      ('4548-4', 'HbA1c/Hemoglobin.total in Blood'),
    'glycated hemoglobin':          ('4548-4', 'HbA1c/Hemoglobin.total in Blood'),
    'estimated average glucose':    ('86911-5', 'Estimated average glucose [Mass/volume]'),
    'eag':                          ('86911-5', 'Estimated average glucose [Mass/volume]'),
    'avg glucose':                  ('86911-5', 'Estimated average glucose [Mass/volume]'),
    'egfr non-african american':    ('33914-3', 'eGFR CKD-EPI'),
    'egfr african american':        ('33914-3', 'eGFR CKD-EPI'),
    'egfr if non african amer':     ('33914-3', 'eGFR CKD-EPI'),
    'egfr if african amer':         ('33914-3', 'eGFR CKD-EPI'),
    'egfr (ckd-epi)':               ('33914-3', 'eGFR CKD-EPI'),
    'gfr estimated':                ('33914-3', 'eGFR CKD-EPI'),
    'gfr':                          ('33914-3', 'eGFR CKD-EPI'),
    'estimated gfr':                ('33914-3', 'eGFR CKD-EPI'),
    'cystatin c':                   ('33863-2', 'Cystatin C [Mass/volume] in Serum or Plasma'),

    # ── Liver — additional variants ──
    'sgpt':                         ('1742-6', 'ALT [Enzymatic activity/volume] in Serum or Plasma'),
    'sgot':                         ('1920-8', 'AST [Enzymatic activity/volume] in Serum or Plasma'),
    'alp':                          ('6768-6', 'ALP [Enzymatic activity/volume] in Serum or Plasma'),
    'alkaline phos':                ('6768-6', 'ALP [Enzymatic activity/volume] in Serum or Plasma'),
    'alk phosphatase':              ('6768-6', 'ALP [Enzymatic activity/volume] in Serum or Plasma'),
    'total bilirubin, s':           ('1975-2', 'Bilirubin.total [Mass/volume] in Serum or Plasma'),
    'indirect bilirubin':           ('1971-1', 'Bilirubin.indirect [Mass/volume] in Serum or Plasma'),
    'bilirubin, indirect':          ('1971-1', 'Bilirubin.indirect [Mass/volume] in Serum or Plasma'),
    'gamma-glutamyltransferase':    ('2324-2', 'GGT [Enzymatic activity/volume] in Serum or Plasma'),
    'gamma-glutamyl transferase':   ('2324-2', 'GGT [Enzymatic activity/volume] in Serum or Plasma'),
    'amylase':                      ('1798-8', 'Amylase [Enzymatic activity/volume] in Serum or Plasma'),
    'amylase, serum':               ('1798-8', 'Amylase [Enzymatic activity/volume] in Serum or Plasma'),
    'ammonia':                      ('16362-6', 'Ammonia [Moles/volume] in Plasma'),
    'ammonia, plasma':              ('16362-6', 'Ammonia [Moles/volume] in Plasma'),

    # ── Iron Studies — additional variants ──
    'tibc, ser/plas':               ('2500-7', 'TIBC [Mass/volume] in Serum or Plasma'),
    'iron binding capacity, total': ('2500-7', 'TIBC [Mass/volume] in Serum or Plasma'),
    'transferrin':                  ('3034-6', 'Transferrin [Mass/volume] in Serum or Plasma'),
    'transferrin, serum':           ('3034-6', 'Transferrin [Mass/volume] in Serum or Plasma'),
    'iron sat':                     ('2502-3', 'Transferrin saturation in Serum or Plasma'),
    'iron saturation':              ('2502-3', 'Transferrin saturation in Serum or Plasma'),
    '%iron sat':                    ('2502-3', 'Transferrin saturation in Serum or Plasma'),
    'iron % saturation':            ('2502-3', 'Transferrin saturation in Serum or Plasma'),

    # ── Inflammation — additional variants ──
    'c-reactive protein (crp)':     ('1988-5', 'CRP [Mass/volume] in Serum or Plasma'),
    'crp, serum':                   ('1988-5', 'CRP [Mass/volume] in Serum or Plasma'),
    'c reactive protein':           ('1988-5', 'CRP [Mass/volume] in Serum or Plasma'),
    'hs-crp':                       ('30522-7', 'hs-CRP [Mass/volume] in Serum or Plasma'),
    'high sensitivity crp':         ('30522-7', 'hs-CRP [Mass/volume] in Serum or Plasma'),
    'crp, high sensitivity':        ('30522-7', 'hs-CRP [Mass/volume] in Serum or Plasma'),
    'high sensitivity c-reactive protein': ('30522-7', 'hs-CRP [Mass/volume] in Serum or Plasma'),
    'sed rate, automated':          ('4537-7', 'ESR [Velocity] in Blood'),
    'sed rate (esr)':               ('4537-7', 'ESR [Velocity] in Blood'),
    'sedimentation rate':           ('4537-7', 'ESR [Velocity] in Blood'),
    'erythrocyte sedimentation rate': ('4537-7', 'ESR [Velocity] in Blood'),

    # ── SPEP / Myeloma — additional variants ──
    'albumin, spep':                ('2862-1', 'Albumin [Mass/volume] in Serum or Plasma by Electrophoresis'),
    'albumin spep':                 ('2862-1', 'Albumin [Mass/volume] in Serum or Plasma by Electrophoresis'),
    'spep albumin':                 ('2862-1', 'Albumin [Mass/volume] in Serum or Plasma by Electrophoresis'),
    'alpha 1 globulin':             ('2865-4', 'Alpha-1-globulin [Mass/volume] in Serum or Plasma by Electrophoresis'),
    'alpha-1 globulin':             ('2865-4', 'Alpha-1-globulin [Mass/volume] in Serum or Plasma by Electrophoresis'),
    'alpha 2 globulin':             ('2867-0', 'Alpha-2-globulin [Mass/volume] in Serum or Plasma by Electrophoresis'),
    'alpha-2 globulin':             ('2867-0', 'Alpha-2-globulin [Mass/volume] in Serum or Plasma by Electrophoresis'),
    'beta globulin':                ('2868-8', 'Beta-1-globulin [Mass/volume] in Serum or Plasma by Electrophoresis'),
    'beta-1 globulin':              ('2868-8', 'Beta-1-globulin [Mass/volume] in Serum or Plasma by Electrophoresis'),
    'beta-2 globulin':              ('2870-4', 'Beta-2-globulin [Mass/volume] in Serum or Plasma by Electrophoresis'),
    'gamma globulin, spep':         ('2871-2', 'Gamma globulin [Mass/volume] in Serum or Plasma by Electrophoresis'),
    'm-spike, spep':                ('51435-6', 'M-protein [Mass/volume] in Serum or Plasma by Electrophoresis'),
    'abnormal protein band':        ('51435-6', 'M-protein [Mass/volume] in Serum or Plasma by Electrophoresis'),
    'abnormal protein band 2':      ('51435-6', 'M-protein [Mass/volume] in Serum or Plasma by Electrophoresis'),
    'monoclonal protein, spep':     ('51435-6', 'M-protein [Mass/volume] in Serum or Plasma by Electrophoresis'),
    'total protein, spep':          ('2885-2', 'Protein [Mass/volume] in Serum or Plasma'),
    'protein total, spep':          ('2885-2', 'Protein [Mass/volume] in Serum or Plasma'),

    # ── Immunology — additional variants ──
    'kappa light chain, free':      ('11050-2', 'Free Kappa light chains [Mass/volume] in Serum'),
    'lambda light chain, free':     ('11051-0', 'Free Lambda light chains [Mass/volume] in Serum'),
    'kappa light chain free s':     ('11050-2', 'Free Kappa light chains [Mass/volume] in Serum'),
    'lambda light chain free s':    ('11051-0', 'Free Lambda light chains [Mass/volume] in Serum'),
    'kappa light chain free, s':    ('11050-2', 'Free Kappa light chains [Mass/volume] in Serum'),
    'lambda light chain free, s':   ('11051-0', 'Free Lambda light chains [Mass/volume] in Serum'),
    'kappa/lambda free ratio s':    ('11052-8', 'Kappa/Lambda free light chain ratio in Serum'),
    'kappa/lambda free ratio, s':   ('11052-8', 'Kappa/Lambda free light chain ratio in Serum'),
    'free kappa/lambda ratio, s':   ('11052-8', 'Kappa/Lambda free light chain ratio in Serum'),
    'free kappa':                   ('11050-2', 'Free Kappa light chains [Mass/volume] in Serum'),
    'free lambda':                  ('11051-0', 'Free Lambda light chains [Mass/volume] in Serum'),
    'immunoglobulin a':             ('2458-8', 'IgA [Mass/volume] in Serum'),
    'immunoglobulin g':             ('2462-0', 'IgG [Mass/volume] in Serum'),
    'immunoglobulin m':             ('2472-9', 'IgM [Mass/volume] in Serum'),
    'immunoglobulin g (igg)':       ('2462-0', 'IgG [Mass/volume] in Serum'),
    'immunoglobulin a (iga)':       ('2458-8', 'IgA [Mass/volume] in Serum'),
    'immunoglobulin m (igm)':       ('2472-9', 'IgM [Mass/volume] in Serum'),
    'ige':                          ('19113-0', 'IgE [Units/volume] in Serum or Plasma'),
    'ige, total':                   ('19113-0', 'IgE [Units/volume] in Serum or Plasma'),
    'complement c3':                ('4485-9', 'Complement C3 [Mass/volume] in Serum or Plasma'),
    'c3':                           ('4485-9', 'Complement C3 [Mass/volume] in Serum or Plasma'),
    'c3 complement':                ('4485-9', 'Complement C3 [Mass/volume] in Serum or Plasma'),
    'complement c4':                ('4498-2', 'Complement C4 [Mass/volume] in Serum or Plasma'),
    'c4':                           ('4498-2', 'Complement C4 [Mass/volume] in Serum or Plasma'),
    'c4 complement':                ('4498-2', 'Complement C4 [Mass/volume] in Serum or Plasma'),
    'rheumatoid factor':            ('11572-5', 'Rheumatoid factor [Units/volume] in Serum or Plasma'),
    'rf':                           ('11572-5', 'Rheumatoid factor [Units/volume] in Serum or Plasma'),
    'rheumatoid factor, quant':     ('11572-5', 'Rheumatoid factor [Units/volume] in Serum or Plasma'),

    # ── ANA / Autoantibodies ──
    'ana':                          ('8061-4', 'ANA in Serum'),
    'ana titer':                    ('5048-4', 'ANA Titer in Serum by Immunofluorescence'),
    'ana pattern':                  ('13543-4', 'ANA pattern [Identifier] in Serum'),
    'ana, ifa':                     ('5048-4', 'ANA Titer in Serum by Immunofluorescence'),
    'anti-nuclear antibody':        ('8061-4', 'ANA in Serum'),
    'antinuclear antibody':         ('8061-4', 'ANA in Serum'),
    'antinuclear antibodies (ana)': ('8061-4', 'ANA in Serum'),
    'anti-dsdna':                   ('35659-2', 'Anti-dsDNA Ab [Units/volume] in Serum by Immunoassay'),
    'anti-dsdna antibody':          ('35659-2', 'Anti-dsDNA Ab [Units/volume] in Serum by Immunoassay'),
    'dsdna antibody':               ('35659-2', 'Anti-dsDNA Ab [Units/volume] in Serum by Immunoassay'),
    'anti proteinase 3 ab':         ('33582-0', 'Anti-proteinase 3 Ab [Units/volume] in Serum'),
    'anti proteinase 3':            ('33582-0', 'Anti-proteinase 3 Ab [Units/volume] in Serum'),
    'pr3 antibody':                 ('33582-0', 'Anti-proteinase 3 Ab [Units/volume] in Serum'),
    'anti myeloperoxidase ab':      ('33449-2', 'Anti-myeloperoxidase Ab [Units/volume] in Serum'),
    'anti myeloperoxidase':         ('33449-2', 'Anti-myeloperoxidase Ab [Units/volume] in Serum'),
    'mpo antibody':                 ('33449-2', 'Anti-myeloperoxidase Ab [Units/volume] in Serum'),
    'anti-ssa (ro)':                ('49881-6', 'Anti-SSA (Ro) Ab [Units/volume] in Serum'),
    'anti-ssb (la)':                ('49880-8', 'Anti-SSB (La) Ab [Units/volume] in Serum'),
    'anti-rnp':                     ('14318-0', 'Anti-RNP Ab [Units/volume] in Serum'),
    'anti-smith':                   ('14319-8', 'Anti-Smith Ab [Units/volume] in Serum'),
    'anti-smooth muscle ab':        ('5267-5', 'Anti-smooth muscle Ab [Titer] in Serum'),
    'anti-mitochondrial ab':        ('7952-5', 'Anti-mitochondrial Ab [Units/volume] in Serum'),
    'anti-cardiolipin igg':         ('3182-4', 'Anti-cardiolipin IgG Ab [GPL Units/volume] in Serum'),
    'anti-cardiolipin igm':         ('3184-0', 'Anti-cardiolipin IgM Ab [MPL Units/volume] in Serum'),
    'anti-tg':                      ('5380-6', 'Anti-thyroglobulin Ab [Units/volume] in Serum'),
    'anti-thyroglobulin':           ('5380-6', 'Anti-thyroglobulin Ab [Units/volume] in Serum'),
    'thyroglobulin antibody':       ('5380-6', 'Anti-thyroglobulin Ab [Units/volume] in Serum'),
    'anti-tpo':                     ('5382-2', 'Anti-TPO Ab [Units/volume] in Serum'),
    'anti-thyroid peroxidase':      ('5382-2', 'Anti-TPO Ab [Units/volume] in Serum'),
    'thyroid peroxidase antibody':  ('5382-2', 'Anti-TPO Ab [Units/volume] in Serum'),
    'tpo antibody':                 ('5382-2', 'Anti-TPO Ab [Units/volume] in Serum'),

    # ── Coagulation — additional variants ──
    'protime':                      ('5902-2', 'Prothrombin time (PT) in Blood'),
    'prothrombin time (pt)':        ('5902-2', 'Prothrombin time (PT) in Blood'),
    'pt (protime)':                 ('5902-2', 'Prothrombin time (PT) in Blood'),
    'pt, blood':                    ('5902-2', 'Prothrombin time (PT) in Blood'),
    'inr(pt)':                      ('6301-6', 'INR in Blood'),
    'inr (pt)':                     ('6301-6', 'INR in Blood'),
    'd-dimer':                      ('48065-7', 'Fibrin D-dimer FEU [Mass/volume] in Platelet poor plasma'),
    'd dimer':                      ('48065-7', 'Fibrin D-dimer FEU [Mass/volume] in Platelet poor plasma'),
    'd-dimer, quantitative':        ('48065-7', 'Fibrin D-dimer FEU [Mass/volume] in Platelet poor plasma'),
    'fibrinogen, activity':         ('3255-7', 'Fibrinogen [Mass/volume] in Platelet poor plasma'),
    'thrombin time':                ('3243-3', 'Thrombin time'),

    # ── Cardiac Markers ──
    'troponin t':                   ('6598-7', 'Troponin T cardiac [Mass/volume] in Serum or Plasma'),
    'troponin t, 5th gen':          ('67151-1', 'Troponin T cardiac [Mass/volume] in Serum or Plasma by High sensitivity method'),
    'troponin t 5th gen':           ('67151-1', 'Troponin T cardiac [Mass/volume] in Serum or Plasma by High sensitivity method'),
    'high sensitivity troponin':    ('67151-1', 'Troponin T cardiac [Mass/volume] in Serum or Plasma by High sensitivity method'),
    'hs troponin t':                ('67151-1', 'Troponin T cardiac [Mass/volume] in Serum or Plasma by High sensitivity method'),
    'troponin i':                   ('10839-9', 'Troponin I cardiac [Mass/volume] in Serum or Plasma'),
    'hs troponin i':                ('89579-7', 'Troponin I cardiac [Mass/volume] in Serum or Plasma by High sensitivity method'),
    'nt-pro bnp':                   ('33762-6', 'NT-proBNP [Mass/volume] in Serum or Plasma'),
    'nt pro bnp':                   ('33762-6', 'NT-proBNP [Mass/volume] in Serum or Plasma'),
    'n-terminal pro-bnp':           ('33762-6', 'NT-proBNP [Mass/volume] in Serum or Plasma'),
    'bnp':                          ('30934-4', 'BNP [Mass/volume] in Serum or Plasma'),
    'brain natriuretic peptide':    ('30934-4', 'BNP [Mass/volume] in Serum or Plasma'),
    'creatine kinase':              ('2157-6', 'Creatine kinase [Enzymatic activity/volume] in Serum or Plasma'),
    'ck':                           ('2157-6', 'Creatine kinase [Enzymatic activity/volume] in Serum or Plasma'),
    'cpk':                          ('2157-6', 'Creatine kinase [Enzymatic activity/volume] in Serum or Plasma'),
    'ck-mb':                        ('13969-1', 'CK-MB [Mass/volume] in Serum or Plasma'),

    # ── ECG — additional variants ──
    'qrs duration':                 ('8633-0', 'QRS duration'),
    'qrs interval':                 ('8633-0', 'QRS duration'),
    'qt interval':                  ('8634-8', 'QT interval'),
    'qtc':                          ('8636-3', 'QTc interval'),
    'qt corrected':                 ('8636-3', 'QTc interval'),
    'qtc interval':                 ('8636-3', 'QTc interval'),
    'atrial rate':                  ('8636-3', 'Atrial rate'),
    'ventricular rate':             ('8637-1', 'Ventricular rate'),
    'pr interval':                  ('8625-6', 'P-R interval'),

    # ── Thyroid — additional variants ──
    'tsh, 3rd generation':          ('3016-3', 'TSH [Units/volume] in Serum or Plasma'),
    'tsh, serum':                   ('3016-3', 'TSH [Units/volume] in Serum or Plasma'),
    'tsh, ultrasensitive':          ('3016-3', 'TSH [Units/volume] in Serum or Plasma'),
    'thyroid stimulating hormone (tsh)': ('3016-3', 'TSH [Units/volume] in Serum or Plasma'),
    'free t4 (thyroxine)':          ('3024-7', 'Free T4 [Mass/volume] in Serum or Plasma'),
    'thyroxine, free':              ('3024-7', 'Free T4 [Mass/volume] in Serum or Plasma'),
    't4 free':                      ('3024-7', 'Free T4 [Mass/volume] in Serum or Plasma'),
    't3, free':                     ('3051-0', 'Free T3 [Mass/volume] in Serum or Plasma'),
    't3 free':                      ('3051-0', 'Free T3 [Mass/volume] in Serum or Plasma'),
    'total t4':                     ('3026-2', 'Thyroxine [Mass/volume] in Serum or Plasma'),
    'thyroxine total':              ('3026-2', 'Thyroxine [Mass/volume] in Serum or Plasma'),
    't4, total':                    ('3026-2', 'Thyroxine [Mass/volume] in Serum or Plasma'),
    'total t3':                     ('3053-6', 'Triiodothyronine [Mass/volume] in Serum or Plasma'),
    't3, total':                    ('3053-6', 'Triiodothyronine [Mass/volume] in Serum or Plasma'),
    'thyroglobulin':                ('3013-0', 'Thyroglobulin [Mass/volume] in Serum or Plasma'),

    # ── Vitamins & Minerals — additional variants ──
    '25-oh vitamin d total':        ('1989-3', '25-Hydroxyvitamin D [Mass/volume] in Serum or Plasma'),
    '25-oh vitamin d':              ('1989-3', '25-Hydroxyvitamin D [Mass/volume] in Serum or Plasma'),
    '25-oh vit d':                  ('1989-3', '25-Hydroxyvitamin D [Mass/volume] in Serum or Plasma'),
    'vitamin d 25-oh total':        ('1989-3', '25-Hydroxyvitamin D [Mass/volume] in Serum or Plasma'),
    'vitamin d, total':             ('1989-3', '25-Hydroxyvitamin D [Mass/volume] in Serum or Plasma'),
    '25-hydroxyvitamin d':          ('1989-3', '25-Hydroxyvitamin D [Mass/volume] in Serum or Plasma'),
    'vitamin b12, serum':           ('2132-9', 'Vitamin B12 [Mass/volume] in Serum or Plasma'),
    'b12':                          ('2132-9', 'Vitamin B12 [Mass/volume] in Serum or Plasma'),
    'cobalamin':                    ('2132-9', 'Vitamin B12 [Mass/volume] in Serum or Plasma'),
    'folate, serum':                ('2284-8', 'Folate [Mass/volume] in Serum or Plasma'),
    'folic acid':                   ('2284-8', 'Folate [Mass/volume] in Serum or Plasma'),
    'zinc':                         ('2601-3', 'Zinc [Mass/volume] in Serum or Plasma'),
    'zinc, serum':                  ('2601-3', 'Zinc [Mass/volume] in Serum or Plasma'),
    'copper':                       ('2507-2', 'Copper [Mass/volume] in Serum or Plasma'),
    'copper, serum':                ('2507-2', 'Copper [Mass/volume] in Serum or Plasma'),
    'ceruloplasmin':                ('2064-4', 'Ceruloplasmin [Mass/volume] in Serum or Plasma'),
    'lead':                         ('5671-3', 'Lead [Mass/volume] in Blood'),
    'lead, blood':                  ('5671-3', 'Lead [Mass/volume] in Blood'),
    'lead level':                   ('5671-3', 'Lead [Mass/volume] in Blood'),
    'vitamin a':                    ('2923-1', 'Retinol [Mass/volume] in Serum or Plasma'),
    'retinol':                      ('2923-1', 'Retinol [Mass/volume] in Serum or Plasma'),

    # ── Endocrine ──
    'cortisol':                     ('2143-6', 'Cortisol [Mass/volume] in Serum or Plasma'),
    'cortisol, am':                 ('2143-6', 'Cortisol [Mass/volume] in Serum or Plasma'),
    'cortisol, serum':              ('2143-6', 'Cortisol [Mass/volume] in Serum or Plasma'),
    'parathyroid hormone':          ('2731-8', 'PTH intact [Mass/volume] in Serum or Plasma'),
    'pth':                          ('2731-8', 'PTH intact [Mass/volume] in Serum or Plasma'),
    'pth, intact':                  ('2731-8', 'PTH intact [Mass/volume] in Serum or Plasma'),
    'intact parathyroid hormone':   ('2731-8', 'PTH intact [Mass/volume] in Serum or Plasma'),
    'testosterone':                 ('2986-8', 'Testosterone [Mass/volume] in Serum or Plasma'),
    'testosterone, total':          ('2986-8', 'Testosterone [Mass/volume] in Serum or Plasma'),
    'testosterone, free':           ('2991-8', 'Testosterone free [Mass/volume] in Serum or Plasma'),
    'free testosterone':            ('2991-8', 'Testosterone free [Mass/volume] in Serum or Plasma'),
    'estradiol':                    ('2243-4', 'Estradiol [Mass/volume] in Serum or Plasma'),
    'estradiol (e2)':               ('2243-4', 'Estradiol [Mass/volume] in Serum or Plasma'),
    'lh':                           ('10501-5', 'LH [Units/volume] in Serum or Plasma'),
    'luteinizing hormone':          ('10501-5', 'LH [Units/volume] in Serum or Plasma'),
    'fsh':                          ('15067-2', 'FSH [Units/volume] in Serum or Plasma'),
    'follicle stimulating hormone': ('15067-2', 'FSH [Units/volume] in Serum or Plasma'),
    'prolactin':                    ('2842-3', 'Prolactin [Mass/volume] in Serum or Plasma'),
    'dhea-s':                       ('2191-5', 'DHEA-S [Mass/volume] in Serum or Plasma'),
    'dhea sulfate':                 ('2191-5', 'DHEA-S [Mass/volume] in Serum or Plasma'),
    'aldosterone':                  ('1763-2', 'Aldosterone [Mass/volume] in Serum or Plasma'),
    'aldosterone, serum':           ('1763-2', 'Aldosterone [Mass/volume] in Serum or Plasma'),
    'renin':                        ('2915-7', 'Renin [Enzymatic activity/volume] in Plasma'),
    'renin activity':               ('2915-7', 'Renin [Enzymatic activity/volume] in Plasma'),
    'plasma renin activity':        ('2915-7', 'Renin [Enzymatic activity/volume] in Plasma'),
    'insulin':                      ('2484-4', 'Insulin [Units/volume] in Serum or Plasma'),
    'insulin, fasting':             ('2484-4', 'Insulin [Units/volume] in Serum or Plasma'),
    'c-peptide':                    ('1986-9', 'C-peptide [Mass/volume] in Serum or Plasma'),
    'growth hormone':               ('2963-7', 'Growth hormone [Mass/volume] in Serum or Plasma'),
    'igf-1':                        ('2484-8', 'IGF-I [Mass/volume] in Serum or Plasma'),

    # ── Haptoglobin / Hemolysis ──
    'haptoglobin':                  ('4542-7', 'Haptoglobin [Mass/volume] in Serum or Plasma'),
    'haptoglobin, serum':           ('4542-7', 'Haptoglobin [Mass/volume] in Serum or Plasma'),
    'lactic acid':                  ('2524-7', 'Lactate [Moles/volume] in Serum or Plasma'),
    'lactate':                      ('2524-7', 'Lactate [Moles/volume] in Serum or Plasma'),

    # ── Urinalysis — additional variants ──
    'urobilinogen':                 ('5818-0', 'Urobilinogen [Presence] in Urine by Test strip'),
    'urobilinogen, urine':          ('5818-0', 'Urobilinogen [Presence] in Urine by Test strip'),
    'urine urobilinogen':           ('5818-0', 'Urobilinogen [Presence] in Urine by Test strip'),
    'urine glucose':                ('5792-7', 'Glucose [Mass/volume] in Urine by Test strip'),
    'urine bilirubin':              ('5770-3', 'Bilirubin [Presence] in Urine by Test strip'),
    'bilirubin, urine':             ('5770-3', 'Bilirubin [Presence] in Urine by Test strip'),
    'urine ketones':                ('2514-8', 'Ketones [Presence] in Urine'),
    'ketones, urine':               ('2514-8', 'Ketones [Presence] in Urine'),
    'urine blood':                  ('5794-3', 'Hemoglobin [Presence] in Urine by Test strip'),
    'urine ph':                     ('2756-5', 'pH of Urine'),
    'urine protein':                ('5804-0', 'Protein [Mass/volume] in Urine by Test strip'),
    'urine specific gravity':       ('5811-5', 'Specific gravity of Urine'),
    'urine nitrite':                ('5802-4', 'Nitrite [Presence] in Urine by Test strip'),
    'urine nitrites':               ('5802-4', 'Nitrite [Presence] in Urine by Test strip'),
    'urine leukocyte esterase':     ('5799-2', 'Leukocyte esterase [Presence] in Urine'),
    'urine leukocytes':             ('5821-4', 'Leukocytes [#/area] in Urine sediment'),
    'urine rbc':                    ('13945-1', 'Erythrocytes [#/area] in Urine sediment'),
    'urine wbc':                    ('5821-4', 'Leukocytes [#/area] in Urine sediment'),
    'urine bacteria':               ('630-4', 'Bacteria [Presence] in Urine sediment'),
    'urine clarity':                ('32167-9', 'Clarity of Urine'),
    'urine epithelial cells':       ('5787-7', 'Epithelial cells [#/area] in Urine sediment'),
    'urine hyaline cast':           ('5796-0', 'Hyaline casts [#/area] in Urine sediment'),
    'urine protein/creatinine ratio': ('2890-2', 'Protein/Creatinine [Mass ratio] in Urine'),
    'protein/creatinine ratio, urine': ('2890-2', 'Protein/Creatinine [Mass ratio] in Urine'),
    'alb/creat ratio, urine':      ('9318-7', 'Albumin/Creatinine [Mass ratio] in Urine'),
    'albumin/creatinine ratio':     ('9318-7', 'Albumin/Creatinine [Mass ratio] in Urine'),
    'microalbumin':                 ('14957-5', 'Microalbumin [Mass/volume] in Urine'),
    'microalbumin, urine':          ('14957-5', 'Microalbumin [Mass/volume] in Urine'),
    'squamous epithelial cells':    ('5787-7', 'Epithelial cells [#/area] in Urine sediment'),
    'mucus':                        ('8247-2', 'Mucus [Presence] in Urine sediment'),
    'mucus, urine':                 ('8247-2', 'Mucus [Presence] in Urine sediment'),
    'granular cast':                ('5793-5', 'Granular casts [#/area] in Urine sediment'),

    # ── Infectious Disease — additional variants ──
    'sars-cov-2 pcr':               ('94500-6', 'SARS-CoV-2 RNA [Presence] in Respiratory specimen by NAA'),
    'sars-cov-2':                   ('94500-6', 'SARS-CoV-2 RNA [Presence] in Respiratory specimen by NAA'),
    'covid-19 pcr':                 ('94500-6', 'SARS-CoV-2 RNA [Presence] in Respiratory specimen by NAA'),
    'sars-cov-2 rna':               ('94500-6', 'SARS-CoV-2 RNA [Presence] in Respiratory specimen by NAA'),
    'sars-cov-2 antigen':           ('94558-4', 'SARS-CoV-2 Ag [Presence] in Respiratory specimen by Rapid immunoassay'),
    'covid-19 antigen':             ('94558-4', 'SARS-CoV-2 Ag [Presence] in Respiratory specimen by Rapid immunoassay'),
    'hiv 1/2 ab/ag':                ('56888-1', 'HIV 1+2 Ab+Ag [Presence] in Serum or Plasma by Immunoassay'),
    'hiv combo':                    ('56888-1', 'HIV 1+2 Ab+Ag [Presence] in Serum or Plasma by Immunoassay'),
    'hiv 1/2 antigen/antibody':     ('56888-1', 'HIV 1+2 Ab+Ag [Presence] in Serum or Plasma by Immunoassay'),
    'hepatitis b surface antigen':  ('5195-3', 'Hepatitis B surface Ag [Presence] in Serum'),
    'hbsag':                        ('5195-3', 'Hepatitis B surface Ag [Presence] in Serum'),
    'hepatitis b surface antibody': ('10900-9', 'Hepatitis B surface Ab [Units/volume] in Serum'),
    'hbsab':                        ('10900-9', 'Hepatitis B surface Ab [Units/volume] in Serum'),
    'hepatitis b core antibody':    ('16933-4', 'Hepatitis B core Ab [Presence] in Serum'),
    'hbcab':                        ('16933-4', 'Hepatitis B core Ab [Presence] in Serum'),
    'hepatitis c antibody':         ('16128-1', 'Hepatitis C Ab [Presence] in Serum'),
    'hcv antibody':                 ('16128-1', 'Hepatitis C Ab [Presence] in Serum'),
    'hepatitis c virus antibody':   ('16128-1', 'Hepatitis C Ab [Presence] in Serum'),
    'hepatitis a antibody':         ('32018-4', 'Hepatitis A Ab [Presence] in Serum'),
    'hepatitis a ab total':         ('32018-4', 'Hepatitis A Ab [Presence] in Serum'),
    'hepatitis a igg':              ('32018-4', 'Hepatitis A Ab [Presence] in Serum'),
    'hepatitis b dna':              ('5009-6', 'Hepatitis B virus DNA [Units/volume] in Serum by NAA'),
    'hbv dna':                      ('5009-6', 'Hepatitis B virus DNA [Units/volume] in Serum by NAA'),
    'hepatitis c rna':              ('11259-9', 'Hepatitis C virus RNA [Units/volume] in Serum by NAA'),
    'hcv rna':                      ('11259-9', 'Hepatitis C virus RNA [Units/volume] in Serum by NAA'),
    'rpr':                          ('20507-0', 'Reagin Ab [Presence] in Serum by RPR'),
    'rapid plasma reagin':          ('20507-0', 'Reagin Ab [Presence] in Serum by RPR'),
    'quantiferon tb':               ('64083-9', 'IGRA for Mycobacterium tuberculosis'),
    'tb quantiferon':               ('64083-9', 'IGRA for Mycobacterium tuberculosis'),
    'interferon gamma release assay': ('64083-9', 'IGRA for Mycobacterium tuberculosis'),

    # ── Cancer Markers — additional variants ──
    'psa, total':                   ('2857-1', 'PSA [Mass/volume] in Serum or Plasma'),
    'psa, free':                    ('10886-0', 'PSA free [Mass/volume] in Serum or Plasma'),
    'free psa':                     ('10886-0', 'PSA free [Mass/volume] in Serum or Plasma'),
    'psa free/total ratio':         ('12841-3', 'PSA free/PSA total in Serum or Plasma'),
    'cea':                          ('2039-6', 'CEA [Mass/volume] in Serum or Plasma'),
    'carcinoembryonic antigen':     ('2039-6', 'CEA [Mass/volume] in Serum or Plasma'),
    'afp':                          ('1834-1', 'AFP [Mass/volume] in Serum or Plasma'),
    'alpha-fetoprotein':            ('1834-1', 'AFP [Mass/volume] in Serum or Plasma'),
    'ca 19-9':                      ('24108-3', 'CA 19-9 [Units/volume] in Serum or Plasma'),
    'ca 125':                       ('10334-1', 'CA 125 [Units/volume] in Serum or Plasma'),
    'beta-2 microglobulin, serum':  ('1952-1', 'Beta-2-microglobulin [Mass/volume] in Serum or Plasma'),
    'b2 microglobulin':             ('1952-1', 'Beta-2-microglobulin [Mass/volume] in Serum or Plasma'),
    'ldh, serum':                   ('2532-0', 'LDH [Enzymatic activity/volume] in Serum or Plasma'),
    'lactic dehydrogenase':         ('2532-0', 'LDH [Enzymatic activity/volume] in Serum or Plasma'),

    # ── POC / iStat variants ──
    'poc glucose':                  ('2345-7', 'Glucose [Mass/volume] in Serum or Plasma'),
    'istat sodium':                 ('2951-2', 'Sodium [Moles/volume] in Serum or Plasma'),
    'istat potassium':              ('2823-3', 'Potassium [Moles/volume] in Serum or Plasma'),
    'istat chloride':               ('2075-0', 'Chloride [Moles/volume] in Serum or Plasma'),
    'istat tco2':                   ('2028-9', 'CO2 [Moles/volume] in Serum or Plasma'),
    'istat bun':                    ('3094-0', 'BUN [Mass/volume] in Serum or Plasma'),
    'istat creatinine':             ('2160-0', 'Creatinine [Mass/volume] in Serum or Plasma'),
    'istat glucose':                ('2345-7', 'Glucose [Mass/volume] in Serum or Plasma'),
    'istat hemoglobin':             ('718-7', 'Hemoglobin [Mass/volume] in Blood'),
    'istat hematocrit':             ('4544-3', 'Hematocrit [Volume Fraction] of Blood'),
    'istat ionized calcium':        ('1994-3', 'Ionized calcium [Moles/volume] in Serum or Plasma'),
    'istat lactate':                ('2524-7', 'Lactate [Moles/volume] in Serum or Plasma'),
    'poc hemoglobin':               ('718-7', 'Hemoglobin [Mass/volume] in Blood'),
    'poc hematocrit':               ('4544-3', 'Hematocrit [Volume Fraction] of Blood'),
    'poc creatinine':               ('2160-0', 'Creatinine [Mass/volume] in Serum or Plasma'),
    'poc bun':                      ('3094-0', 'BUN [Mass/volume] in Serum or Plasma'),
    'poc na':                       ('2951-2', 'Sodium [Moles/volume] in Serum or Plasma'),
    'poc k':                        ('2823-3', 'Potassium [Moles/volume] in Serum or Plasma'),
    'poc cl':                       ('2075-0', 'Chloride [Moles/volume] in Serum or Plasma'),
    'poc ionized calcium':          ('1994-3', 'Ionized calcium [Moles/volume] in Serum or Plasma'),
    'poc lactate':                  ('2524-7', 'Lactate [Moles/volume] in Serum or Plasma'),

    # ── Vitals — additional variants ──
    'heart rate variability':       ('80404-7', 'Heart rate variability R-R interval'),
    'hrv':                          ('80404-7', 'Heart rate variability R-R interval'),
    'systolic blood pressure':      ('8480-6', 'Systolic blood pressure'),
    'diastolic blood pressure':     ('8462-4', 'Diastolic blood pressure'),
    'blood pressure systolic':      ('8480-6', 'Systolic blood pressure'),
    'blood pressure diastolic':     ('8462-4', 'Diastolic blood pressure'),
    'bp systolic':                  ('8480-6', 'Systolic blood pressure'),
    'bp diastolic':                 ('8462-4', 'Diastolic blood pressure'),
    'height':                       ('8302-2', 'Body height'),
    'body height':                  ('8302-2', 'Body height'),
    'bmi':                          ('39156-5', 'BMI'),
    'waist circumference':          ('56086-2', 'Waist circumference'),
    'vo2 max':                      ('60842-2', 'VO2 max [Volume rate]'),
    'walking speed':                ('79000-7', 'Walking speed'),
    'flights climbed':              ('93831-6', 'Flights of stairs climbed'),
    'active energy burned':         ('41981-2', 'Calories burned'),
    'walking distance':             ('41953-1', 'Walking distance'),
    'resting heart rate':           ('40443-4', 'Heart rate resting'),
    'environmental sound level':    ('89020-2', 'Environmental sound level'),

    # ── Miscellaneous Chemistry ──
    'amylase, lipase':              ('1798-8', 'Amylase [Enzymatic activity/volume] in Serum or Plasma'),
    'homocysteine':                 ('13965-9', 'Homocysteine [Moles/volume] in Serum or Plasma'),
    'prealbumin':                   ('14338-8', 'Prealbumin [Mass/volume] in Serum or Plasma'),
    'alpha-1 antitrypsin':          ('6770-2', 'Alpha-1-antitrypsin [Mass/volume] in Serum or Plasma'),
    'calprotectin':                 ('83993-7', 'Calprotectin [Mass/volume] in Stool'),
    'calprotectin, stool':          ('83993-7', 'Calprotectin [Mass/volume] in Stool'),
    'fecal calprotectin':           ('83993-7', 'Calprotectin [Mass/volume] in Stool'),

    # ── Blood Gas ──
    'pco2':                         ('2019-8', 'pCO2 [Partial pressure] in Arterial blood'),
    'po2':                          ('2703-7', 'pO2 [Partial pressure] in Arterial blood'),
    'ph, blood':                    ('2744-1', 'pH of Arterial blood'),
    'base excess':                  ('1925-7', 'Base excess in Arterial blood'),
    'o2 saturation':                ('2708-6', 'Oxygen saturation in Arterial blood'),

    # ── Pancreatic / GI ──
    'lipase, serum':                ('3040-3', 'Lipase [Enzymatic activity/volume] in Serum or Plasma'),
    'fecal occult blood':           ('2335-8', 'Hemoglobin.gastrointestinal [Presence] in Stool'),
    'occult blood, stool':          ('2335-8', 'Hemoglobin.gastrointestinal [Presence] in Stool'),
    'stool occult blood':           ('2335-8', 'Hemoglobin.gastrointestinal [Presence] in Stool'),
    'h. pylori ab':                 ('5184-7', 'Helicobacter pylori Ab [Units/volume] in Serum'),
    'h. pylori antigen':            ('70169-8', 'Helicobacter pylori Ag [Presence] in Stool'),
    'h pylori antibody':            ('5184-7', 'Helicobacter pylori Ab [Units/volume] in Serum'),
    'celiac panel':                 ('31017-7', 'Tissue transglutaminase IgA Ab [Units/volume] in Serum'),
    'ttg iga':                      ('31017-7', 'Tissue transglutaminase IgA Ab [Units/volume] in Serum'),
    'tissue transglutaminase':      ('31017-7', 'Tissue transglutaminase IgA Ab [Units/volume] in Serum'),

    # ── CSF ──
    'csf protein':                  ('2880-3', 'Protein [Mass/volume] in Cerebral spinal fluid'),
    'csf glucose':                  ('2342-4', 'Glucose [Mass/volume] in Cerebral spinal fluid'),
    'csf wbc':                      ('26464-8', 'Leukocytes [#/volume] in Cerebral spinal fluid'),
    'csf rbc':                      ('26453-1', 'Erythrocytes [#/volume] in Cerebral spinal fluid'),

    # ── Urine Chemistry (24h and spot) ──
    'urine sodium':                 ('2955-3', 'Sodium [Moles/volume] in Urine'),
    'urine potassium':              ('2828-2', 'Potassium [Moles/volume] in Urine'),
    'urine chloride':               ('2078-4', 'Chloride [Moles/volume] in Urine'),
    'urine calcium':                ('17862-4', 'Calcium [Mass/volume] in Urine'),
    'urine creatinine':             ('2161-8', 'Creatinine [Mass/volume] in Urine'),
    'urine phosphorus':             ('2778-9', 'Phosphate [Mass/volume] in Urine'),
    'urine uric acid':              ('3087-4', 'Urate [Mass/volume] in Urine'),
    'urine oxalate':                ('2705-2', 'Oxalate [Moles/volume] in Urine'),
    'urine citrate':                ('2100-6', 'Citrate [Moles/volume] in Urine'),
    '24 hour urine protein':        ('2889-4', 'Protein [Mass/time] in 24 hour Urine'),
    '24 hour urine creatinine':     ('2162-6', 'Creatinine [Mass/time] in 24 hour Urine'),

    # ── Catecholamines / Metanephrines ──
    'metanephrines, plasma':        ('2680-7', 'Metanephrines [Mass/volume] in Plasma'),
    'normetanephrine, plasma':      ('2669-0', 'Normetanephrine [Mass/volume] in Plasma'),
    'metanephrine, plasma':         ('2668-2', 'Metanephrine [Mass/volume] in Plasma'),
    'urine metanephrines':          ('2681-5', 'Metanephrines [Mass/time] in 24 hour Urine'),
    'urine catecholamines':         ('2088-3', 'Catecholamines [Mass/time] in 24 hour Urine'),
    'vanillylmandelic acid':        ('3042-9', 'Vanillylmandelate [Mass/time] in 24 hour Urine'),
    'vma':                          ('3042-9', 'Vanillylmandelate [Mass/time] in 24 hour Urine'),

    # ── Porphyrins ──
    'porphyrins, urine':            ('2739-1', 'Porphyrins [Mass/time] in 24 hour Urine'),
    'porphobilinogen, urine':       ('2738-3', 'Porphobilinogen [Mass/time] in 24 hour Urine'),
    'ala, urine':                   ('41551-3', 'Delta aminolevulinate [Mass/time] in 24 hour Urine'),

    # ── Exact unmatched variants from stats dump ──

    # Chemistry — specific portal name variants
    '25-oh vitamin d, total':       ('1989-3', '25-Hydroxyvitamin D [Mass/volume] in Serum or Plasma'),
    'vitamin d, 25-hydroxy':        ('1989-3', '25-Hydroxyvitamin D [Mass/volume] in Serum or Plasma'),
    'calcium, total, serum / plasma': ('17861-6', 'Calcium [Mass/volume] in Serum or Plasma'),
    'chloride, serum / plasma':     ('2075-0', 'Chloride [Moles/volume] in Serum or Plasma'),
    'potassium, serum / plasma':    ('2823-3', 'Potassium [Moles/volume] in Serum or Plasma'),
    'sodium, serum / plasma':       ('2951-2', 'Sodium [Moles/volume] in Serum or Plasma'),
    'protein, total, serum / plasma': ('2885-2', 'Protein [Mass/volume] in Serum or Plasma'),
    'albumin, serum / plasma':      ('1751-7', 'Albumin [Mass/volume] in Serum or Plasma'),
    'creatinine, serum / plasma':   ('2160-0', 'Creatinine [Mass/volume] in Serum or Plasma'),
    'carbon dioxide (co2) total plasma': ('2028-9', 'CO2 [Moles/volume] in Serum or Plasma'),
    'carbon dioxide (co2) total':   ('2028-9', 'CO2 [Moles/volume] in Serum or Plasma'),
    'total protein serum':          ('2885-2', 'Protein [Mass/volume] in Serum or Plasma'),
    'total protein, plasma':        ('2885-2', 'Protein [Mass/volume] in Serum or Plasma'),
    'total protein plasma':         ('2885-2', 'Protein [Mass/volume] in Serum or Plasma'),
    'total protein, s':             ('2885-2', 'Protein [Mass/volume] in Serum or Plasma'),
    'sodium plasma':                ('2951-2', 'Sodium [Moles/volume] in Serum or Plasma'),
    'potassium plasma':             ('2823-3', 'Potassium [Moles/volume] in Serum or Plasma'),
    'calcium plasma':               ('17861-6', 'Calcium [Mass/volume] in Serum or Plasma'),
    'albumin plasma':               ('1751-7', 'Albumin [Mass/volume] in Serum or Plasma'),
    'chloride plasma':              ('2075-0', 'Chloride [Moles/volume] in Serum or Plasma'),
    'creatinine plasma':            ('2160-0', 'Creatinine [Mass/volume] in Serum or Plasma'),
    'glucose plasma':               ('2345-7', 'Glucose [Mass/volume] in Serum or Plasma'),
    'glucose, p':                   ('2345-7', 'Glucose [Mass/volume] in Serum or Plasma'),
    'bilirubin total plasma':       ('1975-2', 'Bilirubin.total [Mass/volume] in Serum or Plasma'),
    'bilirubin total':              ('1975-2', 'Bilirubin.total [Mass/volume] in Serum or Plasma'),
    'bilirubin, direct':            ('1968-7', 'Bilirubin.direct [Mass/volume] in Serum or Plasma'),
    'anion gap plasma':             ('33037-3', 'Anion gap in Serum or Plasma'),
    'blood urea nitrogen (bun) plasma': ('3094-0', 'BUN [Mass/volume] in Serum or Plasma'),
    'urea nitrogen,blood (bun)':    ('3094-0', 'BUN [Mass/volume] in Serum or Plasma'),
    'corrected calcium plasma':     ('29265-6', 'Calcium.ionized adjusted to pH 7.4 [Moles/volume] in Serum or Plasma'),
    'ionized calcium calc':         ('1994-3', 'Ionized calcium [Moles/volume] in Serum or Plasma'),
    'osmolality calc':              ('2692-2', 'Osmolality of Serum or Plasma'),
    'creatinine conc':              ('2161-8', 'Creatinine [Mass/volume] in Urine'),
    'creatinine, conc.':            ('2161-8', 'Creatinine [Mass/volume] in Urine'),
    'prot tot conc':                ('2888-6', 'Protein [Mass/volume] in Urine'),
    'protein, total random':        ('2888-6', 'Protein [Mass/volume] in Urine'),
    'prot concentration,ur':        ('2888-6', 'Protein [Mass/volume] in Urine'),
    'protein/creatinine ration':    ('2890-2', 'Protein/Creatinine [Mass ratio] in Urine'),

    # Liver — specific portal name variants
    'alanine aminotransferase (alt)': ('1742-6', 'ALT [Enzymatic activity/volume] in Serum or Plasma'),
    'alanine aminotransferase (alt) plasma': ('1742-6', 'ALT [Enzymatic activity/volume] in Serum or Plasma'),
    'alanine transaminase':         ('1742-6', 'ALT [Enzymatic activity/volume] in Serum or Plasma'),
    'aspartate aminotransferase (ast)': ('1920-8', 'AST [Enzymatic activity/volume] in Serum or Plasma'),
    'aspartate aminotransferase (ast) plasma': ('1920-8', 'AST [Enzymatic activity/volume] in Serum or Plasma'),
    'aspartate aminotransferase (ast), s': ('1920-8', 'AST [Enzymatic activity/volume] in Serum or Plasma'),
    'alkaline phosphatase (alk)':   ('6768-6', 'ALP [Enzymatic activity/volume] in Serum or Plasma'),
    'alkaline phosphatase (alk) plasma': ('6768-6', 'ALP [Enzymatic activity/volume] in Serum or Plasma'),
    'alkaline phosphatase, s':      ('6768-6', 'ALP [Enzymatic activity/volume] in Serum or Plasma'),

    # Iron — specific portal name variants
    'iron total':                   ('2498-4', 'Iron [Mass/volume] in Serum or Plasma'),
    'total iron binding capacity':  ('2500-7', 'TIBC [Mass/volume] in Serum or Plasma'),
    'iron bind. cap.(tibc)':        ('2500-7', 'TIBC [Mass/volume] in Serum or Plasma'),
    'ibc total':                    ('2500-7', 'TIBC [Mass/volume] in Serum or Plasma'),
    '%transferrin saturation':      ('2502-3', 'Transferrin saturation in Serum or Plasma'),
    'percent saturation':           ('2502-3', 'Transferrin saturation in Serum or Plasma'),
    'uibc':                         ('2501-5', 'UIBC [Mass/volume] in Serum or Plasma'),

    # Hematology — specific portal name variants
    'wbc in blood by automated count': ('6690-2', 'Leukocytes [#/volume] in Blood'),
    'rbc in blood by automated count': ('789-8', 'Erythrocytes [#/volume] in Blood'),
    'hemoglobin by automated count': ('718-7', 'Hemoglobin [Mass/volume] in Blood'),
    'nrbc by automated count':      ('58413-6', 'Nucleated RBC [#/volume] in Blood'),
    'nucleated rbc auto':           ('58413-6', 'Nucleated RBC [#/volume] in Blood'),
    'nrbc':                         ('58413-6', 'Nucleated RBC [#/volume] in Blood'),
    'abs nrbc':                     ('58413-6', 'Nucleated RBC [#/volume] in Blood'),
    '% nrbc':                       ('19048-8', 'Nucleated RBC/100 leukocytes in Blood'),
    'rbc distrib width':            ('788-0', 'RDW [Ratio]'),
    'rdw (sd)':                     ('21000-5', 'RDW [Entitic volume] by Automated count'),
    'leukocytes':                   ('6690-2', 'Leukocytes [#/volume] in Blood'),
    'erythrocytes':                 ('789-8', 'Erythrocytes [#/volume] in Blood'),
    'lymphs':                       ('736-9', 'Lymphocytes/100 leukocytes in Blood'),
    'eos':                          ('713-8', 'Eosinophils/100 leukocytes in Blood'),
    'basos':                        ('706-2', 'Basophils/100 leukocytes in Blood'),
    'neutrophil':                   ('770-8', 'Neutrophils/100 leukocytes in Blood'),
    'lymphocyte':                   ('736-9', 'Lymphocytes/100 leukocytes in Blood'),
    'eosinophil':                   ('713-8', 'Eosinophils/100 leukocytes in Blood'),
    'basophil':                     ('706-2', 'Basophils/100 leukocytes in Blood'),
    'imm granulocyte':              ('53115-2', 'Immature granulocytes [#/volume] in Blood'),
    'abs neutrophil':               ('751-8', 'Neutrophils [#/volume] in Blood'),
    'abs lymphocyte':               ('731-0', 'Lymphocytes [#/volume] in Blood'),
    'abs monocyte':                 ('742-7', 'Monocytes [#/volume] in Blood'),
    'abs eosinophil':               ('711-2', 'Eosinophils [#/volume] in Blood'),
    'abs basophil':                 ('704-7', 'Basophils [#/volume] in Blood'),
    'abs imm granulocyte':          ('53115-2', 'Immature granulocytes [#/volume] in Blood'),
    'abs immature grans':           ('53115-2', 'Immature granulocytes [#/volume] in Blood'),
    'neutrophils (absolute)':       ('751-8', 'Neutrophils [#/volume] in Blood'),
    'lymphs (absolute)':            ('731-0', 'Lymphocytes [#/volume] in Blood'),
    'monocytes(absolute)':          ('742-7', 'Monocytes [#/volume] in Blood'),
    'eos (absolute)':               ('711-2', 'Eosinophils [#/volume] in Blood'),
    'baso (absolute)':              ('704-7', 'Basophils [#/volume] in Blood'),
    'lymphocytes, absolute':        ('731-0', 'Lymphocytes [#/volume] in Blood'),
    'abs. eosinophils':             ('711-2', 'Eosinophils [#/volume] in Blood'),
    'abs. basophils':               ('704-7', 'Basophils [#/volume] in Blood'),
    'neutrophils absolute':         ('751-8', 'Neutrophils [#/volume] in Blood'),
    '% immature granulocytes':      ('71695-1', 'Immature granulocytes/100 leukocytes in Blood'),
    '% imm granulocytes':           ('71695-1', 'Immature granulocytes/100 leukocytes in Blood'),
    '% lymphocytes':                ('736-9', 'Lymphocytes/100 leukocytes in Blood'),
    'squamous cells':               ('5787-7', 'Epithelial cells [#/area] in Urine sediment'),

    # Reticulocytes — specific variants
    'retic, absolute (automated)':  ('60474-4', 'Reticulocytes [#/volume] in Blood by Automated count'),
    'retic % (automated)':          ('4679-7', 'Reticulocytes/100 erythrocytes in Blood'),
    'reticulocyte, automated':      ('17849-1', 'Reticulocytes [#/volume] in Blood'),
    'retic (abs value)':            ('60474-4', 'Reticulocytes [#/volume] in Blood by Automated count'),
    'retic count, flow cytometry':  ('17849-1', 'Reticulocytes [#/volume] in Blood'),
    'ret he':                       ('76768-1', 'Reticulocyte hemoglobin content [Mass/volume]'),

    # eGFR — specific portal variants
    'egfr - low estimate':          ('33914-3', 'eGFR CKD-EPI'),
    'egfr - high estimate':         ('33914-3', 'eGFR CKD-EPI'),
    'egfr (creat/cystatin c)':      ('33914-3', 'eGFR CKD-EPI'),
    'egfr (cystatin c)':            ('33914-3', 'eGFR CKD-EPI'),
    'egfr 2021':                    ('33914-3', 'eGFR CKD-EPI'),
    'egfr 2021 plasma':             ('33914-3', 'eGFR CKD-EPI'),
    'estimated gfr (egfr)':         ('33914-3', 'eGFR CKD-EPI'),

    # SPEP — specific portal variants with suffix ", s"
    'alpha-1 globulin':             ('2865-4', 'Alpha-1-globulin [Mass/volume] in Serum or Plasma by Electrophoresis'),
    'alpha-2 globulin':             ('2867-0', 'Alpha-2-globulin [Mass/volume] in Serum or Plasma by Electrophoresis'),
    'gamma-globulin':               ('2871-2', 'Gamma globulin [Mass/volume] in Serum or Plasma by Electrophoresis'),
    'beta-globulin':                ('2868-8', 'Beta-1-globulin [Mass/volume] in Serum or Plasma by Electrophoresis'),
    'alpha 1':                      ('2865-4', 'Alpha-1-globulin [Mass/volume] in Serum or Plasma by Electrophoresis'),
    'alpha 2':                      ('2867-0', 'Alpha-2-globulin [Mass/volume] in Serum or Plasma by Electrophoresis'),
    'beta 1':                       ('2868-8', 'Beta-1-globulin [Mass/volume] in Serum or Plasma by Electrophoresis'),
    'beta 2':                       ('2870-4', 'Beta-2-globulin [Mass/volume] in Serum or Plasma by Electrophoresis'),
    'gamma':                        ('2871-2', 'Gamma globulin [Mass/volume] in Serum or Plasma by Electrophoresis'),
    'm spike 1':                    ('51435-6', 'M-protein [Mass/volume] in Serum or Plasma by Electrophoresis'),
    'm spike 2':                    ('51435-6', 'M-protein [Mass/volume] in Serum or Plasma by Electrophoresis'),
    'm spike 3':                    ('51435-6', 'M-protein [Mass/volume] in Serum or Plasma by Electrophoresis'),
    'm-protein (monoclonal)':       ('51435-6', 'M-protein [Mass/volume] in Serum or Plasma by Electrophoresis'),
    'm-protein/spep quant':         ('51435-6', 'M-protein [Mass/volume] in Serum or Plasma by Electrophoresis'),
    'm spike':                      ('51435-6', 'M-protein [Mass/volume] in Serum or Plasma by Electrophoresis'),
    'albumin pe':                   ('2862-1', 'Albumin [Mass/volume] in Serum or Plasma by Electrophoresis'),
    'lambda free light chain, s':   ('11051-0', 'Free Lambda light chains [Mass/volume] in Serum'),
    'kappa free light chain, s':    ('11050-2', 'Free Kappa light chains [Mass/volume] in Serum'),
    'kappa/lambda flc ratio':       ('11052-8', 'Kappa/Lambda free light chain ratio in Serum'),
    'kappa light chain, serum, free': ('11050-2', 'Free Kappa light chains [Mass/volume] in Serum'),
    'lambda light chain, serum, free': ('11051-0', 'Free Lambda light chains [Mass/volume] in Serum'),
    'kappa/lambda ratio, serum, free': ('11052-8', 'Kappa/Lambda free light chain ratio in Serum'),
    'lambda light chain,free,s':    ('11051-0', 'Free Lambda light chains [Mass/volume] in Serum'),
    'kappa light chain,free,s':     ('11050-2', 'Free Kappa light chains [Mass/volume] in Serum'),
    'kappa/lambda,free ratio,s':    ('11052-8', 'Kappa/Lambda free light chain ratio in Serum'),
    'b2m':                          ('1952-1', 'Beta-2-microglobulin [Mass/volume] in Serum or Plasma'),
    'beta-2-microglobulin':         ('1952-1', 'Beta-2-microglobulin [Mass/volume] in Serum or Plasma'),
    'lactate dehydrogenase (ld), s': ('2532-0', 'LDH [Enzymatic activity/volume] in Serum or Plasma'),
    'lactate dehydrogenase (ldh)':  ('2532-0', 'LDH [Enzymatic activity/volume] in Serum or Plasma'),

    # Urine protein electrophoresis
    'albumin upe':                  ('2862-1', 'Albumin [Mass/volume] in Serum or Plasma by Electrophoresis'),
    'fraction 2 urine protein electrophoresis': ('13991-3', 'Alpha-1-globulin [Mass/volume] in Urine by Electrophoresis'),
    'fraction 3 urine protein electrophoresis': ('13993-9', 'Alpha-2-globulin [Mass/volume] in Urine by Electrophoresis'),
    'fraction 4 urine protein electrophoresis': ('13995-4', 'Beta globulin [Mass/volume] in Urine by Electrophoresis'),
    'fraction 5 urine protein electrophoresis': ('13997-0', 'Gamma globulin [Mass/volume] in Urine by Electrophoresis'),
    '% m-protein (monoclonal), urine': ('56759-4', 'M-protein [Mass/volume] in Urine by Electrophoresis'),
    'm spike 1 upe':                ('51435-6', 'M-protein [Mass/volume] in Serum or Plasma by Electrophoresis'),
    'm spike 2 upe':                ('51435-6', 'M-protein [Mass/volume] in Serum or Plasma by Electrophoresis'),
    'albumin/creat ratio, ur':      ('9318-7', 'Albumin/Creatinine [Mass ratio] in Urine'),
    'protein/creat ratio, urine':   ('2890-2', 'Protein/Creatinine [Mass ratio] in Urine'),
    'microalbumin creatinine ratio, rand ur': ('9318-7', 'Albumin/Creatinine [Mass ratio] in Urine'),

    # Cardiac markers — specific portal name variants
    'high sensitivity troponin i (ng/l)': ('89579-7', 'Troponin I cardiac [Mass/volume] in Serum or Plasma by High sensitivity method'),
    'high sensitivity troponin i (pg/ml)': ('89579-7', 'Troponin I cardiac [Mass/volume] in Serum or Plasma by High sensitivity method'),
    'troponin t, 5th gen':          ('67151-1', 'Troponin T cardiac [Mass/volume] in Serum or Plasma by High sensitivity method'),
    'creatine kinase, total':       ('2157-6', 'Creatine kinase [Enzymatic activity/volume] in Serum or Plasma'),
    'creatine kinase (ck), s':      ('2157-6', 'Creatine kinase [Enzymatic activity/volume] in Serum or Plasma'),

    # Inflammation — specific portal name variants
    'c-reactive protein, quant':    ('1988-5', 'CRP [Mass/volume] in Serum or Plasma'),
    'c-reactive protein (crp), s':  ('1988-5', 'CRP [Mass/volume] in Serum or Plasma'),
    'crp, highly sensitive':        ('30522-7', 'hs-CRP [Mass/volume] in Serum or Plasma'),
    'sedimentation rate':           ('4537-7', 'ESR [Velocity] in Blood'),
    'sedimentation rate (esr)':     ('4537-7', 'ESR [Velocity] in Blood'),
    'erythrocyte sedimentation rate (esr)': ('4537-7', 'ESR [Velocity] in Blood'),
    'sedimentation rate (mb)':      ('4537-7', 'ESR [Velocity] in Blood'),
    'sedimentation rate, b':        ('4537-7', 'ESR [Velocity] in Blood'),
    'esr, manual':                  ('4537-7', 'ESR [Velocity] in Blood'),
    'mma':                          ('74708-0', 'Methylmalonic acid [Moles/volume] in Serum or Plasma'),
    'interleukin 6':                ('26881-3', 'Interleukin 6 [Mass/volume] in Serum or Plasma'),
    'tryptase':                     ('21370-2', 'Tryptase [Mass/volume] in Serum or Plasma'),

    # Coagulation — specific portal name variants
    'prothrombin (factor ii)':      ('5902-2', 'Prothrombin time (PT) in Blood'),
    'd-dimer':                      ('48065-7', 'Fibrin D-dimer FEU [Mass/volume] in Platelet poor plasma'),
    'lupus anticoagulant':          ('34515-7', 'Lupus anticoagulant in Platelet poor plasma'),
    'ptt-la screen':                ('3173-2', 'aPTT in Blood'),
    'drvvt screen':                 ('34515-7', 'Lupus anticoagulant in Platelet poor plasma'),
    'factor v (leiden) mutation':   ('21668-9', 'Factor V Leiden mutation analysis'),

    # ECG — specific portal name variants
    'qtc calculation(bezet)':       ('8636-3', 'QTc interval'),
    'q-t interval':                 ('8634-8', 'QT interval'),
    'qtcb':                         ('8636-3', 'QTc interval'),
    'qtc interval':                 ('8636-3', 'QTc interval'),
    'qrsd interval':                ('8633-0', 'QRS duration'),

    # Thyroid — specific portal name variants
    'a1c (glycohemoglobin)':        ('4548-4', 'HbA1c/Hemoglobin.total in Blood'),
    'hemoglobin a1c, b':            ('4548-4', 'HbA1c/Hemoglobin.total in Blood'),

    # Autoantibodies — specific portal name variants
    'anti-hbc antibody':            ('16933-4', 'Hepatitis B core Ab [Presence] in Serum'),
    'hep b surface antigen':        ('5195-3', 'Hepatitis B surface Ag [Presence] in Serum'),
    'hep b surf ag':                ('5195-3', 'Hepatitis B surface Ag [Presence] in Serum'),
    'hbs antigen, s':               ('5195-3', 'Hepatitis B surface Ag [Presence] in Serum'),
    'hep b surf ab, quant':         ('10900-9', 'Hepatitis B surface Ab [Units/volume] in Serum'),
    'hbs antibody, s':              ('10900-9', 'Hepatitis B surface Ab [Units/volume] in Serum'),
    'hbs antibody, quantitative, s': ('10900-9', 'Hepatitis B surface Ab [Units/volume] in Serum'),
    'hep b core ab total':          ('16933-4', 'Hepatitis B core Ab [Presence] in Serum'),
    'hep b core ab igm':            ('31204-1', 'Hepatitis B core IgM Ab [Presence] in Serum'),
    'hbc total ab, s':              ('16933-4', 'Hepatitis B core Ab [Presence] in Serum'),
    'hep c ab, qual':               ('16128-1', 'Hepatitis C Ab [Presence] in Serum'),
    'hcv ab, s':                    ('16128-1', 'Hepatitis C Ab [Presence] in Serum'),
    'hiv ag/ab combo':              ('56888-1', 'HIV 1+2 Ab+Ag [Presence] in Serum or Plasma by Immunoassay'),
    'anti-nuclear ab (ifa)':        ('5048-4', 'ANA Titer in Serum by Immunofluorescence'),
    'anti-nuclear antibodies':      ('8061-4', 'ANA in Serum'),
    'ana screen, ifa':              ('5048-4', 'ANA Titer in Serum by Immunofluorescence'),
    'antinuclear ab, s':            ('8061-4', 'ANA in Serum'),
    'antinuclear ab, hep-2 substrate, s': ('5048-4', 'ANA Titer in Serum by Immunofluorescence'),
    'dna (ds)':                     ('35659-2', 'Anti-dsDNA Ab [Units/volume] in Serum by Immunoassay'),
    'sm antibody':                  ('14319-8', 'Anti-Smith Ab [Units/volume] in Serum'),
    'sm ab, igg, s':                ('14319-8', 'Anti-Smith Ab [Units/volume] in Serum'),
    'sm/rnp':                       ('14318-0', 'Anti-RNP Ab [Units/volume] in Serum'),
    'rnp ab':                       ('14318-0', 'Anti-RNP Ab [Units/volume] in Serum'),
    'rnp ab, igg, s':               ('14318-0', 'Anti-RNP Ab [Units/volume] in Serum'),
    'chromatin ab':                 ('55932-8', 'Anti-chromatin Ab [Units/volume] in Serum'),
    'ssa':                          ('49881-6', 'Anti-SSA (Ro) Ab [Units/volume] in Serum'),
    'ssb':                          ('49880-8', 'Anti-SSB (La) Ab [Units/volume] in Serum'),
    'ssb antibody':                 ('49880-8', 'Anti-SSB (La) Ab [Units/volume] in Serum'),
    'ss-a/ro ab, igg, s':          ('49881-6', 'Anti-SSA (Ro) Ab [Units/volume] in Serum'),
    'ss-b/la ab, igg, s':          ('49880-8', 'Anti-SSB (La) Ab [Units/volume] in Serum'),
    'anti-ro 52 (ss-a) antibody':   ('49881-6', 'Anti-SSA (Ro) Ab [Units/volume] in Serum'),
    'anti-ro 60 (ss-a) antibody':   ('49881-6', 'Anti-SSA (Ro) Ab [Units/volume] in Serum'),
    'scl-70 ab':                    ('16598-2', 'Anti-Scl-70 Ab [Units/volume] in Serum'),
    'scl 70 ab, igg, s':           ('16598-2', 'Anti-Scl-70 Ab [Units/volume] in Serum'),
    'jo-1 ab':                      ('4560-9', 'Anti-Jo-1 Ab [Units/volume] in Serum'),
    'jo 1 ab, igg, s':             ('4560-9', 'Anti-Jo-1 Ab [Units/volume] in Serum'),
    'centromere b ab':              ('4484-2', 'Anti-centromere Ab [Units/volume] in Serum'),
    'ribosomal p ab':               ('11201-4', 'Anti-ribosomal P Ab [Units/volume] in Serum'),
    'cardiolipin ab (igg)':         ('3182-4', 'Anti-cardiolipin IgG Ab [GPL Units/volume] in Serum'),
    'cardiolipin ab (igm)':         ('3184-0', 'Anti-cardiolipin IgM Ab [MPL Units/volume] in Serum'),
    'cardiolipin ab (iga)':         ('3186-5', 'Anti-cardiolipin IgA Ab [APL Units/volume] in Serum'),
    'b2 glycoprotein i (igg) ab':   ('21198-1', 'Anti-beta-2-glycoprotein I IgG Ab [Units/volume] in Serum'),
    'b2 glycoprotein i (iga) ab':   ('53767-0', 'Anti-beta-2-glycoprotein I IgA Ab [Units/volume] in Serum'),
    'b2 glycoprotein i (igm) ab':   ('21190-8', 'Anti-beta-2-glycoprotein I IgM Ab [Units/volume] in Serum'),
    'myeloperoxidase abs':          ('33449-2', 'Anti-myeloperoxidase Ab [Units/volume] in Serum'),
    'myeloperoxidase ab, s':        ('33449-2', 'Anti-myeloperoxidase Ab [Units/volume] in Serum'),
    'proteinase-3 abs':             ('33582-0', 'Anti-proteinase 3 Ab [Units/volume] in Serum'),
    'proteinase 3 ab (pr3), s':     ('33582-0', 'Anti-proteinase 3 Ab [Units/volume] in Serum'),
    'cyclic citrullinated peptide ab, s': ('53027-9', 'Anti-CCP Ab [Units/volume] in Serum'),
    'rheumatoid factor, s':         ('11572-5', 'Rheumatoid factor [Units/volume] in Serum or Plasma'),
    'gastric parietal cell ab':     ('28390-3', 'Anti-parietal cell Ab [Titer] in Serum'),
    'intrinsic factor blocking ab': ('5140-4', 'Anti-intrinsic factor Ab [Presence] in Serum'),
    'lyme disease antibody total (eia)': ('16481-5', 'Borrelia burgdorferi Ab [Units/volume] in Serum'),

    # Immunology — additional
    'immunoglobulin a (iga), s':    ('2458-8', 'IgA [Mass/volume] in Serum'),
    'tissue transglutaminase ab, iga, s': ('31017-7', 'Tissue transglutaminase IgA Ab [Units/volume] in Serum'),
    'iga anti ttg antibody':        ('31017-7', 'Tissue transglutaminase IgA Ab [Units/volume] in Serum'),
    'iga anti ttg level':           ('31017-7', 'Tissue transglutaminase IgA Ab [Units/volume] in Serum'),
    'igg anti dgp antibody':        ('63557-3', 'Deamidated gliadin peptide IgG Ab [Units/volume] in Serum'),
    'igg anti dgp level':           ('63557-3', 'Deamidated gliadin peptide IgG Ab [Units/volume] in Serum'),
    'ttg ab,iga':                   ('31017-7', 'Tissue transglutaminase IgA Ab [Units/volume] in Serum'),
    'gliadin dgp ab iga':           ('63556-5', 'Deamidated gliadin peptide IgA Ab [Units/volume] in Serum'),
    'igg index':                     ('28637-7', 'IgG index in Cerebral spinal fluid and Serum'),

    # Infectious Disease — additional portal variants
    'covid-19 rna, rt-pcr/nucleic acid amplification': ('94500-6', 'SARS-CoV-2 RNA [Presence] in Respiratory specimen by NAA'),
    'rpr (monitor) w/refl titer':   ('20507-0', 'Reagin Ab [Presence] in Serum by RPR'),
    'cytomegalovirus antibody, igg': ('5124-8', 'CMV IgG Ab [Units/volume] in Serum'),
    'cytomegalovirus igm':          ('5126-3', 'CMV IgM Ab [Units/volume] in Serum'),
    'vzv igg result':               ('19162-7', 'VZV IgG Ab [Presence] in Serum'),
    'vzv igg level':                ('19162-7', 'VZV IgG Ab [Presence] in Serum'),
    'hsv-1 igg result':             ('32689-2', 'HSV-1 IgG Ab [Presence] in Serum'),
    'hsv-2 igg result':             ('32690-0', 'HSV-2 IgG Ab [Presence] in Serum'),
    'h pylori ag':                  ('70169-8', 'Helicobacter pylori Ag [Presence] in Stool'),
    'h.pylori ab,quant':            ('5184-7', 'Helicobacter pylori Ab [Units/volume] in Serum'),
    'helicobacter pylori elisa':    ('5184-7', 'Helicobacter pylori Ab [Units/volume] in Serum'),

    # POC — specific iStat variants
    'poc tco2, istat':              ('2028-9', 'CO2 [Moles/volume] in Serum or Plasma'),
    'poc hemoglobin, istat':        ('718-7', 'Hemoglobin [Mass/volume] in Blood'),
    'poc hematocrit, istat':        ('4544-3', 'Hematocrit [Volume Fraction] of Blood'),
    'poc creatinine, istat':        ('2160-0', 'Creatinine [Mass/volume] in Serum or Plasma'),
    'poc bun, istat':               ('3094-0', 'BUN [Mass/volume] in Serum or Plasma'),
    'poc glucose, istat':           ('2345-7', 'Glucose [Mass/volume] in Serum or Plasma'),
    'poc ionized calcium, istat':   ('1994-3', 'Ionized calcium [Moles/volume] in Serum or Plasma'),
    'poc cl, istat':                ('2075-0', 'Chloride [Moles/volume] in Serum or Plasma'),
    'poc k, istat':                 ('2823-3', 'Potassium [Moles/volume] in Serum or Plasma'),
    'poc na, istat':                ('2951-2', 'Sodium [Moles/volume] in Serum or Plasma'),
    'glucose by meter':             ('2345-7', 'Glucose [Mass/volume] in Serum or Plasma'),
    'glucose fingerstick':          ('2345-7', 'Glucose [Mass/volume] in Serum or Plasma'),
    'glucose, glucometer':          ('2345-7', 'Glucose [Mass/volume] in Serum or Plasma'),
    'glucose, istat':               ('2345-7', 'Glucose [Mass/volume] in Serum or Plasma'),

    # Cancer markers — additional
    'total psa':                    ('2857-1', 'PSA [Mass/volume] in Serum or Plasma'),
    'lipoprotein a':                ('10835-7', 'Lipoprotein(a) [Mass/volume] in Serum or Plasma'),
    'lipoprotein (a)':              ('10835-7', 'Lipoprotein(a) [Mass/volume] in Serum or Plasma'),
    'apolipoprotein b':             ('1884-6', 'Apolipoprotein B [Mass/volume] in Serum or Plasma'),

    # Endocrine — specific portal variants
    'renin activity, pl':           ('2915-7', 'Renin [Enzymatic activity/volume] in Plasma'),
    'dehydroepiandrosterone':       ('2191-5', 'DHEA-S [Mass/volume] in Serum or Plasma'),
    'angiotensin-1-converting enzyme': ('2021-4', 'ACE [Enzymatic activity/volume] in Serum'),

    # Vitamins — specific portal variants
    'thiamine pyrophosphate':       ('2995-9', 'Thiamine [Mass/volume] in Blood'),
    'thiamine (vitamin b1), wb':    ('2995-9', 'Thiamine [Mass/volume] in Blood'),
    'vitamin b6':                   ('2641-9', 'Pyridoxal phosphate [Moles/volume] in Plasma'),
    'pyridoxal 5-phosphate (plp), p': ('2641-9', 'Pyridoxal phosphate [Moles/volume] in Plasma'),
    'a-tocopherol, vitamin e':      ('14590-4', 'Alpha tocopherol [Mass/volume] in Serum or Plasma'),
    'vitamin b12 assay, s':         ('2132-9', 'Vitamin B12 [Mass/volume] in Serum or Plasma'),
    'zinc, plasma or serum':        ('2601-3', 'Zinc [Mass/volume] in Serum or Plasma'),
    'zinc, s':                      ('2601-3', 'Zinc [Mass/volume] in Serum or Plasma'),
    'copper, s':                    ('2507-2', 'Copper [Mass/volume] in Serum or Plasma'),
    'homocysteine':                 ('13965-9', 'Homocysteine [Moles/volume] in Serum or Plasma'),

    # Urinalysis — specific portal name variants
    'appearance, ua':               ('5767-9', 'Appearance of Urine'),
    'leukocytes, ua':               ('5799-2', 'Leukocyte esterase [Presence] in Urine'),
    'nitrite, ua':                  ('5802-4', 'Nitrite [Presence] in Urine by Test strip'),
    'protein, ua':                  ('5804-0', 'Protein [Mass/volume] in Urine by Test strip'),
    'glucose, ua':                  ('5792-7', 'Glucose [Mass/volume] in Urine by Test strip'),
    'ketones, ua':                  ('2514-8', 'Ketones [Presence] in Urine'),
    'bilirubin, ua':                ('5770-3', 'Bilirubin [Presence] in Urine by Test strip'),
    'hemoglobin, ua':               ('5794-3', 'Hemoglobin [Presence] in Urine by Test strip'),
    'rbcs, urine':                  ('13945-1', 'Erythrocytes [#/area] in Urine sediment'),
    'wbcs, ur':                     ('5821-4', 'Leukocytes [#/area] in Urine sediment'),
    'epithelial cells, ua':         ('5787-7', 'Epithelial cells [#/area] in Urine sediment'),
    'bacteria ,urine':              ('630-4', 'Bacteria [Presence] in Urine sediment'),
    'mucus, ua':                    ('8247-2', 'Mucus [Presence] in Urine sediment'),
    'specific gravity, ua':         ('5811-5', 'Specific gravity of Urine'),
    'ph, ua':                       ('2756-5', 'pH of Urine'),
    'urine nitrite':                ('5802-4', 'Nitrite [Presence] in Urine by Test strip'),
    'urine white blood cells':      ('5821-4', 'Leukocytes [#/area] in Urine sediment'),
    'ascorbic acid urine':          ('5794-3', 'Hemoglobin [Presence] in Urine by Test strip'),
    'total casts, urine':           ('5796-0', 'Hyaline casts [#/area] in Urine sediment'),
    'hyaline casts':                ('5796-0', 'Hyaline casts [#/area] in Urine sediment'),
    'porphobilinogen, rand ur':     ('2738-3', 'Porphobilinogen [Mass/time] in 24 hour Urine'),
    'delta ala, ur':                ('41551-3', 'Delta aminolevulinate [Mass/time] in 24 hour Urine'),

    # GI / Stool — specific tests
    'calprotectin quant':           ('83993-7', 'Calprotectin [Mass/volume] in Stool'),
    'calprotectin result':          ('83993-7', 'Calprotectin [Mass/volume] in Stool'),
    'calprotectin, fecal':          ('83993-7', 'Calprotectin [Mass/volume] in Stool'),
    'pancreatic elastase 1':        ('14651-4', 'Elastase 1 [Mass/volume] in Stool'),
    'lactoferrin, stool':           ('80656-2', 'Lactoferrin [Presence] in Stool'),
    'c. difficile toxin gene pcr':  ('54067-4', 'Clostridioides difficile toxin B gene [Presence] in Stool by NAA'),
    'shiga toxin':                  ('32777-5', 'Shiga toxin [Presence] in Stool'),
    'stool wbc':                    ('72163-9', 'Leukocytes [Presence] in Stool'),
    'stool rbc':                    ('14323-0', 'Erythrocytes [Presence] in Stool'),

    # Stool pathogen panel (GI PCR panel)
    'campylobacter species':        ('82300-5', 'Campylobacter [Presence] in Stool by NAA'),
    'salmonella enterica':          ('82301-3', 'Salmonella enterica [Presence] in Stool by NAA'),
    'shigella species':             ('82302-1', 'Shigella [Presence] in Stool by NAA'),
    'yersinia enterocolitica':      ('82303-9', 'Yersinia enterocolitica [Presence] in Stool by NAA'),
    'norovirus gi/gii':             ('54205-0', 'Norovirus [Presence] in Stool by NAA'),
    'rotavirus a':                  ('82196-7', 'Rotavirus A Ag [Presence] in Stool by Rapid immunoassay'),
    'adenovirus f 40/41':           ('82197-5', 'Adenovirus 40+41 [Presence] in Stool by NAA'),
    'astrovirus':                   ('82198-3', 'Astrovirus [Presence] in Stool by NAA'),
    'sapovirus':                    ('82199-1', 'Sapovirus [Presence] in Stool by NAA'),
    'giardia lamblia':              ('82204-9', 'Giardia lamblia [Presence] in Stool by NAA'),
    'giardia duodenalis':           ('82204-9', 'Giardia lamblia [Presence] in Stool by NAA'),
    'cryptosporidium species':      ('82205-6', 'Cryptosporidium [Presence] in Stool by NAA'),
    'cryptosporidium':              ('82205-6', 'Cryptosporidium [Presence] in Stool by NAA'),
    'entamoeba histolytica':        ('82206-4', 'Entamoeba histolytica [Presence] in Stool by NAA'),
    'cyclospora cayetanensis':      ('82207-2', 'Cyclospora cayetanensis [Presence] in Stool by NAA'),
    'vibrio species, not vibrio cholerae': ('82199-1', 'Vibrio [Presence] in Stool by NAA'),
    'vibrio cholerae':              ('82200-7', 'Vibrio cholerae [Presence] in Stool by NAA'),
    'plesiomonas shigelloides':     ('82202-3', 'Plesiomonas shigelloides [Presence] in Stool by NAA'),
    'shiga-like toxin-producing e. coli non 0157 strain': ('82208-0', 'STEC non-O157 [Presence] in Stool by NAA'),
    'shiga-like toxin-producing e. coli , 0157 strain': ('82209-8', 'STEC O157 [Presence] in Stool by NAA'),

    # CSF — specific portal name variants
    'albumin, csf':                 ('1746-7', 'Albumin [Mass/volume] in Cerebral spinal fluid'),
    'igg, csf':                     ('2464-6', 'IgG [Mass/volume] in Cerebral spinal fluid'),
    'glucose, csf':                 ('2342-4', 'Glucose [Mass/volume] in Cerebral spinal fluid'),
    'protein, total, csf':          ('2880-3', 'Protein [Mass/volume] in Cerebral spinal fluid'),
    'total nucleated cells, csf (wbc+ others)': ('26464-8', 'Leukocytes [#/volume] in Cerebral spinal fluid'),
    'rbcs, csf':                    ('26453-1', 'Erythrocytes [#/volume] in Cerebral spinal fluid'),

    # Echocardiography measurements
    'left ventricular ejection fraction': ('10230-1', 'Left ventricular Ejection fraction'),
    'ef simpson (bp)':              ('10230-1', 'Left ventricular Ejection fraction'),

    # Catecholamines — specific portal variants
    'total metanephrines':          ('2680-7', 'Metanephrines [Mass/volume] in Plasma'),
    '5-hiaa':                       ('11145-0', '5-HIAA [Mass/time] in 24 hour Urine'),
    'norepinephrine 24 hr':         ('2672-4', 'Norepinephrine [Mass/time] in 24 hour Urine'),
    'epinephrine 24 hr':            ('2232-7', 'Epinephrine [Mass/time] in 24 hour Urine'),
    'dopamine 24 hr':               ('2174-1', 'Dopamine [Mass/time] in 24 hour Urine'),
    'metanephrine':                 ('2668-2', 'Metanephrine [Mass/volume] in Plasma'),
    'normetanephrine 24 hr':        ('2669-0', 'Normetanephrine [Mass/volume] in Plasma'),

    # Porphyrins — specific portal variants
    'uroporphyrin iii, ur':         ('2739-1', 'Porphyrins [Mass/time] in 24 hour Urine'),
    'uroporphyrin i, ur':           ('2739-1', 'Porphyrins [Mass/time] in 24 hour Urine'),
    'coproporphyrin i, ur':         ('2739-1', 'Porphyrins [Mass/time] in 24 hour Urine'),
    'coproporphyrin iii, ur':       ('2739-1', 'Porphyrins [Mass/time] in 24 hour Urine'),
    'heptacarboxyporphyrin, ur':    ('2739-1', 'Porphyrins [Mass/time] in 24 hour Urine'),
    'hexacarboxyporphyrin, ur':     ('2739-1', 'Porphyrins [Mass/time] in 24 hour Urine'),
    'pentacarboxylporph,ur':        ('2739-1', 'Porphyrins [Mass/time] in 24 hour Urine'),
    'total porphyrin, ur':          ('2739-1', 'Porphyrins [Mass/time] in 24 hour Urine'),

    # Blood bank
    'abo/rh (automation)':          ('882-1', 'ABO+Rh group [Type] in Blood'),
    'abo/rh':                       ('882-1', 'ABO+Rh group [Type] in Blood'),
    'abo/rh(d)':                    ('882-1', 'ABO+Rh group [Type] in Blood'),

    # Miscellaneous
    'g6pd, quantitative':           ('32546-4', 'G6PD [Enzymatic activity/volume] in Red Blood Cells'),
    'alpha-galactosidase,s':        ('2079-2', 'Alpha-galactosidase [Enzymatic activity/volume] in Serum or Plasma'),
    'vegf, p':                      ('13316-5', 'VEGF [Mass/volume] in Serum or Plasma'),
    'viscosity':                    ('2646-8', 'Viscosity of Serum'),
    'lead, blood':                  ('5671-3', 'Lead [Mass/volume] in Blood'),
    'bun/creatinine ratio':         ('3097-3', 'BUN/Creatinine [Mass ratio] in Serum or Plasma'),
    'bun/creatinine ratio (external lab)': ('3097-3', 'BUN/Creatinine [Mass ratio] in Serum or Plasma'),
    'amylase':                      ('1798-8', 'Amylase [Enzymatic activity/volume] in Serum or Plasma'),
    'absolute immature granulocytes': ('53115-2', 'Immature granulocytes [#/volume] in Blood'),

    # ── ECG axes ──
    'p axis':                       ('8626-4', 'P wave axis'),
    'r axis':                       ('8632-2', 'QRS axis'),
    't axis':                       ('8638-9', 'T wave axis'),
    'calculated p axis':            ('8626-4', 'P wave axis'),
    'calculated r axis':            ('8632-2', 'QRS axis'),
    'calculated t axis':            ('8638-9', 'T wave axis'),
    'qrs axis':                     ('8632-2', 'QRS axis'),
    't wave axis':                  ('8638-9', 'T wave axis'),
    'rr':                           ('8637-1', 'Ventricular rate'),

    # ── Cryoglobulin / Cryofibrinogen ──
    'cryoglobulin':                 ('13085-6', 'Cryoglobulin [Presence] in Serum'),
    'cryoglobulin, s':              ('13085-6', 'Cryoglobulin [Presence] in Serum'),
    'cryoglobulin, serum - mayo':   ('13085-6', 'Cryoglobulin [Presence] in Serum'),
    'cryofibrinogen, plasma':       ('6124-2', 'Cryofibrinogen [Presence] in Plasma'),
    'cryofibrinogen, plasma - mayo': ('6124-2', 'Cryofibrinogen [Presence] in Plasma'),

    # ── QuantiFERON TB Gold sub-results ──
    'tb1 ag - nil':                 ('71774-4', 'IGRA TB1 Ag minus Nil [Units/volume] in Blood'),
    'tb2 ag - nil':                 ('71776-9', 'IGRA TB2 Ag minus Nil [Units/volume] in Blood'),
    'mitogen - nil':                ('71772-8', 'IGRA Mitogen minus Nil [Units/volume] in Blood'),

    # ── Echocardiography ──
    'lvedd':                        ('10080-0', 'LV internal diastolic dimension'),
    'lv edv':                       ('10081-8', 'LV end diastolic volume'),
    'lv wall thickness (ivsd)':     ('29430-2', 'IVS diastolic thickness'),
    'lv wall thickness (lvpwd)':    ('29432-8', 'LV posterior wall diastolic thickness'),

    # ── CSF differential ──
    '%neuts, csf':                  ('26461-4', 'Neutrophils/100 leukocytes in Cerebral spinal fluid'),
    '%lymphs, csf':                 ('26478-8', 'Lymphocytes/100 leukocytes in Cerebral spinal fluid'),
    '%mono, histiocytes, csf':      ('26485-3', 'Monocytes/100 leukocytes in Cerebral spinal fluid'),
    'oligoclonal bands, csf':       ('13504-6', 'Oligoclonal bands [Presence] in Cerebral spinal fluid'),
    'oligoclonal bands number, csf': ('47247-2', 'Oligoclonal bands [#] in Cerebral spinal fluid'),

    # ── Specific lab tests with 1-2 occurrences ──
    'cagg titer':                   ('58434-2', 'Cold agglutinin [Titer] in Serum'),
    '17-oh pregnenolone':           ('1668-3', '17-OH pregnenolone [Mass/volume] in Serum or Plasma'),
    'troponin i, legacy':           ('10839-9', 'Troponin I cardiac [Mass/volume] in Serum or Plasma'),
    'fecal fat, qualitative':       ('14575-5', 'Fat [Presence] in Stool'),
    'anti-myelin assoc glycop igg': ('40689-2', 'Anti-MAG Ab [Units/volume] in Serum'),
    'donor/recipient htlv ab screen': ('7931-9', 'HTLV I+II Ab [Presence] in Serum'),
    'g6pd, quantitative':           ('32546-4', 'G6PD [Enzymatic activity/volume] in Red Blood Cells'),
    'ova and parasites x3':         ('673-4', 'Ova and parasites identified in Stool'),
    'microscopic yeast':            ('680-9', 'Yeast [Presence] in Urine sediment'),
    'stool mucus':                  ('8247-2', 'Mucus [Presence] in Urine sediment'),
    'protein/creatinine ratio':     ('2890-2', 'Protein/Creatinine [Mass ratio] in Urine'),
    'erythrocyte porphorin':        ('28167-5', 'Porphyrins [Mass/volume] in Red Blood Cells'),
    'pyridoxic acid (pa), p':       ('32194-3', 'Pyridoxic acid [Moles/volume] in Plasma'),

    # ── Stool SCFA (short-chain fatty acids) ──
    'butyrate':                     ('1891-0', 'Butyrate [Moles/volume] in Stool'),
    'total scfa':                   ('2142-8', 'Short chain fatty acids [Moles/volume] in Stool'),
    '% acetate':                    ('1855-5', 'Acetate/Short chain fatty acids in Stool'),
    '% butyrate':                   ('1892-8', 'Butyrate/Short chain fatty acids in Stool'),
    '% propionate':                 ('2837-3', 'Propionate/Short chain fatty acids in Stool'),
    '% valerate':                   ('3046-0', 'Valerate/Short chain fatty acids in Stool'),

    # ── Breath test ──
    'greatest combined h2+ch4':     ('90441-7', 'Hydrogen+Methane [Volume fraction] in Exhaled gas'),

    # ── CSF additional ──
    '%degenert\'d cells, csf':      ('26463-0', 'Other cells/100 leukocytes in Cerebral spinal fluid'),
    'xanthochromia':                ('14417-0', 'Xanthochromia [Presence] in Cerebral spinal fluid'),
    '%basophils, csf':              ('26460-6', 'Basophils/100 leukocytes in Cerebral spinal fluid'),

    # ── RBC Morphology ──
    'schistocytes':                 ('800-3', 'Schistocytes [Presence] in Blood by Light microscopy'),
    'elliptocytes':                 ('10372-1', 'Elliptocytes [Presence] in Blood by Light microscopy'),
    'anisocytosis':                 ('702-1', 'Anisocytosis [Presence] in Blood by Light microscopy'),
    'ovalocytes':                   ('774-0', 'Ovalocytes [Presence] in Blood by Light microscopy'),
    'burr cells':                   ('7789-1', 'Burr cells [Presence] in Blood by Light microscopy'),
    'microcytes':                   ('738-8', 'Microcytes [Presence] in Blood by Light microscopy'),

    # ── Echocardiography ──
    'e/e\' ratio':                  ('77187-3', 'E/e\' ratio by Doppler echocardiography'),

    # ── Manual-entry test variants ──
    'hep c antibody (manual entry) see emr for details': ('16128-1', 'Hepatitis C Ab [Presence] in Serum'),
}


def normalize_name(name):
    """Normalize an observation display name for lookup."""
    if not name:
        return ''
    # Strip leading/trailing whitespace, lowercase
    name = name.strip().lower()
    # Remove trailing qualifiers that don't affect identity
    name = re.sub(r',\s*ser(?:um)?(?:/plas(?:ma)?)?$', '', name)
    return name


def lookup_loinc(display_name, code_text):
    """Look up LOINC code for a display name. Returns (code, display) or None."""
    # Try exact match on display name
    norm = normalize_name(display_name)
    if norm in LOINC_MAP:
        return LOINC_MAP[norm]

    # Try code text
    norm_text = normalize_name(code_text)
    if norm_text in LOINC_MAP:
        return LOINC_MAP[norm_text]

    # Try without common suffixes
    for suffix in [', ser/plas', ', serum', ', plasma', ', blood', ', s', ', p']:
        trimmed = norm.replace(suffix, '')
        if trimmed in LOINC_MAP:
            return LOINC_MAP[trimmed]

    return None


def has_loinc(obs):
    """Check if observation already has a LOINC code."""
    for coding in obs.get('code', {}).get('coding', []):
        if coding.get('system') == 'http://loinc.org' and coding.get('code'):
            return True
    return False


def assign_loinc(obs):
    """Assign LOINC code to an observation if missing. Modifies obs in place.
    Returns (loinc_code, display) if assigned, None otherwise.

    Use this function in ingestion pipelines:
        loinc = assign_loinc(observation)
        if loinc:
            print(f'Assigned LOINC {loinc[0]} to {observation}')
    """
    if has_loinc(obs):
        return None

    codings = obs.get('code', {}).get('coding', [])
    display = codings[0].get('display', '') if codings else ''
    code_text = obs.get('code', {}).get('text', '')

    result = lookup_loinc(display, code_text)
    if not result:
        return None

    loinc_code, loinc_display = result

    # Add LOINC coding
    if 'code' not in obs:
        obs['code'] = {}
    if 'coding' not in obs['code']:
        obs['code']['coding'] = []

    obs['code']['coding'].append({
        'system': 'http://loinc.org',
        'code': loinc_code,
        'display': loinc_display,
    })

    return result


def get_all_observations():
    """Fetch all observations from HAPI."""
    all_obs = []
    url = f'{HAPI_BASE}/Observation?_count=500&_sort=-date'
    page = 0
    while url:
        resp = requests.get(url)
        if resp.status_code != 200:
            break
        bundle = resp.json()
        all_obs.extend(entry['resource'] for entry in bundle.get('entry', []))
        page += 1
        if page % 10 == 0:
            print(f'  Fetched {len(all_obs)} observations...')
        url = None
        for link in bundle.get('link', []):
            if link['relation'] == 'next':
                url = link['url']
                break
    return all_obs


def main():
    print(f'LOINC Mapper — {len(LOINC_MAP)} display names mapped')
    if DRY_RUN:
        print('(DRY RUN)')
    print()

    print('Fetching all observations...')
    observations = get_all_observations()
    print(f'Total: {len(observations)}')

    # Analyze
    already_has = 0
    assigned = 0
    not_found = 0
    failed = 0
    unmatched_names = Counter()

    for obs in observations:
        if has_loinc(obs):
            already_has += 1
            continue

        codings = obs.get('code', {}).get('coding', [])
        display = codings[0].get('display', '') if codings else ''
        code_text = obs.get('code', {}).get('text', '')
        name = display or code_text

        result = lookup_loinc(display, code_text)

        if not result:
            not_found += 1
            norm = normalize_name(name)
            if norm:
                unmatched_names[norm] += 1
            continue

        if STATS_ONLY:
            assigned += 1
            continue

        # Apply the LOINC code
        assign_loinc(obs)

        # Record provenance: which mapper version, and the original raw name
        # so corrections can be traced back to the source.
        add_provenance_tag(obs,
                           f'mapper:v{MAPPER_VERSION}',
                           f'LOINC Mapper v{MAPPER_VERSION}')
        if name:
            add_provenance_tag(obs,
                               f'raw-name:{name}',
                               name)

        # Legacy tag for backward compatibility
        if 'meta' not in obs:
            obs['meta'] = {}
        if 'tag' not in obs['meta']:
            obs['meta']['tag'] = []
        tag_codes = [t.get('code') for t in obs['meta']['tag']]
        if 'loinc-mapper-v1' not in tag_codes:
            obs['meta']['tag'].append({
                'system': 'http://example.org/source',
                'code': 'loinc-mapper-v1',
                'display': 'LOINC Code Mapper v1'
            })

        if DRY_RUN:
            assigned += 1
            continue

        # PUT back
        url = f'{HAPI_BASE}/Observation/{obs["id"]}'
        resp = requests.put(url, json=obs, headers={'Content-Type': 'application/fhir+json'})
        if resp.status_code in (200, 201):
            assigned += 1
        else:
            print(f'  FAIL {obs["id"]}: {resp.status_code}')
            failed += 1

    prefix = '[DRY-RUN] ' if DRY_RUN else '[STATS] ' if STATS_ONLY else ''
    print(f'\n{prefix}=== RESULTS ===')
    print(f'Already had LOINC: {already_has}')
    print(f'LOINC assigned: {assigned}')
    print(f'No match found: {not_found}')
    if failed:
        print(f'Failed: {failed}')
    print(f'Coverage: {(already_has + assigned) / len(observations) * 100:.1f}%')

    if unmatched_names:
        print(f'\n=== ALL UNMATCHED NAMES ===')
        for name, count in unmatched_names.most_common():
            print(f'  {count:4d}  {name}')


if __name__ == '__main__':
    main()
