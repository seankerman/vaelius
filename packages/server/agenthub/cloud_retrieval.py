"""Authorized PostgreSQL lexical/vector candidates, canonical facets and delivery.

HNSW supports bounded nearest-neighbor retrieval; PostgreSQL applies current
permissions before returning candidates. Embedding scores never establish truth.
"""
import hashlib
import json
from decimal import Decimal, InvalidOperation
from datetime import datetime
import re
import time
import uuid
from agentclient.cleaning import terms
from agenthub.processing.semantic import MODEL_KEY,DIMENSION,normalize_vector
from agenthub.enterprise import Denied
from agenthub.request_timing import timed, span

FILTERS={'domain','knowledge_type','subject','event_day'}

def _vector(values):
    normalized=normalize_vector(values,DIMENSION)
    return '['+','.join(str(v) for v in normalized)+']'


def lexical_terms(query):
    """Remove framing from text rank, preserving the full query elsewhere.

    Reported speech is ubiquitous in curated memories. Counting its framing as
    topical evidence can outrank the requested fact. Reuse canonical function
    words, retaining content-bearing status/version/recovery concepts and quoted
    identifiers. This is candidate generation, never an answerability decision.
    """
    from agenthub.processing.retrieval import GENERIC,subject_query
    ignored=GENERIC-{'fixture','version','public','configuration','config','setting','settings',
        'successful','recover','recovery','repair','error','failed','failure','check','steps','task'}
    if re.search(r'\b(?:agent|assistant)\s+(?:report\w*|say\w*|said|stat\w*|mention\w*)\b',query,re.I):
        ignored=ignored|{'report','reports','reported','say','says','said','state','stated','mention','mentioned'}
    quoted={term for text in re.findall(r'"([^"\n]+)"',query) for term in terms(text)}
    return [term for term in terms(subject_query(query)) if term not in ignored or term in quoted]

# Typed subjects are structural constraints; surrounding request verbs are not.
_NUMBERED = re.compile(r'\b(?:Project|Volume|Record|Dataset|Document|Report|Study|Case|Source|Model|Group|Option|Artifact)\s+(?:[A-Z][A-Za-z_]+\s+){0,2}\d+\b')
_IDENTIFIER = re.compile(r'\b[A-Za-z][A-Za-z_-]*[-_]\d+\b')
_ENTITY_NOUN = re.compile(r'\s+(?:dataset|project|document|location|artifact|report|preference|decision|budget|source|file|chose|chooses|uses|prefers)\b',re.I)
_FUNCTION_WORDS = set('what who which where when why how the this that these those my our their his her its a an i we you it in on at for from by after before now today yesterday tomorrow monday tuesday wednesday thursday friday saturday sunday last next current original approved user agent assistant project source doc memory'.split())


def _contains(text,value):
    return bool(re.search(r'(?<![a-z0-9_])'+re.escape(value.casefold())+r'(?![a-z0-9_])',text.casefold()))


def _named_subjects(query):
    from agenthub.processing.retrieval import subject_query
    query=subject_query(query)
    names=re.findall(r'"([^"\n]+)"',query)
    for match in re.finditer(r'\b[A-Z][A-Za-z0-9_]{1,}\b',query):
        name=match.group();before=query[:match.start()].rstrip()
        if name.casefold() in _FUNCTION_WORDS:continue
        # Capitalization at the beginning of a request/sentence does not establish
        # identity. Keep it when syntax supplies a topic, possessive or acronym.
        initial=not before or before[-1:] in '.!?;:'
        following=query[match.end():]
        topic_noun=_ENTITY_NOUN.match(following) and not _NUMBERED.match(following.lstrip())
        if initial and not (name.isupper() or topic_noun or
                            re.match(r"['’]s\b",query[match.end():])):continue
        names.append(name)
    return list(dict.fromkeys(names))


def _clauses(text):
    # Decimal numbers, paths and abbreviations remain intact. Semicolons keep a
    # qualifier with its value ("proposed ...; remains unconfirmed").
    return [part.strip() for part in re.split(
        r'(?<=[.!?])\s+|(?:\s+(?:while|whereas|but|and)\s+|,\s+)(?=[A-Z][A-Za-z_]+\s+(?:chose|choose|uses|prefers|reported|did|has|is)\b)',text) if part.strip()]


def _actor(query):
    from agenthub.processing.durable_memory import intent
    plan=intent(query)
    if plan and plan.get('reason_actor'):return plan['reason_actor']
    match=re.search(r"\b([A-Z][A-Za-z_]+)['’]s\b|\bby\s+([A-Z][A-Za-z_]+)\b",query)
    return next((v for v in match.groups() if v),None) if match else None


def _actor_clauses(clauses,actor):
    if not actor:return clauses
    selected=[];antecedent=False
    for clause in clauses:
        named=re.match(r"(?:In [^,]+,\s*)?([A-Z][A-Za-z_]+)\s+(?:chose|choose|uses|prefers|reported|said|did|saved)\b",clause)
        if named:
            antecedent=named[1].casefold()==actor.casefold()
            if antecedent:selected.append(clause)
        elif _contains(clause,actor):antecedent=True;selected.append(clause)
        elif antecedent and re.search(r'\b(?:she|he|they|her|his|their)\b',clause,re.I):selected.append(clause)
    return selected


def _negative(clause,facet):
    # Negation must govern the requested facet, not an unrelated statement.
    target={'verification':r'verif(?:y|ied|ication)|confirm(?:ed|ation)?|approv(?:ed|al)',
            'money':r'budget|spend(?:ing)?|spent|amount|total|cost|approv(?:ed|al)|authoriz(?:ed|ation)',
            'launch':r'launch|go-live|release|confirm(?:ed|ation)?',
            'verified':r'verif(?:y|ied|ication)', 'confirmed':r'confirm(?:ed|ation)?',
            'approved':r'approv(?:e|ed|al)'}[facet]
    specific=facet in {'verified','confirmed','approved'}
    denials=r'no|not|never' if specific else r'no|not|never|unknown|unconfirmed|unapproved|unverified'
    states='un'+facet if specific else r'unconfirmed|unapproved|unverified|unknown'
    return bool(re.search(r'\b(?:'+denials+r')\b.{0,45}\b(?:'+target+r')\b|\b(?:'+states+r')\b',clause,re.I))


def _value_supported(query,text,facet,identities):
    q=query.casefold();clauses=_clauses(text)
    expression=(r'\b(?:budget|spend(?:ing)?|spent|amount|total|cost|purchase|price|pay|paid)\b' if facet=='money'
                else r'\b(?:launch|go-live|release)\b')
    relevant=[c for c in clauses if re.search(expression,c,re.I) and
              not re.search(r'\b(?:another|other)\s+(?:project|customer|team)\b',c,re.I)]
    # Explicit different typed subjects cannot donate a value to this question.
    relevant=[c for c in relevant if not _NUMBERED.findall(c) or
              not identities or any(_contains(c,i) for i in identities)]
    yes_no=bool(re.match(r'\s*(?:is|are|was|were|does|did|has|have|can)\b',q))
    if yes_no:
        if re.search(r'\b(?:approved|authorized|confirmed)\b',q):
            return any(_negative(c,facet) or (not re.search(r'\b(?:estimated|proposed|tentative|provisional)\b',c,re.I) and
                re.search(r'\b(?:approved|authorized|confirmed)\b',c,re.I)) for c in relevant)
        return bool(relevant)
    approved=bool(re.search(r'\b(?:approv\w*|authoriz\w*|signed-off|confirmed|official\w*)\b',q))
    for clause in relevant:
        if _negative(clause,facet):continue
        if re.search(r'\b(?:estimated|proposed|tentative|provisional|unconfirmed|unapproved)\b',clause,re.I):continue
        if approved and not re.search(r'\b(?:approved|authorized|signed-off|confirmed|official)\b',clause,re.I):continue
        if facet=='money':
            if re.search(r'(?:[$€£]\s*\d[\d,.]*|\b\d[\d,.]*\s*(?:usd|eur|gbp|dollars|euros|pounds)\b)',clause,re.I):return True
        elif re.search(r'\b(?:\d{4}-\d{2}-\d{2}|(?:January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{1,2}(?:,?\s+\d{4})?)\b',clause,re.I):return True
    return False


def _contrast_facts(query):
    # Uncertainty-qualified comparison references are distinct fact requests.
    # Do not bind their speaker to the affirmative artifact/location facet.
    return re.split(r"\s+(?:from|versus|vs\.?|compared with|compared to|as opposed to|in contrast to|with)\s+"
        r"(?=[A-Z][A-Za-z_]+['’]s\s+(?:[A-Za-z_-]+\s+){0,3}(?:unverified|unconfirmed|unapproved|not\s+(?:verified|confirmed|approved))\b)",query,maxsplit=1)



