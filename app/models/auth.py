"""
Internal Hub authentication -- a single shared login for the whole
internal team, guarding the entire application as one boundary. No
signup, roles, teams, or per-user accounts in this phase (see
app/services/auth_service.py's module docstring for why that's the
deliberate, minimal scope here).

AuthSession is the ONLY persisted model this phase introduces.
`session_token_hash` is a SHA-256 hex digest of the actual session token
that lives in the browser's HTTP-only cookie -- the raw token itself is
never stored anywhere (same one-way-hash principle as a password, except
here the input is already high-entropy random, so a plain unsalted hash is
sufficient: there is no dictionary/rainbow-table risk for a 32-byte random
token the way there is for a human-chosen password).
"""

from datetime import datetime

from pydantic import BaseModel


class AuthSession(BaseModel):
    session_token_hash: str
    created_at: datetime
    expires_at: datetime
