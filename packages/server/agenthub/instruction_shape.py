"""Deterministic label for captured text that reads as instructions to an agent.

Shared memory is written by one person and read by other people's agents. Text
that tries to steer a later reader ("ignore previous instructions", "note to AI
agents: you must ...") is still useful history, so it is kept and served under
the usual policy. It is labelled `instruction_like` so clients can down-weight or
mark it. The label never deletes, rewrites, executes or elevates content.

The patterns target text addressed to agents or trying to override a reader's
instructions. Ordinary engineering prose ("the build must pass", "you should see
the log line", "ignore the deprecation warning") is deliberately not matched.
This is a labelling heuristic, not a security boundary or a complete detector.
"""
import re

# "Model" alone is ordinary ML vocabulary, so directives need an agent noun.
_AGENT = (r'(?:(?:ai|llm|coding|autonomous)\s+)?(?:ai|a\.i\.|llms?|language\s+models?|assistants?|agents?|'
          r'chatbots?|copilots?|claude|codex|chatgpt|gpt(?:-\d+)?)')
_RULES = (r'(?:instructions?|directions?|directives?|rules|guidelines|guardrails|prompts?|'
          r'system\s+prompts?|policies|constraints|context)')

_PATTERNS = (
    # Attempts to override whatever instructions the reader currently has.
    r'\b(?:ignore|disregard|forget|override|bypass|drop|discard)\s+(?:(?:all|any|every|the|your|my|of|these|those|'
    r'previous|prior|above|earlier|preceding|existing|original|current|system|developer|safety|other)\s+){0,4}' + _RULES + r'\b',
    # Role reassignment and standing directives for whoever reads this later.
    r'\byou\s+are\s+now\s+(?:in\s+)?(?:an?\s+)?(?:developer|jailbreak|jailbroken|dan|god|unrestricted|admin|root|sudo|debug)\b',
    r'\bfrom\s+now\s+on,?\s+(?:you|the\s+(?:assistant|agent|model|ai))\s+(?:must|will|shall|should|are|have\s+to)\b',
    r'\b(?:note|message|instructions?|reminder|directive|warning)\s+(?:to|for)\s+(?:(?:any|all|every|the|future|other)\s+){0,2}' + _AGENT + r'\b',
    r'\b(?:any|all|every|each|future|other)\s+' + _AGENT + r'\s+(?:reading|that\s+reads|who\s+reads|retrieving|that\s+retrieves|seeing|processing)\b',
    r'\b(?:if|when)\s+you\s+are\s+an?\s+' + _AGENT + r'\b',
    r'\b(?:when|if|once)\s+(?:an?\s+|the\s+)?' + _AGENT + r'\s+(?:reads|retrieves|sees|loads|finds)\s+this\b',
    # Directives addressed to agents by name ("agents must always ...").
    r'\b' + _AGENT + r'\s+(?:must|shall|should|are\s+required\s+to|are\s+instructed\s+to|need\s+to)\s+(?:always|never|ignore|obey|comply|disregard)\b',
    # Absolute directives to the reader, including "you must ... unless" forms.
    r'\byou\s+(?:must|shall)\s+(?:always|never|ignore|obey|comply\s+with|disregard)\b',
    r'\byou\s+must\s+not\b[^.\n]{0,120}\bunless\b',
    r'\byou\s+are\s+(?:required|instructed|ordered|authori[sz]ed|permitted|allowed)\s+to\s+(?:ignore|bypass|skip|disable|override|disregard)\b',
    # Concealment directives; clause-initial so "the job never notifies the user"
    # (a description of software behaviour) is not matched.
    r'(?:^|[.!?;:,\n]\s*|\band\s+)(?:do\s+not|don\'t|never)\s+(?:tell|inform|mention\s+(?:this|it)\s+to|'
    r'reveal\s+(?:this|it)\s+to|alert|notify|warn)\s+(?:the\s+)?(?:user|human|operator|reviewer)s?\b',
    r'\bwithout\s+(?:telling|informing|alerting)\s+(?:the\s+)?(?:user|human|operator|reviewer)s?\b',
    # Counterfeit framing that imitates a host or system channel.
    r'</?\s*(?:system|instructions?|admin|developer)\s*>',
    r'\[\s*(?:system|inst|/inst|admin)\s*\]',
    r'\b(?:begin|start|new|updated|override)\s+(?:system|admin|developer)\s+(?:prompt|instructions?|message)\b',
    r'(?:^|\n)\s*(?:new|updated|real|actual)\s+instructions?\s*:',
)
INSTRUCTION_LIKE = re.compile('|'.join('(?:' + p + ')' for p in _PATTERNS), re.IGNORECASE)
# Unicode tag characters render as nothing and are used to smuggle hidden text.
HIDDEN = re.compile('[\U000E0000-\U000E007F]')
SCAN_CHARS = 262144


def instruction_like(text):
    """True when the text reads as instructions aimed at an agent reader."""
    if not isinstance(text, str) or not text:
        return False
    sample = text[:SCAN_CHARS]
    return bool(HIDDEN.search(sample) or INSTRUCTION_LIKE.search(sample))
