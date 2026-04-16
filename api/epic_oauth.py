"""PHV Epic on FHIR — OAuth 2.0 SMART launch flow for patient-facing apps.

Handles:
  GET  /api/epic/auth-url      — returns the Epic authorize URL to redirect to
  GET  /api/epic/callback       — handles the OAuth redirect, exchanges code for token
  GET  /api/epic/status         — check current connection status
  POST /api/epic/disconnect     — clear stored tokens

The OAuth flow:
  1. Frontend calls /api/epic/auth-url → opens returned URL in browser
  2. Patient logs into MyChart, grants consent
  3. Epic redirects to /callback with ?code=...
  4. /callback page sends code to /api/epic/callback
  5. API exchanges code for access_token, stores it
  6. Frontend can now trigger data import from Epic
"""

import base64
import hashlib
import os
import secrets
from datetime import datetime, timezone, timedelta
from urllib.parse import urlencode

import aiosqlite
import httpx
from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse

router = APIRouter(prefix="/api/epic", tags=["epic"])

# ── Configuration ─────────────────────────────────────────────────────────

# Epic sandbox endpoints (default)
EPIC_FHIR_BASE = os.environ.get(
    "EPIC_FHIR_BASE",
    "https://fhir.epic.com/interconnect-fhir-oauth/api/FHIR/R4",
)
EPIC_AUTHORIZE_URL = os.environ.get(
    "EPIC_AUTHORIZE_URL",
    "https://fhir.epic.com/interconnect-fhir-oauth/oauth2/authorize",
)
EPIC_TOKEN_URL = os.environ.get(
    "EPIC_TOKEN_URL",
    "https://fhir.epic.com/interconnect-fhir-oauth/oauth2/token",
)

# Client IDs — non-production for sandbox, production for real
EPIC_CLIENT_ID = os.environ.get("EPIC_CLIENT_ID", "")
EPIC_REDIRECT_URI = os.environ.get("EPIC_REDIRECT_URI", "https://localhost:3000/callback")
EPIC_CLIENT_SECRET = os.environ.get("EPIC_CLIENT_SECRET", "")

# SMART scopes for patient access
EPIC_SCOPES = os.environ.get(
    "EPIC_SCOPES",
    "openid fhirUser patient/*.read",
)

DB_PATH = os.environ.get("ASSISTANT_DB", "/data/chat.db")

# ── Token Storage ─────────────────────────────────────────────────────────

_db = None


async def _get_db() -> aiosqlite.Connection:
    global _db
    if _db is None:
        os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
        _db = await aiosqlite.connect(DB_PATH)
        _db.row_factory = aiosqlite.Row
        await _db.executescript("""
            CREATE TABLE IF NOT EXISTS epic_tokens (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                access_token TEXT NOT NULL,
                refresh_token TEXT DEFAULT '',
                token_type TEXT DEFAULT 'Bearer',
                expires_at TEXT NOT NULL,
                patient_id TEXT DEFAULT '',
                scope TEXT DEFAULT '',
                fhir_base TEXT DEFAULT '',
                connected_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS epic_pkce (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                code_verifier TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
        """)
        await _db.commit()
    return _db


async def _store_token(token_data: dict, fhir_base: str = ""):
    """Store OAuth token response."""
    db = await _get_db()
    now = datetime.now(timezone.utc)
    expires_in = token_data.get("expires_in", 3600)
    expires_at = now + timedelta(seconds=int(expires_in))

    await db.execute(
        """INSERT INTO epic_tokens (id, access_token, refresh_token, token_type,
               expires_at, patient_id, scope, fhir_base, connected_at)
           VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(id) DO UPDATE SET
               access_token=excluded.access_token,
               refresh_token=excluded.refresh_token,
               token_type=excluded.token_type,
               expires_at=excluded.expires_at,
               patient_id=excluded.patient_id,
               scope=excluded.scope,
               fhir_base=excluded.fhir_base,
               connected_at=excluded.connected_at""",
        (
            token_data.get("access_token", ""),
            token_data.get("refresh_token", ""),
            token_data.get("token_type", "Bearer"),
            expires_at.isoformat(),
            token_data.get("patient", ""),
            token_data.get("scope", ""),
            fhir_base,
            now.isoformat(),
        ),
    )
    await db.commit()


async def _get_token() -> dict | None:
    """Retrieve stored token if it exists."""
    db = await _get_db()
    cursor = await db.execute("SELECT * FROM epic_tokens WHERE id = 1")
    row = await cursor.fetchone()
    if not row:
        return None
    return dict(row)


