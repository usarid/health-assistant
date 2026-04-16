#!/usr/bin/env python3
"""
Parse Apple Health export.xml and convert wearable/sensor data to FHIR R4 Observations.
Uses streaming XML parser to handle 2.86GB file without loading into memory.
"""

import json
import sys
from datetime import datetime, timedelta
from collections import defaultdict
from xml.etree import ElementTree as ET
from pathlib import Path
import time
import uuid

# LOINC codes mapping
LOINC_CODES = {
    'HKQuantityTypeIdentifierHeartRate': '8867-4',
    'HKQuantityTypeIdentifierBloodPressureSystolic': '8480-6',
    'HKQuantityTypeIdentifierBloodPressureDiastolic': '8462-4',
    'HKQuantityTypeIdentifierOxygenSaturation': '59408-5',
    'HKQuantityTypeIdentifierHeartRateVariabilitySDNN': '80404-7',
    'HKQuantityTypeIdentifierRestingHeartRate': '40443-4',
    'HKQuantityTypeIdentifierRespiratoryRate': '9279-1',
    'HKCategoryTypeIdentifierSleepAnalysis': '93832-4',
    'HKQuantityTypeIdentifierStepCount': '55423-8',
    'HKQuantityTypeIdentifierBodyMassIndex': '39156-5',
}

# Unit mappings
UNIT_MAPPING = {
    'count/min': 'count/min',
    '%': '%',
    'ms': 'ms',
    'count': 'count',
    'kg/m2': 'kg/m2',
    'HKCategoryValueSleepAnalysisAsleep': 'asleep',
    'HKCategoryValueSleepAnalysisInBed': 'inBed',
}

