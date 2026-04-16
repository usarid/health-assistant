#!/usr/bin/env python3
"""
Batch C-CDA to FHIR R4 Conversion with Deduplication
Processes all C-CDA XML documents and produces a consolidated FHIR Bundle.
"""

import json
import os
import sys
import uuid
from pathlib import Path
from datetime import datetime
from typing import Dict, List, Set, Tuple, Optional
from lxml import etree
from collections import defaultdict
import hashlib


class CDAToFHIRConverter:
    """Converts C-CDA XML documents to FHIR R4 resources."""

    # HL7 v3 namespace
    NAMESPACES = {
        'cda': 'urn:hl7-org:v3',
        'xsi': 'http://www.w3.org/2001/XMLSchema-instance'
    }

    def __init__(self, xml_path: str):
        self.xml_path = xml_path
        self.tree = None
        self.root = None
        self.resources = []
        self.errors = []
        self.patient_id = None
        self.patient_mrn = None
        self.resource_count = 0

    def parse(self) -> bool:
        """Parse C-CDA XML file and extract FHIR resources."""
        try:
            parser = etree.XMLParser(remove_blank_text=True)
            self.tree = etree.parse(self.xml_path, parser)
            self.root = self.tree.getroot()

            # Extract patient
            patient_resource = self._extract_patient()
            if patient_resource:
                self.patient_id = patient_resource['id']
                self.patient_mrn = self._extract_mrn()
                self.resources.append(patient_resource)
                self.resource_count += 1

            # Process document sections
            self._process_allergies()
            self._process_medications()
            self._process_problems()
            self._process_immunizations()
            self._process_vitals()
            self._process_labs()

            return True
        except Exception as e:
            self.errors.append(f"Parse error: {str(e)}")
            return False

    def _extract_patient(self) -> Optional[Dict]:
        """Extract patient demographics."""
        try:
            patient_role = self.root.find('.//cda:patientRole', self.NAMESPACES)
            if patient_role is None:
                self.errors.append("Patient element not found")
                return None

            person = patient_role.find('.//cda:patient', self.NAMESPACES)
            if person is None:
                return None

            patient_id = str(uuid.uuid4())

            # Extract name
            given_name = "Unknown"
            family_name = "Unknown"
            name_elem = person.find('.//cda:name', self.NAMESPACES)
            if name_elem is not None:
                given = name_elem.find('.//cda:given', self.NAMESPACES)
                family = name_elem.find('.//cda:family', self.NAMESPACES)
                if given is not None and given.text:
                    given_name = given.text
                if family is not None and family.text:
                    family_name = family.text

            # Extract MRN
            mrn = "Unknown"
            id_elem = patient_role.find('.//cda:id', self.NAMESPACES)
            if id_elem is not None and id_elem.get('extension'):
                mrn = id_elem.get('extension')

            # Extract gender
            gender = "unknown"
            gender_elem = person.find('.//cda:administrativeGenderCode', self.NAMESPACES)
            if gender_elem is not None:
                code = gender_elem.get('code', 'U')
                gender_map = {'M': 'male', 'F': 'female', 'U': 'unknown'}
                gender = gender_map.get(code, 'unknown')

            # Extract DOB
            dob = None
            dob_elem = person.find('.//cda:birthTime', self.NAMESPACES)
            if dob_elem is not None and dob_elem.get('value'):
                dob_str = dob_elem.get('value')
                dob = self._format_date(dob_str)

            # Extract address
            address_lines = []
            addr_elem = patient_role.find('.//cda:addr', self.NAMESPACES)
            if addr_elem is not None:
                street = addr_elem.find('.//cda:streetAddressLine', self.NAMESPACES)
                if street is not None and street.text:
                    address_lines.append(street.text)

            patient = {
                'resourceType': 'Patient',
                'id': patient_id,
                'identifier': [
                    {
                        'type': {
                            'coding': [
                                {
                                    'code': 'MR',
                                    'system': 'http://terminology.hl7.org/CodeSystem/v2-0203'
                                }
                            ]
                        },
                        'value': mrn
                    }
                ],
                'name': [
                    {
                        'use': 'official',
                        'family': family_name,
                        'given': [given_name] if given_name else []
                    }
                ],
                'gender': gender
            }

            if dob:
                patient['birthDate'] = dob

            if address_lines:
                patient['address'] = [{'line': address_lines}]

            return patient

        except Exception as e:
            self.errors.append(f"Patient extraction error: {str(e)}")
            return None

    def _extract_mrn(self) -> Optional[str]:
        """Extract MRN from patient."""
        try:
            id_elem = self.root.find('.//cda:patient/cda:patientRole/cda:id', self.NAMESPACES)
            if id_elem is not None and id_elem.get('extension'):
                return id_elem.get('extension')
        except Exception:
            pass
        return None

    def _process_allergies(self):
        """Extract AllergyIntolerance resources."""
        try:
            # Find allergies section (code 48765-2)
            sections = self.root.findall('.//cda:section', self.NAMESPACES)
            for section in sections:
                code_elem = section.find('.//cda:code', self.NAMESPACES)
                if code_elem is not None and code_elem.get('code') == '48765-2':
                    entries = section.findall('.//cda:entry', self.NAMESPACES)
                    for entry in entries:
                        act = entry.find('.//cda:act', self.NAMESPACES)
                        if act is not None:
                            observation = act.find('.//cda:observation', self.NAMESPACES)
                            if observation is not None:
                                allergy = self._extract_allergy(observation)
                                if allergy:
                                    self.resources.append(allergy)
                                    self.resource_count += 1
        except Exception as e:
            self.errors.append(f"Allergy processing error: {str(e)}")

    def _extract_allergy(self, obs_elem) -> Optional[Dict]:
        """Extract single allergy from observation element."""
        try:
            allergy_id = str(uuid.uuid4())

            # Extract display name
            display_name = self._extract_text(obs_elem)

            substance = "Unknown"
            value_elem = obs_elem.find('.//cda:value', self.NAMESPACES)
            if value_elem is not None:
                substance = self._extract_text(value_elem) or "Unknown"

            status = "active"
            status_elem = obs_elem.find('.//cda:statusCode', self.NAMESPACES)
            if status_elem is not None:
                code = status_elem.get('code', 'active')
                if code != 'completed':
                    status = 'active'

            allergy = {
                'resourceType': 'AllergyIntolerance',
                'id': allergy_id,
                'patient': {'reference': f'Patient/{self.patient_id}'},
                'code': {
                    'text': substance,
                    'coding': [
                        {
                            'system': 'http://snomed.info/sct',
                            'display': substance
                        }
                    ]
                },
                'clinicalStatus': {'coding': [{'code': status}]},
                'verificationStatus': {'coding': [{'code': 'confirmed'}]},
                'type': 'allergy'
            }

            return allergy
        except Exception:
            return None

    def _process_medications(self):
        """Extract MedicationStatement resources."""
        try:
            # Find medications section (code 10160-0)
            sections = self.root.findall('.//cda:section', self.NAMESPACES)
            for section in sections:
                code_elem = section.find('.//cda:code', self.NAMESPACES)
                if code_elem is not None and code_elem.get('code') == '10160-0':
                    entries = section.findall('.//cda:entry', self.NAMESPACES)
                    for entry in entries:
                        med_elem = entry.find('.//cda:substanceAdministration', self.NAMESPACES)
                        if med_elem is not None:
                            med = self._extract_medication(med_elem)
                            if med:
                                self.resources.append(med)
                                self.resource_count += 1
        except Exception as e:
            self.errors.append(f"Medication processing error: {str(e)}")

    def _extract_medication(self, med_elem) -> Optional[Dict]:
        """Extract single medication from substanceAdministration element."""
        try:
            med_id = str(uuid.uuid4())

            # Extract medication display name
            med_display = "Unknown"
            participant = med_elem.find('.//cda:participant/cda:participantRole/cda:playingEntity', self.NAMESPACES)
            if participant is not None:
                med_display = self._extract_text(participant) or "Unknown"

            # Determine status
            status = "active"
            status_elem = med_elem.find('.//cda:statusCode', self.NAMESPACES)
            if status_elem is not None:
                code = status_elem.get('code', 'active')
                if code == 'completed':
                    status = 'stopped'
                elif code == 'suspended':
                    status = 'on-hold'

            med_statement = {
                'resourceType': 'MedicationStatement',
                'id': med_id,
                'patient': {'reference': f'Patient/{self.patient_id}'},
                'medication': {'reference': {'display': med_display}},
                'status': status
            }

            # Extract dosage if present
            dose_elem = med_elem.find('.//cda:doseQuantity', self.NAMESPACES)
            if dose_elem is not None:
                dose_val = dose_elem.get('value')
                dose_unit = dose_elem.get('unit')
                if dose_val:
                    try:
                        med_statement['dosage'] = [
                            {
                                'dose': {
                                    'value': float(dose_val),
                                    'unit': dose_unit or 'unit'
                                }
                            }
                        ]
                    except (ValueError, TypeError):
                        pass

            # Add minimal narrative
            med_statement['text'] = {
                'status': 'generated',
                'div': f'<div>{med_display}</div>'
            }

            return med_statement
        except Exception:
            return None

    def _process_problems(self):
        """Extract Condition resources from problems/diagnoses."""
        try:
            # Find problems section (code 11450-4)
            sections = self.root.findall('.//cda:section', self.NAMESPACES)
            for section in sections:
                code_elem = section.find('.//cda:code', self.NAMESPACES)
                if code_elem is not None and code_elem.get('code') == '11450-4':
                    entries = section.findall('.//cda:entry', self.NAMESPACES)
                    for entry in entries:
                        act = entry.find('.//cda:act', self.NAMESPACES)
                        if act is not None:
                            observation = act.find('.//cda:observation', self.NAMESPACES)
                            if observation is not None:
                                condition = self._extract_condition(observation)
                                if condition:
                                    self.resources.append(condition)
                                    self.resource_count += 1

            # Also try resolved problems section (code 11348-0)
            for section in sections:
                code_elem = section.find('.//cda:code', self.NAMESPACES)
                if code_elem is not None and code_elem.get('code') == '11348-0':
                    entries = section.findall('.//cda:entry', self.NAMESPACES)
                    for entry in entries:
                        act = entry.find('.//cda:act', self.NAMESPACES)
                        if act is not None:
                            observation = act.find('.//cda:observation', self.NAMESPACES)
                            if observation is not None:
                                condition = self._extract_condition(observation, resolved=True)
                                if condition:
                                    self.resources.append(condition)
                                    self.resource_count += 1
        except Exception as e:
            self.errors.append(f"Problem processing error: {str(e)}")

    def _extract_condition(self, obs_elem, resolved=False) -> Optional[Dict]:
        """Extract single condition from observation element."""
        try:
            condition_id = str(uuid.uuid4())

            # Extract display name
            display_name = self._extract_text(obs_elem) or "Unknown"

            # Extract code
            code_elem = obs_elem.find('.//cda:code', self.NAMESPACES)
            code_val = "unknown"
            if code_elem is not None:
                code_val = code_elem.get('code', 'unknown')

            # Determine clinical status
            clinical_status = "resolved" if resolved else "active"

            condition = {
                'resourceType': 'Condition',
                'id': condition_id,
                'patient': {'reference': f'Patient/{self.patient_id}'},
                'code': {
                    'text': display_name,
                    'coding': [
                        {
                            'system': 'http://snomed.info/sct',
                            'code': code_val,
                            'display': display_name
                        }
                    ]
                },
                'clinicalStatus': {'coding': [{'code': clinical_status}]},
                'verificationStatus': {'coding': [{'code': 'confirmed'}]}
            }

            # Extract effective date if present
            effective_time = obs_elem.find('.//cda:effectiveTime', self.NAMESPACES)
            if effective_time is not None and effective_time.get('value'):
                onset_date = self._format_date(effective_time.get('value'))
                if onset_date:
                    condition['onsetDateTime'] = onset_date

            return condition
        except Exception:
            return None

    def _process_immunizations(self):
        """Extract Immunization resources."""
        try:
            # Find immunizations section (code 11369-6)
            sections = self.root.findall('.//cda:section', self.NAMESPACES)
            for section in sections:
                code_elem = section.find('.//cda:code', self.NAMESPACES)
                if code_elem is not None and code_elem.get('code') == '11369-6':
                    entries = section.findall('.//cda:entry', self.NAMESPACES)
                    for entry in entries:
                        admin_elem = entry.find('.//cda:substanceAdministration', self.NAMESPACES)
                        if admin_elem is not None:
                            immunization = self._extract_immunization(admin_elem)
                            if immunization:
                                self.resources.append(immunization)
                                self.resource_count += 1
        except Exception as e:
            self.errors.append(f"Immunization processing error: {str(e)}")

    def _extract_immunization(self, admin_elem) -> Optional[Dict]:
        """Extract single immunization from substanceAdministration element."""
        try:
            immunization_id = str(uuid.uuid4())

            # Extract vaccine display name
            vaccine_display = "Unknown"
            participant = admin_elem.find('.//cda:participant/cda:participantRole/cda:playingEntity', self.NAMESPACES)
            if participant is not None:
                vaccine_display = self._extract_text(participant) or "Unknown"

            # Extract administration date
            date_given = None
            effective_time = admin_elem.find('.//cda:effectiveTime', self.NAMESPACES)
            if effective_time is not None and effective_time.get('value'):
                date_given = self._format_date(effective_time.get('value'))

            immunization = {
                'resourceType': 'Immunization',
                'id': immunization_id,
                'patient': {'reference': f'Patient/{self.patient_id}'},
                'vaccineCode': {
                    'text': vaccine_display,
                    'coding': [
                        {
                            'system': 'http://hl7.org/fhir/sid/cvx',
                            'display': vaccine_display
                        }
                    ]
                },
                'status': 'completed'
            }

            if date_given:
                immunization['occurrenceDateTime'] = date_given

            return immunization
        except Exception:
            return None

    def _process_vitals(self):
        """Extract Observation resources for vital signs."""
        try:
            # Find vital signs section (code 8716-3)
            sections = self.root.findall('.//cda:section', self.NAMESPACES)
            for section in sections:
                code_elem = section.find('.//cda:code', self.NAMESPACES)
                if code_elem is not None and code_elem.get('code') == '8716-3':
                    entries = section.findall('.//cda:entry', self.NAMESPACES)
                    for entry in entries:
                        organizer = entry.find('.//cda:organizer', self.NAMESPACES)
                        if organizer is not None:
                            obs_list = organizer.findall('.//cda:observation', self.NAMESPACES)
                            for obs in obs_list:
                                observation = self._extract_observation(obs, category='vital-signs')
                                if observation:
                                    self.resources.append(observation)
                                    self.resource_count += 1
        except Exception as e:
            self.errors.append(f"Vital signs processing error: {str(e)}")

    def _process_labs(self):
        """Extract Observation resources for lab results."""
        try:
            # Find lab results section (code 30954-2)
            sections = self.root.findall('.//cda:section', self.NAMESPACES)
            for section in sections:
                code_elem = section.find('.//cda:code', self.NAMESPACES)
                if code_elem is not None and code_elem.get('code') == '30954-2':
                    entries = section.findall('.//cda:entry', self.NAMESPACES)
                    for entry in entries:
                        organizer = entry.find('.//cda:organizer', self.NAMESPACES)
                        if organizer is not None:
                            obs_list = organizer.findall('.//cda:observation', self.NAMESPACES)
                            for obs in obs_list:
                                observation = self._extract_observation(obs, category='laboratory')
                                if observation:
                                    self.resources.append(observation)
                                    self.resource_count += 1
        except Exception as e:
            self.errors.append(f"Lab results processing error: {str(e)}")

    def _extract_observation(self, obs_elem, category='laboratory') -> Optional[Dict]:
        """Extract single observation (lab/vital) from observation element."""
        try:
            observation_id = str(uuid.uuid4())

            # Extract code and display name
            code_elem = obs_elem.find('.//cda:code', self.NAMESPACES)
            loinc_code = "unknown"
            display_name = "Unknown"
            if code_elem is not None:
                loinc_code = code_elem.get('code', 'unknown')
                display_name = code_elem.get('displayName') or self._extract_text(code_elem) or "Unknown"

            # Extract value
            value_elem = obs_elem.find('.//cda:value', self.NAMESPACES)
            value = None
            unit = None

            if value_elem is not None:
                xsi_type = value_elem.get(f'{{{self.NAMESPACES["xsi"]}}}type', '')
                if 'PQ' in xsi_type:
                    value_str = value_elem.get('value')
                    unit = value_elem.get('unit', '')
                    if value_str:
                        try:
                            value = float(value_str)
                        except (ValueError, TypeError):
                            value = value_str
                elif 'ST' in xsi_type or xsi_type == '':
                    value = value_elem.text

            # Extract effective date
            effective_date = None
            effective_time = obs_elem.find('.//cda:effectiveTime', self.NAMESPACES)
            if effective_time is not None and effective_time.get('value'):
                effective_date = self._format_date(effective_time.get('value'))

            observation = {
                'resourceType': 'Observation',
                'id': observation_id,
                'code': {
                    'coding': [
                        {
                            'code': loinc_code,
                            'system': 'http://loinc.org',
                            'display': display_name
                        }
                    ],
                    'text': display_name
                },
                'status': 'final',
                'patient': {'reference': f'Patient/{self.patient_id}'},
                'category': [
                    {
                        'coding': [
                            {
                                'code': category,
                                'system': 'http://terminology.hl7.org/CodeSystem/observation-category'
                            }
                        ]
                    }
                ]
            }

            # Add value if present
            if value is not None:
                if isinstance(value, float):
                    observation['valueQuantity'] = {
                        'value': value,
                        'unit': unit or 'unknown'
                    }
                else:
                    observation['valueString'] = str(value)

            # Add effective date if present
            if effective_date:
                observation['effectiveDateTime'] = effective_date

            return observation
        except Exception:
            return None

    def _extract_text(self, elem) -> Optional[str]:
        """Extract display name with fallback chain."""
        if elem is None:
            return None

        # Try displayName attribute first
        display_name = elem.get('displayName')
        if display_name:
            return display_name

        # Try originalText
        orig_text = elem.find('.//cda:originalText', self.NAMESPACES)
        if orig_text is not None and orig_text.text:
            return orig_text.text

        # Try translation
        translation = elem.find('.//cda:translation', self.NAMESPACES)
        if translation is not None:
            display_name = translation.get('displayName')
            if display_name:
                return display_name

        # Fallback to element text
        if elem.text:
            return elem.text

        return None

    def _format_date(self, hl7_date: str) -> Optional[str]:
        """Convert HL7 v3 date format (YYYYMMDDHHMMSS) to FHIR (YYYY-MM-DD)."""
        try:
            if not hl7_date or len(hl7_date) < 4:
                return None
            # Extract YYYY-MM-DD
            year = hl7_date[0:4]
            month = hl7_date[4:6] if len(hl7_date) >= 6 else '01'
            day = hl7_date[6:8] if len(hl7_date) >= 8 else '01'
            return f"{year}-{month}-{day}"
        except Exception:
            return None

    def get_bundle(self) -> Dict:
        """Get FHIR Bundle containing all resources."""
        return {
            'resourceType': 'Bundle',
            'type': 'transaction',
            'entry': [
                {
                    'resource': resource,
                    'request': {
                        'method': 'POST',
                        'url': resource['resourceType']
                    }
                }
                for resource in self.resources
            ]
        }


