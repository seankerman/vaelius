"""Shared deterministic credential detection for client capture and server intake.

This one module is the detector for both packages: the server imports it from the
installed client package and pins its checksum (``CLIENT_PIPELINE_PIN.json``), so
client redaction, server re-redaction and the operator scanner cannot drift.

Detection is heuristic. Rules recognize well-known credential formats and
credential-named assignments; they are not a guarantee that text is secret-free.
Every quantifier is bounded and multi-line blocks are located without nested
unbounded scans, so cost stays linear in the input size.

Several token formats are adapted from the Gitleaks default configuration
(https://github.com/gitleaks/gitleaks, config/gitleaks.toml at v8.30.1),
Copyright (c) 2019 Zachary Rice, used under the MIT License. Adapted rule ids keep
their Gitleaks names. Patterns were curated for this use: Gitleaks entropy
thresholds are replaced by explicit structure checks, and lengths are widened
where the previous Vaelius baseline already matched a broader form.
"""
from __future__ import annotations

from bisect import bisect_left
from dataclasses import dataclass
import math
import re

REDACTED_SECRET = '[REDACTED_SECRET]'
_PLACEHOLDER = re.escape(REDACTED_SECRET)

# Names whose assigned value is treated as a credential regardless of shape.
CREDENTIAL_NAME = (
    r'(?:[A-Za-z0-9]+[_-])*(?:password|passwd|passphrase|secret|api[_-]?key|'
    r'access[_-]?token|refresh[_-]?token|session[_-]?token|id[_-]?token|'
    r'client[_-]?secret|secret[_-]?access[_-]?key|private[_-]?key|token|'
    r'authorization|proxy[_-]?authorization|x[_-]?api[_-]?key|'
    r'cookie|set[_-]?cookie|x[_-]?amz[_-]?signature|x[_-]?goog[_-]?signature)'
)
SECRET_KEYS = re.compile(r'^(?:' + CREDENTIAL_NAME + r')$', re.I)

# Unquoted assignment values that are references or code, never the literal.
_REFERENCE_VALUE = re.compile(
    r'(?:None|null|nil|undefined|true|false|True|False|\*+|<[^>]{0,64}>?'
    r'|\$\{?[A-Za-z_][A-Za-z0-9_]*\}?|\$\(.{0,256}|%[A-Za-z_][A-Za-z0-9_]*%|\{\{.{0,256}'
    r'|(?:os\.environ|os\.getenv|process\.env|getenv|env)\b[^\s]{0,128}'
    r'|[A-Za-z_][A-Za-z0-9_]{0,64}(?:\.[A-Za-z_][A-Za-z0-9_]{0,64}){0,6}\(.{0,256}'
    r'|(?:self|this|cls|request|req|config|settings|args|kwargs|params|options)\.[A-Za-z_].{0,256})'
)


@dataclass(frozen=True)
class Finding:
    rule: str
    start: int
    end: int


@dataclass(frozen=True)
class Rule:
    id: str
    pattern: re.Pattern
    group: tuple = (0,)
    check: object = None
    source: str = 'vaelius'


def _has_letter_and_digit(value):
    return any(c.isdigit() for c in value) and any(c.isalpha() for c in value)


def _entropy(value):
    counts = {}
    for char in value:
        counts[char] = counts.get(char, 0) + 1
    return -sum(n / len(value) * math.log2(n / len(value)) for n in counts.values())


_HEX = re.compile(r'[0-9a-fA-F]+')
_UUID = re.compile(r'[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}')
_SLUG = re.compile(r'[a-z][a-z0-9]*(?:[-_.][a-z0-9]+)+')
_DOTTED = re.compile(r'[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)+')


_ASSIGNED_NAME = re.compile(r'([A-Za-z0-9_.-]{0,48})["\']?[ \t]{0,8}(?::=|=>|[=:])[ \t]{0,8}["\']?$')
_PUBLIC_NAME = re.compile(r'(?i)public[_.-]?$|pub[_.-]?$|public[_.-]?key$|pub[_.-]?key$')