class AppleHealthParser:
    def __init__(self, xml_path, output_dir, date_cutoff=None):
        self.xml_path = xml_path
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.date_cutoff = date_cutoff  # Only process records after this date

        # Data storage
        self.blood_pressure_records = []
        self.daily_heart_rate = defaultdict(lambda: {'readings': [], 'resting': []})
        self.daily_spo2 = defaultdict(lambda: {'readings': []})
        self.daily_hrv = defaultdict(lambda: {'readings': []})
        self.daily_steps = defaultdict(lambda: 0)
        self.daily_respiratory = defaultdict(lambda: {'readings': []})
        self.nightly_sleep = defaultdict(lambda: {'asleep': [], 'inBed': []})
        self.bmi_records = []
        self.resting_hr_records = []

        # Statistics
        self.stats = {
            'blood_pressure': 0,
            'heart_rate': 0,
            'spo2': 0,
            'hrv': 0,
            'steps': 0,
            'respiratory': 0,
            'sleep': 0,
            'bmi': 0,
            'resting_hr': 0,
            'errors': 0,
        }

        self.date_range = {'min': None, 'max': None}

    def should_process_record(self, start_date_str):
        """Check if record should be processed based on date cutoff"""
        if not self.date_cutoff:
            return True
        try:
            record_date = self._parse_datetime(start_date_str)
            if record_date and record_date < self.date_cutoff:
                return False
            return True
        except:
            return True

    def update_date_range(self, date_str):
        """Track min/max dates"""
        try:
            record_date = self._parse_datetime(date_str)
            if record_date:
                if self.date_range['min'] is None or record_date < self.date_range['min']:
                    self.date_range['min'] = record_date
                if self.date_range['max'] is None or record_date > self.date_range['max']:
                    self.date_range['max'] = record_date
        except:
            pass

    def get_device_from_source(self, source_name):
        """Extract device name from source"""
        if not source_name:
            return "Unknown"
        if "Apple Watch" in source_name or "Watch" in source_name:
            return "Apple Watch"
        if "Oura" in source_name:
            return "Oura Ring"
        if "iPhone" in source_name:
            return "iPhone"
        return source_name[:50]  # Truncate long names

    def parse_xml(self):
        """Stream parse the XML file"""
        print(f"Starting to parse {self.xml_path}...")
        start_time = time.time()
        record_count = 0

        try:
            context = ET.iterparse(self.xml_path, events=('end',))

            for event, elem in context:
                if elem.tag != 'Record':
                    elem.clear()
                    continue

                record_count += 1
                if record_count % 100000 == 0:
                    elapsed = time.time() - start_time
                    print(f"  Processed {record_count:,} records in {elapsed:.1f}s...")

                try:
                    self._process_record(elem)
                except Exception as e:
                    self.stats['errors'] += 1
                    if self.stats['errors'] <= 5:
                        print(f"  Error processing record: {e}")

                elem.clear()

            elapsed = time.time() - start_time
            print(f"Completed parsing {record_count:,} records in {elapsed:.1f}s")


        except Exception as e:
            print(f"Error parsing XML: {e}")
            raise

    def _process_record(self, elem):
        """Process a single Record element"""
        type_attr = elem.get('type', '')
        start_date = elem.get('startDate', '')
        end_date = elem.get('endDate', '')
        value = elem.get('value', '')
        source_name = elem.get('sourceName', '')
        unit = elem.get('unit', '')

        if not self.should_process_record(start_date):
            return

        self.update_date_range(start_date)

        # Blood Pressure - collect ALL readings, pairing systolic and diastolic by timestamp
        if type_attr == 'HKQuantityTypeIdentifierBloodPressureSystolic':
            try:
                systolic = float(value)
                self.blood_pressure_records.append({
                    'startDate': start_date,
                    'endDate': end_date,
                    'systolic': systolic,
                    'diastolic': None,  # Will be paired later or by matching diastolic record
                    'sourceName': source_name,
                })
                self.stats['blood_pressure'] += 1
            except:
                pass

        elif type_attr == 'HKQuantityTypeIdentifierBloodPressureDiastolic':
            try:
                diastolic = float(value)
                # Pair with most recent systolic record that has the same timestamp
                for bp in reversed(self.blood_pressure_records):
                    if bp['startDate'] == start_date and bp['diastolic'] is None:
                        bp['diastolic'] = diastolic
                        break
                else:
                    # No matching systolic found — store standalone
                    self.blood_pressure_records.append({
                        'startDate': start_date,
                        'endDate': end_date,
                        'systolic': None,
                        'diastolic': diastolic,
                        'sourceName': source_name,
                    })
            except:
                pass

        elif type_attr == 'HKQuantityTypeIdentifierHeartRate':
            try:
                hr = float(value)
                date_obj = self._parse_datetime(start_date)
                if date_obj:
                    day_key = date_obj.strftime('%Y-%m-%d')
                    self.daily_heart_rate[day_key]['readings'].append(hr)
                    self.stats['heart_rate'] += 1
            except:
                pass

        elif type_attr == 'HKQuantityTypeIdentifierRestingHeartRate':
            try:
                rhr = float(value)
                self.resting_hr_records.append({
                    'startDate': start_date,
                    'value': rhr,
                    'sourceName': source_name,
                })
                self.stats['resting_hr'] += 1
            except:
                pass

        elif type_attr == 'HKQuantityTypeIdentifierOxygenSaturation':
            try:
                spo2 = float(value)
                date_obj = self._parse_datetime(start_date)
                if date_obj:
                    day_key = date_obj.strftime('%Y-%m-%d')
                    self.daily_spo2[day_key]['readings'].append(spo2)
                    self.stats['spo2'] += 1
            except:
                pass

        elif type_attr == 'HKQuantityTypeIdentifierHeartRateVariabilitySDNN':
            try:
                hrv = float(value)
                date_obj = self._parse_datetime(start_date)
                if date_obj:
                    day_key = date_obj.strftime('%Y-%m-%d')
                    self.daily_hrv[day_key]['readings'].append(hrv)
                    self.stats['hrv'] += 1
            except:
                pass

        elif type_attr == 'HKQuantityTypeIdentifierStepCount':
            try:
                steps = int(float(value))
                date_obj = self._parse_datetime(start_date)
                if date_obj:
                    day_key = date_obj.strftime('%Y-%m-%d')
                    self.daily_steps[day_key] += steps
                    self.stats['steps'] += 1
            except:
                pass

        elif type_attr == 'HKQuantityTypeIdentifierRespiratoryRate':
            try:
                rr = float(value)
                date_obj = self._parse_datetime(start_date)
                if date_obj:
                    day_key = date_obj.strftime('%Y-%m-%d')
                    self.daily_respiratory[day_key]['readings'].append(rr)
                    self.stats['respiratory'] += 1
            except:
                pass

        elif type_attr == 'HKCategoryTypeIdentifierSleepAnalysis':
            try:
                sleep_value = value
                start_dt = self._parse_datetime(start_date)
                end_dt = self._parse_datetime(end_date)

                if start_dt and end_dt:
                    # Calculate duration from start and end dates
                    duration_seconds = int((end_dt - start_dt).total_seconds())

                    # Use the start date for the day key (night typically starts after midnight)
                    day_key = start_dt.strftime('%Y-%m-%d')

                    if 'Asleep' in sleep_value or 'asleep' in sleep_value:
                        self.nightly_sleep[day_key]['asleep'].append(duration_seconds)
                    elif 'InBed' in sleep_value or 'inBed' in sleep_value:
                        self.nightly_sleep[day_key]['inBed'].append(duration_seconds)

                    self.stats['sleep'] += 1
            except Exception as e:
                if self.stats['sleep'] < 5:
                    pass

        elif type_attr == 'HKQuantityTypeIdentifierBodyMassIndex':
            try:
                bmi = float(value)
                self.bmi_records.append({
                    'startDate': start_date,
                    'value': bmi,
                    'sourceName': source_name,
                })
                self.stats['bmi'] += 1
            except:
                pass

    def create_fhir_bundle(self):
        """Create FHIR R4 bundle with all observations"""
        bundle = {
            "resourceType": "Bundle",
            "type": "collection",
            "timestamp": datetime.now().isoformat(),
            "entry": []
        }

        entries = []

        # Blood Pressure Observations
        paired = sum(1 for bp in self.blood_pressure_records if bp.get('systolic') and bp.get('diastolic'))
        sys_only = sum(1 for bp in self.blood_pressure_records if bp.get('systolic') and not bp.get('diastolic'))
        dia_only = sum(1 for bp in self.blood_pressure_records if not bp.get('systolic') and bp.get('diastolic'))
        print(f"Creating Blood Pressure Observations... ({len(self.blood_pressure_records)} total: {paired} paired, {sys_only} systolic-only, {dia_only} diastolic-only)")
        for bp in self.blood_pressure_records:
            obs = self._create_bp_observation(bp)
            if obs:
                entries.append(obs)

        # Daily Heart Rate Summaries
        print(f"Creating Daily Heart Rate Observations... ({len(self.daily_heart_rate)} days)")
        for day_key in sorted(self.daily_heart_rate.keys()):
            readings = self.daily_heart_rate[day_key]['readings']
            if readings:
                obs = self._create_daily_hr_observation(day_key, readings)
                if obs:
                    entries.append(obs)

        # Resting Heart Rate
        print("Creating Resting Heart Rate Observations...")
        for rhr in self.resting_hr_records:
            obs = self._create_resting_hr_observation(rhr)
            if obs:
                entries.append(obs)

        # Daily SpO2 Summaries
        print(f"Creating Daily SpO2 Observations... ({len(self.daily_spo2)} days)")
        for day_key in sorted(self.daily_spo2.keys()):
            readings = self.daily_spo2[day_key]['readings']
            if readings:
                obs = self._create_daily_spo2_observation(day_key, readings)
                if obs:
                    entries.append(obs)

        # Daily HRV Summaries
        print(f"Creating Daily HRV Observations... ({len(self.daily_hrv)} days)")
        for day_key in sorted(self.daily_hrv.keys()):
            readings = self.daily_hrv[day_key]['readings']
            if readings:
                obs = self._create_daily_hrv_observation(day_key, readings)
                if obs:
                    entries.append(obs)

        # Daily Respiratory Rate Summaries
        print(f"Creating Daily Respiratory Rate Observations... ({len(self.daily_respiratory)} days)")
        for day_key in sorted(self.daily_respiratory.keys()):
            readings = self.daily_respiratory[day_key]['readings']
            if readings:
                obs = self._create_daily_respiratory_observation(day_key, readings)
                if obs:
                    entries.append(obs)

        # Daily Step Count
        print(f"Creating Daily Step Count Observations... ({len(self.daily_steps)} days)")
        for day_key in sorted(self.daily_steps.keys()):
            steps = self.daily_steps[day_key]
            if steps > 0:
                obs = self._create_daily_steps_observation(day_key, steps)
                if obs:
                    entries.append(obs)

        # BMI
        print("Creating BMI Observations...")
        for bmi in self.bmi_records:
            obs = self._create_bmi_observation(bmi)
            if obs:
                entries.append(obs)

        # Sleep Analysis
        print(f"Creating Sleep Observations... ({len(self.nightly_sleep)} nights)")
        for night_key in sorted(self.nightly_sleep.keys()):
            sleep_data = self.nightly_sleep[night_key]
            obs = self._create_sleep_observation(night_key, sleep_data)
            if obs:
                entries.append(obs)

        bundle['entry'] = entries
        bundle['total'] = len(entries)

        return bundle

    def _parse_datetime(self, date_str):
        """Parse datetime from Apple Health format (handles both ISO and space-separated)"""
        try:
            # Try ISO format first
            dt = datetime.fromisoformat(date_str.replace('Z', '+00:00'))
            return dt
        except:
            try:
                # Handle space-separated format: '2016-01-16 01:00:00 -0700'
                dt = datetime.strptime(date_str, '%Y-%m-%d %H:%M:%S %z')
                return dt
            except:
                return None

    def _create_bp_observation(self, bp):
        """Create Blood Pressure Observation"""
        try:
            dt = self._parse_datetime(bp['startDate'])
            if not dt:
                return None
            return {
                "resource": {
                    "resourceType": "Observation",
                    "id": str(uuid.uuid4()),
                    "status": "final",
                    "category": [{
                        "coding": [{
                            "system": "http://terminology.hl7.org/CodeSystem/observation-category",
                            "code": "vital-signs"
                        }]
                    }],
                    "code": {
                        "coding": [{
                            "system": "http://loinc.org",
                            "code": "85354-9",
                            "display": "Blood Pressure"
                        }]
                    },
                    "effectiveDateTime": dt.isoformat(),
                    "issued": dt.isoformat(),
                    "component": self._build_bp_components(bp),
                    "device": {
                        "reference": f"Device/{self.get_device_from_source(bp['sourceName'])}"
                    }
                }
            }
        except Exception as e:
            print(f"Error creating BP observation: {e}")
            return None

    def _build_bp_components(self, bp):
        """Build FHIR component list with systolic and (if available) diastolic."""
        components = []
        if bp.get('systolic') is not None:
            components.append({
                "code": {
                    "coding": [{
                        "system": "http://loinc.org",
                        "code": "8480-6",
                        "display": "Systolic blood pressure"
                    }]
                },
                "valueQuantity": {
                    "value": bp['systolic'],
                    "unit": "mmHg",
                    "system": "http://unitsofmeasure.org",
                    "code": "mm[Hg]"
                }
            })
        if bp.get('diastolic') is not None:
            components.append({
                "code": {
                    "coding": [{
                        "system": "http://loinc.org",
                        "code": "8462-4",
                        "display": "Diastolic blood pressure"
                    }]
                },
                "valueQuantity": {
                    "value": bp['diastolic'],
                    "unit": "mmHg",
                    "system": "http://unitsofmeasure.org",
                    "code": "mm[Hg]"
                }
            })
        return components

    def _create_daily_hr_observation(self, day_key, readings):
        """Create daily Heart Rate summary"""
        try:
            if not readings:
                return None

            dt = datetime.strptime(day_key, '%Y-%m-%d')
            dt = dt.replace(hour=12)  # Noon

            avg_hr = sum(readings) / len(readings)
            min_hr = min(readings)
            max_hr = max(readings)

            return {
                "resource": {
                    "resourceType": "Observation",
                    "id": str(uuid.uuid4()),
                    "status": "final",
                    "category": [{
                        "coding": [{
                            "system": "http://terminology.hl7.org/CodeSystem/observation-category",
                            "code": "vital-signs"
                        }]
                    }],
                    "code": {
                        "coding": [{
                            "system": "http://loinc.org",
                            "code": "8867-4",
                            "display": "Heart rate"
                        }]
                    },
                    "effectiveDateTime": dt.isoformat(),
                    "issued": dt.isoformat(),
                    "valueQuantity": {
                        "value": round(avg_hr, 1),
                        "unit": "beats/minute",
                        "system": "http://unitsofmeasure.org",
                        "code": "{beats}/min"
                    },
                    "component": [
                        {
                            "code": {
                                "coding": [{
                                    "system": "http://loinc.org",
                                    "code": "8867-4",
                                    "display": "Heart rate minimum"
                                }]
                            },
                            "valueQuantity": {
                                "value": min_hr,
                                "unit": "beats/minute",
                                "system": "http://unitsofmeasure.org",
                                "code": "{beats}/min"
                            }
                        },
                        {
                            "code": {
                                "coding": [{
                                    "system": "http://loinc.org",
                                    "code": "8867-4",
                                    "display": "Heart rate maximum"
                                }]
                            },
                            "valueQuantity": {
                                "value": max_hr,
                                "unit": "beats/minute",
                                "system": "http://unitsofmeasure.org",
                                "code": "{beats}/min"
                            }
                        }
                    ]
                }
            }
        except Exception as e:
            print(f"Error creating daily HR observation: {e}")
            return None

    def _create_resting_hr_observation(self, rhr):
        """Create Resting Heart Rate Observation"""
        try:
            dt = self._parse_datetime(rhr['startDate'])
            if not dt:
                return None
            return {
                "resource": {
                    "resourceType": "Observation",
                    "id": str(uuid.uuid4()),
                    "status": "final",
                    "category": [{
                        "coding": [{
                            "system": "http://terminology.hl7.org/CodeSystem/observation-category",
                            "code": "vital-signs"
                        }]
                    }],
                    "code": {
                        "coding": [{
                            "system": "http://loinc.org",
                            "code": "40443-4",
                            "display": "Heart rate, resting"
                        }]
                    },
                    "effectiveDateTime": dt.isoformat(),
                    "issued": dt.isoformat(),
                    "valueQuantity": {
                        "value": rhr['value'],
                        "unit": "beats/minute",
                        "system": "http://unitsofmeasure.org",
                        "code": "{beats}/min"
                    },
                    "device": {
                        "reference": f"Device/{self.get_device_from_source(rhr['sourceName'])}"
                    }
                }
            }
        except Exception as e:
            print(f"Error creating resting HR observation: {e}")
            return None

    def _create_daily_spo2_observation(self, day_key, readings):
        """Create daily SpO2 summary"""
        try:
            if not readings:
                return None

            dt = datetime.strptime(day_key, '%Y-%m-%d')
            dt = dt.replace(hour=12)

            avg_spo2 = sum(readings) / len(readings)
            min_spo2 = min(readings)

            interpretation = []
            if min_spo2 < 92:
                interpretation.append({
                    "coding": [{
                        "system": "http://terminology.hl7.org/CodeSystem/v3-ObservationInterpretation",
                        "code": "L",
                        "display": "Low"
                    }]
                })

            return {
                "resource": {
                    "resourceType": "Observation",
                    "id": str(uuid.uuid4()),
                    "status": "final",
                    "category": [{
                        "coding": [{
                            "system": "http://terminology.hl7.org/CodeSystem/observation-category",
                            "code": "vital-signs"
                        }]
                    }],
                    "code": {
                        "coding": [{
                            "system": "http://loinc.org",
                            "code": "59408-5",
                            "display": "Oxygen saturation"
                        }]
                    },
                    "effectiveDateTime": dt.isoformat(),
                    "issued": dt.isoformat(),
                    "valueQuantity": {
                        "value": round(avg_spo2, 1),
                        "unit": "%",
                        "system": "http://unitsofmeasure.org",
                        "code": "%"
                    },
                    "interpretation": interpretation if interpretation else None,
                    "component": [
                        {
                            "code": {
                                "coding": [{
                                    "system": "http://loinc.org",
                                    "code": "59408-5",
                                    "display": "Oxygen saturation minimum"
                                }]
                            },
                            "valueQuantity": {
                                "value": min_spo2,
                                "unit": "%",
                                "system": "http://unitsofmeasure.org",
                                "code": "%"
                            }
                        }
                    ]
                }
            }
        except Exception as e:
            print(f"Error creating daily SpO2 observation: {e}")
            return None

    def _create_daily_hrv_observation(self, day_key, readings):
        """Create daily HRV summary"""
        try:
            if not readings:
                return None

            dt = datetime.strptime(day_key, '%Y-%m-%d')
            dt = dt.replace(hour=12)

            avg_hrv = sum(readings) / len(readings)

            return {
                "resource": {
                    "resourceType": "Observation",
                    "id": str(uuid.uuid4()),
                    "status": "final",
                    "category": [{
                        "coding": [{
                            "system": "http://terminology.hl7.org/CodeSystem/observation-category",
                            "code": "vital-signs"
                        }]
                    }],
                    "code": {
                        "coding": [{
                            "system": "http://loinc.org",
                            "code": "80404-7",
                            "display": "Heart rate variability SDNN"
                        }]
                    },
                    "effectiveDateTime": dt.isoformat(),
                    "issued": dt.isoformat(),
                    "valueQuantity": {
                        "value": round(avg_hrv, 1),
                        "unit": "ms",
                        "system": "http://unitsofmeasure.org",
                        "code": "ms"
                    }
                }
            }
        except Exception as e:
            print(f"Error creating daily HRV observation: {e}")
            return None

    def _create_daily_respiratory_observation(self, day_key, readings):
        """Create daily Respiratory Rate summary"""
        try:
            if not readings:
                return None

            dt = datetime.strptime(day_key, '%Y-%m-%d')
            dt = dt.replace(hour=12)

            avg_rr = sum(readings) / len(readings)

            return {
                "resource": {
                    "resourceType": "Observation",
                    "id": str(uuid.uuid4()),
                    "status": "final",
                    "category": [{
                        "coding": [{
                            "system": "http://terminology.hl7.org/CodeSystem/observation-category",
                            "code": "vital-signs"
                        }]
                    }],
                    "code": {
                        "coding": [{
                            "system": "http://loinc.org",
                            "code": "9279-1",
                            "display": "Respiratory rate"
                        }]
                    },
                    "effectiveDateTime": dt.isoformat(),
                    "issued": dt.isoformat(),
                    "valueQuantity": {
                        "value": round(avg_rr, 1),
                        "unit": "breaths/minute",
                        "system": "http://unitsofmeasure.org",
                        "code": "/min"
                    }
                }
            }
        except Exception as e:
            print(f"Error creating daily respiratory observation: {e}")
            return None

    def _create_daily_steps_observation(self, day_key, steps):
        """Create daily Step Count Observation"""
        try:
            dt = datetime.strptime(day_key, '%Y-%m-%d')
            dt = dt.replace(hour=12)

            return {
                "resource": {
                    "resourceType": "Observation",
                    "id": str(uuid.uuid4()),
                    "status": "final",
                    "category": [{
                        "coding": [{
                            "system": "http://terminology.hl7.org/CodeSystem/observation-category",
                            "code": "activity"
                        }]
                    }],
                    "code": {
                        "coding": [{
                            "system": "http://loinc.org",
                            "code": "55423-8",
                            "display": "Number of steps"
                        }]
                    },
                    "effectiveDateTime": dt.isoformat(),
                    "issued": dt.isoformat(),
                    "valueQuantity": {
                        "value": steps,
                        "unit": "steps",
                        "system": "http://unitsofmeasure.org",
                        "code": "{steps}"
                    }
                }
            }
        except Exception as e:
            print(f"Error creating daily steps observation: {e}")
            return None

    def _create_bmi_observation(self, bmi):
        """Create BMI Observation"""
        try:
            dt = self._parse_datetime(bmi['startDate'])
            if not dt:
                return None
            return {
                "resource": {
                    "resourceType": "Observation",
                    "id": str(uuid.uuid4()),
                    "status": "final",
                    "category": [{
                        "coding": [{
                            "system": "http://terminology.hl7.org/CodeSystem/observation-category",
                            "code": "vital-signs"
                        }]
                    }],
                    "code": {
                        "coding": [{
                            "system": "http://loinc.org",
                            "code": "39156-5",
                            "display": "Body Mass Index"
                        }]
                    },
                    "effectiveDateTime": dt.isoformat(),
                    "issued": dt.isoformat(),
                    "valueQuantity": {
                        "value": bmi['value'],
                        "unit": "kg/m2",
                        "system": "http://unitsofmeasure.org",
                        "code": "kg/m2"
                    },
                    "device": {
                        "reference": f"Device/{self.get_device_from_source(bmi['sourceName'])}"
                    }
                }
            }
        except Exception as e:
            print(f"Error creating BMI observation: {e}")
            return None

    def _create_sleep_observation(self, night_key, sleep_data):
        """Create Sleep Observation"""
        try:
            dt = datetime.strptime(night_key, '%Y-%m-%d')
            dt = dt.replace(hour=12)

            # Aggregate sleep times
            asleep_total = sum(sleep_data['asleep']) if sleep_data['asleep'] else 0
            inbed_total = sum(sleep_data['inBed']) if sleep_data['inBed'] else 0

            # Convert seconds to hours for display
            sleep_hours = asleep_total / 3600 if asleep_total > 0 else 0

            if sleep_hours == 0 and inbed_total == 0:
                return None

            component = []
            if asleep_total > 0:
                component.append({
                    "code": {
                        "coding": [{
                            "system": "http://loinc.org",
                            "code": "93832-4",
                            "display": "Sleep duration"
                        }]
                    },
                    "valueQuantity": {
                        "value": round(sleep_hours, 2),
                        "unit": "hours",
                        "system": "http://unitsofmeasure.org",
                        "code": "h"
                    }
                })

            if inbed_total > 0:
                component.append({
                    "code": {
                        "coding": [{
                            "system": "http://loinc.org",
                            "code": "93832-4",
                            "display": "Time in bed"
                        }]
                    },
                    "valueQuantity": {
                        "value": round(inbed_total / 3600, 2),
                        "unit": "hours",
                        "system": "http://unitsofmeasure.org",
                        "code": "h"
                    }
                })

            return {
                "resource": {
                    "resourceType": "Observation",
                    "id": str(uuid.uuid4()),
                    "status": "final",
                    "category": [{
                        "coding": [{
                            "system": "http://terminology.hl7.org/CodeSystem/observation-category",
                            "code": "social-history"
                        }]
                    }],
                    "code": {
                        "coding": [{
                            "system": "http://loinc.org",
                            "code": "93832-4",
                            "display": "Sleep duration"
                        }]
                    },
                    "effectiveDateTime": dt.isoformat(),
                    "issued": dt.isoformat(),
                    "component": component if component else None
                }
            }
        except Exception as e:
            print(f"Error creating sleep observation: {e}")
            return None

    def save_bundle(self, bundle, output_file):
        """Save FHIR bundle to JSON"""
        with open(output_file, 'w') as f:
            json.dump(bundle, f, indent=2)
        print(f"Saved FHIR bundle to {output_file}")

    def generate_report(self, bundle):
        """Generate summary report"""
        report = []
        report.append("# Apple Health Wearable Data to FHIR R4 Conversion Report\n")
        report.append(f"Generated: {datetime.now().isoformat()}\n\n")

        report.append("## Date Range\n")
        if self.date_range['min'] and self.date_range['max']:
            report.append(f"- Start: {self.date_range['min'].strftime('%Y-%m-%d')}\n")
            report.append(f"- End: {self.date_range['max'].strftime('%Y-%m-%d')}\n")
            days = (self.date_range['max'] - self.date_range['min']).days + 1
            report.append(f"- Duration: {days} days\n\n")

        report.append("## Data Processing Statistics\n\n")
        report.append("### Input Records (Raw)\n")
        report.append(f"- Heart Rate readings: {self.stats['heart_rate']:,}\n")
        report.append(f"- Blood Pressure readings: {self.stats['blood_pressure']:,}\n")
        report.append(f"- SpO2 readings: {self.stats['spo2']:,}\n")
        report.append(f"- HRV readings: {self.stats['hrv']:,}\n")
        report.append(f"- Step Count readings: {self.stats['steps']:,}\n")
        report.append(f"- Respiratory Rate readings: {self.stats['respiratory']:,}\n")
        report.append(f"- Sleep Analysis records: {self.stats['sleep']:,}\n")
        report.append(f"- Resting Heart Rate readings: {self.stats['resting_hr']:,}\n")
        report.append(f"- BMI readings: {self.stats['bmi']:,}\n")
        report.append(f"- Processing errors: {self.stats['errors']}\n\n")

        report.append("### Output FHIR Observations\n")
        report.append(f"- Total FHIR Observations created: {bundle['total']:,}\n\n")

        report.append("### Aggregation Summary\n")
        report.append(f"- Blood Pressure Observations: {len([e for e in bundle['entry'] if 'BloodPressure' in e['resource'].get('id', '')])}\n")

        bp_count = sum(1 for e in bundle['entry'] if e['resource']['code']['coding'][0]['code'] == '85354-9')
        daily_hr_count = sum(1 for e in bundle['entry'] if e['resource']['code']['coding'][0]['code'] == '8867-4')
        resting_hr_count = sum(1 for e in bundle['entry'] if e['resource']['code']['coding'][0]['code'] == '40443-4')
        spo2_count = sum(1 for e in bundle['entry'] if e['resource']['code']['coding'][0]['code'] == '59408-5')
        hrv_count = sum(1 for e in bundle['entry'] if e['resource']['code']['coding'][0]['code'] == '80404-7')
        rr_count = sum(1 for e in bundle['entry'] if e['resource']['code']['coding'][0]['code'] == '9279-1')
        steps_count = sum(1 for e in bundle['entry'] if e['resource']['code']['coding'][0]['code'] == '55423-8')
        bmi_count = sum(1 for e in bundle['entry'] if e['resource']['code']['coding'][0]['code'] == '39156-5')
        sleep_count = sum(1 for e in bundle['entry'] if e['resource']['code']['coding'][0]['code'] == '93832-4')

        report.append(f"- Blood Pressure: {bp_count} observations\n")
        report.append(f"- Daily Heart Rate (avg/min/max): {daily_hr_count} daily summaries\n")
        report.append(f"- Resting Heart Rate: {resting_hr_count} observations\n")
        report.append(f"- Daily SpO2 (avg/min): {spo2_count} daily summaries\n")
        report.append(f"- Daily HRV (avg SDNN): {hrv_count} daily summaries\n")
        report.append(f"- Daily Respiratory Rate: {rr_count} daily summaries\n")
        report.append(f"- Daily Steps: {steps_count} daily totals\n")
        report.append(f"- BMI: {bmi_count} observations\n")
        report.append(f"- Sleep Analysis: {sleep_count} nightly summaries\n\n")

        report.append("## Clinical Findings\n\n")

        # Find abnormal SpO2 readings
        low_spo2 = [e for e in bundle['entry'] if
                   e['resource']['code']['coding'][0]['code'] == '59408-5' and
                   e['resource'].get('interpretation')]
        if low_spo2:
            report.append(f"**WARNING: {len(low_spo2)} days with SpO2 below 92%** (potential hypoxemia)\n\n")

        # Analyze heart rate ranges
        hr_obs = [e['resource'] for e in bundle['entry'] if
                  e['resource']['code']['coding'][0]['code'] == '8867-4' and
                  'component' in e['resource']]
        if hr_obs:
            max_hrs = []
            for obs in hr_obs:
                for comp in obs.get('component', []):
                    if 'maximum' in comp['code']['coding'][0].get('display', ''):
                        max_hrs.append(comp['valueQuantity']['value'])
            if max_hrs:
                report.append(f"**Heart Rate Analysis:**\n")
                report.append(f"- Highest daily max HR: {max(max_hrs)} bpm\n")
                report.append(f"- Elevated HR days (>100 bpm max): {sum(1 for h in max_hrs if h > 100)}\n\n")

        # Analyze sleep
        sleep_obs = [e['resource'] for e in bundle['entry'] if
                    e['resource']['code']['coding'][0]['code'] == '93832-4']
        if sleep_obs:
            sleep_hours = []
            for obs in sleep_obs:
                for comp in obs.get('component', []):
                    if 'Sleep duration' in comp['code']['coding'][0].get('display', ''):
                        sleep_hours.append(comp['valueQuantity']['value'])
            if sleep_hours:
                avg_sleep = sum(sleep_hours) / len(sleep_hours)
                report.append(f"**Sleep Analysis:**\n")
                report.append(f"- Average nightly sleep: {avg_sleep:.1f} hours\n")
                report.append(f"- Nights with <6 hours sleep: {sum(1 for s in sleep_hours if s < 6)}\n")
                report.append(f"- Nights with >9 hours sleep: {sum(1 for s in sleep_hours if s > 9)}\n\n")

        report.append("## Files Generated\n\n")
        report.append(f"- FHIR Bundle: `apple_wearable_fhir_bundle.json`\n")
        report.append(f"- Report: `apple_wearable_report.md`\n")

        return ''.join(report)


