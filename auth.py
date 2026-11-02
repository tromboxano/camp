"""Password hashing helpers for application user accounts."""

import hashlib
import hmac
import secrets


PASSWORD_HASH_ITERATIONS = 600_000


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    password_hash = hashlib.pbkdf2_hmac(
        'sha256',
        (password or '').encode(),
        salt,
        PASSWORD_HASH_ITERATIONS,
    )
    return f'pbkdf2_sha256${PASSWORD_HASH_ITERATIONS}${salt.hex()}${password_hash.hex()}'


def verify_password(password: str, stored_hash: str | None) -> tuple[bool, bool]:
    if not stored_hash:
        return False, False
    if stored_hash.startswith('pbkdf2_sha256$'):
        try:
            algorithm, iterations, salt_hex, expected_hex = stored_hash.split('$', 3)
            if algorithm != 'pbkdf2_sha256':
                return False, False
            actual = hashlib.pbkdf2_hmac(
                'sha256',
                (password or '').encode(),
                bytes.fromhex(salt_hex),
                int(iterations),
            ).hex()
        except (ValueError, TypeError):
            return False, False
        return hmac.compare_digest(actual, expected_hex), False

    legacy_hash = hashlib.sha256((password or '').encode()).hexdigest()
    return hmac.compare_digest(legacy_hash, stored_hash), True