def _supported_claim(query,claim):
    """Direct-evidence guard after canonical authorization and facet selection.

    Names/identifiers, attributed reasons, verified facts and actual amounts or
    dates remain mandatory. A supported negative yes/no fact is an answer; an
    absent value is not. Ambiguous actor evidence fails closed.
    """
    from agenthub.processing.retrieval import GENERIC,TEMPORAL_WORDS,subject_query
    query=subject_query(query);text=claim.get('lesson','');q=query.casefold()
    if not text.strip():return False
    reason_request=bool(re.search(r'\b(?:why|reason|rationale)\b',q))
    curated=None;reason_evidence=''
    if reason_request and 'memory_context' in claim:
        from agenthub.processing.durable_memory import VERSION,FACETS
        curated=claim['memory_context']
        if not isinstance(curated,dict) or curated.get('policy')!=VERSION:return False
        facets=curated.get('facets');actors=curated.get('actors')
        quote=curated.get('reason_quote');speaker=curated.get('reason_actor')
        if (not isinstance(facets,list) or not facets or set(facets)-set(FACETS) or
                not isinstance(actors,list) or not isinstance(quote,str) or not quote.strip() or
                not isinstance(speaker,str) or not speaker or speaker not in actors or
                not isinstance(curated.get('subject'),str) or not curated['subject'].strip()):return False
        # Only current canonical compiled rationale is projected into evidence.
        # Its quote/speaker were validated against original cited source spans;
        # display prose is not the selector for this facet.
        reason_evidence=' '+quote+' '+speaker
    evidence_text=text+reason_evidence
    focus=set(terms(query))-GENERIC-TEMPORAL_WORDS-{'where','why','give','original','saved','stored','the','and','what','is'}
    if not focus or not focus.intersection(terms(evidence_text+' '+claim.get('title',''))):return False
    identities=_IDENTIFIER.findall(query)+_NUMBERED.findall(query)
    if any(not _contains(evidence_text,i) for i in identities):return False
    subjects=_named_subjects(query)
    if any(not _contains(evidence_text+' '+claim.get('title',''),s) for s in subjects):return False
    contrasts=_contrast_facts(query)
    if len(contrasts)>1:return all(_supported_claim(part,claim) for part in contrasts)
    quantity=re.search(r'\bhow much\s+(time|storage|memory|disk space|data|capacity|bandwidth)\b',q)
    if quantity:
        unit=(r'milliseconds?|seconds?|minutes?|hours?|days?|weeks?' if quantity[1]=='time' else
              r'bytes?|[kmgt]i?b|[kmgt]i?bytes?|bits?|[kmgt]bps')
        if not re.search(r'\b\d[\d,.]*\s*(?:'+unit+r')\b',text,re.I):return False
    monetary_quantity=bool(re.search(r'\bhow much\b.{0,100}\b(?:authoriz\w*|approv\w*|pay|paid|spend|spent|cost)\b',q))
    money=not quantity and (monetary_quantity or bool(re.search(r'\b(?:budget|spend(?:ing)?|spent|cost|price|purchase\s+(?:amount|total)|(?:amount|total|figure).{0,45}(?:authoriz\w*|approv\w*)|(?:authoriz\w*|approv\w*).{0,45}(?:amount|total|figure))\b',q)))
    launch=bool(re.search(r'\b(?:launch|go-live|release\s+(?:date|day))\b',q))
    if money and not _value_supported(query,text,'money',identities):return False
    if launch and not _value_supported(query,text,'launch',identities):return False
    clauses=_clauses(text);actor=_actor(query)
    for uncertainty in re.findall(r'\b(?:un|not )(verified|confirmed|approved)\b',q):
        relevant=_actor_clauses(clauses,actor)
        if not any(_negative(c,uncertainty) for c in relevant):return False
    if reason_request:
        if curated is not None:
            if actor and curated['reason_actor'].casefold()!=actor.casefold():return False
            # Same canonical focus used by durable_retrieve: incidental options
            # in display narrative cannot become a chosen option's rationale.
            reason_focus=' '.join([claim.get('title',''),curated['subject'],curated['reason_quote']])
            option=re.search(r'\b(?:choose|chose|use|prefer|select|pick)\s+(.+?)(?:\?|[,.]|$)',query,re.I)
            if option:
                wanted=set(terms(option[1]))-GENERIC-TEMPORAL_WORDS-{'the','a','an'}
                if not wanted<=set(terms(reason_focus)):return False
                for clause in _clauses(curated['reason_quote']):
                    if wanted<=set(terms(clause)) and re.search(
                            r'\b(?:did not|does not|never)\s+(?:choose|use|prefer|select)\b',clause,re.I):return False
        else:
            reasons=[c for c in clauses if re.search(r'\b(?:because|so that|reason|rationale|preserve|enables|to avoid)\b',c,re.I)]
            if actor:reasons=[c for c in reasons if c in _actor_clauses(clauses,actor)]
            # The requested chosen option and its actor must be in the same rationale,
            # rather than collected from different actors' sentences.
            option=re.search(r'\b(?:choose|chose|use|prefer|select|pick)\s+(.+?)(?:\?|[,.]|$)',query,re.I)
            if option:
                wanted=set(terms(option[1]))-GENERIC-TEMPORAL_WORDS-{'the','a','an'}
                reasons=[c for c in reasons if wanted<=set(terms(c)) and
                    re.search(r'\b(?:choose|chose|chosen|use|uses|used|prefer|prefers|preferred|selected|picked)\b',c,re.I) and
                    not re.search(r'\b(?:rejected|declined|avoided)\b',c,re.I)]
            reasons=[c for c in reasons if not re.search(r'\b(?:did not|does not|never)\s+(?:choose|use|prefer|select)\b',c,re.I)]
            if not reasons:return False
    if re.search(r'\b(?:where|location|saved|stored|path)\b',q):
        location=[c for c in clauses if re.search(r'[/\\]|\b(?:location|directory|bucket|folder|saved|stored)\b',c,re.I)]
        location=[c for c in location if not re.search(r'\b(?:failed|not saved|not stored|unknown)\b',c,re.I)]
        location_actor=_actor(re.split(r',?\s+and\s+(?=(?:was|is|did|has|were|are)\b)',query,flags=re.I)[0])
        if location_actor:location=[c for c in location if c in _actor_clauses(clauses,location_actor)]
        if re.search(r'\b(?:confirmed|verified)\b',q):
            location=[c for c in location if not _negative(c,'verification')]
        if not location:return False
    # Apply verification separately to each requested fact. A negative report
    # answers "Was Bob's report verified?" but not "Give Bob's verified path".
    for part in re.split(r',?\s+and\s+(?=(?:was|is|did|has|were|are)\b)',query,flags=re.I):
        if not re.search(r'\b(?:confirmed|verified|approved)\b',part,re.I) or money or launch:continue
        requested_actor=_actor(part)
        relevant=[c for c in clauses if not requested_actor or _contains(c,requested_actor)]
        if re.search(r'\b(?:where|location|saved|stored|path)\b',part,re.I):
            relevant=[c for c in relevant if re.search(r'[/\\]|\b(?:location|directory|bucket|folder|saved|stored)\b',c,re.I)]
        if not relevant:return False
        yes_no=bool(re.match(r'\s*(?:is|are|was|were|does|did|has|have)\b',part,re.I))
        if yes_no:
            if not any(re.search(r'\b(?:verif\w*|confirm\w*|approv\w*)\b',c,re.I) for c in relevant):return False
        elif not any(not _negative(c,'verification') and re.search(r'\b(?:confirmed|verified|approved)\b',c,re.I) for c in relevant):return False
    return True


def _prose(text):
    return '\n'.join(line for line in text.splitlines() if line.strip() and
                     not re.match(r'^\s*#{1,6}\s',line)).strip()


def _word_set(text):
    from agenthub.processing.retrieval import GENERIC,TEMPORAL_WORDS
    words=set(terms(text))-GENERIC-TEMPORAL_WORDS-_FUNCTION_WORDS
    words-=set('give find tell explain remind saved stored getting someone something me do de prefer preferred prefers report reported hearing heard recorded needed required'.split())
    # Inflection normalization, not a catalogue of customer entities or answers.
    normalized=set()
    for word in words:
        if len(word)>5 and word.endswith('ies'):word=word[:-3]+'y'
        elif len(word)>4 and word.endswith('s'):word=word[:-1]
        if len(word)>6 and word.endswith('ing'):word=word[:-3]
        elif len(word)>5 and word.endswith('ed'):word=word[:-2]
        if len(word)>4 and word.endswith('e'):word=word[:-1]
        normalized.add(word)
    return normalized


def _request_actor(query,ctx):
    historical=bool(re.search(r'\b(?:did|have|had|was|were)\s+i\b|\b(?:my|i)\b.*\b(?:prefer|preferred|chose|choose|decide|implemented|saved|stored|work)\b|\bwhy\b.*\bi\b',query,re.I))
    if historical or re.search(r'\bmy\s+(?:preferred|preference|report|notes|dataset|file)\b',query,re.I):
        return ctx.get('actor'),True
    explicit=_actor(query)
    if explicit=='user' and re.search(r'\b(?:the\s+)?user\s+(?:choose|chose|prefer\w*|decid\w*|use\w*)\b',query,re.I):
        # The canonical role is resolved through the caller and subsequently
        # checked against original evidence ownership, never curator authorship.
        return ctx.get('actor'),True
    match=re.search(r'\b(?:did|does|do|has|have)\s+([A-Z][A-Za-z_-]+)\b',query)
    if match:explicit=match[1]
    return explicit,False


def _attributed(text,actor,claim):
    if not actor:return text
    result=[];antecedent=False
    owners=claim.get('_verified_source_owners',[])
    for clause in _clauses(text):
        named=re.match(r'(?:In [^,]+,\s*)?([A-Z][A-Za-z_-]+)(?::\s*I|\s+\w+)\b',clause)
        if named:
            antecedent=named[1].casefold()==actor.casefold()
        elif re.match(r'I\s+\w+',clause):
            antecedent=len(owners)==1 and owners[0].casefold()==actor.casefold()
        elif not re.match(r'(?:He|She|They|His|Her|Their)\b',clause):
            antecedent=_contains(clause,actor)
        if antecedent:result.append(clause)
    return ' '.join(result)


def _facet_queries(query):
    # A second interrogative is an independent requested fact. Noun pairs use
    # the same topic and predicate, so credentials cannot donate completion.
    parts=re.split(r',?\s+and\s+(?=(?:what|which|where|when|why|how)\b)',query,flags=re.I)
    if len(parts)>1:
        subjects=_named_subjects(parts[0])
        return [parts[0]]+[('; '.join(subjects)+': ' if subjects else '')+part for part in parts[1:]]
    match=re.match(r'^(What|Which)\s+(.+?)\s+and\s+(.+?)\s+(are|do|must|govern|should)\b(.*)',query,re.I)
    if match and not re.search(r'\b(?:is|contains|stored|saved)\b',match[2],re.I):
        return [f'{match[1]} {noun} {match[4]}{match[5]}' for noun in (match[2],match[3])]
    return [query]


# Typed answer families are shared concepts, independent of source IDs/titles.
_FACET_CUES={
    'credentials':r'certificate|credential|authentication|password|token',
    'completion':r'verif\w*|digest|checksum|acknowledg\w*|completion',
    'approval':r'approv\w*|authoriz\w*|sign.?off',
    'rollback':r'rollback|snapshot|restore|backup',
    'consent':r'consent|opt.?in|permission|agree\w*',
    'retention':r'retain\w*|retention|delet\w*|purge\w*|days?|hours?',
    'integrity':r'integrity|checksum|digest|sha.?256|hash',
    'scope':r'scope|destination|bucket|project|tenant|grant',
    'expiry':r'expir\w*|hours?|days?|until|valid',
    'fields':r'fields?|columns?|schema|timestamp|offset|encoding|utf.?8',
    'inventory':r'contains?|includes?|plates?|controls?|contents?',
}