def main():
    xml_path = '/sessions/admiring-vigilant-brown/mnt/Medical/New exports/apple_health_export/export.xml'
    output_dir = '/sessions/admiring-vigilant-brown/mnt/Medical/Synthesis'

    # Date cutoff: last 2 years (optional - comment out to process all data)
    # date_cutoff = datetime.now() - timedelta(days=730)
    date_cutoff = None

    parser = AppleHealthParser(xml_path, output_dir, date_cutoff)

    print("=" * 70)
    print("APPLE HEALTH WEARABLE DATA TO FHIR R4 CONVERSION")
    print("=" * 70)
    print()

    # Parse XML
    start_time = time.time()
    parser.parse_xml()
    parse_time = time.time() - start_time

    print()
    print(f"Parse completed in {parse_time:.1f} seconds")
    print(f"Date range: {parser.date_range['min']} to {parser.date_range['max']}")
    print()

    # Create FHIR bundle
    print("Creating FHIR R4 bundle...")
    start_time = time.time()
    bundle = parser.create_fhir_bundle()
    bundle_time = time.time() - start_time

    print(f"Bundle creation completed in {bundle_time:.1f} seconds")
    print(f"Total FHIR Observations: {bundle['total']:,}")
    print()

    # Save bundle
    bundle_path = Path(output_dir) / 'apple_wearable_fhir_bundle.json'
    parser.save_bundle(bundle, bundle_path)

    # Generate report
    report = parser.generate_report(bundle)
    report_path = Path(output_dir) / 'apple_wearable_report.md'
    with open(report_path, 'w') as f:
        f.write(report)
    print(f"Saved report to {report_path}")
    print()

    print("=" * 70)
    print("CONVERSION COMPLETE")
    print("=" * 70)
    print()
    print(report)


if __name__ == '__main__':
    main()