async def _clear_token():
    """Remove stored token."""
    db = await _get_db()
    await db.execute("DELETE FROM epic_tokens WHERE id = 1")
    await db.commit()


# ── PKCE (Proof Key for Code Exchange) ────────────────────────────────────

def _generate_pkce() -> tuple[str, str]:
    """Generate PKCE code_verifier and code_challenge (S256).
    Returns (code_verifier, code_challenge).
    """
    # code_verifier: 43-128 unreserved characters
    code_verifier = secrets.token_urlsafe(64)  # ~86 chars
    # code_challenge: BASE64URL(SHA256(code_verifier))
    digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
    code_challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return code_verifier, code_challenge


async def _store_pkce_verifier(verifier: str):
    """Store the PKCE code_verifier for use in the callback."""
    db = await _get_db()
    now = datetime.now(timezone.utc).isoformat()
    await db.execute(
        """INSERT INTO epic_pkce (id, code_verifier, created_at)
           VALUES (1, ?, ?)
           ON CONFLICT(id) DO UPDATE SET code_verifier=excluded.code_verifier, created_at=excluded.created_at""",
        (verifier, now),
    )
    await db.commit()


async def _get_pkce_verifier() -> str | None:
    """Retrieve and delete the stored PKCE code_verifier."""
    db = await _get_db()
    cursor = await db.execute("SELECT code_verifier FROM epic_pkce WHERE id = 1")
    row = await cursor.fetchone()
    if row:
        await db.execute("DELETE FROM epic_pkce WHERE id = 1")
        await db.commit()
        return row["code_verifier"]
    return None


def _is_token_expired(token: dict) -> bool:
    """Check if a stored token is expired."""
    try:
        expires_at = datetime.fromisoformat(token["expires_at"])
        # Add 60s buffer
        return datetime.now(timezone.utc) > expires_at - timedelta(seconds=60)
    except Exception:
        return True


# ── Endpoints ─────────────────────────────────────────────────────────────

@router.get("/auth-url")
async def get_auth_url():
    """Return the Epic OAuth authorize URL for the frontend to open."""
    if not EPIC_CLIENT_ID:
        return JSONResponse(
            status_code=400,
            content={"error": "EPIC_CLIENT_ID not configured. Set it in docker-compose environment."},
        )

    # Generate PKCE challenge (required for public clients)
    code_verifier, code_challenge = _generate_pkce()
    await _store_pkce_verifier(code_verifier)

    params = {
        "response_type": "code",
        "client_id": EPIC_CLIENT_ID,
        "redirect_uri": EPIC_REDIRECT_URI,
        "scope": EPIC_SCOPES,
        "aud": EPIC_FHIR_BASE,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    }
    url = f"{EPIC_AUTHORIZE_URL}?{urlencode(params)}"

    print(f"[epic] Auth URL generated with PKCE (verifier stored)", flush=True)

    return {
        "auth_url": url,
        "client_id": EPIC_CLIENT_ID,
        "redirect_uri": EPIC_REDIRECT_URI,
        "fhir_base": EPIC_FHIR_BASE,
    }


