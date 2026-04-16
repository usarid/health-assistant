#!/usr/bin/env python3
"""
Cross-institution duplicate observation deduplication for HAPI FHIR.

Finds and removes observations that appear to be duplicates across different
institutions (e.g., same lab test from both Sutter and UCSF). Uses fingerprinting
based on LOINC code, test date, and value to identify potential duplicates,
then scores observations by data quality to keep the best version.

Supports dry-run mode (report only) and stats-only mode (counts only).
"""

import argparse
import json
import logging
import requests
from collections import defaultdict
from datetime import datetime
from typing import Dict, List, Tuple, Optional, Set

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Try to import add_provenance_tag, but don't fail if unavailable
try:
    from fhir_utils import add_provenance_tag
except ImportError:
    logger.warning("fhir_utils not found; provenance tagging unavailable")
    add_provenance_tag = None

HAPI_BASE = "http://localhost:8080/fhir"
LOINC_SYSTEM = "http://loinc.org"
SOURCE_TAG_SYSTEM = "http://example.org/source"


def _fetch_page_chain(url: str) -> List[Dict]:
    """Follow a HAPI bundle next-link chain, returning all resources."""
    resources = []
    while url:
        try:
            response = requests.get(url, timeout=60)
            response.raise_for_status()
            bundle = response.json()
        except requests.RequestException as e:
            logger.error(f"Error fetching: {e}")
            break

        entries = bundle.get("entry", [])
        if not entries:
            break

        for entry in entries:
            if entry.get("resource", {}).get("resourceType") == "Observation":
                resources.append(entry["resource"])

        url = None
        for link in bundle.get("link", []):
            if link.get("relation") == "next":
                url = link["url"]
                break

    return resources


def _get_total_count(hapi_base: str = HAPI_BASE) -> Optional[int]:
    """Get total Observation count from HAPI using _summary=count."""
    try:
        r = requests.get(
            f"{hapi_base}/Observation",
            params={"_summary": "count", "_format": "json"},
            timeout=30,
        )
        r.raise_for_status()
        return r.json().get("total")
    except Exception as e:
        logger.warning(f"Could not get total count: {e}")
        return None


def fetch_all_observations(hapi_base: str = HAPI_BASE) -> List[Dict]:
    """
    Fetch all observations from HAPI FHIR by chunking across year ranges.

    HAPI's search result cache can expire before large result sets are fully
    paged through. Fetching by year keeps each search small enough to complete.

    Args:
        hapi_base: Base URL for HAPI FHIR server

    Returns:
        List of observation resources
    """
    observations = []
    seen_ids = set()

    # Chunk by month from 2000-01 to current month.
    # Year-based chunking was too coarse — years with 5000+ observations
    # caused HAPI's search-result cache to expire mid-pagination.
    current_year = datetime.now().year
    current_month = datetime.now().month

    for year in range(2000, current_year + 1):
        year_new = 0
        end_month = 12 if year < current_year else current_month
        for month in range(1, end_month + 1):
            nm = month + 1
            ny = year
            if nm > 12:
                nm = 1
                ny = year + 1
            url = (f"{hapi_base}/Observation?_count=200&_format=json"
                   f"&date=ge{year}-{month:02d}-01&date=lt{ny}-{nm:02d}-01")
            chunk = _fetch_page_chain(url)
            for obs in chunk:
                oid = obs.get("id")
                if oid not in seen_ids:
                    seen_ids.add(oid)
                    observations.append(obs)
                    year_new += 1
        if year_new > 0:
            logger.info(f"  {year}: {year_new} observations")

    # Catch observations without effectiveDateTime (use _count only)
    url = f"{hapi_base}/Observation?_count=200&_format=json&date:missing=true"
    chunk = _fetch_page_chain(url)
    new = 0
    for obs in chunk:
        oid = obs.get("id")
        if oid not in seen_ids:
            seen_ids.add(oid)
            observations.append(obs)
            new += 1
    if new > 0:
        logger.info(f"  (no date): {new} observations")

    # Catch-all: fetch by LOINC code using _offset pagination.
    # The bundle-link page chain (_fetch_page_chain) relies on HAPI's search
    # cache, which expires and truncates large result sets. Using _offset
    # triggers independent DB queries per page — no cache dependency.
    # Each per-code result set is well under HAPI's 10K offset limit.
    total_count = _get_total_count(hapi_base)
    if total_count and len(observations) < total_count:
        missing = total_count - len(observations)
        logger.info(f"  Catch-all: have {len(observations)} of {total_count} "
                    f"— sweeping {missing} remaining by LOINC code (_offset mode)")

        # Collect all distinct LOINC codes from observations we already have
        known_codes = set()
        for obs in observations:
            code = extract_loinc_code(obs)
            if code:
                known_codes.add(code)

        sweep_new = 0
        for code in sorted(known_codes):
            offset = 0
            page_size = 200
            while True:
                try:
                    r = requests.get(
                        f"{hapi_base}/Observation",
                        params={
                            "code": f"http://loinc.org|{code}",
                            "_count": str(page_size),
                            "_offset": str(offset),
                            "_format": "json",
                        },
                        timeout=60,
                    )
                    r.raise_for_status()
                    bundle = r.json()
                except requests.RequestException:
                    break

                entries = bundle.get("entry", [])
                if not entries:
                    break

                code_new = 0
                for entry in entries:
                    obs = entry.get("resource", {})
                    if obs.get("resourceType") != "Observation":
                        continue
                    oid = obs.get("id")
                    if oid and oid not in seen_ids:
                        seen_ids.add(oid)
                        observations.append(obs)
                        code_new += 1
                        sweep_new += 1

                # If we got fewer than page_size, this was the last page
                if len(entries) < page_size:
                    break
                offset += page_size

        # Also try observations with no code at all
        offset = 0
        while True:
            try:
                r = requests.get(
                    f"{hapi_base}/Observation",
                    params={
                        "code:missing": "true",
                        "_count": "200",
                        "_offset": str(offset),
                        "_format": "json",
                    },
                    timeout=60,
                )
                r.raise_for_status()
                bundle = r.json()
            except requests.RequestException:
                break
            entries = bundle.get("entry", [])
            if not entries:
                break
            for entry in entries:
                obs = entry.get("resource", {})
                oid = obs.get("id")
                if oid and oid not in seen_ids:
                    seen_ids.add(oid)
                    observations.append(obs)
                    sweep_new += 1
            if len(entries) < 200:
                break
            offset += 200

        logger.info(f"  After LOINC-code sweep: +{sweep_new} new, "
                    f"{len(observations)} total")

    logger.info(f"  Total observations: {len(observations)}")
    return observations


