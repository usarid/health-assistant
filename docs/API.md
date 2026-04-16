# FHIR R4 Server API Documentation

## Overview

This document describes the REST API endpoints for the FHIR R4 server implementation. All endpoints follow the FHIR REST specification and return FHIR-compliant JSON responses.

**Base URL**: `http://localhost:8080/fhir/R4`

**Content-Type**: `application/fhir+json; charset=utf-8`

## HTTP Methods

| Method | Operation | Endpoint |
|--------|-----------|----------|
| GET | Search/List | `/[ResourceType]` |
| GET | Read | `/[ResourceType]/[id]` |
| POST | Create | `/[ResourceType]` |
| PUT | Update | `/[ResourceType]/[id]` |

## Patient Resource

### Create Patient

Create a new patient record with demographic information.

**Request**
```
POST /fhir/R4/Patient
Content-Type: application/fhir+json; charset=utf-8

{
  "resourceType": "Patient",
  "name": [
    {
      "use": "official",
      "given": ["John"],
      "family": "Doe"
    }
  ],
  "gender": "male",
  "birthDate": "1990-01-15",
  "identifier": [
    {
      "type": {
        "coding": [
          {
            "system": "http://terminology.hl7.org/CodeSystem/v2-0203",
            "code": "MR"
          }
        ]
      },
      "value": "MRN123456"
    }
  ],
  "telecom": [
    {
      "system": "phone",
      "value": "+1-555-0100"
    },
    {
      "system": "email",
      "value": "john.doe@example.com"
    }
  ],
  "address": [
    {
      "use": "home",
      "line": ["123 Main Street"],
      "city": "Springfield",
      "state": "IL",
      "postalCode": "62701",
      "country": "USA"
    }
  ]
}
```

**Response** (Status 201 Created)
```json
{
  "resourceType": "Patient",
  "id": "123e4567-e89b-12d3-a456-426614174000",
  "meta": {
    "versionId": "1",
    "lastUpdated": "2026-03-30T14:30:00+00:00"
  },
  "name": [
    {
      "use": "official",
      "given": ["John"],
      "family": "Doe"
    }
  ],
  "gender": "male",
  "birthDate": "1990-01-15",
  "identifier": [
    {
      "type": {
        "coding": [
          {
            "system": "http://terminology.hl7.org/CodeSystem/v2-0203",
            "code": "MR"
          }
        ]
      },
      "value": "MRN123456"
    }
  ]
}
```

**Response Headers**
```
Location: /fhir/R4/Patient/123e4567-e89b-12d3-a456-426614174000
Content-Type: application/fhir+json; charset=utf-8
```

### Get Patient

Retrieve a specific patient by ID.

**Request**
```
GET /fhir/R4/Patient/123e4567-e89b-12d3-a456-426614174000
```

**Response** (Status 200 OK)
```json
{
  "resourceType": "Patient",
  "id": "123e4567-e89b-12d3-a456-426614174000",
  "meta": {
    "versionId": "1",
    "lastUpdated": "2026-03-30T14:30:00+00:00"
  },
  "name": [
    {
      "use": "official",
      "given": ["John"],
      "family": "Doe"
    }
  ],
  "gender": "male",
  "birthDate": "1990-01-15"
}
```

**Error Response** (Status 404 Not Found)
```json
{
  "resourceType": "OperationOutcome",
  "issue": [
    {
      "severity": "error",
      "code": "processing",
      "diagnostics": "Patient/nonexistent not found"
    }
  ]
}
```

### Search Patients

Search for patients using various criteria.

**Request Examples**

Search by name:
```
GET /fhir/R4/Patient?name=John
```

Search by identifier (medical record number):
```
GET /fhir/R4/Patient?identifier=MRN123456
```

Search all patients:
```
GET /fhir/R4/Patient
```

