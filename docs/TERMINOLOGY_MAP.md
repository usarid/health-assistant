# FHIR-to-UI Terminology Map

This dictionary maps FHIR R4 resource types and fields to patient-friendly
labels used in the Personal Health Vault web UI and in Epic MyChart.

Last updated: 2026-04-02

## Resource Types

| FHIR Resource | Vault UI Label | Epic MyChart Label | Notes |
|---|---|---|---|
| Observation | Test Result | Test Results (individual line) | A single measured value (e.g., Sodium: 144) |
| DiagnosticReport | Test Panel | Test Results (panel grouping) | Groups observations into an order (e.g., "Comprehensive Metabolic Panel") |
| Encounter | Visit | Visits / Appointments | An in-person, virtual, or ED visit |
| DocumentReference | Document | Health Record → various | Clinical notes, letters, imaging reports |
| Communication | Message | Messages | MyChart portal messages with care team |
| MedicationRequest | Medication | Medications | Active and historical prescriptions |
| Condition | Condition | Health Record → Conditions | Diagnoses and problem list entries |
| Procedure | Procedure | Health Record → Procedures | Surgeries, biopsies, etc. |
| Immunization | Immunization | Immunizations | Vaccine records |
| AllergyIntolerance | Allergy | Health Record → Allergies | Drug and environmental allergies |

## Status Bar Abbreviations

| Vault Status Bar | Meaning |
|---|---|
| Tests | Observation count |
| Panels | DiagnosticReport count |
| Rx | MedicationRequest count |
| Conditions | Condition count |
| Docs | DocumentReference count |
| Messages | Communication count |
| Visits | Encounter count |

## Observation Fields

| FHIR Field | UI Label | Notes |
|---|---|---|
| code.coding[].display | Name | Test name (e.g., "Hemoglobin") |
| valueQuantity.value | Value | Numeric result |
| valueQuantity.comparator | (prefix) | "<", ">", etc. shown before value |
| valueQuantity.unit | Unit | Shown after value |
| valueString | Value | Text result (e.g., "Negative") |
| referenceRange | Normal Range | Epic shows as "Normal value:" |
| interpretation | Flag | H/L/N/HH/LL/NEG/POS → Epic shows "Abnormal" badge |
| effectiveDateTime | Date | Collection/result date |
| performer | Ordered By | Provider who ordered the test |
| meta.tag[source] | Source | Which portal/system the data came from |

## Interpretation Codes

| FHIR Code | UI Display | Epic Display |
|---|---|---|
| N | Normal | (no badge) |
| H | High | Abnormal (yellow) |
| HH | Critical High | Abnormal (red) |
| L | Low | Abnormal (yellow) |
| LL | Critical Low | Abnormal (red) |
| NEG | Negative | (varies) |
| POS | Positive | Abnormal |
