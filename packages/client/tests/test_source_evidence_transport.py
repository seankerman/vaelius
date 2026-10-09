import unittest

class SourceEvidenceContract(unittest.TestCase):

    def test_explicit_evidence_response_accepts_only_bounded_exact_spans(self):
        from agentclient.cloud_contract import validate_response
        route = '/enterprise/v3/document-evidence'
        value = {'id': 'doc', 'revision': 'r1', 'sources': [{'source_id': 's', 'segment_id': 'seg', 'text': 'Exact quote.', 'start': 5, 'end': 17, 'source_version': 1, 'owner': 'alice', 'kind': 'Stop', 'occurred_at': None}], 'diagnostic_source_inspection': True, 'coverage_gaps': [], 'next_offset': None}
        self.assertEqual(validate_response(route, value), value)
        self.assertEqual(validate_response(route, {'id': 'doc', 'status': 'invalidated', 'current_revision': 'r2'})['status'], 'invalidated')
        for update in [{'next_offset': True}, {'sources': [dict(value['sources'][0], end=100)]}, {'notice': 'x' * 4000}, {'diagnostic_source_inspection': False}]:
            with self.assertRaises(ValueError):
                validate_response(route, dict(value, **update))