**Response** (Status 200 OK)
```json
{
  "resourceType": "Bundle",
  "type": "searchset",
  "total": 1,
  "entry": [
    {
      "fullUrl": "/fhir/R4/Patient/123e4567-e89b-12d3-a456-426614174000",
      "resource": {
        "resourceType": "Patient",
        "id": "123e4567-e89b-12d3-a456-426614174000",
        "meta": {
          "versionId": "1",
          "lastUpdated": "2026-03-30T14:30:00+00:00"
        },
        "name": [
          {
            "use": "official",
            "given": ["John"],
            "family": "Doe"
          }
        ],
        "gender": "male",
        "birthDate": "1990-01-15"
      }
    }
  ]
}
```

**Search Parameters**

| Parameter | Description | Example |
|-----------|-------------|---------|
| name | Search by given or family name | `?name=John` |
| identifier | Search by identifier value | `?identifier=MRN123456` |

### Update Patient

Update an existing patient record.

**Request**
```
PUT /fhir/R4/Patient/123e4567-e89b-12d3-a456-426614174000
Content-Type: application/fhir+json; charset=utf-8

{
  "resourceType": "Patient",
  "id": "123e4567-e89b-12d3-a456-426614174000",
  "name": [
    {
      "use": "official",
      "given": ["John", "Michael"],
      "family": "Doe-Smith"
    }
  ],
  "gender": "male",
  "birthDate": "1990-01-15"
}
```

**Response** (Status 200 OK)
```json
{
  "resourceType": "Patient",
  "id": "123e4567-e89b-12d3-a456-426614174000",
  "meta": {
    "versionId": "2",
    "lastUpdated": "2026-03-30T15:45:00+00:00"
  },
  "name": [
    {
      "use": "official",
      "given": ["John", "Michael"],
      "family": "Doe-Smith"
    }
  ],
  "gender": "male",
  "birthDate": "1990-01-15"
}
```

Note: Updates create new versions. The `versionId` is incremented automatically.

## Observation Resource

### Create Observation

Create a clinical observation (measurement, finding, or vital sign).

**Request**
```
POST /fhir/R4/Observation
Content-Type: application/fhir+json; charset=utf-8

{
  "resourceType": "Observation",
  "status": "final",
  "code": {
    "coding": [
      {
        "system": "http://loinc.org",
        "code": "8480-6",
        "display": "Systolic blood pressure"
      }
    ]
  },
  "subject": {
    "reference": "Patient/123e4567-e89b-12d3-a456-426614174000",
    "type": "Patient"
  },
  "effectiveDateTime": "2026-03-30T14:30:00+00:00",
  "valueQuantity": {
    "value": 120,
    "unit": "mmHg",
    "system": "http://unitsofmeasure.org",
    "code": "mm[Hg]"
  }
}
```

**Response** (Status 201 Created)
```json
{
  "resourceType": "Observation",
  "id": "987f6543-e89b-12d3-a456-426614174999",
  "meta": {
    "versionId": "1",
    "lastUpdated": "2026-03-30T14:30:00+00:00"
  },
  "status": "final",
  "code": {
    "coding": [
      {
        "system": "http://loinc.org",
        "code": "8480-6",
        "display": "Systolic blood pressure"
      }
    ]
  },
  "subject": {
    "reference": "Patient/123e4567-e89b-12d3-a456-426614174000",
    "type": "Patient"
  },
  "effectiveDateTime": "2026-03-30T14:30:00+00:00",
  "valueQuantity": {
    "value": 120,
    "unit": "mmHg",
    "system": "http://unitsofmeasure.org",
    "code": "mm[Hg]"
  }
}
```

### Get Observation

Retrieve a specific observation by ID.

**Request**
```
GET /fhir/R4/Observation/987f6543-e89b-12d3-a456-426614174999
```

