# Cloudflare Access — Protect Bina Health

Cloudflare Access adds an authentication layer in front of bina.saridium.com.
Only approved users (you) can reach the app. Everyone else gets a login page.

## Setup via Cloudflare Dashboard

1. Go to https://one.dash.cloudflare.com → select your account
2. Sidebar: **Access** → **Applications** → **Add an application**
3. Choose **Self-hosted**

### Application config:
- **Application name:** Bina Health
- **Session duration:** 24 hours (or 7 days for convenience)
- **Subdomain:** `bina` | **Domain:** `saridium.com`

### Policy:
- **Policy name:** Owner only
- **Action:** Allow
- **Include rule:** Emails — `usarid@gmail.com`

### Authentication:
- Under **Identity providers**, enable **One-time PIN** (simplest — sends a code to your email)
- Or add **Google** as a provider (no config needed if using Gmail — just toggle it on)

4. Click **Save**

## What happens

- Visit https://bina.saridium.com
- Cloudflare shows a login page before any traffic reaches your Mac Mini
- Enter your email → get a code (or use Google SSO)
- After auth, you get a session cookie and access the app normally
- The tunnel only accepts traffic from Cloudflare's edge, so there's no way to bypass the login

## Testing

```bash
# Should get a 302 redirect to the Cloudflare Access login page:
curl -I https://bina.saridium.com

# After logging in via browser, the cf_authorization cookie grants access
```

## Optional: Add more users

To give a family member or caregiver access, add their email to the Access policy.
Each person authenticates independently.
