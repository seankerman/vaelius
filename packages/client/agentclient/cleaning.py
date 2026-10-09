"""Conservative deterministic baseline, not a claim of complete sanitization."""
import hashlib
import re

# Credential rules deliberately avoid entropy-only matching: dataset hashes and
# identifiers are useful knowledge. Structured fields complement text patterns.
REDACTED_SECRET = '[REDACTED_SECRET]'
CREDENTIAL_NAME = (
    r'(?:[A-Za-z0-9]+[_-])*(?:password|passwd|secret|api[_-]?key|'
    r'access[_-]?token|refresh[_-]?token|session[_-]?token|id[_-]?token|'
    r'client[_-]?secret|secret[_-]?access[_-]?key|private[_-]?key|token|'
    r'authorization|proxy[_-]?authorization|x[_-]?api[_-]?key|'
    r'cookie|set[_-]?cookie|x[_-]?amz[_-]?signature|x[_-]?goog[_-]?signature)'
)
SECRET_KEYS = re.compile(r'^(?:' + CREDENTIAL_NAME + r')$', re.I)
SECRET = re.compile(
    r"-----BEGIN [^-]*PRIVATE KEY-----[\s\S]*?-----END [^-]*PRIVATE KEY-----"
    r"|\b(?:sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9_]{16,}|github_pat_[A-Za-z0-9_]{16,}|(?:AKIA|ASIA)[A-Z0-9]{16}|xox[baprs]-[A-Za-z0-9-]{12,})\b"
    r"|\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+"
    r"|(?i:\bbearer\s+[A-Za-z0-9._~+/=-]{12,})"
    r"|(?i:\b(?:set-cookie|cookie|authorization|proxy-authorization)\s*:\s*(?!\[REDACTED_SECRET\])[^\r\n]+)"
    r"|(?i:(?<![\w-])" + CREDENTIAL_NAME +
    r"[\"']?\s*[=:]\s*(?:\"(?!\[REDACTED_SECRET\])(?:\\.|[^\"\\])*\"|'(?!\[REDACTED_SECRET\])(?:\\.|[^'\\])*'|(?!\[REDACTED_SECRET\])[^\s\"',;}\]&#]+))"
    r"|[A-Za-z][A-Za-z0-9+.-]*://[^\s/:]+:[^\s/@]+@[^\s\"'<>]+"
)
EMAIL = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
HOME_PATH = re.compile(r"/(?:Users|home)/[^\s/\"']+(?:/[^\s\"'<>]*)?")
PRIVATE_SOURCE = re.compile(r"(?i)(?:\.env\b|\.ssh/|credentials|printenv|\benv\b|keychain|localhost|\.internal\b|\.local\b|\.\./|127\.0\.0\.1|10\.\d+\.\d+\.\d+|172\.(?:1[6-9]|2\d|3[01])\.\d+\.\d+|192\.168\.\d+\.\d+)")
INJECTION = re.compile(r"(?i)(?:ignore (?:all |previous |prior )*instructions|system prompt|developer message|curl\b[^\n]*\|\s*(?:ba)?sh\b|disable (?:security|safety))")


def clean(text: str) -> tuple[str, list[str]]:
    reasons = []
    if SECRET.search(text):
        reasons.append("possible_secret")
    text = SECRET.sub("[REDACTED_SECRET]", text)
    if EMAIL.search(text):
        reasons.append("personal_identifier")
    text = EMAIL.sub("[EMAIL]", text)
    text = HOME_PATH.sub("[LOCAL_PATH]", text)
    return text, reasons


def clean_private(text: str) -> tuple[str, list[str]]:
    """Keep permitted location/person facts in a private authority; reject secrets."""
    reasons=["possible_secret"] if SECRET.search(text) else []
    return SECRET.sub("[REDACTED_SECRET]",text),reasons


def fingerprint(command: str) -> str:
    # Conservative: match only the same command; no invented equivalence.
    return hashlib.sha256(command.strip().encode()).hexdigest()


def terms(text: str) -> list[str]:
    stop = {"the", "this", "that", "with", "from", "have", "what", "when", "then", "which", "about", "please", "tool", "output", "command", "exit", "code", "true", "false", "null"}
    return list(dict.fromkeys(t.lower() for t in re.findall(r"[A-Za-z][A-Za-z0-9_-]{2,}", text) if t.lower() not in stop))[:32]