def _high_entropy_value(match, value):
    """Conservative: random-looking, mixed, and not a digest, UUID, slug or path."""
    window = match.string[max(0, match.start() - 48):match.start(1)]
    name = _ASSIGNED_NAME.search(window)
    if name and _PUBLIC_NAME.search(name.group(1)):
        return False
    value = value.strip('.')
    if len(value) < 20 or not _has_letter_and_digit(value):
        return False
    if (_HEX.fullmatch(value) or _UUID.fullmatch(value) or _SLUG.fullmatch(value)
            or _DOTTED.fullmatch(value) or value.startswith(('/', './', '../'))):
        return False
    upper = any(c.isupper() for c in value); lower = any(c.islower() for c in value)
    return upper and lower and _entropy(value) >= 3.5


def _assignment_value(match, value):
    if value.startswith(REDACTED_SECRET[:-1]):
        return False
    if match.group(match.lastindex) == value and match.lastindex == 3:
        return not _REFERENCE_VALUE.fullmatch(value)
    return bool(value.strip())


def _token_body(match, value):
    return _has_letter_and_digit(value)


_RULES = (
    # Gitleaks: aws-access-token (prefix set; baseline keeps [A-Z0-9]).
    Rule('aws-access-token', re.compile(r'\b(?:A3T[A-Z0-9]|AKIA|ASIA|ABIA|ACCA)[A-Z0-9]{16}\b'), source='gitleaks'),
    # Gitleaks: github-pat, github-oauth, github-app-token, github-refresh-token,
    # github-fine-grained-pat (baseline keeps the broader minimum length).
    Rule('github-token', re.compile(r'\b(?:gh[pousr]_[A-Za-z0-9_]{16,255}|github_pat_[A-Za-z0-9_]{16,255})\b'), source='gitleaks'),
    # Gitleaks: slack-bot/user/app/legacy/config tokens.
    Rule('slack-token', re.compile(r'\b(?:xox[abeoprs]-[A-Za-z0-9-]{10,255}|xapp-\d-[A-Za-z0-9-]{10,255})'), source='gitleaks'),
    # Gitleaks: gcp-api-key.
    Rule('gcp-api-key', re.compile(r'\bAIza[0-9A-Za-z_-]{35}(?![0-9A-Za-z_-])'), source='gitleaks'),
    # Gitleaks: stripe-access-token.
    Rule('stripe-access-token', re.compile(r'\b(?:sk|rk)_(?:live|test|prod)_[0-9A-Za-z]{10,99}\b'), source='gitleaks'),
    # OpenAI/Anthropic-style keys (Gitleaks openai-api-key, anthropic-api-key are
    # exact-length subsets of this). A digit is required to spare prose slugs.
    Rule('sk-api-key', re.compile(r'\bsk-[A-Za-z0-9_-]{16,255}'), check=_token_body),
    # Gitleaks: jwt (baseline form; signature may be empty for alg=none tokens).
    Rule('jwt', re.compile(r'\beyJ[A-Za-z0-9_-]{8,4096}\.[A-Za-z0-9_-]{1,8192}\.[A-Za-z0-9_-]{0,4096}'), source='gitleaks'),
    Rule('bearer-token', re.compile(r'(?i)\bbearer\s{1,8}(?!' + _PLACEHOLDER + r')([A-Za-z0-9._~+/=-]{12,4096})'), group=(1,)),
    Rule('authorization-header', re.compile(
        r'(?i)\b(?:set-cookie|cookie|authorization|proxy-authorization)[ \t]{0,8}:[ \t]{0,8}'
        r'(?![ \t]*' + _PLACEHOLDER + r')([^\s][^\r\n]{0,8191})'), group=(1,)),
    Rule('credential-assignment', re.compile(
        r'(?i)(?<![\w-])' + CREDENTIAL_NAME + r'["\']?[ \t]{0,8}[=:][ \t]{0,8}'
        r'(?:"((?:\\.|[^"\\\r\n]){1,4096})"?|\'((?:\\.|[^\'\\\r\n]){1,4096})\'?|([^\s"\',;}\]&#]{1,4096}))'),
        group=(1, 2, 3), check=_assignment_value),
    # Generic high-entropy value assigned to a credential-like name (any prefix,
    # including camelCase). Gitleaks: generic-api-key, made more conservative.
    Rule('high-entropy-assignment', re.compile(
        r'(?i)(?:key|secret|token|passw(?:or)?d|pwd|credentials?|auth|signature)["\']?[ \t]{0,8}'
        r'(?::=|=>|[=:])[ \t]{0,8}["\']?([A-Za-z0-9+/_.=~-]{20,512})'), group=(1,),
        check=_high_entropy_value, source='gitleaks'),
    Rule('url-credentials', re.compile(
        r'\b[A-Za-z][A-Za-z0-9+.-]{0,31}://([^\s/:@"\'<>]{1,256}:[^\s/"\'<>]{1,1024})@'), group=(1,)),
)

