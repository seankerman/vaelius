"""Conservative deterministic baseline, not a claim of complete sanitization."""
import hashlib
import re

# Credential rules deliberately avoid entropy-only matching: dataset hashes and
# identifiers are useful knowledge. Structured fields complement text patterns.
# Detection lives in agentclient.secret_detection, shared with server intake.
from agentclient.secret_detection import (  # noqa: E402  (public re-exports)
    CREDENTIAL_NAME, REDACTED_SECRET, SECRET, SECRET_KEYS, redact_text)
EMAIL = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
HOME_PATH = re.compile(r"/(?:Users|home)/[^\s/\"']+(?:/[^\s\"'<>]*)?")
PRIVATE_SOURCE = re.compile(r"(?i)(?:\.env\b|\.ssh/|credentials|printenv|\benv\b|keychain|localhost|\.internal\b|\.local\b|\.\./|127\.0\.0\.1|10\.\d+\.\d+\.\d+|172\.(?:1[6-9]|2\d|3[01])\.\d+\.\d+|192\.168\.\d+\.\d+)")
INJECTION = re.compile(r"(?i)(?:ignore (?:all |previous |prior )*instructions|system prompt|developer message|curl\b[^\n]*\|\s*(?:ba)?sh\b|disable (?:security|safety))")


def clean(text: str) -> tuple[str, list[str]]:
    reasons = []
    text, rules = redact_text(text)
    if rules:
        reasons.append("possible_secret")
    if EMAIL.search(text):
        reasons.append("personal_identifier")
    text = EMAIL.sub("[EMAIL]", text)
    text = HOME_PATH.sub("[LOCAL_PATH]", text)
    return text, reasons


def clean_private(text: str) -> tuple[str, list[str]]:
    """Keep permitted location/person facts in a private authority; reject secrets."""
    text, rules = redact_text(text)
    return text, (["possible_secret"] if rules else [])


def fingerprint(command: str) -> str:
    # Conservative: match only the same command; no invented equivalence.
    return hashlib.sha256(command.strip().encode()).hexdigest()


def terms(text: str) -> list[str]:
    stop = {"the", "this", "that", "with", "from", "have", "what", "when", "then", "which", "about", "please", "tool", "output", "command", "exit", "code", "true", "false", "null"}
    return list(dict.fromkeys(t.lower() for t in re.findall(r"[A-Za-z][A-Za-z0-9_-]{2,}", text) if t.lower() not in stop))[:32]
