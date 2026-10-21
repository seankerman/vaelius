"""Per-item provenance for delivered memory, read after authorization.

Measured on shared memory, agents followed injected memory as instructions almost
every time unless each item said who wrote it, in which project, when, and whether
the text is that person's own words. A header alone barely helped. Every delivered
item therefore carries this block next to its text.

Callers pass only IDs that the current reader is already authorized to receive.
These reads add metadata to those items and never widen what is delivered. The
author is the owning principal's handle, never an e-mail address or source text.
"""
from datetime import datetime, timezone
import json

from agenthub.instruction_shape import instruction_like

# Wire keys stay short: provenance shares the automatic-context byte budget.
ROLE = {'UserPromptSubmit': 'user_prompt', 'Stop': 'assistant_reply',
        'AssistantMessage': 'assistant_reply', 'PostToolUse': 'tool_call_or_result',
        'Interrupt': 'interrupted_turn', 'SourceBoundary': 'context_boundary'}
ORIGINAL = {'raw', 'source'}
MAX_BLOCK = 512


def display_handle(principal):
    """Principal ID as shown to readers. An e-mail address is reduced to its local part."""
    if not isinstance(principal, str) or not principal:
        return 'unknown'
    return principal.split('@', 1)[0][:64] or 'unknown'


def _iso(value):
    try:
        return datetime.fromtimestamp(float(value), timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _block(owners, projects, first, last, source_types, wording, role=None):
    owners = sorted(set(owners))
    projects = sorted(set(projects))
    types = sorted({t for t in source_types if t})
    block = {'author': display_handle(owners[0]) if owners else 'unknown'}
    if len(owners) > 1:
        block['other_authors'] = len(owners) - 1
    block['project'] = projects[0] if projects else 'unknown'
    if len(projects) > 1:
        block['other_projects'] = len(projects) - 1
    block['captured_at'] = _iso(first)
    if last is not None and _iso(last) != block['captured_at']:
        block['captured_until'] = _iso(last)
    block['source_type'] = types[0] if len(types) == 1 else ('mixed' if types else 'unknown')
    if role:
        block['role'] = role
    block['wording'] = wording
    return block


_SOURCE_SQL = '''SELECT s.id,s.owner,s.external_project,s.created,s.instruction_like,m.kind,
        CASE WHEN r.source_id IS NULL THEN 'agent'
             WHEN jsonb_array_length(c.source_types::jsonb)=1 THEN c.source_types::jsonb->>0
             ELSE r.payload::jsonb->>'source_type' END AS source_type
    FROM enterprise_sources s LEFT JOIN memories m ON m.id=s.id
    LEFT JOIN backend_source_revisions r ON r.source_id=s.id
    LEFT JOIN backend_connections c ON c.id=r.connection'''


def _postgres(db):
    return getattr(db, 'dialect', None) == 'postgres'


def _source_rows(db, source_ids):
    ids = sorted({i for i in source_ids if isinstance(i, str)})
    if not ids or not _postgres(db):
        return {}
    return {row['id']: row for row in db.execute(_SOURCE_SQL + ' WHERE s.id=ANY(?::text[])', (ids,)).fetchall()}


def source_provenance(db, source_ids):
    """{source_id: (block, flagged)} for authorized original sources."""
    return {ident: (_block([row['owner']], [row['external_project']], row['created'], None,
                           [row['source_type']], 'original', ROLE.get(row['kind'])),
                    bool(row['instruction_like'])) for ident, row in _source_rows(db, source_ids).items()}


def derived_provenance(db, groups):
    """{key: (block, flagged)} for derived items, given {key: [source_id, ...]}."""
    rows = _source_rows(db, [ident for ids in groups.values() for ident in ids])
    result = {}
    for key, ids in groups.items():
        found = [rows[i] for i in ids if i in rows]
        if not found:
            continue
        created = [r['created'] for r in found]
        result[key] = (_block([r['owner'] for r in found], [r['external_project'] for r in found],
                              min(created), max(created), [r['source_type'] for r in found], 'derived'),
                       any(r['instruction_like'] for r in found))
    return result


def document_provenance(db, document_ids):
    """{document_id: (block, flagged)}; temporal assertion IDs resolve to their document."""
    ids = sorted({i for i in document_ids if isinstance(i, str)})
    if not ids or not _postgres(db):
        return {}
    assertions = {}
    temporal = [i for i in ids if i.startswith('ta_')]
    if temporal:
        assertions = {row['assertion_id']: row['document_id'] for row in db.execute(
            'SELECT assertion_id,document_id FROM knowledge_temporal_assertions WHERE assertion_id=ANY(?::text[])',
            (temporal,)).fetchall()}
    documents = sorted({assertions.get(i, i) for i in ids})
    rows = db.execute('''WITH requested AS MATERIALIZED (
            SELECT x.document_id,x.source_id FROM enterprise_dependencies x WHERE x.document_id=ANY(?::text[])
        ), sourced AS (''' + _SOURCE_SQL.replace('SELECT s.id,', 'SELECT q.document_id,s.id,', 1).replace(
            'FROM enterprise_sources s', 'FROM requested q JOIN enterprise_sources s ON s.id=q.source_id', 1) + ''')
        SELECT x.document_id,ed.representation,array_agg(DISTINCT x.owner) owners,
            array_agg(DISTINCT x.external_project) projects,min(x.created) first_captured,
            max(x.created) last_captured,bool_or(x.instruction_like=1) flagged,
            array_agg(DISTINCT x.source_type) source_types,array_agg(DISTINCT x.kind) kinds,
            count(*) sources
        FROM sourced x JOIN enterprise_documents ed ON ed.id=x.document_id
        GROUP BY x.document_id,ed.representation''', (documents,)).fetchall()
    found = {}
    for row in rows:
        original = row['representation'] in ORIGINAL and row['sources'] == 1
        kinds = [k for k in row['kinds'] if k]
        role = ROLE.get(kinds[0]) if original and len(kinds) == 1 else None
        found[row['document_id']] = (_block(row['owners'], row['projects'], row['first_captured'],
                                            row['last_captured'], row['source_types'],
                                            'original' if original else 'derived', role),
                                     bool(row['flagged']))
    result = {}
    for ident in ids:
        document = assertions.get(ident, ident)
        if document in found:
            block, flagged = found[document]
            if ident.startswith('ta_'):
                block = dict(block, wording='derived')
                block.pop('role', None)
            result[ident] = (block, flagged)
    return result


def _text(item, keys):
    values = (item.get(key) for key in keys)
    return '\n'.join(value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
                     for value in values if value)


def decorate(item, block, flagged, text_keys):
    """Attach provenance and, only when true, the instruction_like marker."""
    item = dict(item, provenance=block or {'author': 'unknown', 'wording': 'derived'})
    if flagged or instruction_like(_text(item, text_keys)):
        item['instruction_like'] = True
    return item


def decorate_cards(db, cards):
    known = document_provenance(db, [card['id'] for card in cards])
    return [decorate(card, *known.get(card['id'], (None, False)), ('title', 'lesson', 'text'))
            for card in cards]


def _fit_card(card, card_bound):
    """A discovery excerpt is an exact prefix of a readable original: shorten it the
    way delivery packing already does. Any other oversized card is dropped."""
    if len(json.dumps(card, ensure_ascii=True)) <= card_bound:
        return card
    if card.get('evidence_status') != 'discovery_only' or not isinstance(card.get('lesson'), str):
        return None
    card = dict(card, excerpt_truncated=True)
    while card['lesson'] and len(json.dumps(card, ensure_ascii=True)) > card_bound:
        card['lesson'] = card['lesson'][:-64]
    return card if card['lesson'] else None


def bound_search(result, *, bound, card_bound=1300):
    """Keep the wire bound after provenance; drop whole cards, never rewrite text
    (other than shortening a discovery excerpt's exact prefix)."""
    output = dict(result)
    original = output.get('results', [])
    fitted = [_fit_card(card, card_bound) for card in original]
    cards = [card for card in fitted if card is not None]
    # A dropped card or a newly shortened excerpt both leave evidence undelivered.
    omitted = any(new is not old for new, old in zip(fitted, original))
    output['results'] = cards
    while cards and len(json.dumps(output, ensure_ascii=True)) > bound:
        cards.pop()
        omitted = True
    if omitted:
        output['answerable'] = False
        output['coverage_gaps'] = list(dict.fromkeys([*output.get('coverage_gaps', []),
                                                      'delivery_evidence_omitted']))
        while cards and len(json.dumps(output, ensure_ascii=True)) > bound:
            cards.pop()
    if not cards:
        output['answerable'] = False
    if 'support' in output:
        output['support'] = 'complete' if output['answerable'] else ('partial' if cards else 'none')
    return output
