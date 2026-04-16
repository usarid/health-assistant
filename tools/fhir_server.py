#!/usr/bin/env python3
"""
FHIR R4 Server - Python Implementation
A lightweight FHIR-compliant server for personal health records using SQLite.
"""

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple
from pathlib import Path
import http.server
import socketserver
from urllib.parse import urlparse, parse_qs
import re


class FHIRDatabase:
    """SQLite-based FHIR resource storage."""

    def __init__(self, db_path: str = "fhir.db"):
        self.db_path = db_path
        self._init_db()

    def _init_db(self):
        """Initialize the database schema."""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()

            # Main resources table
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS resources (
                    id TEXT PRIMARY KEY,
                    resource_type TEXT NOT NULL,
                    logical_id TEXT NOT NULL,
                    version_id TEXT NOT NULL,
                    data JSON NOT NULL,
                    created_at TIMESTAMP NOT NULL,
                    updated_at TIMESTAMP NOT NULL,
                    is_deleted INTEGER DEFAULT 0,
                    UNIQUE(resource_type, logical_id, version_id)
                )
            """)

            # Full-text search index
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS search_index (
                    resource_id TEXT PRIMARY KEY,
                    resource_type TEXT NOT NULL,
                    logical_id TEXT NOT NULL,
                    search_text TEXT,
                    FOREIGN KEY (resource_id) REFERENCES resources(id)
                )
            """)

            # Reference index for linking resources
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS resource_references (
                    source_id TEXT NOT NULL,
                    source_type TEXT NOT NULL,
                    target_type TEXT NOT NULL,
                    target_id TEXT NOT NULL,
                    reference_field TEXT NOT NULL,
                    FOREIGN KEY (source_id) REFERENCES resources(id)
                )
            """)

            conn.commit()

    def create_resource(self, resource_type: str, data: Dict) -> Dict:
        """Create a new FHIR resource."""
        logical_id = str(uuid.uuid4())
        version_id = "1"
        resource_id = f"{resource_type}/{logical_id}"

        now = datetime.now(timezone.utc).isoformat()

        # Ensure resource has required FHIR metadata
        data['resourceType'] = resource_type
        data['id'] = logical_id
        data['meta'] = {
            'versionId': version_id,
            'lastUpdated': now
        }

        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO resources
                (id, resource_type, logical_id, version_id, data, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (resource_id, resource_type, logical_id, version_id, json.dumps(data), now, now))

            # Index for search
            search_text = self._extract_search_text(data)
            cursor.execute("""
                INSERT INTO search_index
                (resource_id, resource_type, logical_id, search_text)
                VALUES (?, ?, ?, ?)
            """, (resource_id, resource_type, logical_id, search_text))

            # Index references
            self._index_references(cursor, resource_id, resource_type, data)

            conn.commit()

        return data

    def get_resource(self, resource_type: str, logical_id: str) -> Optional[Dict]:
        """Retrieve a specific resource by type and ID."""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT data FROM resources
                WHERE resource_type = ? AND logical_id = ? AND is_deleted = 0
                ORDER BY version_id DESC LIMIT 1
            """, (resource_type, logical_id))

            row = cursor.fetchone()
            if row:
                return json.loads(row[0])
            return None

    def search_resources(self, resource_type: str, query_params: Dict) -> List[Dict]:
        """Search for resources by type and parameters."""
        results = []

        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()

            sql = "SELECT DISTINCT r.data FROM resources r LEFT JOIN search_index s ON r.id = s.resource_id "
            sql += "WHERE r.resource_type = ? AND r.is_deleted = 0"
            params = [resource_type]

            # Build query based on parameters
            if 'name' in query_params:
                # Case-insensitive search on search index
                sql += " AND LOWER(s.search_text) LIKE LOWER(?)"
                params.append(f"%{query_params['name']}%")

            if 'identifier' in query_params:
                # Search in the JSON data for identifier (case-insensitive)
                sql += " AND LOWER(r.data) LIKE LOWER(?)"
                params.append(f"%{query_params['identifier']}%")

            if 'subject' in query_params:
                # For observations, search by subject reference (case-insensitive)
                sql += " AND LOWER(r.data) LIKE LOWER(?)"
                params.append(f"%{query_params['subject']}%")

            cursor.execute(sql + " LIMIT 100", params)
            rows = cursor.fetchall()

            for row in rows:
                results.append(json.loads(row[0]))

        return results

    def update_resource(self, resource_type: str, logical_id: str, data: Dict) -> Dict:
        """Update an existing resource."""
        # Get current version
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT version_id FROM resources
                WHERE resource_type = ? AND logical_id = ? AND is_deleted = 0
                ORDER BY version_id DESC LIMIT 1
            """, (resource_type, logical_id))

            row = cursor.fetchone()
            if not row:
                raise ValueError(f"Resource {resource_type}/{logical_id} not found")

            current_version = int(row[0])
            new_version = str(current_version + 1)
            resource_id = f"{resource_type}/{logical_id}"
            now = datetime.now(timezone.utc).isoformat()

            # Prepare data
            data['resourceType'] = resource_type
            data['id'] = logical_id
            data['meta'] = {
                'versionId': new_version,
                'lastUpdated': now
            }

            new_resource_id = f"{resource_type}/{logical_id}#{new_version}"

            cursor.execute("""
                INSERT INTO resources
                (id, resource_type, logical_id, version_id, data, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (new_resource_id, resource_type, logical_id, new_version, json.dumps(data),
                  datetime.now(timezone.utc).isoformat(), now))

            search_text = self._extract_search_text(data)
            cursor.execute("""
                INSERT OR REPLACE INTO search_index
                (resource_id, resource_type, logical_id, search_text)
                VALUES (?, ?, ?, ?)
            """, (new_resource_id, resource_type, logical_id, search_text))

            self._index_references(cursor, new_resource_id, resource_type, data)

            conn.commit()

        return data

    def _extract_search_text(self, data: Dict) -> str:
        """Extract searchable text from resource."""
        text_parts = []

        # Common searchable fields
        if 'name' in data:
            if isinstance(data['name'], list):
                for name_entry in data['name']:
                    if isinstance(name_entry, dict):
                        # Extract from 'text' field if available
                        if 'text' in name_entry:
                            text_parts.append(name_entry['text'])
                        # Also extract given and family names
                        if 'given' in name_entry:
                            if isinstance(name_entry['given'], list):
                                text_parts.extend(name_entry['given'])
                            else:
                                text_parts.append(str(name_entry['given']))
                        if 'family' in name_entry:
                            text_parts.append(str(name_entry['family']))
            elif isinstance(data['name'], str):
                text_parts.append(data['name'])

        # Handle top-level given/family (for edge cases)
        if 'given' in data and not isinstance(data.get('name'), list):
            if isinstance(data['given'], list):
                text_parts.extend(data['given'])
            else:
                text_parts.append(str(data['given']))

        if 'family' in data and not isinstance(data.get('name'), list):
            text_parts.append(str(data['family']))

        # Extract identifier values
        if 'identifier' in data and isinstance(data['identifier'], list):
            for ident in data['identifier']:
                if isinstance(ident, dict) and 'value' in ident:
                    text_parts.append(str(ident['value']))

        return ' '.join(str(p) for p in text_parts)

    def _index_references(self, cursor, resource_id: str, resource_type: str, data: Dict):
        """Index references within a resource."""
        # Find all reference fields
        for field, value in data.items():
            if isinstance(value, dict) and 'reference' in value:
                ref = value['reference']
                ref_parts = ref.split('/')
                if len(ref_parts) == 2:
                    target_type, target_id = ref_parts
                    cursor.execute("""
                        INSERT OR IGNORE INTO resource_references
                        (source_id, source_type, target_type, target_id, reference_field)
                        VALUES (?, ?, ?, ?, ?)
                    """, (resource_id, resource_type, target_type, target_id, field))
            elif isinstance(value, list):
                for item in value:
                    if isinstance(item, dict) and 'reference' in item:
                        ref = item['reference']
                        ref_parts = ref.split('/')
                        if len(ref_parts) == 2:
                            target_type, target_id = ref_parts
                            cursor.execute("""
                                INSERT OR IGNORE INTO references
                                (source_id, source_type, target_type, target_id, reference_field)
                                VALUES (?, ?, ?, ?, ?)
                            """, (resource_id, resource_type, target_type, target_id, field))

    def get_bundle(self, entries: List[Dict]) -> Dict:
        """Create a FHIR Bundle resource."""
        return {
            'resourceType': 'Bundle',
            'type': 'searchset',
            'total': len(entries),
            'entry': entries
        }


