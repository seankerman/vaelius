"""Frozen authored regressions for the final generic selector development round.

These synthetic cases diagnose consumed development failures. They are neither
held-out evidence nor permission fixtures; the caller verifies current policy.
"""
import hashlib
import unittest

from agenthub.cloud_retrieval import supported_answer, select_supported_cards


def claim(title, body, owner='alice', **extra):
    return dict(title=title, lesson=body, _verified_source_owners=[owner], **extra)


def row(number, title, body, cosine=.75, owner='alice', **extra):
    return dict(document_id=str(number), revision_id='1',
                claim=claim(title, body, owner, **extra), cosine=cosine, rrf=.03)


class BalancedSelectorTests(unittest.TestCase):
    def answer(self, query, body, *, title='Record', actor='alice', owner='alice', **extra):
        return supported_answer(query, claim(title, body, owner, **extra),
                                ctx={'actor':actor}, policy='facets_v2', semantic_score=.75)

    def select(self, query, rows, actor='alice'):
        return select_supported_cards(query, rows, {'actor':actor}, policy='facets_v2')

    def test_current_entity_is_not_automatically_a_person(self):
        self.assertTrue(self.answer('What currency does Jasper use for its budget?',
            'The approved Jasper budget is 284000 EUR.'))
        self.assertTrue(self.answer('How long does Jasper retain audit evidence?',
            'Jasper retains audit evidence for 21 days.'))
        self.assertFalse(self.answer('Why did Bob choose Jasper?',
            'Alice chose Jasper because the pilot had lower error rates.'))

    def test_when_modifier_does_not_require_calendar_evidence(self):
        from agenthub.processing.durable_memory import VERSION
        body='I chose to check Jasper permissions at delivery because cached grants can outlive a revocation.'
        memory=dict(policy=VERSION,facets=['decision'],actors=['user'],reason_actor='user',reason_quote=body)
        self.assertTrue(self.answer('Why do I check Jasper permissions when delivering results?',body,memory_context=memory))
        self.assertFalse(self.answer('When did I choose Jasper?',body,memory_context=memory))

    def test_conditional_user_preference_binds_verified_private_owner(self):
        body='Charlie prefers progress updates only when a decision or blocker changes. Unchanged polling should stay quiet.'
        self.assertTrue(self.answer('When should I receive progress updates?',body,actor='charlie',owner='charlie'))
        self.assertFalse(self.answer('When should I receive progress updates?',body,actor='alice',owner='charlie'))
        self.assertFalse(self.answer('When should I receive progress updates?',
            'Alice heard that Charlie prefers updates after every tool call.',actor='charlie',owner='alice'))

    def test_pending_final_reason_is_absence_even_with_rationale_noun(self):
        self.assertFalse(self.answer('Why did we choose streaming for Jasper?',
            'Jasper has streaming and nightly import under review. No final selection or rationale has been recorded.'))

    def test_discovery_prefers_direct_prose_over_related_review(self):
        rows=[row(1,'Jasper ownership register','The Jasper ownership register assigns alarm triage to the duty engineer.',.71),
              row(2,'Jasper ownership register review','Related note for Jasper ownership register: A neighboring inventory records contacts but does not override the named specification. The source identity is immutable even when the title changes.',.9),
              row(3,'Jasper ownership register','# Jasper ownership register',.95)]
        cards,complete=self.select('Find the Jasper ownership register.',rows)
        self.assertTrue(complete);self.assertEqual([c['id'] for c in cards],['1'])

    def test_remedy_requires_action_instead_of_just_ticket_reference(self):
        self.assertTrue(self.answer('What is the remedy in ticket OPS-847?',
            'Ticket OPS-847 records a stale endpoint map. The accepted remedy is to invalidate the map before reopening traffic.'))
        self.assertFalse(self.answer('What is the remedy in ticket OPS-847?',
            'Related note for OPS-847: The source identity is immutable even when the title changes.'))

    def test_consent_and_retention_need_separate_full_facts(self):
        q='What consent and retention constraints govern Jasper survey responses?'
        rows=[row(1,'Jasper consent','Jasper participation requires explicit respondent consent. Consent is checked before acceptance.'),
              row(2,'Jasper retention','Jasper responses expire after 45 days unless the respondent renews research consent.',.9)]
        cards,complete=self.select(q,rows)
        self.assertTrue(complete);self.assertEqual({c['id'] for c in cards},{'1','2'})
        self.assertFalse(self.select(q,rows[1:])[1])

    def test_fields_and_integrity_keep_topic_on_both_facets(self):
        q='Which Jasper fields and integrity check do downstream consumers need?'
        rows=[row(1,'Jasper fields','Jasper exports must include the customer reference and event timezone.'),
              row(2,'Jasper integrity','Each Jasper export has a manifest containing the row count and content checksum.'),
              row(3,'Other fields','Other exports include a date and a sample count.',.95)]
        cards,complete=self.select(q,rows)
        self.assertTrue(complete);self.assertEqual({c['id'] for c in cards},{'1','2'})

    def test_scope_and_expiry_are_not_just_a_renewal_clause(self):
        q='What are the scope and expiry rules for Jasper access?'
        rows=[row(1,'Jasper scope','Jasper access is granted to a named user in a specific project.'),
              row(2,'Jasper expiry','Jasper access expires after seven days unless an operator renews the project grant.')]
        cards,complete=self.select(q,rows)
        self.assertTrue(complete);self.assertEqual({c['id'] for c in cards},{'1','2'})
        self.assertFalse(self.select(q,rows[1:])[1])

    def test_instrument_question_can_use_one_card_for_complete_facets(self):
        q='What unit does Jasper use for turbidity, and how is the instrument identified?'
        rows=[row(1,'Jasper measurement catalog','The Jasper catalog lists turbidity in nephelometric units. The instrument serial is recorded separately from the sample identifier.')]
        cards,complete=self.select(q,rows)
        self.assertTrue(complete);self.assertEqual(len(cards),1)

    def test_unrelated_semantic_similarity_cannot_donate_mechanism(self):
        q='What stops removed account information reappearing after recovery?'
        rows=[row(1,'Deletion recovery','Expired records are physically purged. A tombstone is written first so restored backups cannot revive a deleted customer record.',.7),
              row(2,'Evidence retention','Audit evidence is retained for 14 days after the privacy review.',.85)]
        cards,complete=self.select(q,rows)
        self.assertTrue(complete);self.assertEqual([c['id'] for c in cards],['1'])

    def test_conflicting_values_and_wrong_identifier_still_fail(self):
        rows=[row(i,'Jasper pressure',f'The approved Jasper operating pressure is {v} kPa.') for i,v in enumerate((42,57))]
        self.assertFalse(self.select('What is the approved Jasper operating pressure?',rows)[1])
        self.assertFalse(self.answer('What is the remedy for OPS-848?',
            'Ticket OPS-847 requires replacing the leaking rotor seal.'))


if __name__=='__main__':unittest.main()
