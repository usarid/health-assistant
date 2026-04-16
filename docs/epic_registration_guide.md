# Epic FHIR Patient App Registration Guide

**Why this matters**: The approval process is slow (2-3 months for production). Start now to get in the queue.

---

## 1. What This Gets You

**Epic FHIR API** lets you pull real-time EHR data directly from Epic-using health systems without requiring manual MyChart exports:
- Access to ~1,177 FHIR R4 endpoints across US health systems
- Patient demographics, medications, conditions, observations (vitals, labs)
- DiagnosticReports, DocumentReference, Immunizations
- **Faster than manual export**: Programmatic, real-time, patient-consented access
- Alternative to building custom integrations per health system

**vs. Manual MyChart Export**: MyChart is restricted to one patient, one system, one-time download. FHIR enables recurring, multi-system access at scale.

---

## 2. Step-by-Step Registration

### Phase 1: Create Developer Account (5 min)

1. Go to **https://fhir.epic.com/**
2. Click **"Developers"** or **"Sign Up"** (top-right)
3. Create account using your email (must be your personal Epic Vendor Services account email)
   - If you don't have Vendor Services credentials, register at https://open.epic.com first
4. Verify email address from confirmation link
5. **You now have an Epic developer account**

### Phase 2: Create App Record (10 min)

1. Log in to **https://fhir.epic.com/Developer/Apps** (or after login, click "My Apps")
2. Click **"Create App"** or **"Build Apps"**
3. Fill in required fields:
   - **App Name**: e.g., "PersonalHealthVault" (descriptive, patient-facing friendly)
   - **App Description**: Briefly explain what it does (e.g., "Patient app for viewing consolidated EHR data")
   - **Vendor Name**: Your organization name
4. Select **Launch Type**: `Patient-Facing`
5. Select **OAuth 2.0 / SMART on FHIR** as authentication method
6. **Redirect URI**: For local dev, use `http://localhost:3000/callback` (HTTPS required for production)
   - You can add multiple URIs (dev/staging/prod) later
7. Click **"Save"** — you now have an app record

### Phase 3: Request FHIR Scopes (within same form or section)

1. In the app record, go to **"Permissions"** or **"API Access"** tab
2. Select scopes you need (see Section 3 below)
3. **Do NOT request write scopes** for first submission (read-only gets faster approval)
4. Click **"Save"**

### Phase 4: Submit for Approval (1 min)

1. In app settings, look for **"Submit for Review"** or **"Go to Production"** button
2. You may be prompted to:
   - Confirm your security posture (basic: "app uses OAuth 2.0, encrypts data in transit")
   - Confirm clinical safety mitigation (basic: "app is read-only, does not modify records")
   - Confirm support model (e.g., "Support provided in-house")
3. Click **"Submit"**
4. **Approval typically: 2-3 weeks (not days)**
   - Epic reviews for security, clinical appropriateness, data handling
   - You may receive follow-up questions via email

---

## 3. Recommended API Scopes for Personal Health Record App

**Start with these read-only scopes** (highest approval likelihood):

| Scope | FHIR Resource | What You Get |
|-------|--------------|-------------|
| `patient/Patient.read` | Patient | Name, DOB, demographics |
| `patient/Condition.read` | Condition | Problem list, diagnoses |
| `patient/MedicationRequest.read` | MedicationRequest | Medications, active & inactive |
| `patient/Observation.read` | Observation | Vitals (BP, heart rate), labs (glucose, etc.) |
| `patient/Immunization.read` | Immunization | Vaccine history |
| `patient/AllergyIntolerance.read` | AllergyIntolerance | Allergies, intolerances |
| `patient/DiagnosticReport.read` | DiagnosticReport | Lab/imaging reports |
| `patient/DocumentReference.read` | DocumentReference | Clinical documents (notes, PDFs) |

**Why these?** They cover ~95% of personal health record use cases and carry lowest risk (read-only, no clinical modifications).

**Do NOT request** (first submission):
- Write scopes (`MedicationRequest.write`, `Communication.write`) — need additional clinical review
- `patient/Appointment.write` — scheduling requires additional safety review
- `user/*` scopes — admin-level, requires organizational agreement

---

