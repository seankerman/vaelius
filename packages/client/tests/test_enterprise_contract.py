import json
from pathlib import Path
import unittest

from agentclient.enterprise_contract import (
    VERSION, validate_lifecycle, validate_search, validate_source, validate_response,
)


class EnterpriseContractTests(unittest.TestCase):
    def setUp(self):
        self.fixture=json.loads((Path(__file__).resolve().parents[2]/'server/tests/fixtures/processing/enterprise_local_v1.json').read_text())

    def source(self):
        item=self.fixture['sources'][0]
        return {'version':VERSION,'external_id':item['id'],'session':item['session'],
            'turn':item['turn'],'project':item['project'],'kind':item['kind'],
            'body':item['body'],'occurred_at':12345,'visibility':item['visibility']}

    def test_private_location_is_valid_but_authority_claims_are_not(self):
        self.assertEqual(validate_source(self.source())['external_id'],'same-source')
        for change in ({'tenant':'harbor'},{'owner':'mallory'},{'raw_visibility':'team'},
                       {'version':'0.2.0'},{'exit_code':True}):
            with self.subTest(change=change),self.assertRaises(ValueError):
                validate_source(dict(self.source(),**change))

    def test_search_and_lifecycle_are_bounded(self):
        self.assertEqual(validate_search({'version':VERSION,'query':'Maple dataset'})['query'],'Maple dataset')
        with self.assertRaises(ValueError):
            validate_search({'version':VERSION,'query':'Maple dataset','tenant':'orchard'})
        with self.assertRaises(ValueError):
            validate_search({'version':VERSION,'query':'Maple dataset','limit':1000})
        request={'version':VERSION,'target_id':'source','expected_revision':'1',
            'idempotency_key':'once','reason':'incorrect','operation':'withdraw'}
        self.assertEqual(validate_lifecycle(request)['operation'],'withdraw')
        with self.assertRaises(ValueError):
            validate_lifecycle(dict(request,principal='alice'))

    def test_response_envelopes_reject_extra_fields_and_inconsistent_cards(self):
        path='/enterprise/v1/search'
        good={'results':[{'id':'doc_1','revision':'rev_1','title':'Maple',
            'lesson':'A stable header was required.','evidence_status':None}],
            'answerable':True}
        self.assertEqual(validate_response(path,good),good)
        for malformed in (dict(good,tenant='forged'),dict(good,answerable=False),
                          {'results':[{'id':'doc_1','revision':'rev_1',
                              'title':'Maple','lesson':'A stable header was required.',
                              'evidence_status':None,'raw_body':'secret'}],
                           'answerable':True}):
            with self.subTest(malformed=malformed),self.assertRaises(ValueError):
                validate_response(path,malformed)

    def test_schema_manifest_matches_versioned_envelopes(self):
        path=Path(__file__).resolve().parents[2]/'server/tests/fixtures/processing/enterprise_local_v1_schemas.json'
        definitions=json.loads(path.read_text())['$defs']
        self.assertEqual(definitions['sourceRequest']['properties']['version']['const'],VERSION)
        self.assertEqual(definitions['searchResponse']['properties']['results']['maxItems'],20)
        self.assertEqual(set(definitions['sourceRequest']['required']),
            {'version','external_id','session','turn','project','kind','body','occurred_at'})


if __name__=='__main__':unittest.main()