def extract_loinc_code(observation: Dict) -> Optional[str]:
    """Extract LOINC code from observation.code.coding."""
    if "code" not in observation or "coding" not in observation["code"]:
        return None

    for coding in observation["code"]["coding"]:
        if coding.get("system") == LOINC_SYSTEM:
            return coding.get("code")

    return None


def extract_date(observation: Dict) -> Optional[str]:
    """Extract effective date as YYYY-MM-DD.

    Checks effectiveDateTime first, then effectivePeriod.start, then issued.
    """
    dt_str = None

    if "effectiveDateTime" in observation:
        dt_str = observation["effectiveDateTime"]
    elif "effectivePeriod" in observation:
        dt_str = observation["effectivePeriod"].get("start")
    elif "issued" in observation:
        dt_str = observation["issued"]

    if dt_str:
        try:
            dt = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
            return dt.strftime("%Y-%m-%d")
        except (ValueError, AttributeError):
            return None

    return None


def extract_value(observation: Dict) -> Optional[str]:
    """
    Extract value from observation.
    - For valueQuantity: numeric value rounded to 2 decimal places
    - For valueString: first 50 characters
    - Otherwise: None
    """
    if "valueQuantity" in observation:
        value = observation["valueQuantity"].get("value")
        if isinstance(value, (int, float)):
            return f"{round(float(value), 2):.2f}"

    if "valueString" in observation:
        return observation["valueString"][:50]

    return None


def extract_institution(observation: Dict) -> Optional[str]:
    """
    Extract institution/source identifier from the observation.

    Checks (in order):
    1. meta.tag with system='http://example.org/source' (institution tag)
    2. meta.source (provenance source URI)
    3. meta.tag with system='urn:phv:tag' containing institution-like codes

    Returns a string identifier or None.
    """
    meta = observation.get("meta", {})

    # 1. Check institution tag
    for tag in meta.get("tag", []):
        if tag.get("system") == SOURCE_TAG_SYSTEM:
            return tag.get("code")

    # 2. Check meta.source
    source = meta.get("source")
    if source:
        return source

    # 3. Check PHV tags for institution-like codes
    for tag in meta.get("tag", []):
        if tag.get("system") == "urn:phv:tag":
            code = tag.get("code", "")
            if code.startswith("institution:"):
                return code

    return None