@router.get("/callback")
async def handle_callback(code: str = Query(...), state: str = Query(None)):
    """Exchange authorization code for access token."""
    if not EPIC_CLIENT_ID:
        return JSONResponse(status_code=400, content={"error": "EPIC_CLIENT_ID not configured"})

    # Retrieve PKCE code_verifier (stored during auth-url generation)
    code_verifier = await _get_pkce_verifier()
    if not code_verifier:
        print("[epic] Warning: No PKCE code_verifier found — token exchange may fail", flush=True)

    # Exchange code for token (with PKCE verifier)
    token_params = {
        "grant_type": "authorization_code",
        "code": code,
        "client_id": EPIC_CLIENT_ID,
        "redirect_uri": EPIC_REDIRECT_URI,
    }
    if code_verifier:
        token_params["code_verifier"] = code_verifier

    # Build headers — include Basic auth if client_secret is configured
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    if EPIC_CLIENT_SECRET:
        # Epic expects HTTP Basic auth: base64(client_id:client_secret)
        credentials = base64.b64encode(
            f"{EPIC_CLIENT_ID}:{EPIC_CLIENT_SECRET}".encode()
        ).decode()
        headers["Authorization"] = f"Basic {credentials}"
        print(f"[epic] Using client_secret (Basic auth) for token exchange", flush=True)
    else:
        print(f"[epic] No client_secret — using public client flow", flush=True)

    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            resp = await client.post(
                EPIC_TOKEN_URL,
                data=token_params,
                headers=headers,
            )

            if resp.status_code != 200:
                error_detail = resp.text[:500]
                print(f"[epic] Token exchange failed ({resp.status_code}): {error_detail}", flush=True)
                return JSONResponse(
                    status_code=resp.status_code,
                    content={"error": "Token exchange failed", "detail": error_detail},
                )

            token_data = resp.json()
            print(f"[epic] Token received. Patient: {token_data.get('patient', 'unknown')}, "
                  f"Scope: {token_data.get('scope', '')}", flush=True)

            # Store the token
            await _store_token(token_data, EPIC_FHIR_BASE)

            return {
                "ok": True,
                "patient_id": token_data.get("patient", ""),
                "scope": token_data.get("scope", ""),
                "expires_in": token_data.get("expires_in", 0),
            }

        except httpx.HTTPError as e:
            print(f"[epic] Token exchange HTTP error: {e}", flush=True)
            return JSONResponse(
                status_code=502,
                content={"error": f"Failed to reach Epic token endpoint: {str(e)}"},
            )


@router.get("/status")
async def get_status():
    """Check current Epic connection status."""
    token = await _get_token()

    if not token or not token.get("access_token"):
        return {
            "connected": False,
            "configured": bool(EPIC_CLIENT_ID),
            "client_id": EPIC_CLIENT_ID[:8] + "..." if EPIC_CLIENT_ID else "",
            "fhir_base": EPIC_FHIR_BASE,
        }

    expired = _is_token_expired(token)

    result = {
        "connected": not expired,
        "configured": True,
        "expired": expired,
        "patient_id": token.get("patient_id", ""),
        "scope": token.get("scope", ""),
        "fhir_base": token.get("fhir_base", EPIC_FHIR_BASE),
        "connected_at": token.get("connected_at", ""),
        "expires_at": token.get("expires_at", ""),
    }

    # If connected, verify with a quick FHIR call
    if not expired:
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(
                    f"{token.get('fhir_base', EPIC_FHIR_BASE)}/metadata",
                    headers={"Authorization": f"Bearer {token['access_token']}"},
                )
                result["verified"] = resp.status_code == 200
                if resp.status_code != 200:
                    result["verify_error"] = f"HTTP {resp.status_code}"
        except Exception as e:
            result["verified"] = False
            result["verify_error"] = str(e)

    return result


@router.post("/disconnect")
async def disconnect():
    """Clear stored Epic tokens."""
    await _clear_token()
    print("[epic] Disconnected — tokens cleared", flush=True)
    return {"ok": True}


@router.get("/test")
async def test_connection():
    """Test the current Epic connection by fetching the Patient resource."""
    token = await _get_token()
    if not token or not token.get("access_token"):
        return JSONResponse(status_code=401, content={"error": "Not connected to Epic"})

    if _is_token_expired(token):
        return JSONResponse(status_code=401, content={"error": "Token expired — please reconnect"})

    fhir_base = token.get("fhir_base", EPIC_FHIR_BASE)
    patient_id = token.get("patient_id", "")

    results = {}

    async with httpx.AsyncClient(timeout=15.0) as client:
        headers = {"Authorization": f"Bearer {token['access_token']}"}

        # Test 1: Metadata (capability statement)
        try:
            resp = await client.get(f"{fhir_base}/metadata", headers=headers)
            results["metadata"] = {"status": resp.status_code, "ok": resp.status_code == 200}
        except Exception as e:
            results["metadata"] = {"status": 0, "ok": False, "error": str(e)}

        # Test 2: Patient resource
        if patient_id:
            try:
                resp = await client.get(f"{fhir_base}/Patient/{patient_id}", headers=headers)
                if resp.status_code == 200:
                    pt = resp.json()
                    name = ""
                    for n in pt.get("name", []):
                        given = " ".join(n.get("given", []))
                        family = n.get("family", "")
                        name = f"{given} {family}".strip()
                        if n.get("use") == "official":
                            break
                    results["patient"] = {"status": 200, "ok": True, "name": name, "id": patient_id}
                else:
                    results["patient"] = {"status": resp.status_code, "ok": False}
            except Exception as e:
                results["patient"] = {"status": 0, "ok": False, "error": str(e)}

    return results