**Response** (Status 200 OK)
```json
{
  "resourceType": "Observation",
  "id": "987f6543-e89b-12d3-a456-426614174999",
  "meta": {
    "versionId": "1",
    "lastUpdated": "2026-03-30T14:30:00+00:00"
  },
  "status": "final",
  "code": {
    "coding": [
      {
        "system": "http://loinc.org",
        "code": "8480-6",
        "display": "Systolic blood pressure"
      }
    ]
  },
  "subject": {
    "reference": "Patient/123e4567-e89b-12d3-a456-426614174000",
    "type": "Patient"
  },
  "effectiveDateTime": "2026-03-30T14:30:00+00:00",
  "valueQuantity": {
    "value": 120,
    "unit": "mmHg",
    "system": "http://unitsofmeasure.org",
    "code": "mm[Hg]"
  }
}
```

### Search Observations

Search for observations using various criteria.

**Request Examples**

Search by subject (patient):
```
GET /fhir/R4/Observation?subject=Patient/123e4567-e89b-12d3-a456-426614174000
```

Search all observations:
```
GET /fhir/R4/Observation
```

**Response** (Status 200 OK)
```json
{
  "resourceType": "Bundle",
  "type": "searchset",
  "total": 3,
  "entry": [
    {
      "fullUrl": "/fhir/R4/Observation/987f6543-e89b-12d3-a456-426614174999",
      "resource": {
        "resourceType": "Observation",
        "id": "987f6543-e89b-12d3-a456-426614174999",
        "meta": {
          "versionId": "1",
          "lastUpdated": "2026-03-30T14:30:00+00:00"
        },
        "status": "final",
        "code": {
          "coding": [
            {
              "system": "http://loinc.org",
              "code": "8480-6",
              "display": "Systolic blood pressure"
            }
          ]
        },
        "subject": {
          "reference": "Patient/123e4567-e89b-12d3-a456-426614174000",
          "type": "Patient"
        },
        "valueQuantity": {
          "value": 120,
          "unit": "mmHg"
        }
      }
    }
  ]
}
```

**Search Parameters**

| Parameter | Description | Example |
|-----------|-------------|---------|
| subject | Search by subject (patient reference) | `?subject=Patient/123e4567...` |

### Update Observation

Update an existing observation.

**Request**
```
PUT /fhir/R4/Observation/987f6543-e89b-12d3-a456-426614174999
Content-Type: application/fhir+json; charset=utf-8

{
  "resourceType": "Observation",
  "id": "987f6543-e89b-12d3-a456-426614174999",
  "status": "amended",
  "code": {
    "coding": [
      {
        "system": "http://loinc.org",
        "code": "8480-6",
        "display": "Systolic blood pressure"
      }
    ]
  },
  "subject": {
    "reference": "Patient/123e4567-e89b-12d3-a456-426614174000"
  },
  "effectiveDateTime": "2026-03-30T14:30:00+00:00",
  "valueQuantity": {
    "value": 118,
    "unit": "mmHg"
  }
}
```

**Response** (Status 200 OK)
```json
{
  "resourceType": "Observation",
  "id": "987f6543-e89b-12d3-a456-426614174999",
  "meta": {
    "versionId": "2",
    "lastUpdated": "2026-03-30T16:00:00+00:00"
  },
  "status": "amended",
  "code": {
    "coding": [
      {
        "system": "http://loinc.org",
        "code": "8480-6",
        "display": "Systolic blood pressure"
      }
    ]
  },
  "subject": {
    "reference": "Patient/123e4567-e89b-12d3-a456-426614174000"
  },
  "effectiveDateTime": "2026-03-30T14:30:00+00:00",
  "valueQuantity": {
    "value": 118,
    "unit": "mmHg"
  }
}
```

## Common Observation Codes (LOINC)

| Code | Description | Units |
|------|-------------|-------|
| 8480-6 | Systolic blood pressure | mmHg |
| 8462-4 | Diastolic blood pressure | mmHg |
| 8310-5 | Body temperature | Cel, [degF] |
| 8867-4 | Heart rate | /min |
| 3141-9 | Body weight | kg, lbs |
| 3137-7 | Body height | cm, [in_i] |
| 2085-9 | Cholesterol | mg/dL |
| 2345-7 | Glucose | mg/dL |
| 718-7 | Hemoglobin | g/dL |