def create_fingerprint(observation: Dict) -> Optional[Tuple]:
    """
    Create fingerprint tuple: (loinc_code, date_YYYY-MM-DD, value_rounded/truncated).
    Returns None if unable to create complete fingerprint.
    """
    loinc = extract_loinc_code(observation)
    date = extract_date(observation)
    value = extract_value(observation)

    if loinc and date and value:
        return (loinc, date, value)

    return None


def calculate_quality_score(observation: Dict) -> int:
    """
    Score observation on data quality.

    Scoring:
    +10 for having valueQuantity
    +8  for having interpretation
    +6  for having referenceRange
    +4  for having performer
    +3  for having identifier
    +2  for having narrative text
    +1  for each additional tag (beyond source tag)
    """
    score = 0

    if "valueQuantity" in observation:
        score += 10

    if "interpretation" in observation:
        score += 8

    if "referenceRange" in observation:
        score += 6

    if "performer" in observation and observation["performer"]:
        score += 4

    if "identifier" in observation and observation["identifier"]:
        score += 3

    if "text" in observation and observation.get("text", {}).get("div"):
        score += 2

    # Count additional tags beyond source tag
    if "meta" in observation and "tag" in observation["meta"]:
        additional_tags = len([
            tag for tag in observation["meta"]["tag"]
            if tag.get("system") != SOURCE_TAG_SYSTEM
        ])
        score += additional_tags

    return score


def group_observations(observations: List[Dict]) -> Dict[Tuple, List[Dict]]:
    """
    Group observations by fingerprint.
    Returns dict mapping fingerprint -> list of observations.
    """
    groups = defaultdict(list)

    for obs in observations:
        fingerprint = create_fingerprint(obs)
        if fingerprint:
            groups[fingerprint].append(obs)

    return dict(groups)


def find_duplicates(
    groups: Dict[Tuple, List[Dict]]
) -> Dict[Tuple, List[Dict]]:
    """
    Filter groups to those with >1 observation sharing the same fingerprint.
    Catches both cross-source duplicates (same test from different institutions)
    and same-source duplicates (identical records loaded twice).
    Returns dict mapping fingerprint -> list of duplicate observations.
    """
    duplicates = {}

    for fingerprint, obs_list in groups.items():
        if len(obs_list) > 1:
            duplicates[fingerprint] = obs_list

    return duplicates


def select_keeper_observation(obs_list: List[Dict]) -> Tuple[Dict, List[Dict]]:
    """
    Select the highest-quality observation to keep.
    Returns (keeper_obs, obs_to_delete).
    """
    if not obs_list:
        return None, []

    # Score all observations
    scored = [
        (calculate_quality_score(obs), obs)
        for obs in obs_list
    ]

    # Sort by score (descending) then by ID for determinism
    scored.sort(
        key=lambda x: (-x[0], x[1].get("id", ""))
    )

    keeper = scored[0][1]
    to_delete = [obs for _, obs in scored[1:]]

    return keeper, to_delete


def build_institution_pair_key(institutions: Set[str]) -> str:
    """
    Create a sorted key for institution pair.
    E.g., "stanford-myhealth-results↔ucsf-mychart-results"
    """
    sorted_insts = sorted(institutions)
    return "↔".join(sorted_insts)


def delete_observation(obs_id: str, hapi_base: str = HAPI_BASE) -> bool:
    """
    Delete observation from HAPI FHIR.

    Args:
        obs_id: Observation ID
        hapi_base: Base URL for HAPI FHIR server

    Returns:
        True if successful, False otherwise
    """
    url = f"{hapi_base}/Observation/{obs_id}"

    try:
        response = requests.delete(url, timeout=30)
        response.raise_for_status()
        logger.info(f"Deleted observation {obs_id}")
        return True
    except requests.RequestException as e:
        logger.error(f"Failed to delete observation {obs_id}: {e}")
        return False


