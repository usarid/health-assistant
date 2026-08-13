"""Portal registry — Python-side loader for portal metadata.

Reads from two split files:
  1. mobile/assets/portals/<portal_id>.json — PUBLIC config (URLs,
     hosts, auth field names). Same file the Flutter app bundles.
     Tracked in git.
  2. tools/portal_ingest_config.json — PRIVATE per-portal metadata
     (FHIR patient refs, identifier system prefixes, src-portal
     tags, input directory names). Gitignored, restic-backed. See
     portal_ingest_config.example.json for the template.

The split keeps portal-scrape config (public: what URL is Stanford's
login page?) separate from ingest config (private: which FHIR Patient
represents "me at Stanford" in this vault?). Only the Python side
sees both; the mobile app only ever needs the public part.

Usage:
    from portal_registry import get_portal
    p = get_portal('stanford')
    p.name                          # 'Stanford MyChart' (from mobile JSON)
    p.patient_ref                   # 'Patient/eLnGIs...' (from ingest cfg)
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
_INGEST_CFG = _REPO_ROOT / 'tools' / 'portal_ingest_config.json'


@dataclass(frozen=True)
class Portal:
    """A single portal's identity + ingest metadata. Merged from the
    two config sources — only the fields the Python converters
    actually consume are exposed."""
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
    if not _INGEST_CFG.exists():
        raise FileNotFoundError(
            f'Ingest config not found at {_INGEST_CFG}. Copy '
            f'{_INGEST_CFG.with_suffix(".example.json").name} to '
            f'{_INGEST_CFG.name}, fill in your HAPI patient sub-identity '
            f'refs, then re-run. The example file has instructions.'
        )
    ingest_by_id = json.loads(_INGEST_CFG.read_text())
    for jf in sorted(_PORTALS_DIR.glob('*.json')):
        raw = json.loads(jf.read_text())
        pid = raw['id']
        ing = ingest_by_id.get(pid)
        if not ing or not isinstance(ing, dict) or 'patientRef' not in ing:
            # Skip portals with no ingest metadata — mobile-only portal.
            # Real converters will KeyError on get_portal, which is the
            # right failure mode (tells the user to add ingest config).
            continue
        p = Portal(
            id=pid,
            name=raw['name'],
            patient_ref=ing['patientRef'],
            identifier_system_prefix=ing['identifierSystemPrefix'],
            src_portal_tag=ing['srcPortalTag'],
            input_dir_name=ing['inputDirName'],
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
            f'Add ingest metadata for it in {_INGEST_CFG.name}, '
            f'or add a mobile config in {_PORTALS_DIR}/{portal_id}.json '
            f'to define one.'
        )
    return portals[portal_id]


def all_portals() -> Dict[str, Portal]:
    """All loaded portals, keyed by id. Handy for listing / iteration."""
    return dict(_load_all())
