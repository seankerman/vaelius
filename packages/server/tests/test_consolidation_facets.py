import hashlib
import json
from pathlib import Path
import unittest
from agenthub.processing.episode_curator import validate_resolution,resolution_schema,EpisodeError


class ConsolidationFacetTests(unittest.TestCase):
    def test_validated_location_cannot_be_erased_by_support_or_ignore(self):
        folder=Path(__file__).resolve().parent/'fixtures/processing'
        raw=(folder/'consolidation_facets_v2.json').read_bytes()
        self.assertEqual(hashlib.sha256(raw).hexdigest(),json.loads((folder/'consolidation_facets_v2_manifest.json').read_text())['sha256'])
        fixture=json.loads(raw);payload={'related_artifacts':[{'artifact_id':'prior','claim':fixture['earlier_claim']}]}
        save,correction=fixture['candidates']
        for candidate,op,target in ((save,'SUPPORT','prior'),(correction,'IGNORE','')):
            with self.assertRaises(EpisodeError):validate_resolution({'candidate_key':candidate['candidate_key'],'operation':op,
                'target_artifact_id':target,'reason':'compatible_support' if op=='SUPPORT' else 'not_durable_or_not_atomic'},candidate,payload)
            self.assertNotIn('IGNORE',resolution_schema(candidate,payload)['properties']['operation']['enum'])
            self.assertEqual(validate_resolution({'candidate_key':candidate['candidate_key'],'operation':'CREATE',
                'target_artifact_id':'','reason':'new_claim'},candidate,payload)['operation'],'CREATE')