def _balanced_facet_queries(query):
    parts=_facet_queries(query)
    if len(parts)>1 and re.search(
            r"\b(?:agent|assistant)['’]s\s+(?:stated\s+|reported\s+)?preference\b",parts[0],re.I):
        # A dependent question keeps its explicit speaker. An independently
        # named speaker or the caller's preference must never inherit that role.
        parts=[parts[0]]+[
            "Regarding the assistant's stated preference: "+part
            if (re.search(r'\b(?:it|that|this)\b',part,re.I) and
                re.search(r'\bpreference\b',part,re.I) and
                not _named_subjects(part) and
                not re.search(r'\b(?:i|my|we|our|user)\b',part,re.I)) else part
            for part in parts[1:]]
    if len(parts)==1:
        match=re.match(r'^(What|Which)\s+(?:are\s+(?:the\s+)?)?(.+?)\s+and\s+(.+?)\s+(rules|constraints|requirements)\b(.*)',query,re.I)
        if match:
            parts=[f'{match[1]} {noun} {match[4]}{match[5]}' for noun in (match[2],match[3])]
    subjects=_named_subjects(query)
    # A later facet must keep the original entity even when the first noun is
    # "Jasper fields" and the second is merely "integrity check".
    return [('Regarding '+', '.join(subjects)+': ' if subjects and
             any(not _contains(part,subject) for subject in subjects) else '')+part for part in parts]


def _balanced_request_actor(query,ctx):
    actor,deictic=_request_actor(query,ctx)
    if deictic:return actor,True
    if re.search(r'\b(?:should|would)\s+i\s+(?:receive|get)\b|\bmy\s+(?:updates|notifications)\b',query,re.I):
        return ctx.get('actor'),True
    # "How long does Jasper retain ..." names an entity, not a human speaker.
    # Historical actions, possessives and explicit reported preferences retain
    # their actor constraints. Present procedural requests remain unbound.
    if re.search(r'\b(?:did|has|had)\s+[A-Z][A-Za-z_-]+\b',query) or _actor(query):
        return actor,False
    if re.search(r'\bprefer\w*|\breport\w*|\bhear\w*|\bheard\b',query,re.I):
        return actor,False
    return None,False


def _balanced_family_supported(query,text):
    q=query.casefold()
    # Topic similarity does not establish a requested contact value. These
    # attributes require a value in the cited prose, not merely a matching
    # project name or a statement that contact details are unknown.
    if re.search(r'\b(?:phone|telephone|mobile)\s+number\b',q):
        if not re.search(r'(?<!\w)\+?\d[\d ()-]{5,}\d(?!\w)',text):return False
    if re.search(r'\b(?:e-mail|email)\s+address\b',q):
        if not re.search(r'[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}',text,re.I):return False
    # Source identity/review commentary is useful provenance, but cannot answer
    # a question about the underlying operational result by sharing its title.
    if re.search(r'\brelated note for\b',text,re.I) and re.search(
        r'does not (?:override|establish|report)|cannot (?:establish|authorize)|evidence is pending|source identity is immutable',text,re.I):return False
    if re.search(r'\b(?:why|reason|rationale)\b',q) and re.search(
        r'\b(?:no|not|never|unrecorded|unknown|missing)\b.{0,65}\b(?:reason|rationale|final selection)\b|\b(?:reason|rationale|final selection)\b.{0,50}\b(?:not|never|unrecorded|unknown|missing)\b',text,re.I):return False
    families={
        r'\b(?:remedy|resolution|fix)\b':r'\b(?:remedy|resolution|fix|repair|replace|invalidat\w*|restart|clear|rebuild)\b',
        r'\bcurrency\b':r'\b(?:USD|EUR|GBP|JPY|CAD|AUD|CHF|dollars?|euros?|pounds?|yen)\b|[$€£]',
        r'\b(?:fields|columns)\b':r'\b(?:fields?|columns?|schema|timezone|encoding|offset|reference)\b|\b(?:must|required to)\s+include\b',
        r'\bconsent\b':r'\b(?:require\w*|explicit|check\w*|accept\w*|opt.?in)\b.{0,55}\bconsent\b|\bconsent\b.{0,55}\b(?:require\w*|explicit|check\w*|accept\w*|opt.?in)\b',
        r'\bscope\b':r'\b(?:named user|specific project|scope|destination|tenant|bucket)\b',
        r'\b(?:how long|retention|expiry)\b':r'\b(?:retain\w*|expir\w*|days?|hours?|weeks?|until|valid)\b',
        r'\b(?:what|which)\s+unit\b':r'\b(?:units?|percent|kpa|psi|grams?|liters?|metres?|meters?)\b',
        r'\b(?:instrument|device)\b.{0,35}\bidentif\w*\b':r'\b(?:serial|identifier|identity)\b',
        r'\b(?:removed|deleted)\b.{0,70}\b(?:reappear\w*|recovery|restor\w*)\b':r'\b(?:tombstone|deletion marker|purge\w*|reviv\w*|prevent\w*)\b',
        r'\b(?:duplicate|twice)\b.{0,70}\b(?:purchase|billing|bill\w*|submissions?)\b|\b(?:purchase|bill\w*)\b.{0,70}\b(?:duplicate|twice)\b':r'\b(?:idempoten\w*|deduplicat\w*|transaction|purchase|bill\w*|charge\w*)\b',
    }
    return all(not re.search(request,q) or re.search(evidence,text,re.I) for request,evidence in families.items())


def _facets_supported(query,claim,ctx,semantic_score=None,*,balanced=False):
    from agenthub.processing.retrieval import subject_query
    query=subject_query(query);q=query.casefold();text=_prose(claim.get('lesson',''))
    if balanced and not _balanced_family_supported(query,text):return False
    if not text:return False
    if re.search(r'\bmy\s+(?:coworker|colleague|teammate)\b',q):return False
    identities=_IDENTIFIER.findall(query)+_NUMBERED.findall(query)
    if any(not _contains(text,ident) for ident in identities):return False
    subjects=_named_subjects(query)
    if any(not _contains(text+' '+claim.get('title',''),subject) for subject in subjects):return False
    prose=[clause for clause in _clauses(text) if not re.search(
        r'\b(?:does not|do not|did not|cannot)\s+(?:establish|report|record|prove|identify)|\bevidence\s+(?:is\s+)?pending\b',clause,re.I)]
    if not prose:return False
    text=' '.join(prose)
    memory=claim.get('memory_context',{})
    if memory:
        from agenthub.processing.durable_memory import VERSION
        canonical=memory.get('policy')==VERSION
        historical_event=re.search(
            r'\b(?:first|initial|earliest|previous|last)\s+(?:[\w-]+\s+){0,2}'
            r'(?:playtest|trial|test|run|build|experiment|iteration|session)\b',q)
        # A reported inability is a past outcome, not a proposal. Keep explicit
        # proposal/recommendation cues so negative future advice remains excluded.
        proposal_text=re.sub(r"\bcould\s+not\b|\bcouldn['’]t\b",'',text,flags=re.I)
        if (canonical and historical_event and memory.get('attribution')=='agent_reported'
                and memory.get('state')!='observed' and re.search(
                    r'\b(?:propos\w*|recommend\w*|should|could|would|next)\b',proposal_text,re.I)):
            return False
        if (canonical and memory.get('state')=='attempted' and re.search(
                r'\b(?:did|have|had)\s+(?:i|we)\s+(?:actually\s+)?'
                r'(?:implement\w*|sav\w*|finish\w*|run|ran|build|built|verif\w*)\b',q)):
            return False
    actor,deictic=(_balanced_request_actor if balanced else _request_actor)(query,ctx)
    if deictic and not actor:return False
    reported=bool(re.search(r'\b(?:report\w*|hear|heard|hearing|said|say)\b',q))
    preference=bool(re.search(r'\bprefer\w*|\bpreference\b',q) or
        balanced and re.search(r'\b(?:should|would)\s+i\s+(?:receive|get)\b|\bmy\s+(?:updates|notifications)\b',q))
    agent_preference=preference and bool(re.search(
        r"\b(?:the\s+)?(?:agent|assistant)['’]s\s+(?:stated\s+|reported\s+)?preference\b|"
        r"\b(?:the\s+)?(?:agent|assistant)\s+(?:prefer\w*|recommend\w*)\b",q))
    if agent_preference:
        # An explicitly attributed assistant recommendation is a historical
        # fact, not a private preference of the caller. The original role and
        # attribution still must have survived canonical validation.
        from agenthub.processing.durable_memory import VERSION
        if (memory.get('policy')!=VERSION or 'agent' not in memory.get('actors',[]) or
                memory.get('attribution') not in {'agent_reported','execution_result'}):return False
        actor=None
    if preference and not reported:
        if not agent_preference:
            actor=actor or (ctx.get('actor') if re.search(r'\b(?:i|my)\b',q) else None)
            owners=claim.get('_verified_source_owners',[])
            pref=claim.get('memory_context',{})
            private_owner=pref.get('preference_owner') or (owners[0] if len(owners)==1 else None)
            if not actor or not private_owner or actor.casefold()!=private_owner.casefold():return False
    if actor and not reported:
        attributed=_attributed(text,actor,claim)
        memory=claim.get('memory_context',{})
        # Canonical conversation actor 'user' resolves only through its actual
        # evidence owner; never through the author of a derived summary.
        owners=claim.get('_verified_source_owners',[])
        if memory.get('reason_actor')=='user' and owners==[actor]:attributed=text
        from agenthub.processing.durable_memory import VERSION
        if (memory.get('policy')==VERSION and memory.get('attribution')=='user_reported'
                and 'user' in memory.get('actors',[]) and owners==[actor]
                and (not preference or re.search(
                    r'\b(?:the\s+)?user\s+(?:prefer\w*|want\w*|chose|choose\w*|'
                    r'ask\w*\s+(?:to|for|that)|request\w*\s+(?:a|an|the|to|that)|'
                    r'(?:report\w*|stat\w*)\s+(?:a|an|the|their)\s+preference)\b',text,re.I))):
            # The canonical validator derives this role from the cited original
            # user event. Resolve it through current evidence ownership, rather
            # than requiring the private account ID to appear in curator prose.
            attributed=text
        if not attributed:return False
        text=attributed
    wanted=_word_set(query);found=_word_set(text)
    # Semantic scores provide candidate relevance for paraphrases; they never
    # override names, current policy, actor, requested facet or absence guards.
    if not wanted.intersection(found) and not (semantic_score is not None and semantic_score>=.55):return False
    head=re.match(r'^(?:What|Which)\s+(.+?)\s+and\s+(.+?)\s+(?:are|do|must|govern|should)\b',query,re.I)
    if head:
        for noun in head.groups():
            cues=[pattern for name,pattern in _FACET_CUES.items() if re.search(r'\b'+name+r'\b',noun,re.I)]
            if not (_word_set(noun)&found or any(re.search(pattern,text,re.I) for pattern in cues)):return False
    reason=bool(re.search(r'\b(?:why|reason|rationale)\b',q))
    if reason:
        memory=claim.get('memory_context')
        if memory is not None:
            from agenthub.processing.durable_memory import VERSION,FACETS
            if (not isinstance(memory,dict) or memory.get('policy')!=VERSION or
                not isinstance(memory.get('facets'),list) or set(memory['facets'])-set(FACETS) or
                not isinstance(memory.get('actors'),list) or memory.get('reason_actor') not in memory['actors'] or
                not isinstance(memory.get('reason_quote'),str) or not memory['reason_quote'].strip()):return False
            # The canonical compiler already checked the reason relation,
            # literal source span and speaker. A substantive excerpt need not
            # repeat the causal connective from its surrounding source sentence.
            reason_text=memory['reason_quote']
            compiled_reason=True
        else:reason_text=text;compiled_reason=False
        reasons=[c for c in _clauses(reason_text) if (compiled_reason or re.search(r'\b(?:because|so that|to avoid|reason|rationale|prevent\w*|ensur\w*)\b',c,re.I))
                 and not re.search(r'\b(?:reason|rationale)\b.{0,40}\b(?:not|never|unknown|missing|unrecorded)\b|\b(?:no|missing|unknown|unrecorded)\s+(?:reason|rationale)\b',c,re.I)]
        if not reasons:return False
    location=bool(re.match(r'\s*(?:regarding [^:]+:\s*)?where\b' if balanced else r'\s*where\b',q)
                  or re.search(r'\b(?:location|saved|stored|path)\b',q))
    if location:
        if not any(re.search(r'[/\\]|\b(?:directory|bucket|folder|saved|stored|at)\b',c,re.I) and
            not re.search(r'\b(?:not saved|not stored|unknown|provisional|unconfirmed)\b',c,re.I) for c in _clauses(text)):return False
    when_requested=bool(re.search(r'\bwhen\b',q)) if not balanced else bool(re.match(r'\s*(?:Regarding [^:]+:\s*)?when\b',query,re.I))
    if when_requested and not re.search(r'\b(?:after|before|until|\d{4}-\d{2}-\d{2}|\d+\s*(?:hours?|days?|minutes?))\b',text,re.I):
        if not (balanced and preference and re.search(r'\b(?:when|if|only|unless)\b',text,re.I)):return False
    if re.search(r'\b(?:approved|confirmed|verified)\b',q) and not re.match(r'\s*(?:is|was|are|were|did)\b',q):
        if not any(re.search(r'\b(?:approved|confirmed|verified|authoriz\w*)\b',c,re.I) and not _negative(c,'verification') for c in _clauses(text)):return False
    for name,pattern in _FACET_CUES.items():
        if re.search(r'\b'+name+r'\b',q) and not re.search(r'\b(?:'+pattern+r')\b',text,re.I):
            if not (balanced and name=='fields' and _balanced_family_supported('which fields?',text)):return False
    # Explicit scope qualifiers must be present as facts, not inferred from a
    # generic mechanism bearing the same project name.
    qualifiers=set(re.findall(r'\b(?:third.party|whole.region|withdrawn|prototype|checksum|registry|unrecorded|edition|grant)\b',q))
    if any(not re.search(re.escape(word).replace(r'\-',r'.'),text,re.I) for word in qualifiers):return False
    if re.search(r'\b(?:confirmed|approved)\b',q) and re.search(r'\b(?:unknown|never recorded|not recorded|unconfirmed|unapproved)\b',text,re.I):return False
    return True


