#!/usr/bin/env python3
"""
Specimen Type Mismatch Detector and Fixer for LOINC Mapper

This script runs as a post-processing step after loinc_mapper.py to detect and correct
cases where LOINC codes have been assigned to observations with mismatched specimen types.

Problem: The LOINC mapper sometimes assigns serum LOINC codes to urine observations
(and vice versa) because the lookup table doesn't consider specimen type.

Example: "Protein Electrophoresis, Urine" might get mapped to a serum electrophoresis LOINC code.

Solution:
1. Fetch all observations from HAPI FHIR server
2. Extract specimen hints from the raw observation name and LOINC display
3. Detect mismatches between raw specimen type and LOINC specimen type
4. Apply known corrections from SPECIMEN_CORRECTIONS mapping
5. Report remaining mismatches for manual review
"""

import argparse
import json
import re
import sys
from typing import Dict, List, Optional, Set, Tuple
from dataclasses import dataclass
from enum import Enum

import requests
from fhir_utils import add_provenance_tag

# HAPI FHIR Server configuration
FHIR_BASE_URL = "http://localhost:8080/fhir"
OBSERVATION_ENDPOINT = f"{FHIR_BASE_URL}/Observation"


class SpecimenType(Enum):
    """Standard specimen types extracted from observation data."""
    URINE = "urine"
    SERUM = "serum"
    PLASMA = "plasma"
    BLOOD = "blood"
    CSF = "csf"
    STOOL = "stool"
    UNKNOWN = "unknown"


@dataclass
class SpecimenMismatch:
    """Represents a specimen type mismatch detected in an observation."""
    observation_id: str
    code: str
    raw_name: str
    loinc_display: str
    raw_specimen: SpecimenType
    loinc_specimen: SpecimenType

    def __str__(self) -> str:
        return (
            f"Obs {self.observation_id}: {self.raw_name} (specimen: {self.raw_specimen.value}) "
            f"vs LOINC {self.code}: {self.loinc_display} (specimen: {self.loinc_specimen.value})"
        )


def extract_specimen_type(text: str, context: str = "raw") -> SpecimenType:
    """
    Extract specimen type from text using keyword patterns.

    Args:
        text: The text to search (observation name, LOINC display, etc.)
        context: Either "raw" (direct keywords) or "loinc" (keywords preceded by "in")

    Returns:
        SpecimenType enum value
    """
    if not text:
        return SpecimenType.UNKNOWN

    text_lower = text.lower()

    if context == "loinc":
        # LOINC display: look for "in Serum", "in Urine", etc.
        # Case-insensitive, preceded by " in "
        patterns = {
            SpecimenType.URINE: r'\bin\s+urine\b',
            SpecimenType.SERUM: r'\bin\s+serum\b',
            SpecimenType.PLASMA: r'\bin\s+plasma\b',
            SpecimenType.BLOOD: r'\bin\s+blood\b',
            SpecimenType.CSF: r'\bin\s+csf\b',
            SpecimenType.STOOL: r'\bin\s+stool\b',
        }
    else:
        # Raw name: direct keyword match
        patterns = {
            SpecimenType.URINE: r'\b(?:urine|24\s*hour\s*urine|24h\s*urine)\b',
            SpecimenType.SERUM: r'\bserum\b',
            SpecimenType.PLASMA: r'\bplasma\b',
            SpecimenType.BLOOD: r'\bblood\b',
            SpecimenType.CSF: r'\bcsf\b',
            SpecimenType.STOOL: r'\b(?:stool|feces)\b',
        }

    # Check patterns in priority order (urine and serum are most common)
    for specimen_type, pattern in patterns.items():
        if re.search(pattern, text_lower):
            return specimen_type

    return SpecimenType.UNKNOWN


def extract_raw_name(observation: Dict) -> str:
    """
    Extract the raw observation name from an observation resource.

    Tries multiple sources:
    1. code.text (user-friendly display)
    2. code.coding[0].display (FHIR standard display)
    3. Custom 'raw-name' extension or tag
    4. Empty string if none found
    """
    if not observation:
        return ""

    # Try code.text first
    if observation.get("code", {}).get("text"):
        return observation["code"]["text"]

    # Try code.coding display
    codings = observation.get("code", {}).get("coding", [])
    if codings and codings[0].get("display"):
        return codings[0]["display"]

    # Try to find raw-name in extensions
    for extension in observation.get("extension", []):
        if extension.get("url", "").endswith("raw-name"):
            return extension.get("valueString", "")

    return ""


def extract_loinc_display(observation: Dict) -> str:
    """
    Extract the LOINC code display from an observation.

    Returns the display text of the LOINC coding if present.
    """
    codings = observation.get("code", {}).get("coding", [])
    for coding in codings:
        # Check if this is a LOINC code (system = http://loinc.org)
        if "loinc.org" in coding.get("system", ""):
            return coding.get("display", "")

    return ""