# Gitleaks: private-key, located by markers. A truncated block (no END marker in
# a bounded window) redacts the header and the following key-material lines.
_KEY_BEGIN = re.compile(r'-----BEGIN[ A-Z0-9_-]{0,100}PRIVATE KEY(?: BLOCK)?-----')
_KEY_END = re.compile(r'-----END[ A-Z0-9_-]{0,100}PRIVATE KEY(?: BLOCK)?-----')
_KEY_TAIL = re.compile(r'(?:[ \t]*\r?\n[A-Za-z0-9+/=:, \t-]{1,256}){0,256}')
_KEY_WINDOW = 65536

RULE_IDS = tuple(sorted({rule.id for rule in _RULES} | {'private-key'}))
RULE_SOURCES = {rule.id: rule.source for rule in _RULES} | {'private-key': 'gitleaks'}


def _private_keys(text):
    if '-----BEGIN' not in text:
        return []
    ends = [m.span() for m in _KEY_END.finditer(text)]
    starts = [s for s, _ in ends]
    found = []
    for begin in _KEY_BEGIN.finditer(text):
        index = bisect_left(starts, begin.end())
        if index < len(ends) and ends[index][0] - begin.end() <= _KEY_WINDOW:
            found.append(Finding('private-key', begin.start(), ends[index][1]))
        else:
            tail = _KEY_TAIL.match(text, begin.end())
            found.append(Finding('private-key', begin.start(), tail.end()))
    return found


def findings(text):
    """Return unmerged rule matches in text order. Never returns matched values."""
    if not isinstance(text, str):
        raise TypeError('text_required')
    found = _private_keys(text)
    for rule in _RULES:
        for match in rule.pattern.finditer(text):
            group = next((g for g in rule.group if match.start(g) >= 0), None)
            if group is None:
                continue
            start, end = match.span(group)
            if start == end:
                continue
            if rule.check is not None and not rule.check(match, match.group(group)):
                continue
            found.append(Finding(rule.id, start, end))
    return sorted(found, key=lambda item: (item.start, -item.end, item.rule))


def _merged(found):
    spans = []
    for item in found:
        if spans and item.start <= spans[-1][1]:
            spans[-1][1] = max(spans[-1][1], item.end)
        else:
            spans.append([item.start, item.end])
    return spans


def redact_text(text, placeholder=REDACTED_SECRET):
    """Replace every detected span; return (safe text, sorted matched rule ids)."""
    found = findings(text)
    if not found:
        return text, []
    parts = []; position = 0
    for start, end in _merged(found):
        parts.append(text[position:start]); parts.append(placeholder); position = end
    parts.append(text[position:])
    return ''.join(parts), sorted({item.rule for item in found})


def contains_secret(text):
    return bool(findings(text))


class _Compatible:
    """Keeps the historical ``cleaning.SECRET.search/sub`` call shape."""
    pattern = 'agentclient.secret_detection'

    @staticmethod
    def search(text):
        found = findings(text)
        return found[0] if found else None

    @staticmethod
    def sub(replacement, text):
        if not isinstance(replacement, str):
            raise TypeError('literal_replacement_required')
        return redact_text(text, replacement)[0]


SECRET = _Compatible()