def supported_answer(query,claim,*,ctx=None,policy='baseline',semantic_score=None):
    if policy=='baseline':return _supported_claim(query,claim)
    if policy not in {'facets_v1','facets_v2'}:raise ValueError('invalid_retrieval_selection_policy')
    return _facets_supported(query,claim,ctx or {},semantic_score,balanced=policy=='facets_v2')


_CURRENCY_UNITS={'$':'usd','usd':'usd','dollar':'usd','dollars':'usd',
                 '€':'eur','eur':'eur','euro':'eur','euros':'eur',
                 '£':'gbp','gbp':'gbp','pound':'gbp','pounds':'gbp',
                 'jpy':'jpy','yen':'jpy','cad':'cad','aud':'aud','chf':'chf'}
_MONEY_VALUE=re.compile(
    r'(?<!\w)(?:(?P<symbol>[$€£])\s*(?P<prefixed>\d[\d,.]*)|'
    r'(?P<amount>\d[\d,.]*)\s*(?P<unit>USD|EUR|GBP|JPY|CAD|AUD|CHF|dollars?|euros?|pounds?|yen)\b)',re.I)
_NUMBER_VALUE=re.compile(r'(?<!\w)(\d[\d,.]*)\s*(kpa|psi|percent|%|requests?)\b|(?<!\w)(\d[\d,.]*)\s*%(?!\w)',re.I)
_PATH_VALUE=re.compile(r'(?<!\w)(?:/[\w./%+@-]+|[A-Za-z]:\\[^\s,;]+)')
_DATE_VALUE=re.compile(r'\b\d{4}-\d{2}-\d{2}\b')
_MONTH_DATE_VALUE=re.compile(r'\b(January|February|March|April|May|June|July|August|September|October|November|December)\s+(\d{1,2}),?\s+(\d{4})\b',re.I)
_VERSION_VALUE=re.compile(r'\b(?:version|release)\s+(?:is\s+)?(v?\d+(?:\.\d+)*)(?!\w)',re.I)
_OLD_VALUE=re.compile(r'(?<![/\\])\b(?:previous|prior|old|superseded|obsolete|replaced|proposed|estimated|tentative|provisional|unconfirmed|unapproved)\b(?![./\\])',re.I)


def _single_value_kind(query):
    if re.search(r'\b(?:previous|prior|old|superseded|as of)\b',query,re.I):return None
    if re.search(r'\b(?:currency|currencies)\b',query,re.I):return 'currency'
    if re.search(r'\b(?:where|path|location|saved|stored)\b',query,re.I):return 'path'
    if re.search(r'\b(?:date|when)\b',query,re.I):return 'date'
    if re.search(r'\b(?:who owns|owner|owned by|responsible)\b',query,re.I):return 'owner'
    if re.search(r'\bversion\b',query,re.I):return 'version'
    if re.search(r'\b(?:budget|spend(?:ing)?|cost|price|purchase amount)\b',query,re.I):return 'money'
    if re.search(r'\b(?:pressure|threshold|concentration|quota|limit)\b',query,re.I):return 'quantity'
    return None


def _canonical_number(value):
    try:return format(Decimal(value.replace(',','')).normalize(),'f')
    except InvalidOperation:return None


def _typed_values(clause,kind):
    if kind in {'money','currency'}:
        values=set()
        for match in _MONEY_VALUE.finditer(clause):
            unit=_CURRENCY_UNITS[(match['symbol'] or match['unit']).casefold()]
            number=_canonical_number(match['prefixed'] or match['amount'])
            if number is not None:values.add(unit if kind=='currency' else (number,unit))
        return values
    if kind=='quantity':
        values=set()
        for match in _NUMBER_VALUE.finditer(clause):
            number=_canonical_number(match[1] or match[3]);unit=(match[2] or '%').casefold()
            if number is not None:values.add((number,'percent' if unit=='%' else unit.rstrip('s')))
        return values
    if kind=='path':return {match.group().rstrip('.') for match in _PATH_VALUE.finditer(clause)}
    if kind=='date':
        values=set(_DATE_VALUE.findall(clause))
        for match in _MONTH_DATE_VALUE.finditer(clause):
            month=datetime.strptime(match[1][:3].title(),'%b').month
            values.add(f'{int(match[3]):04d}-{month:02d}-{int(match[2]):02d}')
        return values
    if kind=='version':return {value.casefold().removeprefix('v') for value in _VERSION_VALUE.findall(clause)}
    if kind=='owner':
        values=set()
        name=r'([A-Z][A-Za-z_-]+(?:\s+[A-Z][A-Za-z_-]+){0,2})'
        for pattern in (r'\b'+name+r'\s+owns\b',
                        r'\b(?:owned by|owner is|assigned to|responsible owner is)\s+'+name+r'\b'):
            values.update(name.casefold() for name in re.findall(pattern,clause))
        return values
    return set()


def _current_typed_values(query,claim,kind):
    text=_prose(claim.get('lesson',''))
    clauses=[part.strip() for clause in _clauses(text) for part in clause.split(';') if part.strip()]
    subjects=_named_subjects(query)
    if subjects and any(all(_contains(clause,subject) for subject in subjects) for clause in clauses):
        clauses=[clause for clause in clauses if all(_contains(clause,subject) for subject in subjects)]
    cues={'money':('budget','spend\\w*','cost','price','purchase','amount'),
          'currency':('budget','spend\\w*','cost','price','purchase','amount','currency'),
          'quantity':('pressure','threshold','concentration','quota','limit'),
          'path':('path','location','saved','stored','report'),
          'date':('date','launch','release','scheduled'),
          'owner':('owner','owns','owned','assigned','responsible'),
          'version':('version','release')}[kind]
    requested=[] if kind=='owner' else [cue for cue in cues if re.search(r'\b(?:'+cue+r')\b',query,re.I)]
    cue='|'.join(requested or cues)
    focus=_word_set(query)-_word_set(' '.join(subjects))-_word_set(' '.join(cues))
    values=set();old_values=False;focus_match=not focus
    for clause in clauses:
        if not re.search(r'\b(?:'+cue+r')\b',clause,re.I):continue
        found=_typed_values(clause,kind)
        if not found:continue
        if _OLD_VALUE.search(clause):old_values=True
        else:
            values.update(found)
            if focus<=_word_set(clause):focus_match=True
    return values,old_values and not values,focus_match