def run_deduplication(
    dry_run: bool = False,
    stats_only: bool = False
) -> Dict:
    """
    Run the cross-source deduplication process.

    Args:
        dry_run: Report only, don't delete
        stats_only: Only report statistics, don't show details

    Returns:
        Dictionary with results
    """
    logger.info("=" * 80)
    logger.info("CROSS-SOURCE DUPLICATE DEDUPLICATION")
    logger.info("=" * 80)
    logger.info(f"HAPI Server: {HAPI_BASE}")
    logger.info(f"Dry Run: {dry_run}")
    logger.info(f"Stats Only: {stats_only}")
    logger.info("")

    # Step 1: Fetch all observations
    logger.info("Step 1: Fetching all observations...")
    observations = fetch_all_observations()
    total_scanned = len(observations)
    logger.info(f"  Total observations: {total_scanned}")
    logger.info("")

    if total_scanned == 0:
        logger.warning("No observations found!")
        return {
            "total_scanned": 0,
            "duplicate_clusters": 0,
            "duplicates_found": 0,
            "deletions_completed": 0,
            "by_institution_pair": {}
        }

    # Step 2: Group by fingerprint
    logger.info("Step 2: Grouping observations by fingerprint...")
    groups = group_observations(observations)
    logger.info(f"  Total fingerprints: {len(groups)}")
    logger.info("")

    # Step 3: Identify duplicates (same fingerprint = same LOINC + date + value)
    logger.info("Step 3: Identifying duplicates...")
    duplicates = find_duplicates(groups)
    total_clusters = len(duplicates)
    logger.info(f"  Duplicate clusters found: {total_clusters}")
    logger.info("")

    if total_clusters == 0:
        logger.info("No duplicates found!")
        return {
            "total_scanned": total_scanned,
            "duplicate_clusters": 0,
            "duplicates_found": 0,
            "deletions_completed": 0,
            "by_institution_pair": {}
        }

    # Step 4: Score and select keepers
    logger.info("Step 4: Scoring observations and selecting keepers...")

    total_duplicates = 0
    deletions_completed = 0
    by_institution_pair = defaultdict(int)

    deletion_queue = []

    for fingerprint, obs_list in duplicates.items():
        keeper, to_delete = select_keeper_observation(obs_list)

        if not stats_only:
            loinc, date, value = fingerprint
            keeper_inst = extract_institution(keeper)
            institutions = set(
                extract_institution(obs) for obs in obs_list
                if extract_institution(obs)
            )

            logger.info(f"\n  Cluster: {loinc} on {date} (value: {value})")
            logger.info(f"    Institutions involved: {', '.join(sorted(institutions))}")
            logger.info(f"    Keeper: {keeper.get('id', 'unknown')} from {keeper_inst} "
                       f"(quality score: {calculate_quality_score(keeper)})")
            logger.info(f"    Duplicates ({len(to_delete)}):")

            for obs in to_delete:
                obs_inst = extract_institution(obs)
                score = calculate_quality_score(obs)
                logger.info(f"      - {obs.get('id', 'unknown')} from {obs_inst} "
                           f"(quality score: {score})")

        # Track by institution pair
        institutions = set(
            extract_institution(obs) for obs in obs_list
            if extract_institution(obs)
        )
        if len(institutions) > 1:
            pair_key = build_institution_pair_key(institutions)
            by_institution_pair[pair_key] += len(to_delete)
        else:
            pair_key = "same-source"
            by_institution_pair[pair_key] += len(to_delete)

        total_duplicates += len(to_delete)
        deletion_queue.extend([(obs.get("id"), pair_key) for obs in to_delete])

    logger.info("")
    logger.info("Step 5: Deletion phase...")
    logger.info(f"  Observations to delete: {total_duplicates}")

    if dry_run:
        logger.info("  DRY RUN: No deletions performed")
        deletions_completed = 0
    else:
        for obs_id, _ in deletion_queue:
            if delete_observation(obs_id):
                deletions_completed += 1

    logger.info("")
    logger.info("=" * 80)
    logger.info("RESULTS")
    logger.info("=" * 80)
    logger.info(f"Total observations scanned:      {total_scanned}")
    logger.info(f"Cross-source duplicate clusters: {total_clusters}")
    logger.info(f"Observations flagged for delete: {total_duplicates}")
    logger.info(f"Deletions completed:             {deletions_completed}")

    if by_institution_pair:
        logger.info("")
        logger.info("Breakdown by institution pair:")
        for pair, count in sorted(by_institution_pair.items()):
            logger.info(f"  {pair}: {count} duplicates")

    logger.info("=" * 80)

    return {
        "total_scanned": total_scanned,
        "duplicate_clusters": total_clusters,
        "duplicates_found": total_duplicates,
        "deletions_completed": deletions_completed,
        "by_institution_pair": dict(by_institution_pair)
    }


def main():
    """Entry point with argument parsing."""
    parser = argparse.ArgumentParser(
        description="Find and remove cross-institution duplicate observations in HAPI FHIR"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report duplicates but don't delete them"
    )
    parser.add_argument(
        "--stats",
        action="store_true",
        help="Only report statistics, not individual duplicate details"
    )

    args = parser.parse_args()

    result = run_deduplication(
        dry_run=args.dry_run,
        stats_only=args.stats
    )

    return 0


if __name__ == "__main__":
    exit(main())
