# Security policy

## Credentials

Meshy credentials must be supplied through `MESHY_API_KEY`. Store the value in
the local `.env` file or the process environment. The real value must never be
added to source code, examples, logs, reports, screenshots, or Git history.

The repository ignores `.env` and common private-key formats. `.env.example`
contains only a placeholder.

## Before committing

Run:

```powershell
python scripts/check_secrets.py
python -m unittest discover -s tests
```

Also inspect the staged diff and confirm that it contains no generated jobs,
signed download URLs, task responses, personal paths, or large 3D assets.

## If a credential is exposed

Revoke the affected key in the Meshy API settings, create a replacement, remove
the secret from Git history, and update only the local environment. Removing a
key from the newest commit alone is not sufficient.

## Network separation

The API client uses an authenticated session only for Meshy API endpoints.
Asset downloads use a separate unauthenticated session so the bearer token is
not forwarded to a returned storage or CDN URL.