See [LOINC](https://loinc.org) for complete code system.

## Response Codes

| Code | Description |
|------|-------------|
| 200 | OK - Successful read or update |
| 201 | Created - Resource successfully created |
| 400 | Bad Request - Invalid input or malformed JSON |
| 404 | Not Found - Resource does not exist |
| 500 | Internal Server Error - Server processing error |

## Error Handling

All errors return an `OperationOutcome` resource:

```json
{
  "resourceType": "OperationOutcome",
  "issue": [
    {
      "severity": "error",
      "code": "processing",
      "diagnostics": "Detailed error message"
    }
  ]
}
```

**Possible Error Scenarios**

1. **Missing resourceType**:
   ```
   "diagnostics": "Resource type mismatch"
   ```

2. **Invalid JSON**:
   ```
   "diagnostics": "Invalid JSON"
   ```

3. **Resource not found**:
   ```
   "diagnostics": "Patient/nonexistent-id not found"
   ```

4. **Server error**:
   ```
   "diagnostics": "Database connection error"
   ```

## Examples Using curl

### Create a Patient

```bash
curl -X POST http://localhost:8080/fhir/R4/Patient \
  -H "Content-Type: application/fhir+json; charset=utf-8" \
  -d '{
    "resourceType": "Patient",
    "name": [{
      "use": "official",
      "given": ["Jane"],
      "family": "Smith"
    }],
    "gender": "female",
    "birthDate": "1985-05-20"
  }'
```

### Get a Patient

```bash
curl http://localhost:8080/fhir/R4/Patient/[patient-id]
```

### Search Patients

```bash
curl "http://localhost:8080/fhir/R4/Patient?name=Jane"
```

### Create an Observation

```bash
curl -X POST http://localhost:8080/fhir/R4/Observation \
  -H "Content-Type: application/fhir+json; charset=utf-8" \
  -d '{
    "resourceType": "Observation",
    "status": "final",
    "code": {
      "coding": [{
        "system": "http://loinc.org",
        "code": "2345-7",
        "display": "Glucose"
      }]
    },
    "subject": {
      "reference": "Patient/[patient-id]"
    },
    "effectiveDateTime": "2026-03-30T10:00:00Z",
    "valueQuantity": {
      "value": 95,
      "unit": "mg/dL"
    }
  }'
```

### Search Observations for a Patient

```bash
curl "http://localhost:8080/fhir/R4/Observation?subject=Patient/[patient-id]"
```

## FHIR Compliance Notes

This implementation follows the FHIR R4 specification for:

1. **Resource Structure**: All resources include required FHIR elements
2. **Metadata**: `meta.versionId` and `meta.lastUpdated` are maintained
3. **Identifiers**: Resources are identified by UUID logical IDs
4. **References**: Patient-Observation relationships use proper FHIR references
5. **Bundles**: Search results returned as FHIR Bundle searchset
6. **Error Handling**: Errors returned as OperationOutcome resources

## Data Model

### Resource Storage

Each resource is stored with:
- **Logical ID**: Unique identifier (UUID format)
- **Version ID**: Auto-incrementing version number
- **Resource Type**: FHIR resource type (Patient, Observation, etc.)
- **Full JSON**: Complete FHIR resource definition
- **Timestamps**: Creation and last update time

### Relationships

Patient and Observation resources are linked through:
- **FHIR References**: `Observation.subject` references `Patient`
- **Reference Index**: Database maintains relationship for efficient queries
- **Referential Integrity**: References point to existing resources

## Performance Considerations

- **Search Performance**: Indexed search across all resources
- **Concurrent Access**: SQLite handles multiple readers, single writer
- **Scalability**: Suitable for personal health records (100s-1000s of resources)
- **Data Growth**: Database file grows as resources are added

For large datasets or many concurrent users, consider migrating to PostgreSQL with the server modifications.

---

*For deployment instructions and architecture details, see `setup_report.md`*