class FHIRRequestHandler(http.server.BaseHTTPRequestHandler):
    """HTTP request handler for FHIR API."""

    db = None  # Class variable to share database connection

    def do_GET(self):
        """Handle GET requests."""
        try:
            path = urlparse(self.path).path
            query = parse_qs(urlparse(self.path).query)

            # Clean up query params (parse_qs returns lists)
            clean_query = {k: v[0] if v else '' for k, v in query.items()}

            # FHIR API endpoint: /fhir/R4/[ResourceType]/[id]
            match = re.match(r'/fhir/R4/(\w+)(?:/([a-f0-9\-]+))?$', path)

            if not match:
                self._send_error(400, "Invalid FHIR endpoint")
                return

            resource_type = match.group(1)
            logical_id = match.group(2)

            if logical_id:
                # GET specific resource
                resource = self.db.get_resource(resource_type, logical_id)
                if resource:
                    self._send_json(200, resource)
                else:
                    self._send_error(404, f"{resource_type}/{logical_id} not found")
            else:
                # GET resource search
                resources = self.db.search_resources(resource_type, clean_query)
                bundle_entries = [
                    {
                        'fullUrl': f"/fhir/R4/{r['resourceType']}/{r['id']}",
                        'resource': r
                    }
                    for r in resources
                ]
                bundle = self.db.get_bundle(bundle_entries)
                self._send_json(200, bundle)

        except Exception as e:
            self._send_error(500, str(e))

    def do_POST(self):
        """Handle POST requests."""
        try:
            path = urlparse(self.path).path

            # Get request body
            content_length = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(content_length)
            data = json.loads(body) if body else {}

            # Create resource endpoint: /fhir/R4/[ResourceType]
            match = re.match(r'/fhir/R4/(\w+)$', path)

            if not match:
                self._send_error(400, "Invalid FHIR endpoint")
                return

            resource_type = match.group(1)

            if data.get('resourceType') != resource_type:
                self._send_error(400, "Resource type mismatch")
                return

            resource = self.db.create_resource(resource_type, data)

            self._send_json(201, resource,
                           location=f"/fhir/R4/{resource['resourceType']}/{resource['id']}")

        except json.JSONDecodeError:
            self._send_error(400, "Invalid JSON")
        except Exception as e:
            self._send_error(500, str(e))

    def do_PUT(self):
        """Handle PUT requests."""
        try:
            path = urlparse(self.path).path

            # Get request body
            content_length = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(content_length)
            data = json.loads(body) if body else {}

            # Update resource endpoint: /fhir/R4/[ResourceType]/[id]
            match = re.match(r'/fhir/R4/(\w+)/([a-f0-9\-]+)$', path)

            if not match:
                self._send_error(400, "Invalid FHIR endpoint")
                return

            resource_type = match.group(1)
            logical_id = match.group(2)

            if data.get('resourceType') != resource_type:
                self._send_error(400, "Resource type mismatch")
                return

            resource = self.db.update_resource(resource_type, logical_id, data)
            self._send_json(200, resource)

        except json.JSONDecodeError:
            self._send_error(400, "Invalid JSON")
        except ValueError as e:
            self._send_error(404, str(e))
        except Exception as e:
            self._send_error(500, str(e))

    def _send_json(self, status_code: int, data: Dict, location: str = None):
        """Send JSON response."""
        response_data = json.dumps(data, indent=2)

        self.send_response(status_code)
        self.send_header('Content-Type', 'application/fhir+json; charset=utf-8')
        if location:
            self.send_header('Location', location)
        self.send_header('Content-Length', len(response_data))
        self.end_headers()
        self.wfile.write(response_data.encode())

    def _send_error(self, status_code: int, message: str):
        """Send error response."""
        error_resource = {
            'resourceType': 'OperationOutcome',
            'issue': [
                {
                    'severity': 'error',
                    'code': 'processing',
                    'diagnostics': message
                }
            ]
        }
        self._send_json(status_code, error_resource)

    def log_message(self, format, *args):
        """Suppress default logging."""
        print(f"[{self.log_date_time_string()}] {format % args}")


def start_server(host: str = 'localhost', port: int = 8080, db_path: str = 'fhir.db'):
    """Start the FHIR server."""

    # Create database and attach to handler
    db = FHIRDatabase(db_path)
    FHIRRequestHandler.db = db

    # Create server
    with socketserver.TCPServer((host, port), FHIRRequestHandler) as httpd:
        print(f"\nFHIR R4 Server started at http://{host}:{port}")
        print(f"Database: {db_path}")
        print(f"\nEndpoints:")
        print(f"  GET /fhir/R4/Patient - Search patients")
        print(f"  GET /fhir/R4/Patient/[id] - Get specific patient")
        print(f"  POST /fhir/R4/Patient - Create patient")
        print(f"  PUT /fhir/R4/Patient/[id] - Update patient")
        print(f"  GET /fhir/R4/Observation - Search observations")
        print(f"  POST /fhir/R4/Observation - Create observation")
        print(f"\nPress Ctrl+C to stop the server.\n")

        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nServer stopped.")


if __name__ == '__main__':
    start_server()