## 4. App Configuration

### Suggested Settings

| Setting | Recommendation |
|---------|---|
| **App Name** | "PersonalHealthVault", "MyEHR", "HealthBridge" (keep simple, patient-friendly) |
| **Contact Email** | Your personal Epic account email (Uri's email) |
| **Redirect URI (Dev)** | `http://localhost:3000/callback` (start here for testing) |
| **Redirect URI (Staging)** | `https://staging.yourapp.com/callback` |
| **Redirect URI (Prod)** | `https://yourapp.com/callback` (HTTPS mandatory) |
| **Data Retention** | State your policy (e.g., "Data cached 24 hours, not persisted") |
| **De-identification** | Specify if you de-identify or pseudonymize data |

### Key Points

- **Redirect URI**: Epic will redirect here after OAuth login. Match your app exactly (including `http://` vs `https://`)
- **Multiple URIs**: You can add dev/staging/prod URIs now or update later
- **HTTPS**: Production URIs must use HTTPS; localhost is exception for dev

---

## 5. Testing Against Epic Sandbox

### Sandbox Credentials & Setup

1. In your app record, Epic provides a **Sandbox Patient ID** and **Sandbox Credentials**
2. Use these to test FHIR calls before going live:

```bash
# Example: Test FHIR read with sandbox endpoint
curl -H "Authorization: Bearer {sandbox_token}" \
  https://fhirtest.epic.com/interconnect-fhir-oauth/api/FHIR/R4/Patient/{patient_id}
```

### What to Test

- OAuth 2.0 redirect flow (login → consent → token)
- FHIR GET requests for each resource (Patient, Condition, Observation, etc.)
- Token refresh (if your app uses long-lived tokens)
- Error handling (missing data, invalid scopes, etc.)

### Sandbox Limitations

- **Sample data only** — no real patient data
- **Not for performance testing** — not production-grade
- Useful for: API familiarity, scope validation, OAuth flow verification

### Access Sandbox

- Some health systems provide separate sandbox endpoints
- Epic's public sandbox: Available via LaunchPad (https://open.epic.com/launchpad)

---

## 6. Production Approval Process

### Timeline

| Phase | Duration | What Happens |
|-------|----------|---|
| **Submission** | Immediate | You submit app record + scopes |
| **Initial Review** | 3-5 business days | Epic checks for completeness, security red flags |
| **Detailed Review** | 1-2 weeks | Security, clinical safety, data handling review |
| **Approval/Feedback** | 1-3 weeks total | Epic approves or requests changes |
| **After Approval** | Variable | Individual health systems may have own reviews (additional 2-4 weeks) |

**Total: 2-3 months typical** for first-time apps (can be 4+ months if Epic requests changes).

### What Epic Reviews

- **Security**: OAuth 2.0 implementation, token handling, HTTPS enforcement
- **Clinical Safety**: Read-only vs. write, data modification safeguards
- **Data Privacy**: How data is stored, encrypted, retained, deleted
- **Support Model**: Who supports the app? SLA? Bug fixes?

### Common Requests for Changes

- "Add HTTPS to redirect URI" — fix and resubmit
- "Clarify data retention policy" — update app description
- "Remove write scopes if not used" — simplify scope list

### After Approval

1. Epic emails you an **"approved" notification** with production endpoint URLs
2. You're now authorized to request access from individual health systems
3. **Health systems still need to opt-in**: UCSF, Stanford, Mayo, etc. may have their own approval process (1-4 weeks each)
4. Once health system approves, you get their **OAuth authorization endpoint** and **FHIR endpoint URL**

---

## 7. Production FHIR Endpoint URLs

### Where to Find Them

1. **Most comprehensive**: https://open.epic.com/MyApps/Endpoints
   - Download as FHIR Bundle (R4), JSON, or User-access Brands Bundle
   - Includes 1,177+ Epic endpoints across US health systems
   - Includes UCSF Health, Stanford Healthcare, Mayo Clinic, Sutter Health (if available on Epic)

2. **Download formats**:
   - **FHIR Bundle**: Includes endpoint URLs + organization branding
   - **User-access Brands Bundle**: Recommended for new apps (includes facility addresses, logos)

### For Specific Health Systems

Once your app is production-approved, you request access per health system:

| Health System | Epic? | Next Step |
|---------------|-------|-----------|
| **UCSF Health** | Likely | Visit open.epic.com/Endpoints, search "UCSF" → get FHIR URL |
| **Stanford Healthcare** | Likely | Same process |
| **Mayo Clinic** | Likely | Same process |
| **Sutter Health** | Likely | Same process |

**Action**: After Epic approves your app, you'll receive instructions to request access per health system through Epic's app management portal.

### Example FHIR Endpoint URL (Format)

```
https://fhir.{healthsystem}.com/api/FHIR/R4
```

You'll get the exact URL from Epic's endpoints list or directly from the health system.

---

## 8. Alternative: TEFCA / Individual Access Services (IAS)

### What is TEFCA IAS?

**TEFCA** (Trusted Exchange Framework and Common Agreement) is a new nationwide interoperability standard that lets patients access records from *any* provider on the network (not just one Epic system).

**Individual Access Services (IAS)** on TEFCA: Patient-facing apps that consolidate records across multiple providers + health systems using a single integration.

### Pros vs. Epic-only FHIR

| Aspect | Epic FHIR | TEFCA IAS |
|--------|-----------|-----------|
| **Health Systems** | ~1,177 Epic endpoints | ~800+ apps, 1000s+ health systems (growing) |
| **Setup Time** | 2-3 months per app | Similar (new, still maturing) |
| **Non-Epic Systems** | Must integrate separately | Included in TEFCA network |
| **Patient Experience** | Patient logs in per health system | Single sign-on across all systems |
| **Status** | Production, mature | Production, growing (2025-2026) |

### When to Use TEFCA IAS Instead

- If you need data from **non-Epic systems** (Cerner, Athena, etc.) in same app
- If you want **nationwide coverage** without per-system integrations
- If your patients see providers across multiple EHR vendors

### When to Stick with Epic FHIR

- If your users are **primarily Epic patients** (56% of US)
- If you need **faster time-to-patient** (Epic mature, TEFCA still expanding)
- If you want **guaranteed endpoint stability** (Epic well-tested)

### Recommendation for Now

**Start with Epic FHIR** (this guide). It's production-ready, mature, and covers 56% of US patients. TEFCA IAS is complementary — you can add it later after Epic is live.

---

## 9. Timeline for Uri

**Today (NOW)**:
- [ ] Register at https://fhir.epic.com (5 min)
- [ ] Create app record, set name/redirect URI (5 min)
- [ ] Select read-only scopes (Patient, Condition, Medication, Observation, etc.) (5 min)
- [ ] Submit for Epic approval (1 min)
- [ ] **Result: You're in the queue. Expect approval in 2-3 weeks.**

**Week 1-2**:
- Watch for email from Epic (approval or questions)
- If questions, respond within 2-3 days

**Week 2-3**:
- Epic approves (likely)
- You receive production endpoint URLs + authorization endpoints

**Week 3+**:
- Request access from individual health systems (UCSF, Stanford, Mayo, Sutter)
- Each system: 1-4 weeks additional review
- Test with their sandbox endpoints
- Go live per health system

---

## 10. Key Contacts & Resources

| Resource | URL |
|----------|-----|
| Epic Developer Portal | https://fhir.epic.com/ |
| Epic Endpoints Directory | https://open.epic.com/MyApps/Endpoints |
| Epic App Management | https://fhir.epic.com/Developer/Apps |
| Epic Documentation | https://fhir.epic.com/Documentation |
| Epic FAQ | https://fhir.epic.com/FAQ |
| FHIR Specs | https://www.hl7.org/fhir/R4/ |
| Epic TEFCA IAS Info | https://open.epic.com/Home/Interoperate/TEFCA/IAS |

---

## Checklist: Before You Submit

- [ ] App name is patient-friendly (not technical)
- [ ] Redirect URI is correct (match exactly, including http/https)
- [ ] You've selected only read-only scopes
- [ ] You have a brief description of what the app does
- [ ] You've confirmed your vendor/organization name
- [ ] You've checked the "Patient-Facing" launch type

**Once submitted, you're done. Approval happens async — check email weekly.**