class BatchConverter:
    """Batch converts C-CDA documents and deduplicates resources."""

    def __init__(self, input_dir: str, output_path: str):
        self.input_dir = input_dir
        self.output_path = output_path
        self.all_resources = {}
        self.dedup_stats = defaultdict(lambda: {'total': 0, 'duplicates': 0, 'kept': 0})
        self.resource_hashes = defaultdict(set)
        self.documents_processed = 0
        self.documents_failed = 0
        self.total_resources_before = 0
        self.total_resources_after = 0
        self.error_log = []
        self.date_range = {'earliest': None, 'latest': None}

    def run(self) -> bool:
        """Execute batch conversion."""
        print(f"\nBatch Conversion Starting")
        print(f"Input directory: {self.input_dir}")
        print(f"Output path: {self.output_path}")

        # Get list of XML files
        xml_files = sorted([f for f in os.listdir(self.input_dir) if f.endswith('.XML')])
        total_files = len(xml_files)

        print(f"Found {total_files} XML documents to process")

        # Process each document
        for idx, filename in enumerate(xml_files, 1):
            if filename.upper() == 'METADATA.XML':
                continue

            input_path = os.path.join(self.input_dir, filename)

            print(f"[{idx}/{total_files}] Processing {filename}...", end=' ')
            sys.stdout.flush()

            try:
                converter = CDAToFHIRConverter(input_path)
                success = converter.parse()

                if success:
                    bundle = converter.get_bundle()
                    self.documents_processed += 1

                    # Deduplicate and add resources
                    for entry in bundle.get('entry', []):
                        resource = entry['resource']
                        self._add_resource(resource)
                        self.total_resources_before += 1

                    print(f"OK ({converter.resource_count} resources)")
                else:
                    self.documents_failed += 1
                    error_msg = f"{filename}: {', '.join(converter.errors[:3])}"
                    self.error_log.append(error_msg)
                    print(f"FAILED")

            except Exception as e:
                self.documents_failed += 1
                error_msg = f"{filename}: {str(e)}"
                self.error_log.append(error_msg)
                print(f"ERROR: {str(e)[:50]}")

        # Finalize
        self._finalize()
        return self.documents_processed > 0

    def _add_resource(self, resource: Dict):
        """Add resource with deduplication."""
        resource_type = resource.get('resourceType')

        if resource_type not in self.dedup_stats:
            self.dedup_stats[resource_type]['total'] = 0
            self.dedup_stats[resource_type]['duplicates'] = 0
            self.dedup_stats[resource_type]['kept'] = 0

        self.dedup_stats[resource_type]['total'] += 1

        # Generate dedup hash
        dedup_hash = self._generate_dedup_hash(resource)

        # Check if duplicate
        if dedup_hash in self.resource_hashes[resource_type]:
            self.dedup_stats[resource_type]['duplicates'] += 1
            return

        # Add to resources (generate new ID)
        new_id = str(uuid.uuid4())
        resource['id'] = new_id

        # Update references to patient
        if 'patient' in resource and isinstance(resource['patient'], dict):
            # Patient will be handled separately
            pass

        self.all_resources[f"{resource_type}/{new_id}"] = resource
        self.resource_hashes[resource_type].add(dedup_hash)
        self.dedup_stats[resource_type]['kept'] += 1

        # Track date range
        if 'effectiveDateTime' in resource:
            self._update_date_range(resource['effectiveDateTime'])
        elif 'occurrenceDateTime' in resource:
            self._update_date_range(resource['occurrenceDateTime'])
        elif 'birthDate' in resource:
            self._update_date_range(resource['birthDate'])

    def _generate_dedup_hash(self, resource: Dict) -> str:
        """Generate hash for deduplication."""
        resource_type = resource.get('resourceType')

        if resource_type == 'Patient':
            # Hash on MRN
            mrn = None
            for ident in resource.get('identifier', []):
                if ident.get('value'):
                    mrn = ident.get('value')
                    break
            return hashlib.md5(f"Patient:{mrn}".encode()).hexdigest()

        elif resource_type == 'Observation':
            # Hash on LOINC code + date + value
            code = None
            for coding in resource.get('code', {}).get('coding', []):
                if coding.get('code') != 'unknown':
                    code = coding.get('code')
                    break
            date_val = resource.get('effectiveDateTime', '')
            value_hash = ''
            if 'valueQuantity' in resource:
                value_hash = str(resource['valueQuantity'].get('value', ''))
            elif 'valueString' in resource:
                value_hash = resource['valueString']

            hash_str = f"Obs:{code}:{date_val}:{value_hash}"
            return hashlib.md5(hash_str.encode()).hexdigest()

        elif resource_type == 'Condition':
            # Hash on code + patient + onset date
            code = None
            for coding in resource.get('code', {}).get('coding', []):
                if coding.get('code') != 'unknown':
                    code = coding.get('code')
                    break
            onset = resource.get('onsetDateTime', '')
            hash_str = f"Cond:{code}:{onset}"
            return hashlib.md5(hash_str.encode()).hexdigest()

        elif resource_type == 'MedicationStatement':
            # Hash on medication + status
            med = resource.get('medication', {}).get('reference', {}).get('display', '')
            status = resource.get('status', '')
            hash_str = f"Med:{med}:{status}"
            return hashlib.md5(hash_str.encode()).hexdigest()

        elif resource_type == 'Immunization':
            # Hash on vaccine code + date
            vaccine = resource.get('vaccineCode', {}).get('text', '')
            date_val = resource.get('occurrenceDateTime', '')
            hash_str = f"Imm:{vaccine}:{date_val}"
            return hashlib.md5(hash_str.encode()).hexdigest()

        elif resource_type == 'AllergyIntolerance':
            # Hash on substance
            substance = resource.get('code', {}).get('text', '')
            hash_str = f"Allergy:{substance}"
            return hashlib.md5(hash_str.encode()).hexdigest()

        else:
            # Default: hash entire resource (minus ID)
            resource_copy = dict(resource)
            resource_copy.pop('id', None)
            hash_str = json.dumps(resource_copy, sort_keys=True, default=str)
            return hashlib.md5(hash_str.encode()).hexdigest()

    def _update_date_range(self, date_str: str):
        """Update earliest/latest dates."""
        try:
            if date_str:
                date_obj = date_str.split('T')[0]
                if self.date_range['earliest'] is None or date_obj < self.date_range['earliest']:
                    self.date_range['earliest'] = date_obj
                if self.date_range['latest'] is None or date_obj > self.date_range['latest']:
                    self.date_range['latest'] = date_obj
        except Exception:
            pass

    def _finalize(self):
        """Finalize and save consolidated bundle."""
        # Get unique patient resource
        patient_resource = None
        for resource_id, resource in self.all_resources.items():
            if resource.get('resourceType') == 'Patient':
                patient_resource = resource
                break

        # Build consolidated bundle
        entry_list = []
        for resource_id, resource in sorted(self.all_resources.items()):
            # Update patient references to use the single patient ID
            if patient_resource and resource.get('resourceType') != 'Patient':
                if 'patient' in resource:
                    resource['patient'] = {'reference': f'Patient/{patient_resource["id"]}'}

            entry_list.append({
                'resource': resource,
                'request': {
                    'method': 'POST',
                    'url': resource['resourceType']
                }
            })

        consolidated_bundle = {
            'resourceType': 'Bundle',
            'type': 'transaction',
            'entry': entry_list
        }

        # Save bundle
        with open(self.output_path, 'w') as f:
            json.dump(consolidated_bundle, f, indent=2)

        self.total_resources_after = len(self.all_resources)

        print(f"\nConsolidated bundle saved to {self.output_path}")
        print(f"Total resources in bundle: {self.total_resources_after}")

    def get_statistics(self) -> Dict:
        """Get conversion statistics."""
        stats = {
            'timestamp': datetime.now().isoformat(),
            'documents': {
                'total': self.documents_processed + self.documents_failed,
                'successful': self.documents_processed,
                'failed': self.documents_failed
            },
            'resources': {
                'before_dedup': self.total_resources_before,
                'after_dedup': self.total_resources_after,
                'duplicates_removed': self.total_resources_before - self.total_resources_after,
                'by_type': {}
            },
            'date_range': self.date_range,
            'errors': self.error_log[:10]
        }

        for resource_type in sorted(self.dedup_stats.keys()):
            counts = self.dedup_stats[resource_type]
            stats['resources']['by_type'][resource_type] = {
                'total_encountered': counts['total'],
                'duplicates_removed': counts['duplicates'],
                'unique_kept': counts['kept']
            }

        return stats


