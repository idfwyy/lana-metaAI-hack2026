"""The access code gate: codes in, signed 12-hour passes out.

Nothing that spends API credits (POST /cases, the case WebSocket) works without a valid pass.
A pass is `<payload>.<signature>`, both base64url. The payload is {exp, c}: the expiry (epoch
seconds) and a fingerprint of the code it was issued for. The signature is HMAC-SHA256 of the
payload with ACCESS_TOKEN_SECRET. Because the fingerprint is checked against the current
ACCESS_CODES, removing a code ends the passes issued for it at the next request, not 12 hours later.

With ACCESS_CODES empty the gate is off. With codes set but no secret it fails closed: no pass
can be issued or accepted.
"""

import base64
import hashlib
import hmac
import json
import time

from app.config import get_settings

PASS_TTL_S = 12 * 60 * 60
WRONG_CODE_MESSAGE = "That code didn't work."
TOO_MANY_MESSAGE = "Too many tries. Try again in a few minutes."
REQUIRED_MESSAGE = "Enter an access code to continue."
NOT_SET_UP_MESSAGE = "Access codes aren't set up on the server."


def enabled() -> bool:
    """True when ACCESS_CODES has at least one code."""
    return bool(get_settings().access_code_list)


def misconfigured() -> bool:
    """Codes are set but there is no secret to sign passes with."""
    return enabled() and not get_settings().access_token_secret.get_secret_value()


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _key() -> bytes:
    return get_settings().access_token_secret.get_secret_value().encode("utf-8")


def _sign(message: str) -> str:
    return _b64(hmac.new(_key(), message.encode("ascii"), hashlib.sha256).digest())


def _fingerprint(code: str) -> str:
    return hmac.new(_key(), b"code:" + code.encode("utf-8"), hashlib.sha256).hexdigest()[:16]


def match_code(code: str) -> str | None:
    """The valid code this one matches (normalized), or None. Compares against every code in constant time."""
    given = code.strip().lower().encode("utf-8")
    found: str | None = None
    for valid in get_settings().access_code_list:
        if hmac.compare_digest(given, valid.encode("utf-8")):
            found = valid
    return found


def issue_pass(code: str, now: float | None = None) -> tuple[str, int]:
    """A signed pass for a matched code, and when it expires (epoch seconds)."""
    expires_at = int((time.time() if now is None else now) + PASS_TTL_S)
    payload = _b64(json.dumps({"exp": expires_at, "c": _fingerprint(code)}, separators=(",", ":")).encode("ascii"))
    return f"{payload}.{_sign(payload)}", expires_at


def verify_pass(token: str | None, now: float | None = None) -> bool:
    """True if the pass is ours, unexpired, and issued for a code that is still valid."""
    if not token or not token.isascii() or not enabled() or misconfigured():
        return False
    payload, dot, signature = token.partition(".")
    if not dot or not hmac.compare_digest(signature.encode("ascii"), _sign(payload).encode("ascii")):
        return False
    try:
        data = json.loads(_unb64(payload))
        expires_at, fingerprint = float(data["exp"]), str(data["c"])
    except (ValueError, KeyError, TypeError):
        return False
    if (time.time() if now is None else now) >= expires_at:
        return False
    return any(hmac.compare_digest(fingerprint, _fingerprint(c)) for c in get_settings().access_code_list)


def bearer(header: str | None) -> str | None:
    """The token in 'Authorization: Bearer <token>', or None."""
    scheme, _, token = (header or "").partition(" ")
    return (token.strip() or None) if scheme.lower() == "bearer" else None


def allows(token: str | None) -> bool:
    """The gate: open when it is off, else a valid pass."""
    return not enabled() or verify_pass(token)