@timed('selection')
def select_supported_cards(query,candidates,ctx,*,policy='facets_v1'):
    """Minimal complementary current evidence; caller retains policy rechecks."""
    facets=(_balanced_facet_queries if policy=='facets_v2' else _facet_queries)(query);eligible=[]
    for candidate in candidates:
        claim=candidate.get('claim') or json.loads(candidate['claim_json'])
        covered={i for i,part in enumerate(facets) if supported_answer(part,claim,
            ctx=ctx,policy=policy,semantic_score=candidate.get('cosine'))}
        if covered:eligible.append((candidate,claim,covered))
    # A single current fact cannot be settled by rank when its authorized
    # answer-bearing evidence disagrees. Compare the requested type only;
    # unrelated quantities and explicitly previous values are not alternatives.
    kind=_single_value_kind(query) if len(facets)==1 else None
    if kind:
        current=[]
        for candidate,claim,covered in eligible:
            found,old_only,focus_match=_current_typed_values(query,claim,kind)
            if old_only:continue
            current.append((candidate,claim,covered,found,focus_match))
        if any(found and matched for _,_,_,found,matched in current):
            current=[row for row in current if row[4]]
        values=set().union(*(row[3] for row in current)) if current else set()
        if len(values)>1:return [],False
        eligible=[row[:3] for row in current]
    selected=[];covered=set();seen=set()
    while eligible and len(selected)<8:
        if policy=='facets_v2':
            # Candidate fusion already combines lexical precision and semantic
            # recall. Selecting by cosine alone silently discarded that work.
            eligible.sort(key=lambda item:(-len(item[2]-covered),-item[0].get('rrf',0),
                -item[0].get('cosine',0),
                -len(_word_set(query)&_word_set(item[1]['lesson'])),item[0]['document_id']))
        else:
            eligible.sort(key=lambda item:(-len(item[2]-covered),-item[0].get('cosine',0),
                -len(_word_set(query)&_word_set(item[1]['lesson'])),-item[0].get('rrf',0),item[0]['document_id']))
        candidate,claim,adds=eligible.pop(0)
        if not adds-covered:continue
        body=_prose(claim['lesson']);key=' '.join(body.casefold().split())
        if key in seen:continue
        card={'id':candidate['document_id'],'revision':candidate['revision_id'],'title':claim.get('title',''),
            'lesson':claim['lesson'],'evidence_status':claim.get('evidence_status','source_linked_unverified')}
        # The evidence body remains exact. Shorten display metadata before
        # discarding an otherwise complete claim just over the wire ceiling.
        if len(json.dumps(card,ensure_ascii=True))>1300:
            title=card['title']
            while len(title)>1 and len(json.dumps(card,ensure_ascii=True))>1300:
                title=title[:-1];card['title']=title+'...'
        if len(json.dumps(card,ensure_ascii=True))>1300:continue
        selected.append(card);seen.add(key);covered|=adds
    return selected,len(covered)==len(facets)

def rank_scoped_candidates(db,query,*,prefix='',scope_join='',ranked_where='TRUE',
                           prefix_args=(),ranked_args=(),filter_join='',limit=20,
                           lexical=True,vector=True,embedder=None,model_key=MODEL_KEY,
                           lexical_weight=1,vector_weight=1,indexed_lexical=False):
    """Shared PostgreSQL scoring/fusion after caller-established authorization.

    Production supplies its compiled policy scope. Historical evaluation may
    supply connection-local temporary tables only after canonical batch source
    validation. This function grants no permissions and does not cache them.
    """
    match=' OR '.join('"'+w.replace('"','""')+'"' for w in lexical_terms(query))
    # Only the service's restricted transaction uses the fixed database entry
    # point. Disposable evaluation relations retain their caller-established scope.
    text_relation='search_lexical_matches(?)' if indexed_lexical else 'knowledge_fts'
    text_score='f.score' if indexed_lexical else "ts_rank_cd(to_tsvector('simple',f.body),websearch_to_tsquery('simple',?))"
    text_match='TRUE' if indexed_lexical else "to_tsvector('simple',f.body) @@ websearch_to_tsquery('simple',?)"
    text_args=(*prefix_args,match,*ranked_args,*(() if indexed_lexical else (match,)),limit)
    with span('lexical_sql'):
        lexical=db.execute(prefix+'''SELECT ranked.document_id,ranked.revision_id,r.claim_json,ranked.score
            FROM (SELECT d.document_id,d.active_revision_id revision_id,'''+text_score+''' score
            FROM knowledge_documents d
            JOIN '''+text_relation+''' f ON f.document_id=d.document_id AND f.revision_id=d.active_revision_id
            JOIN enterprise_documents ed ON ed.id=d.document_id
            '''+filter_join+scope_join+' WHERE '+ranked_where+' AND '+text_match+'''
            ORDER BY score DESC,d.document_id LIMIT ?) ranked
            JOIN knowledge_revisions r ON r.revision_id=ranked.revision_id
            ORDER BY ranked.score DESC,ranked.document_id''',text_args).fetchall() if match and lexical else []
    semantic=[]
    if vector and embedder is not None:
        active=db.execute('SELECT generation_id,model_key FROM cloud_vector_state WHERE singleton=1').fetchone()
        if active and active['model_key']==model_key:
            with span('embedding'):
                values=embedder.embed_queries([query]);query_vector=_vector(values[0])
            with span('vector_sql'):
                # Iterative scans compensate for generation, revision and RLS
                # filtering inside PostgreSQL. Never fetch denied rows to Python.
                db.execute("SELECT set_config('hnsw.iterative_scan','strict_order',true), "
                    "set_config('hnsw.ef_search','100',true), "
                    "set_config('hnsw.max_scan_tuples','20000',true)")
                # Keep the vector scan as the ordered relation. The correlated
                # scope check must not flatten into a document-first join that
                # forces a corpus sort and prevents the HNSW access path.
                # Fence the single document-key lookup before applying candidate
            # arrays. Otherwise PostgreSQL can combine ANY(thousands of IDs)
            # into the correlated index condition and probe the array per vector.
            semantic=db.execute(prefix+'''SELECT ranked.document_id,ranked.revision_id,r.claim_json,ranked.score
                    FROM (SELECT v.document_id,v.revision_id,
                    1-(v.embedding <=> ?::vector) score FROM cloud_document_vectors v
                    WHERE EXISTS(SELECT 1 FROM (
                    SELECT * FROM knowledge_documents WHERE document_id=v.document_id OFFSET 0) d
                    JOIN enterprise_documents ed ON ed.id=d.document_id '''+filter_join+scope_join+' WHERE d.document_id=v.document_id AND d.active_revision_id=v.revision_id AND '+ranked_where+'''
                    OFFSET 0) AND v.generation_id=? ORDER BY v.embedding <=> ?::vector LIMIT ?) ranked
                    JOIN knowledge_revisions r ON r.revision_id=ranked.revision_id
                    ORDER BY ranked.score DESC,ranked.document_id''',
                    (*prefix_args,query_vector,*ranked_args,active['generation_id'],query_vector,limit)).fetchall()
    fused={}
    for name,rows in [('lexical',lexical),('vector',semantic)]:
        for position,row in enumerate(rows):
            ident=row['document_id'];item=fused.setdefault(ident,dict(row,rrf=0,channels=[]))
            weight=lexical_weight if name=='lexical' else vector_weight
            item['rrf']+=weight/(60+position+1);item['channels'].append(name)
            if name=='vector':item['cosine']=row['score']
    return sorted(fused.values(),key=lambda x:(-x['rrf'],x['document_id']))[:limit]

