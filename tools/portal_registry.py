"""Portal registry — Python-side loader for the same portal JSON files
the mobile app reads at startup.

Every FHIR converter under tools/mobile/ used to hardcode a Stanford
patient ref, identifier system, src-portal tag, and input directory
name. R-3 moves those to the .ingest block of
mobile/assets/portals/<portal_id>.json and lets converters look them
up by --portal argument.

Usage:
    from portal_registry import get_portal
    p = get_portal('stanford')
    p.patient_ref                   # 'Patient/eLnGIs...'
    p.identifier_system('allergy')  # 'urn:stanford:myhealth:allergy'
    p.src_portal_tag                # 'stanford.mychart'
    p.input_dir(base_out_dir, 'clinical')  # PosixPath('.../stanford-clinical')
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Dict

_REPO_ROOT = Path(__file__).resolve().parent.parent
_PORTALS_DIR = _REPO_ROOT / 'mobile' / 'assets' / 'portals'


@dataclass(frozen=True)
class Portal:
    """A single portal's identity + ingest metadata. Not a full mirror
    of the Dart-side PortalConfig — only the fields the Python
    converters actually consume."""
    id: str
    name: str
    patient_ref: str
    identifier_system_prefix: str
    src_portal_tag: str
    input_dir_name: str

    def identifier_system(self, data_type: str) -> str:
        """Compose a full FHIR Identifier.system for a data type.
        Example: identifier_system('allergy')
            → 'urn:stanford:myhealth:allergy'"""
        return f'{self.identifier_system_prefix}:{data_type}'

    def input_dir(self, base_out_dir: Path, suffix: str) -> Path:
        """Compose the tools/v3/out/<slug>-<suffix> directory path.
        Example: input_dir(REPO_ROOT/'tools/v3/out', 'clinical')
            → tools/v3/out/stanford-clinical"""
        return base_out_dir / f'{self.input_dir_name}-{suffix}'


_cache: Dict[str, Portal] = {}


def _load_all() -> Dict[str, Portal]:
    if _cache:
        return _cache
    if not _PORTALS_DIR.exists():
        raise FileNotFoundError(
            f'Portal configs not found at {_PORTALS_DIR}. Check the '
            f'repo root — this module expects the mobile app to live at '
            f'<repo>/mobile/assets/portals/.'
        )
    for jf in sorted(_PORTALS_DIR.glob('*.json')):
        raw = json.loads(jf.read_text())
        ingest = raw['ingest']
        p = Portal(
            id=raw['id'],
            name=raw['name'],
            patient_ref=ingest['patientRef'],
            identifier_system_prefix=ingest['identifierSystemPrefix'],
            src_portal_tag=ingest['srcPortalTag'],
            input_dir_name=ingest['inputDirName'],
        )
        if p.id in _cache:
            raise ValueError(f'Duplicate portal id "{p.id}" in {_PORTALS_DIR}')
        _cache[p.id] = p
    return _cache


def get_portal(portal_id: str) -> Portal:
    """Look up a portal by id (e.g. 'stanford', 'ucsf'). Raises
    KeyError with a helpful message listing known ids on miss."""
    portals = _load_all()
    if portal_id not in portals:
        known = ', '.join(sorted(portals.keys())) or '<none>'
        raise KeyError(
            f'Unknown portal id "{portal_id}". Known portals: {known}. '
            f'Add a config file at {_PORTALS_DIR}/{portal_id}.json to define one.'
        )
    return portals[portal_id]


def all_portals() -> Dict[str, Portal]:
    """All loaded portals, keyed by id. Handy for listing / iteration."""
    return dict(_load_all())
