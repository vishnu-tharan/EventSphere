"""PBKDF2-HMAC-SHA256 password hashing; passwords are never stored as plain text."""
import base64
import hashlib
import hmac
import os

ITERATIONS = 350_000


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac('sha256',password.encode('utf-8'),salt,ITERATIONS)
    return f"pbkdf2_sha256${ITERATIONS}${base64.b64encode(salt).decode()}${base64.b64encode(dk).decode()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algorithm, iterations, salt, expected = stored.split('$')
        if algorithm != 'pbkdf2_sha256':
            return False
        actual = hashlib.pbkdf2_hmac('sha256',password.encode('utf-8'),
                                     base64.b64decode(salt),int(iterations))
        return hmac.compare_digest(actual,base64.b64decode(expected))
    except (ValueError,TypeError):
        return False
