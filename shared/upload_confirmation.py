"""Upload-only clock tolerance; never changes authentication or other signers."""
import time

from itsdangerous import SignatureExpired

UPLOAD_CONFIRM_MAX_AGE = 3600
UPLOAD_CONFIRM_CLOCK_SKEW = 5


def load_upload_confirmation(signer, token):
    # Verify the signature and obtain its authenticated timestamp first. Apply
    # our explicit age window instead of disabling expiry for future tokens.
    payload, signed_at = signer.loads(token, max_age=None, return_timestamp=True)
    # Match TimestampSigner's integer-second expiry semantics for old tokens.
    age = int(time.time()) - int(signed_at.timestamp())
    if age < -UPLOAD_CONFIRM_CLOCK_SKEW:
        raise SignatureExpired("clock_skew", date_signed=signed_at)
    if age > UPLOAD_CONFIRM_MAX_AGE:
        raise SignatureExpired("expired", date_signed=signed_at)
    return payload