class HybridRetrievalMixin:
    # Direct algorithm callers may inspect retained derived records. The service
    # runtime explicitly defaults to source discovery.
    retrieval_corpus='all'
    semantic_model_key=MODEL_KEY
    hybrid_enabled=False
    semantic_embedder=None
    answerability_judge=None
    # Internal, bounded experiment controls. Defaults preserve installed B17.
    retrieval_candidate_limit=20
    retrieval_numeric_mode='legacy'
    retrieval_lexical_weight=1
    retrieval_vector_weight=1
    retrieval_require_prose=False
    retrieval_lexical_enabled=True
    retrieval_authorization_shape='compiled'
    retrieval_selection_policy='baseline'

    def reindex_vectors(self,*,embedder=None,max_documents=10000):
        model=embedder or self.semantic_embedder
        if model is None:raise ValueError('local_embedding_model_required')
        if not 1<=max_documents<=100000:raise ValueError('embedding_bound')
        generation='vectors-'+uuid.uuid4().hex
        from agenthub.draft_review import selected_documents
        review=selected_documents(self)
        with self.open() as state,state.db:
            rows=state.db.execute('''SELECT d.document_id,d.active_revision_id,r.claim_json
                FROM knowledge_documents d JOIN knowledge_revisions r ON r.revision_id=d.active_revision_id
                JOIN enterprise_documents e ON e.id=d.document_id WHERE d.lifecycle='active' AND e.active=1
                AND e.tenant=?'''+(' AND d.document_id=ANY(?::text[])' if review is not None else '')+' ORDER BY d.document_id LIMIT ?',
                (self.tenant_id,*([review] if review is not None else []),max_documents+1)).fetchall()
            if len(rows)>max_documents:raise ValueError('embedding_document_bound')
            state.db.execute('INSERT INTO cloud_vector_generations(id,model_key,dimension,status,created) VALUES(?,?,?,\'building\',?)',
                (generation,self.semantic_model_key,DIMENSION,time.time()))
        try:
            for first in range(0,len(rows),16):
                batch=rows[first:first+16];texts=[self._vector_text(json.loads(r['claim_json'])) for r in batch]
                vectors=model.embed_documents(texts)
                if len(vectors)!=len(batch):raise ValueError('embedding_batch_shape')
                with self.open() as state,state.db:
                    for row,vector,text in zip(batch,vectors,texts):
                        current=state.db.execute('SELECT active_revision_id,lifecycle FROM knowledge_documents WHERE document_id=?',(row['document_id'],)).fetchone()
                        if not current or current['lifecycle']!='active' or current['active_revision_id']!=row['active_revision_id']:
                            raise ValueError('embedding_source_changed')
                        state.db.execute('INSERT INTO cloud_document_vectors VALUES(?,?,?,?,?::vector)',
                            (generation,row['document_id'],row['active_revision_id'],hashlib.sha256(text.encode()).hexdigest(),_vector(vector)))
            with self.delivery_lock(),self.open() as state,state.db:
                stale=state.db.execute('''SELECT 1 FROM cloud_document_vectors v JOIN knowledge_documents d ON d.document_id=v.document_id
                    WHERE v.generation_id=? AND (d.lifecycle!='active' OR d.active_revision_id!=v.revision_id) LIMIT 1''',(generation,)).fetchone()
                if stale:raise ValueError('embedding_activation_source_changed')
                state.db.execute("UPDATE cloud_vector_generations SET status='retired' WHERE status='active'")
                state.db.execute("UPDATE cloud_vector_generations SET status='active',completed=?,document_count=? WHERE id=?",(time.time(),len(rows),generation))
                state.db.execute('''INSERT INTO cloud_vector_state VALUES(1,?,?,?) ON CONFLICT(singleton)
                    DO UPDATE SET generation_id=excluded.generation_id,model_key=excluded.model_key,updated=excluded.updated''',(generation,self.semantic_model_key,time.time()))
            from agenthub.cloud_ops import Meter
            Meter(self).metric('embedding:'+generation,'embedding_documents',len(rows),details={'generation':generation})
            return {'generation':generation,'model_key':self.semantic_model_key,'dimension':DIMENSION,'documents':len(rows),'search':'pgvector_hnsw_eligible'}
        except BaseException:
            with self.open() as state,state.db:state.db.execute("UPDATE cloud_vector_generations SET status='failed' WHERE id=?",(generation,))
            raise
    @staticmethod
    def _vector_text(claim):return '\n'.join(str(claim.get(k,'')) for k in ('title','lesson','applicability'))

    def _document_allowed(self,db,ctx,document_id):
        # Resolve current identity once per document, rather than opening the
        # control database for every dependency. Reuse the same scoped SQL
        # predicate used before ranking; never cache authorization across requests.
        if db.execute('SELECT 1 FROM cloud_preferences WHERE document_id=? LIMIT 1',(document_id,)).fetchone():
            return super()._document_allowed(db,ctx,document_id)
        from types import SimpleNamespace
        try:where,args=self._authorization_predicate(SimpleNamespace(db=db),ctx,candidate_ids=[document_id])
        except Denied:return None
        row=db.execute('''SELECT ed.*,d.lifecycle,d.active_revision_id FROM enterprise_documents ed
            JOIN knowledge_documents d ON d.document_id=ed.id WHERE d.document_id=? AND '''+where,
            (document_id,*args)).fetchone()
        if row:return row
        # Explicit releases have stricter source/version semantics, preserved
        # by their existing oracle. User preferences keep their separate scope.
        if db.execute('SELECT 1 FROM backend_releases WHERE document_id=? LIMIT 1',(document_id,)).fetchone():
            return super()._document_allowed(db,ctx,document_id)
        return None

    def _authorization_predicate(self, state, ctx, project=None, *, candidate_ids=None):
        self.current_identity(state.db, ctx)
        self._need(ctx, 'read')
        scopes = self._readable_scopes(state.db, ctx, project)
        sql = "ed.tenant=? AND ed.active=1 AND d.lifecycle='active'\n            AND (ed.internal_project=ANY(?::text[]) OR EXISTS(SELECT 1 FROM cloud_preferences pf\n                WHERE pf.document_id=d.document_id AND pf.tenant=? AND pf.owner=? AND pf.valid_until IS NULL\n                AND pf.revision_id=d.active_revision_id AND (pf.scope='user' OR (pf.scope='project' AND pf.project=?))))\n            AND (NOT EXISTS(SELECT 1 FROM knowledge_generation_documents gd WHERE gd.document_id=d.document_id)\n                OR EXISTS(SELECT 1 FROM knowledge_generation_documents gd JOIN knowledge_generations g\n                    ON g.generation_id=gd.generation_id WHERE gd.document_id=d.document_id AND g.status='active'))\n            AND NOT EXISTS(SELECT 1 FROM cloud_preference_candidates pc WHERE pc.document_id=d.document_id AND pc.status IN ('pending','held'))\n            AND NOT EXISTS(SELECT 1 FROM cloud_preferences pf WHERE pf.document_id=d.document_id\n                AND (pf.owner!=? OR pf.tenant!=?))\n            AND (NOT EXISTS(SELECT 1 FROM cloud_preferences pf WHERE pf.document_id=d.document_id)\n                OR EXISTS(SELECT 1 FROM cloud_preferences pf WHERE pf.document_id=d.document_id\n                    AND pf.valid_until IS NULL AND pf.revision_id=d.active_revision_id))\n            "
        from agenthub.search_permissions import predicate
        compact, policy_args = predicate(ctx)
        sql += compact
        args = (ctx['tenant'], scopes, ctx['tenant'], ctx['actor'], project, ctx['actor'], ctx['tenant'], *policy_args)
        from agenthub.draft_review import selected_documents
        review = selected_documents(self, ctx)
        if review is not None:
            sql = sql.replace("g.status='active'))", "g.status='active') OR d.document_id=ANY(?::text[]))")
            args = (*args[:5], review, *args[5:], review)
            sql += ' AND d.document_id=ANY(?::text[])'
        return (sql, args)

    def _authorized_documents(self,state,ctx,project=None):
        where,args=self._authorization_predicate(state,ctx,project)
        rows=state.db.execute('SELECT ed.id FROM enterprise_documents ed JOIN knowledge_documents d ON d.document_id=ed.id WHERE '+where,args)
        allowed={r[0] for r in rows}
        # Explicit reviewed releases are scarce and bind exact source versions;
        # use their existing canonical rule rather than approximate its semantics.
        releases=state.db.execute('SELECT document_id FROM backend_releases WHERE tenant=?',(ctx['tenant'],))
        for row in releases:
            if self._document_allowed(state.db,ctx,row[0]):allowed.add(row[0])
        return sorted(allowed)

    @timed('candidates')
    def candidates(self, ctx, query, *, project=None, filters=None, limit=10, vector=True, lexical=True):
        self._need(ctx, 'read')
        filters = filters or {}
        if not 1 <= limit <= 100:
            raise ValueError('candidate_limit')
        with self.open() as state:
            exact_ids = [r[0] for r in state.db.execute("SELECT d.document_id FROM knowledge_revisions r\n                JOIN knowledge_documents d ON d.active_revision_id=r.revision_id\n                WHERE lower(r.claim_json::jsonb->>'title')=lower(?)\n                UNION SELECT document_id FROM knowledge_documents WHERE document_id=?", (query, query))]
            numeric_ids = re.findall('(?<![\\w.])\\d{6,}(?![\\w.])', query)
            if self.retrieval_numeric_mode == 'typed':
                numeric_ids = re.findall('\\b(?:record|document|doc|source|issue|ticket|case|dataset\\s+id)\\s+(?:id\\s*)?[:#-]?\\s*(\\d{6,})\\b', query, re.I)
            entity_ids = None
            if numeric_ids and (not exact_ids):
                entity_query = ' & '.join(numeric_ids)
                entity_ids = [r[0] for r in state.db.execute("SELECT f.document_id FROM knowledge_fts f\n                    JOIN knowledge_documents d ON d.document_id=f.document_id AND d.active_revision_id=f.revision_id\n                    WHERE to_tsvector('simple',f.body) @@ to_tsquery('simple',?)", (entity_query,))]
                if not entity_ids:
                    self.current_identity(state.db, ctx)
                    return []
            from agenthub.search_reader import restrict
            reader = restrict(self, state, ctx, project)
            with reader:
                predicates = ['TRUE', "d.lifecycle='active'"]
                args = []
                if self.retrieval_corpus=='sources':
                    predicates.append("ed.representation IN ('raw','source')")
                    cutoff=filters.get('source_as_of')
                    if cutoff:
                        from agenthub.conversation_context import stamp
                        point=stamp(cutoff)+(86400 if re.fullmatch(r'\d{4}-\d{2}-\d{2}',cutoff) else 0)
                        predicates.append('ed.source_time IS NOT NULL AND ed.source_time<?')
                        args.append(point)
                        if filters.get('event_day'):
                            predicates.append('ed.source_time>=?');args.append(point-86400)
                        if filters.get('time_mode')=='known_at':
                            predicates.append('ed.source_captured<?');args.append(point)
                if entity_ids is not None:
                    predicates.append('d.document_id=ANY(?::text[])')
                    args.append(entity_ids)
                for key in ('domain', 'knowledge_type'):
                    if filters.get(key):
                        predicates.append("r.claim_json::jsonb->>'" + key + "'=?")
                        args.append(filters[key])
                if filters.get('subject'):
                    predicates.append("EXISTS(SELECT 1 FROM jsonb_array_elements_text(COALESCE(r.claim_json::jsonb->'subjects','[]'::jsonb)) s WHERE lower(s.value)=lower(?))")
                    args.append(filters['subject'])
                where = ' AND '.join(predicates)

                prefix, scope_join, ranked_where, prefix_args, ranked_args = '', '', where, (), args
                exact = state.db.execute(prefix + 'SELECT d.document_id,r.revision_id,r.claim_json FROM knowledge_documents d\n                    JOIN knowledge_revisions r ON r.revision_id=d.active_revision_id\n                    JOIN enterprise_documents ed ON ed.id=d.document_id ' + scope_join + ' WHERE ' + ranked_where + '\n                    AND d.document_id=ANY(?::text[])\n                    ORDER BY d.document_id LIMIT ?', (*prefix_args, *ranked_args, exact_ids, limit)).fetchall() if exact_ids else []
                if exact:
                    return [dict(row, rrf=1, channels=['exact']) for row in exact]
                filter_join = ' JOIN knowledge_revisions r ON r.revision_id=d.active_revision_id ' if any((filters.get(k) for k in ('domain', 'knowledge_type', 'subject'))) else ''
                return rank_scoped_candidates(state.db, query, prefix=prefix, scope_join=scope_join, ranked_where=ranked_where, prefix_args=prefix_args, ranked_args=ranked_args, filter_join=filter_join, limit=limit, lexical=lexical, vector=vector, embedder=self.semantic_embedder, model_key=self.semantic_model_key, lexical_weight=self.retrieval_lexical_weight, vector_weight=self.retrieval_vector_weight,indexed_lexical=True)

    def _compound_references(self,ctx,location_query,reason_query,location):
        """Resolve only explicit single-actor, source-backed compound references."""
        pronoun=re.search(r'\bwhy\s+(?:did|do|does|would|should)\s+(he|she|they)\s+',reason_query,re.I)
        anaphor=re.search(r'\b(?:this|that|the same)\s+(?:format|option)\b',reason_query,re.I)
        if not pronoun and not anaphor:return location,reason_query
        named=re.search(r'(?i:\bwhere\s+(?:did|do|does|has|have)\s+)([A-Z][A-Za-z_-]*(?:\s+[A-Z][A-Za-z_-]*){0,2})(?i:\s+(?:save|store|put|move)\b)',location_query)
        if not named:return location,None
        actor=named[1];requested=_actor(reason_query)
        if requested and not pronoun and requested.casefold()!=actor.casefold():return location,None
        options={};cards=[];identities=_IDENTIFIER.findall(location_query)+_NUMBERED.findall(location_query)
        with self.open() as state:
            for card in location['results']:
                row=state.db.execute('SELECT d.active_revision_id,d.lifecycle,r.claim_json FROM knowledge_documents d JOIN knowledge_revisions r ON r.revision_id=d.active_revision_id WHERE d.document_id=?',(card['id'],)).fetchone()
                if (not row or row['lifecycle']!='active' or row['active_revision_id']!=card['revision'] or
                        not self._document_allowed(state.db,ctx,card['id'])):continue
                claim=json.loads(row['claim_json']);clauses=_actor_clauses(_clauses(claim.get('lesson','')),actor)
                # A reason by Alice cannot borrow a path saved only by Bob.
                if not any(re.search(r'[/\\]|\b(?:location|directory|bucket|folder|saved|stored)\b',c,re.I) and
                           not re.search(r'\b(?:failed|not saved|not stored|unknown)\b',c,re.I) for c in clauses):continue
                cards.append(card)
                for clause in clauses:
                    numbered=_NUMBERED.findall(clause)+_IDENTIFIER.findall(clause)
                    if numbered and identities and not all(_contains(clause,i) for i in identities):continue
                    choice=re.search(r'\b(?:'+re.escape(actor)+r'|he|she|they)\s+(?:chose|choose|uses|used|prefers|preferred|selected|picked)\s+(.+?)\s+(?:because|so that|to avoid)\b',clause,re.I)
                    if not choice:continue
                    option=re.split(r'\s+(?:for|on)\b',choice[1],maxsplit=1,flags=re.I)[0].strip()
                    if (not option or len(option)>120 or re.search(r'\b(?:no|none|neither|not|never|rejected|declined|avoided)\b',option,re.I)):continue
                    options[option.casefold()]=option
        location=dict(location,results=cards,answerable=location['answerable'] and bool(cards))
        if not location['answerable']:return location,None
        if anaphor:
            if len(options)!=1:return location,None
            reason_query=reason_query[:anaphor.start()]+next(iter(options.values()))+reason_query[anaphor.end():]
        if pronoun:
            reason_query=reason_query[:pronoun.start(1)]+actor+reason_query[pronoun.end(1):]
        return location,reason_query

    @timed('search')
    def search(self,ctx,value,*,candidate_pool=False):
        # Validate advertised additions explicitly while retaining the existing API envelope.
        if self.retrieval_selection_policy not in {'baseline','facets_v1','facets_v2'}:raise ValueError('invalid_retrieval_selection_policy')
        value=dict(value);filters={key:value.pop(key) for key in FILTERS if key in value}
        for key,item in filters.items():
            if not isinstance(item,str) or not item or len(item)>120:raise ValueError('invalid_search_filter')
        if filters.get('event_day'):
            if value.get('as_of'):raise ValueError('ambiguous_temporal_search')
            value['as_of']=filters['event_day']
        from agentclient.enterprise_contract import validate_search
        validate_search(value)
        if self.retrieval_corpus=='sources':
            from agenthub.source_index import search
            return search(self,ctx,value,filters,candidate_pool=candidate_pool)
        parts=re.split(r',?\s+and\s+(?=why\b)',value['query'],maxsplit=1,flags=re.I)
        if not candidate_pool and self.retrieval_selection_policy=='baseline' and len(parts)==2 and re.search(r'\b(?:where|location|saved|stored)\b',parts[0],re.I):
            subjects=_named_subjects(parts[0])
            subjects+=re.findall(r'\b[A-Z][A-Za-z_]+(?: [A-Z][A-Za-z_]+){0,3} \d+\b',parts[0])
            subjects+=re.findall(r'\b[A-Za-z][A-Za-z_-]*[-_]\d+\b',parts[0])
            # Later facets inherit the explicitly named subject of the question.
            queries=[parts[0],('; '.join(subjects)+': ' if subjects else '')+parts[1]]
            child_filters={key:item for key,item in filters.items() if key!='event_day'}
            location=self.search(ctx,{**value,**child_filters,'query':queries[0]})
            location,reason=self._compound_references(ctx,parts[0],parts[1],location)
            reason_query=('; '.join(subjects)+': ' if subjects else '')+reason if reason else None
            selected=[location,self.search(ctx,{**value,**child_filters,'query':reason_query}) if reason_query else
                      {'results':[],'answerable':False}]
            cards={card['id']:card for result in selected for card in result['results']}
            output={'results':list(cards.values()),'answerable':all(r['answerable'] for r in selected)}
            bound=1500 if value.get('mode')=='automatic' else 4000
            while output['results'] and len(json.dumps(output,ensure_ascii=True))>bound:
                output['results'].pop();output['answerable']=False
            if not output['answerable']:output['coverage_gaps']=['one_or_more_requested_facets_unsupported']
            return output
        candidates=None
        temporal_requested=bool(value.get('as_of') or value.get('time_mode') or
            re.search(r'\bas\s+of\b|\b\d{4}-\d{2}-\d{2}\b',value['query'],re.I))
        if not temporal_requested and re.search(r'\b(?:now|current|currently|latest)\b',value['query'],re.I):
            # Current-state prose remains searchable, but typed validity must not
            # be bypassed by returning an expired assertion's parent document.
            candidates=self.candidates(ctx,value['query'],project=value.get('project'),filters=filters,
                limit=self.retrieval_candidate_limit,vector=self.hybrid_enabled,lexical=self.retrieval_lexical_enabled)
            with self.open() as state:
                temporal_requested=bool(state.db.execute(
                    'SELECT 1 FROM knowledge_temporal_assertions WHERE document_id=ANY(?::text[]) LIMIT 1',
                    ([c['document_id'] for c in candidates],)).fetchone())
        if temporal_requested:
            with self.open() as state:
                scopes=self._readable_scopes(state.db,ctx,value.get('project'))
                temporal=self.temporal_search(state,ctx,value,scopes,
                    candidate_ids=[c['document_id'] for c in candidates] if candidates is not None else None)
                if temporal is not None:
                    if value.get('mode')=='automatic' and value.get('session'):
                        from agenthub.processing.context_delivery import current_epoch
                        receiver=self._receiver(ctx,value['session']);context=current_epoch(state.db,receiver)
                        since=0 if context['continuity_status']=='known' else time.time()-300
                        receipts=state.db.execute("""SELECT document_id,revision FROM enterprise_receipts
                            WHERE session=? AND epoch=? AND status='offered' AND created>=?
                            AND document_id=ANY(?::text[])""",(receiver,str(context['epoch']),since,
                            [card['id'] for card in temporal['results']])).fetchall()
                        seen={(r['document_id'],r['revision']) for r in receipts}
                        temporal['results']=[c for c in temporal['results'] if (c['id'],c['revision']) not in seen]
                        temporal['answerable']=bool(temporal['results'])
                    if filters:
                        temporal['results']=[card for card in temporal['results'] if self._filter_card(state,card,filters)]
                        temporal['answerable']=temporal['answerable'] and bool(temporal['results'])
                    return temporal
        if candidates is None:
            candidates=self.candidates(ctx,value['query'],project=value.get('project'),filters=filters,limit=self.retrieval_candidate_limit,vector=self.hybrid_enabled,lexical=self.retrieval_lexical_enabled)
        if candidate_pool:
            # Internal serving stage: permissions already scoped candidate SQL.
            # No heuristic relevance or wire-size cuts before model selection.
            from agenthub.processing.knowledge import compatibility
            cards=[{'id':row['document_id'],'revision':row['revision_id'],
                'title':claim.get('title',''),'lesson':claim.get('lesson',''),
                'evidence_status':claim.get('evidence_status','source_linked_unverified')}
                for row in candidates for claim in [json.loads(row['claim_json'])]
                if compatibility(claim,value['query'],value.get('project',row.get('project','')))[0]!='incompatible']
            with self.open() as state:
                if value.get('mode')=='automatic' and value.get('session'):
                    from agenthub.processing.context_delivery import current_epoch
                    receiver=self._receiver(ctx,value['session']);epoch=str(current_epoch(state.db,receiver)['epoch'])
                    seen={(r['document_id'],r['revision']) for r in state.db.execute(
                        "SELECT document_id,revision FROM enterprise_receipts WHERE session=? AND epoch=? AND status='offered' AND document_id=ANY(?::text[])",
                        (receiver,epoch,[c['id'] for c in cards])).fetchall()}
                    cards=[c for c in cards if (c['id'],c['revision']) not in seen]
                partial=state.db.execute("""SELECT 1 FROM curation_episode_jobs e
                    WHERE e.id IN (SELECT DISTINCT job_id FROM episode_candidates
                        WHERE document_id=ANY(?::text[]) AND status='applied')
                    AND e.progress::jsonb#>>'{coverage,source_capture,completion}'='partial' LIMIT 1""",
                    ([c['id'] for c in cards],)).fetchone() if cards else None
            result={'results':cards,'answerable':False,'_candidate_pool':True}
            if partial:result['coverage_gaps']=['incomplete_source_capture']
            return result
        from agenthub.processing.knowledge import compatibility
        offered=[];selection_rows=[];complete=False;capture_gaps=[]
        with self.open() as state:
            allowed=[candidate['document_id'] for candidate in candidates]
            from agenthub.processing.durable_memory import retrieve as durable_retrieve
            scopes=self._readable_scopes(state.db,ctx,value.get('project'))
            durable=durable_retrieve(state,value['query'],scopes[0],read_projects=scopes,authorized_document_ids=allowed) if scopes and self.retrieval_selection_policy=='baseline' else None
            durable_ids={row['document_id'] for row in durable} if durable is not None else None
            owners={}
            if allowed and self.retrieval_selection_policy in {'facets_v1','facets_v2'}:
                owner_rows=state.db.execute('''SELECT DISTINCT x.document_id,p.definition->>'owner' AS owner
                    FROM search_document_policies x JOIN search_policies p ON p.id=x.policy_id
                    WHERE x.document_id IN ('''+','.join('?' for _ in allowed)+')',allowed).fetchall()
                for row in owner_rows:owners.setdefault(row['document_id'],set()).add(row['owner'])
            receiver=None;epoch=None
            if value.get('mode')=='automatic' and value.get('session'):
                from agenthub.processing.context_delivery import current_epoch
                receiver=self._receiver(ctx,value['session']);epoch=str(current_epoch(state.db,receiver)['epoch'])
            for candidate in candidates:
                claim=json.loads(candidate['claim_json'])
                if self.retrieval_require_prose and not any(
                        line.strip() and not re.match(r'^\s*#{1,6}\s',line)
                        for line in claim.get('lesson','').splitlines()):continue
                if compatibility(claim,value['query'],scopes[0])[0]=='incompatible':continue
                if 'memory_context' in claim and durable_ids is not None and candidate['document_id'] not in durable_ids:continue
                if self.retrieval_selection_policy=='baseline' and not supported_answer(value['query'],claim):continue
                if receiver and state.db.execute("SELECT 1 FROM enterprise_receipts WHERE session=? AND epoch=? AND document_id=? AND revision=? AND status='offered' LIMIT 1",(receiver,epoch,candidate['document_id'],candidate['revision_id'])).fetchone():continue
                if self.retrieval_selection_policy in {'facets_v1','facets_v2'}:
                    # Ranking already applied the complete current source policy.
                    # Selection uses those claims internally; recheck each chosen
                    # card below at delivery, rather than repeating policy/control
                    # roundtrips for every unselected candidate as well.
                    claim=dict(claim,_verified_source_owners=sorted(owners.get(candidate['document_id'],set())))
                    selection_rows.append(dict(candidate,claim=claim));continue
                if not self._document_allowed(state.db,ctx,candidate['document_id']):continue
                card={'id':candidate['document_id'],'revision':candidate['revision_id'],'title':claim.get('title',''),
                    'lesson':claim.get('lesson',''),'evidence_status':claim.get('evidence_status','source_linked_unverified')}
                if len(json.dumps(card,ensure_ascii=True))<=1300:offered.append(card)
            if self.retrieval_selection_policy in {'facets_v1','facets_v2'}:
                offered,complete=select_supported_cards(value['query'],selection_rows,ctx,policy=self.retrieval_selection_policy)
                final=[]
                for card in offered:
                    current=self._document_allowed(state.db,ctx,card['id'])
                    if current and current['active_revision_id']==card['revision']:final.append(card)
                if len(final)!=len(offered):complete=False
                offered=final
            if self.answerability_judge is not None:
                chosen=self.answerability_judge(value['query'],offered)
                if chosen!='NONE' and chosen not in {r['id'] for r in offered}:raise ValueError('answerability_judge_unoffered_id')
                offered=[r for r in offered if r['id']==chosen]
            if offered and state.db.execute("""SELECT 1 FROM curation_episode_jobs e
                    WHERE e.id IN (SELECT DISTINCT job_id FROM episode_candidates
                        WHERE document_id=ANY(?::text[]) AND status='applied')
                    AND e.progress::jsonb#>>'{coverage,source_capture,completion}'='partial'
                    LIMIT 1""",([card['id'] for card in offered],)).fetchone():
                capture_gaps=['incomplete_source_capture']
        result={'results':offered[:value.get('limit',8)],'answerable':complete if self.retrieval_selection_policy in {'facets_v1','facets_v2'} else bool(offered)}
        if len(result['results'])!=len(offered):result['answerable']=False
        bound=1500 if value.get('mode')=='automatic' else 4000
        while result['results'] and len(json.dumps(result,ensure_ascii=True))>bound:
            result['results'].pop();result['answerable']=False
        if self.retrieval_selection_policy=='baseline':result['answerable']=bool(result['results'])
        if capture_gaps:result['coverage_gaps']=capture_gaps
        if not result['answerable']:result['coverage_gaps']=capture_gaps+['no_authorized_supported_answer']
        # Coverage warnings are part of the wire response and its delivery bound.
        while result['results'] and len(json.dumps(result,ensure_ascii=True))>bound:
            result['results'].pop();result['answerable']=False
            result['coverage_gaps']=capture_gaps+['no_authorized_supported_answer']
        return result

    def validate_search_delivery(self,ctx,value,result):
        """Short delivery checkpoint after unlocked ranking; preserve history rules."""
        final=[]
        with self.open() as state:
            self.current_identity(state.db,ctx);self._need(ctx,'read')
            from agenthub.search_permissions import current_document_revisions
            ordinary=[card for card in result['results'] if card.get('evidence_status')!='source_linked_temporal']
            current_revisions=current_document_revisions(self,state.db,ctx,ordinary)
            for card in result['results']:
                if card.get('evidence_status')=='source_linked_temporal':
                    current=self._temporal_allowed(state.db,ctx,card['id'],known_at=value.get('time_mode')=='known_at')
                    valid=current and current['revision_id']==card['revision']
                else:
                    valid=current_revisions.get(card['id'])==card['revision']
                if valid:final.append(card)
        if len(final)==len(result['results']):return result
        output=dict(result,results=final,answerable=False)
        if 'support' in output:output['support']='partial' if final else 'none'
        output['coverage_gaps']=list(dict.fromkeys([*output.get('coverage_gaps',[]),'authorization_changed_before_delivery']))
        bound=1500 if value.get('mode')=='automatic' else 4000
        while not result.get('_candidate_pool') and output['results'] and len(json.dumps(output,ensure_ascii=True))>bound:output['results'].pop()
        return output

    @staticmethod
    def _filter_card(state,card,filters):
        row=state.db.execute('SELECT claim_json FROM knowledge_revisions WHERE revision_id=?',(card['revision'],)).fetchone()
        if not row:return False
        claim=json.loads(row[0])
        return all(not filters.get(key) or claim.get(key)==filters[key] for key in ('domain','knowledge_type')) and (
            not filters.get('subject') or filters['subject'].casefold() in [str(s).casefold() for s in claim.get('subjects',[])])

    def timeline(self,ctx,document_id,*,offset=0,limit=3,cursor=None,cursor_mode=False):
        if type(offset) is not int or not 0<=offset<=1000 or type(limit) is not int or not 1<=limit<=3:raise ValueError('timeline_bounds')
        if (type(cursor_mode) is not bool or (cursor is not None and
                (not cursor_mode or not isinstance(cursor,str) or
                 not re.fullmatch(r'tc1_[0-9a-f]{64}',cursor))) or
                (cursor_mode and offset!=0)):
            raise ValueError('timeline_cursor_request')
        from agenthub.processing.knowledge import active_generation_id
        from agenthub.processing.episode_pipeline import episode_links_for_document
        self._need(ctx,'read')
        with self.open() as state:
            doc=self._document_allowed(state.db,ctx,document_id)
            if not doc:raise Denied()
            generation=active_generation_id(state.db)
            if cursor is not None and not generation:raise ValueError('timeline_cursor_stale')
            if not generation:return {'id':document_id,'revision':doc['active_revision_id'],'episodes':[],
                'coverage_gaps':['generation_unavailable'],'has_more':False,
                **({'next_cursor':None} if cursor_mode else {})}
            # The public offset counts visible episodes. Scanning raw links a
            # page at a time prevents hidden links from shifting the next page
            # or revealing their number in a returned offset or gap marker.
            visible=[];raw_offset=0;scan_bound=False;link_gap=False
            while cursor_mode or len(visible)<offset+limit+1:
                if raw_offset>1000:
                    scan_bound=True;break
                selected=episode_links_for_document(state.db,generation,doc['internal_project'],
                    document_id,offset=raw_offset,limit=20)
                rows=selected.get('episodes',[])
                if len(rows)>20:raise ValueError('timeline_link_page_bound')
                if selected.get('coverage_gaps'):link_gap=True
                for entry in rows:
                    if self._timeline_episode_allowed(state.db,ctx,generation,entry):visible.append(entry)
                raw_offset+=len(rows)
                if not selected.get('has_more',False):break
                if not rows:scan_bound=True;break
            if cursor_mode and scan_bound:raise ValueError('timeline_cursor_scan_bound')
            cursor_for=None
            if cursor_mode:
                # A cursor is a state fingerprint, not an authorization grant.
                # It hashes only authorized projections and is checked against
                # a fresh current-policy scan before any continuation is sent.
                identity={key:ctx.get(key) for key in ('tenant','actor','enrollment','acting_for')}
                identity['delegated_projects']=sorted(ctx.get('delegated_projects') or [])
                projection=[{key:entry.get(key) for key in ('episode_id','session','source_turn',
                    'summary_revision','summary','source_range','coverage_gaps','supporting_source_ids')}
                    for entry in visible]
                fingerprint=hashlib.sha256(json.dumps([identity,document_id,doc['active_revision_id'],
                    generation,projection],sort_keys=True,ensure_ascii=True).encode()).hexdigest()
                def cursor_for(index):
                    return 'tc1_'+hashlib.sha256((fingerprint+':'+str(index)).encode()).hexdigest()
                if cursor is not None:
                    found=next((index for index in range(min(len(visible),1000)+1)
                        if cursor_for(index)==cursor),None)
                    if found is None:raise ValueError('timeline_cursor_stale')
                    offset=found
            gaps=[]
            if link_gap:gaps.append('timeline_link_unavailable')
            # A raw scan bound cannot distinguish restricted links from a
            # permitted continuation; report incompleteness only when this
            # request already has a visible episode.
            if scan_bound and visible:gaps.append('timeline_scan_bound')
            output={'id':document_id,'revision':doc['active_revision_id'],'episodes':[],
                    'coverage_gaps':gaps,'has_more':False}
            if cursor_mode:output['next_cursor']='tc1_'+'0'*64
            page=visible[offset:offset+limit]
            for entry in page:
                item={k:entry.get(k) for k in ('summary','summary_revision','source_range','coverage_gaps')}
                trial={**output,'episodes':output['episodes']+[item]}
                if len(json.dumps(trial,ensure_ascii=True))<=4000:
                    output['episodes'].append(item);continue
                if output['episodes']:break
                # A single long summary still consumes exactly one visible
                # offset. Keep its evidence handle and explicit truncation gap.
                item['summary']=str(item.get('summary') or '')
                item['coverage_gaps']=list(dict.fromkeys([*(item.get('coverage_gaps') or []),
                    'episode_summary_truncated']))
                item['source_range']=item.get('source_range')
                trial={**output,'episodes':[item]}
                if len(json.dumps(trial,ensure_ascii=True))>4000:
                    item['coverage_gaps']=['episode_summary_truncated']
                    item['source_range']=None
                summary=item['summary'];lo=0;hi=len(summary)
                while lo<hi:
                    middle=(lo+hi+1)//2;item['summary']=summary[:middle]
                    if len(json.dumps({**output,'episodes':[item]},ensure_ascii=True))<=4000:lo=middle
                    else:hi=middle-1
                item['summary']=summary[:lo]
                output['episodes'].append(item)
                break
            emitted=len(output['episodes'])
            more=len(visible)>offset+emitted
            if more and offset+emitted>1000:
                output['coverage_gaps'].append('timeline_offset_bound')
            else:output['has_more']=more
            if cursor_mode:output['next_cursor']=cursor_for(offset+emitted) if output['has_more'] else None
            current=self._document_allowed(state.db,ctx,document_id)
            if not current or current['active_revision_id']!=doc['active_revision_id']:raise Denied()
            if active_generation_id(state.db)!=generation:raise Denied()
            if any(not self._timeline_episode_allowed(state.db,ctx,generation,entry)
                   for entry in visible):raise Denied()
            if len(json.dumps(output,ensure_ascii=True))>4000:raise ValueError('timeline_response_bound')
            return output

    def _timeline_episode_allowed(self,db,ctx,generation,entry):
        job=db.execute('''SELECT source_ids FROM curation_episode_jobs WHERE generation_id=? AND episode_id=?
            AND session=? AND turn=? AND status IN ('done','withdrawn')''',
            (generation,entry.get('episode_id'),entry.get('session'),entry.get('source_turn'))).fetchone()
        if not job:return False
        sources=json.loads(job['source_ids']);required=set(sources)|set(entry.get('supporting_source_ids',[]))
        if not required:return False
        for ident in required:
            source=db.execute('SELECT * FROM enterprise_sources WHERE id=?',(ident,)).fetchone()
            if not self._visible_source(db,ctx,source):return False
        return True