def extract_loinc_code(observation: Dict) -> Optional[str]:
    """
    Extract the LOINC code from an observation.

    Returns the code value if a LOINC coding exists.
    """
    codings = observation.get("code", {}).get("coding", [])
    for coding in codings:
        if "loinc.org" in coding.get("system", ""):
            return coding.get("code")

    return None


def detect_mismatch(observation: Dict) -> Optional[SpecimenMismatch]:
    """
    Detect if an observation has a specimen type mismatch.

    Returns a SpecimenMismatch object if mismatch is detected, None otherwise.
    """
    obs_id = observation.get("id", "unknown")
    loinc_code = extract_loinc_code(observation)

    # Skip if no LOINC code
    if not loinc_code:
        return None

    raw_name = extract_raw_name(observation)
    loinc_display = extract_loinc_display(observation)

    # Extract specimen types
    raw_specimen = extract_specimen_type(raw_name, context="raw")
    loinc_specimen = extract_specimen_type(loinc_display, context="loinc")

    # If either is unknown, we can't determine a mismatch
    if raw_specimen == SpecimenType.UNKNOWN or loinc_specimen == SpecimenType.UNKNOWN:
        return None

    # Check if there's a mismatch
    if raw_specimen != loinc_specimen:
        return SpecimenMismatch(
            observation_id=obs_id,
            code=loinc_code,
            raw_name=raw_name,
            loinc_display=loinc_display,
            raw_specimen=raw_specimen,
            loinc_specimen=loinc_specimen,
        )

    return None


# Known corrections for specimen mismatches
# Maps (wrong_code, specimen_hint) -> (correct_code, correct_display)
SPECIMEN_CORRECTIONS = {
    # UPEP fractions: serum code → urine code
    ('2865-4', 'urine'): ('13991-3', 'Alpha-1 globulin [Mass/volume] in Urine by Electrophoresis'),
    ('2867-0', 'urine'): ('13993-9', 'Alpha-2 globulin [Mass/volume] in Urine by Electrophoresis'),
    ('2868-8', 'urine'): ('13995-4', 'Beta globulin [Mass/volume] in Urine by Electrophoresis'),
    ('2871-2', 'urine'): ('13997-0', 'Gamma globulin [Mass/volume] in Urine by Electrophoresis'),
    # M-protein serum → urine
    ('51435-6', 'urine'): ('56759-4', 'M-protein [Mass/volume] in Urine by Electrophoresis'),
}


def find_correction(code: str, specimen: SpecimenType) -> Optional[Tuple[str, str]]:
    """
    Look up a correction for a specimen mismatch.

    Args:
        code: The LOINC code that was incorrectly assigned
        specimen: The correct specimen type that should have been used

    Returns:
        Tuple of (correct_code, correct_display) if found, None otherwise
    """
    key = (code, specimen.value)
    if key in SPECIMEN_CORRECTIONS:
        correct_code, correct_display = SPECIMEN_CORRECTIONS[key]
        return (correct_code, correct_display)

    return None


def fetch_all_observations(skip: int = 0, count: int = 50) -> List[Dict]:
    """
    Fetch all observations from HAPI FHIR server using pagination.

    Args:
        skip: Number of records to skip (for pagination)
        count: Number of records to fetch per request

    Returns:
        List of all observation resources
    """
    all_observations = []
    offset = skip

    while True:
        params = {
            "_count": count,
            "_offset": offset,
        }

        try:
            response = requests.get(OBSERVATION_ENDPOINT, params=params, timeout=30)
            response.raise_for_status()
        except requests.RequestException as e:
            print(f"Error fetching observations: {e}", file=sys.stderr)
            return all_observations

        data = response.json()
        entries = data.get("entry", [])

        if not entries:
            break

        for entry in entries:
            if entry.get("resource", {}).get("resourceType") == "Observation":
                all_observations.append(entry["resource"])

        # Check if there are more results
        total = data.get("total", 0)
        if offset + count >= total:
            break

        offset += count

    return all_observations


def apply_correction(observation: Dict, new_code: str, new_display: str) -> Dict:
    """
    Apply a specimen correction to an observation.

    Updates the LOINC code and display, and adds a provenance tag.

    Args:
        observation: The observation resource to correct
        new_code: The correct LOINC code
        new_display: The correct LOINC display

    Returns:
        The updated observation resource
    """
    # Update LOINC coding
    codings = observation.get("code", {}).get("coding", [])
    for coding in codings:
        if "loinc.org" in coding.get("system", ""):
            coding["code"] = new_code
            coding["display"] = new_display
            break

    # Add provenance tag
    observation = add_provenance_tag(observation, "specimen-fix:v1")

    return observation


