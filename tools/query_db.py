#!/usr/bin/env python3
"""
FHIR Database Query Tool
Query and inspect the FHIR SQLite database directly.
"""

import sqlite3
import json
import sys
from pathlib import Path


class FHIRDatabaseQuery:
    """Query interface for FHIR database."""

    def __init__(self, db_path="fhir.db"):
        self.db_path = db_path

    def list_patients(self):
        """List all patients in the database."""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT data FROM resources
                WHERE resource_type = 'Patient' AND is_deleted = 0
                ORDER BY updated_at DESC
            """)

            rows = cursor.fetchall()
            print(f"\n{'='*60}")
            print(f"Patients ({len(rows)})")
            print(f"{'='*60}")

            if not rows:
                print("No patients found.")
                return

            for i, row in enumerate(rows, 1):
                patient = json.loads(row[0])
                name_data = patient['name'][0] if patient.get('name') else {}
                given = ' '.join(name_data.get('given', []))
                family = name_data.get('family', '')
                birth_date = patient.get('birthDate', 'N/A')
                gender = patient.get('gender', 'N/A')
                patient_id = patient.get('id', 'N/A')

                print(f"\n{i}. {given} {family}")
                print(f"   ID: {patient_id}")
                print(f"   Birth Date: {birth_date}")
                print(f"   Gender: {gender}")

                if patient.get('identifier'):
                    for ident in patient['identifier']:
                        print(f"   Identifier: {ident.get('value', 'N/A')}")

            print(f"\n{'='*60}\n")

    def list_observations(self):
        """List all observations in the database."""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT data FROM resources
                WHERE resource_type = 'Observation' AND is_deleted = 0
                ORDER BY updated_at DESC
            """)

            rows = cursor.fetchall()
            print(f"\n{'='*60}")
            print(f"Observations ({len(rows)})")
            print(f"{'='*60}")

            if not rows:
                print("No observations found.")
                return

            for i, row in enumerate(rows, 1):
                obs = json.loads(row[0])
                code = obs.get('code', {}).get('coding', [{}])[0].get('code', 'N/A')
                display = obs.get('code', {}).get('coding', [{}])[0].get('display', code)
                status = obs.get('status', 'N/A')
                subject = obs.get('subject', {}).get('reference', 'N/A')
                obs_id = obs.get('id', 'N/A')

                value_str = 'N/A'
                if 'valueQuantity' in obs:
                    val = obs['valueQuantity'].get('value', 'N/A')
                    unit = obs['valueQuantity'].get('unit', '')
                    value_str = f"{val} {unit}".strip()

                print(f"\n{i}. {display}")
                print(f"   ID: {obs_id}")
                print(f"   Code: {code}")
                print(f"   Status: {status}")
                print(f"   Subject: {subject}")
                print(f"   Value: {value_str}")

            print(f"\n{'='*60}\n")

    def patient_observations(self, patient_id):
        """List observations for a specific patient."""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()

            # Get patient
            cursor.execute("""
                SELECT data FROM resources
                WHERE resource_type = 'Patient' AND logical_id = ? AND is_deleted = 0
            """, (patient_id,))

            patient_row = cursor.fetchone()
            if not patient_row:
                print(f"Patient {patient_id} not found.")
                return

            patient = json.loads(patient_row[0])
            name_data = patient['name'][0] if patient.get('name') else {}
            given = ' '.join(name_data.get('given', []))
            family = name_data.get('family', '')

            print(f"\n{'='*60}")
            print(f"Observations for {given} {family}")
            print(f"{'='*60}")

            # Get observations
            cursor.execute("""
                SELECT data FROM resources
                WHERE resource_type = 'Observation' AND is_deleted = 0
                AND data LIKE ?
                ORDER BY updated_at DESC
            """, (f"%{patient_id}%",))

            rows = cursor.fetchall()

            if not rows:
                print(f"No observations found for this patient.")
                print(f"{'='*60}\n")
                return

            print(f"\nTotal: {len(rows)} observation(s)\n")

            for i, row in enumerate(rows, 1):
                obs = json.loads(row[0])
                code = obs.get('code', {}).get('coding', [{}])[0].get('code', 'N/A')
                display = obs.get('code', {}).get('coding', [{}])[0].get('display', code)
                status = obs.get('status', 'N/A')
                eff_date = obs.get('effectiveDateTime', 'N/A')

                value_str = 'N/A'
                if 'valueQuantity' in obs:
                    val = obs['valueQuantity'].get('value', 'N/A')
                    unit = obs['valueQuantity'].get('unit', '')
                    value_str = f"{val} {unit}".strip()

                print(f"{i}. {display}")
                print(f"   Code: {code}")
                print(f"   Status: {status}")
                print(f"   Effective Date: {eff_date}")
                print(f"   Value: {value_str}\n")

            print(f"{'='*60}\n")

    def statistics(self):
        """Show database statistics."""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()

            # Total resources
            cursor.execute("SELECT COUNT(*) FROM resources WHERE is_deleted = 0")
            total = cursor.fetchone()[0]

            # By type
            cursor.execute("""
                SELECT resource_type, COUNT(*) as count
                FROM resources WHERE is_deleted = 0
                GROUP BY resource_type
            """)
            type_counts = cursor.fetchall()

            # Total versions
            cursor.execute("""
                SELECT COUNT(*) FROM resources
            """)
            all_versions = cursor.fetchone()[0]

            print(f"\n{'='*60}")
            print(f"Database Statistics")
            print(f"{'='*60}")
            print(f"\nDatabase file: {self.db_path}")
            print(f"\nActive Resources: {total}")
            print(f"Total Resource Versions: {all_versions}")
            print(f"\nBreakdown by Type:")

            for res_type, count in type_counts:
                print(f"  {res_type}: {count}")

            # File size
            path = Path(self.db_path)
            if path.exists():
                size_mb = path.stat().st_size / (1024 * 1024)
                print(f"\nDatabase Size: {size_mb:.2f} MB")

            print(f"\n{'='*60}\n")

    def show_patient(self, patient_id):
        """Show detailed patient information."""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT data FROM resources
                WHERE resource_type = 'Patient' AND logical_id = ? AND is_deleted = 0
            """, (patient_id,))

            row = cursor.fetchone()
            if not row:
                print(f"Patient {patient_id} not found.")
                return

            patient = json.loads(row[0])
            print(f"\n{'='*60}")
            print(f"Patient Details")
            print(f"{'='*60}")
            print(json.dumps(patient, indent=2))
            print(f"{'='*60}\n")

    def show_observation(self, obs_id):
        """Show detailed observation information."""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT data FROM resources
                WHERE resource_type = 'Observation' AND logical_id = ? AND is_deleted = 0
            """, (obs_id,))

            row = cursor.fetchone()
            if not row:
                print(f"Observation {obs_id} not found.")
                return

            obs = json.loads(row[0])
            print(f"\n{'='*60}")
            print(f"Observation Details")
            print(f"{'='*60}")
            print(json.dumps(obs, indent=2))
            print(f"{'='*60}\n")


def main():
    """Command-line interface."""
    db = FHIRDatabaseQuery()

    if len(sys.argv) < 2:
        print("\nFHIR Database Query Tool")
        print("\nUsage:")
        print("  python3 query_db.py stats              - Show database statistics")
        print("  python3 query_db.py patients           - List all patients")
        print("  python3 query_db.py patient [id]       - Show patient details")
        print("  python3 query_db.py observations       - List all observations")
        print("  python3 query_db.py patient-obs [id]   - Show observations for patient")
        print("  python3 query_db.py observation [id]   - Show observation details")
        print()
        return

    command = sys.argv[1].lower()

    if command == 'stats':
        db.statistics()
    elif command == 'patients':
        db.list_patients()
    elif command == 'patient' and len(sys.argv) > 2:
        db.show_patient(sys.argv[2])
    elif command == 'observations':
        db.list_observations()
    elif command == 'patient-obs' and len(sys.argv) > 2:
        db.patient_observations(sys.argv[2])
    elif command == 'observation' and len(sys.argv) > 2:
        db.show_observation(sys.argv[2])
    else:
        print(f"Unknown command: {command}")
        print("Use 'python3 query_db.py' with no arguments for usage info.")


if __name__ == '__main__':
    main()
