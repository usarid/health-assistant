#!/usr/bin/env python3
"""Build a SQLite database from the NLM Pillbox metadata CSV.

Run once after downloading the CSV. Creates ~/Public/BinaHealth/data/pillbox/pillbox.db,
a searchable DB that the API queries to look up pill images by drug name.

Usage:
  python3 scripts/build_pillbox_db.py
"""

import csv
import os
import re
import sqlite3
import sys

DATA_DIR = os.path.expanduser('~/Public/BinaHealth/data/pillbox')
CSV_PATH = os.path.join(DATA_DIR, 'pillbox_meta.csv')
DB_PATH = os.path.join(DATA_DIR, 'pillbox.db')


def normalize(s: str) -> str:
    """Lowercase, strip whitespace, drop trailing semicolons/punctuation."""
    if not s:
        return ''
    return s.strip().rstrip(';').strip().lower()


def extract_active_ingredient(spl_ingredients: str) -> str:
    """spl_ingredients looks like 'DOXYCYCLINE HYCLATE[DOXYCYCLINE];'.
    Take the first ingredient name before any bracket or semicolon."""
    if not spl_ingredients:
        return ''
    s = spl_ingredients.split(';')[0].strip()
    if '[' in s:
        s = s.split('[')[0].strip()
    return s.lower()


def primary_word(s: str) -> str:
    """Return the first 'real' token, useful for fuzzy matching.
    'doxycycline hyclate' -> 'doxycycline'."""
    if not s:
        return ''
    parts = re.split(r'[\s,/]+', s.strip())
    return parts[0].lower() if parts else ''


def main():
    if not os.path.exists(CSV_PATH):
        print(f"ERROR: CSV not found at {CSV_PATH}", file=sys.stderr)
        print("Download it first with:", file=sys.stderr)
        print('  curl -L -o ~/Public/BinaHealth/data/pillbox/pillbox_meta.csv \\', file=sys.stderr)
        print('    "https://datadiscovery.nlm.nih.gov/api/views/crzr-uvwg/rows.csv?accessType=DOWNLOAD"', file=sys.stderr)
        sys.exit(1)

    print(f"Building Pillbox database from {CSV_PATH}")
    print(f"Output: {DB_PATH}")

    if os.path.exists(DB_PATH):
        os.remove(DB_PATH)

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    c.execute('''
        CREATE TABLE pills (
            id INTEGER PRIMARY KEY,
            ingredient TEXT,            -- normalized active ingredient (e.g. 'doxycycline hyclate')
            ingredient_first TEXT,       -- first word for fuzzy match (e.g. 'doxycycline')
            ingredient_raw TEXT,
            brand_name TEXT,             -- normalized medicine_name (e.g. 'doxycycline')
            brand_first TEXT,
            brand_raw TEXT,
            rxnorm_name TEXT,            -- e.g. 'doxycycline 100 MG Oral Tablet'
            rxcui TEXT,
            strength TEXT,               -- e.g. 'DOXYCYCLINE 100 mg;'
            imprint TEXT,                -- e.g. '5892;V'
            shape TEXT,                  -- e.g. 'CAPSULE', 'ROUND'
            color TEXT,                  -- e.g. 'PINK'
            dosage_form TEXT,
            ndc TEXT,
            manufacturer TEXT,
            setid TEXT,
            image_filename TEXT,         -- splimage value
            has_image INTEGER,           -- 0/1
            dea_schedule TEXT
        )
    ''')

    count = 0
    with_image = 0
    with open(CSV_PATH, 'r', encoding='utf-8', errors='replace') as f:
        reader = csv.DictReader(f)
        for row in reader:
            ingredient = extract_active_ingredient(row.get('spl_ingredients', ''))
            brand_name = normalize(row.get('medicine_name', ''))

            splimage = (row.get('splimage', '') or '').strip()
            has_image_flag = (row.get('has_image', '') or '').strip().lower()
            has_img = 1 if (splimage or has_image_flag in ('true', '1', 'yes')) else 0

            try:
                rec_id = int(row.get('ID', 0))
            except (ValueError, TypeError):
                continue

            c.execute('''
                INSERT OR REPLACE INTO pills (
                    id, ingredient, ingredient_first, ingredient_raw,
                    brand_name, brand_first, brand_raw,
                    rxnorm_name, rxcui, strength, imprint, shape, color,
                    dosage_form, ndc, manufacturer, setid, image_filename,
                    has_image, dea_schedule
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ''', (
                rec_id,
                ingredient,
                primary_word(ingredient),
                row.get('spl_ingredients', ''),
                brand_name,
                primary_word(brand_name),
                row.get('medicine_name', ''),
                row.get('rxstring', ''),
                row.get('rxcui', ''),
                row.get('spl_strength', ''),
                row.get('splimprint', ''),
                row.get('splshape_text', ''),
                row.get('splcolor_text', ''),
                row.get('dosage_form', ''),
                row.get('ndc9', ''),
                row.get('author', ''),
                row.get('setid', ''),
                splimage,
                has_img,
                row.get('dea_schedule_code', ''),
            ))
            count += 1
            if has_img:
                with_image += 1
            if count % 10000 == 0:
                print(f"  imported {count:,} rows...", flush=True)

    print("Creating indexes...")
    for col in ('ingredient', 'ingredient_first', 'brand_name', 'brand_first',
                'rxcui', 'imprint', 'has_image'):
        c.execute(f'CREATE INDEX idx_pills_{col} ON pills({col})')

    conn.commit()
    conn.close()

    print()
    print(f"Done.")
    print(f"  Total rows imported: {count:,}")
    print(f"  Rows with images:    {with_image:,}")
    print(f"  DB file:             {DB_PATH}")
    print(f"  DB size:             {os.path.getsize(DB_PATH) / 1024 / 1024:.1f} MB")


if __name__ == '__main__':
    main()