def update_observation(observation: Dict) -> bool:
    """
    Send an updated observation back to the HAPI FHIR server.

    Args:
        observation: The updated observation resource

    Returns:
        True if successful, False otherwise
    """
    obs_id = observation.get("id")
    if not obs_id:
        print(f"Error: observation has no id", file=sys.stderr)
        return False

    url = f"{OBSERVATION_ENDPOINT}/{obs_id}"

    try:
        response = requests.put(
            url,
            json=observation,
            headers={"Content-Type": "application/fhir+json"},
            timeout=30
        )
        response.raise_for_status()
        return True
    except requests.RequestException as e:
        print(f"Error updating observation {obs_id}: {e}", file=sys.stderr)
        return False


def main():
    """Main entry point for the specimen mismatch fixer."""
    parser = argparse.ArgumentParser(
        description="Detect and fix specimen type mismatches in LOINC-mapped observations"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report mismatches but don't apply corrections"
    )
    parser.add_argument(
        "--stats",
        action="store_true",
        help="Print detailed statistics at the end"
    )

    args = parser.parse_args()

    print("=" * 80)
    print("LOINC Specimen Type Mismatch Fixer")
    print("=" * 80)
    print()

    # Fetch all observations
    print("Fetching observations from HAPI FHIR server...")
    observations = fetch_all_observations()
    print(f"Fetched {len(observations)} observations")
    print()

    # Detect mismatches
    print("Scanning for specimen type mismatches...")
    mismatches: List[SpecimenMismatch] = []
    observations_with_loinc = 0

    for obs in observations:
        if extract_loinc_code(obs):
            observations_with_loinc += 1

        mismatch = detect_mismatch(obs)
        if mismatch:
            mismatches.append(mismatch)

    print(f"Found {len(mismatches)} mismatches out of {observations_with_loinc} observations with LOINC codes")
    print()

    # Categorize mismatches
    correctable: List[Tuple[SpecimenMismatch, str, str]] = []
    uncorrectable: List[SpecimenMismatch] = []

    for mismatch in mismatches:
        correction = find_correction(mismatch.code, mismatch.raw_specimen)
        if correction:
            correctable.append((mismatch, correction[0], correction[1]))
        else:
            uncorrectable.append(mismatch)

    print(f"Correctable mismatches: {len(correctable)}")
    print(f"Uncorrectable mismatches (need manual review): {len(uncorrectable)}")
    print()

    # Apply corrections if not dry-run
    corrections_applied = 0

    if correctable and not args.dry_run:
        print("Applying corrections...")
        for mismatch, correct_code, correct_display in correctable:
            # Find the observation
            obs = next((o for o in observations if o.get("id") == mismatch.observation_id), None)
            if not obs:
                print(f"  Warning: Could not find observation {mismatch.observation_id}", file=sys.stderr)
                continue

            # Apply correction
            obs = apply_correction(obs, correct_code, correct_display)

            # Update in HAPI
            if update_observation(obs):
                corrections_applied += 1
                print(f"  FIXED: {mismatch.observation_id}")
                if args.stats:
                    print(f"    {mismatch.code} → {correct_code}")
            else:
                print(f"  FAILED: {mismatch.observation_id}", file=sys.stderr)

        print()
    elif correctable and args.dry_run:
        print("DRY RUN: Would apply the following corrections:")
        for mismatch, correct_code, correct_display in correctable:
            print(f"  {mismatch.observation_id}: {mismatch.code} → {correct_code}")
        print()

    # Report uncorrectable mismatches for manual review
    if uncorrectable:
        print("=" * 80)
        print(f"MANUAL REVIEW REQUIRED ({len(uncorrectable)} mismatches)")
        print("=" * 80)
        for mismatch in uncorrectable:
            print()
            print(f"Observation ID: {mismatch.observation_id}")
            print(f"Raw name: {mismatch.raw_name}")
            print(f"Raw specimen type: {mismatch.raw_specimen.value}")
            print(f"Current LOINC code: {mismatch.code}")
            print(f"LOINC display: {mismatch.loinc_display}")
            print(f"LOINC specimen type: {mismatch.loinc_specimen.value}")
        print()

    # Final summary
    print("=" * 80)
    print("SUMMARY")
    print("=" * 80)
    print(f"Total observations scanned: {len(observations)}")
    print(f"Observations with LOINC codes: {observations_with_loinc}")
    print(f"Specimen mismatches detected: {len(mismatches)}")
    print(f"  - Correctable (with known fixes): {len(correctable)}")
    print(f"  - Uncorrectable (manual review needed): {len(uncorrectable)}")

    if not args.dry_run:
        print(f"Corrections applied: {corrections_applied}")
    else:
        print(f"DRY RUN: No corrections applied")

    print()

    return 0 if not args.dry_run or len(mismatches) == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