if __name__ == '__main__':
    input_dir = "/sessions/admiring-vigilant-brown/mnt/Medical/UCSF MyChart extracts/HealthSummary_Nov_26_2025/IHE_XDM/Uri1"
    output_path = "/sessions/admiring-vigilant-brown/mnt/Medical/Synthesis/consolidated_fhir_bundle.json"

    converter = BatchConverter(input_dir, output_path)
    success = converter.run()

    if success:
        stats = converter.get_statistics()
        print("\n" + "=" * 70)
        print("BATCH CONVERSION STATISTICS")
        print("=" * 70)
        print(f"\nDocuments Processed: {stats['documents']['successful']}/{stats['documents']['total']}")
        print(f"Documents Failed: {stats['documents']['failed']}")
        print(f"\nResources Before Deduplication: {stats['resources']['before_dedup']}")
        print(f"Resources After Deduplication: {stats['resources']['after_dedup']}")
        print(f"Duplicates Removed: {stats['resources']['duplicates_removed']}")
        print(f"Deduplication Rate: {(stats['resources']['duplicates_removed'] / max(stats['resources']['before_dedup'], 1) * 100):.1f}%")
        print(f"\nData Date Range: {stats['date_range']['earliest']} to {stats['date_range']['latest']}")
        print(f"\nResources by Type:")
        for rtype in sorted(stats['resources']['by_type'].keys()):
            counts = stats['resources']['by_type'][rtype]
            print(f"  {rtype:25} Total: {counts['total_encountered']:6} Duplicates: {counts['duplicates_removed']:6} Unique: {counts['unique_kept']:6}")

        # Save statistics
        stats_path = "/sessions/admiring-vigilant-brown/mnt/Medical/Synthesis/batch_conversion_stats.json"
        with open(stats_path, 'w') as f:
            json.dump(stats, f, indent=2)
        print(f"\nStatistics saved to {stats_path}")
    else:
        print("Batch conversion failed!")
        sys.exit(1)
