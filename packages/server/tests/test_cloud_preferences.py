"""Preference extraction/selection fixtures frozen before implementation."""
import json
from pathlib import Path
import unittest

from agenthub.cloud_preferences import validate_preference_candidate, select_preference_values

FIXTURE = json.loads((Path(__file__).resolve().parents[1]/'tests/fixtures/service/cloud_identity_v1.json').read_text())


class PreferenceSemanticsTests(unittest.TestCase):
    def test_durable_grounded_and_quoted_or_one_task_rejected(self):
        for item in FIXTURE['preferences']:
            candidate = {k:item[k] for k in ('key','value','scope')}
            candidate['quote'] = item['evidence']
            if item.get('project'): candidate['project'] = item['project']
            if item['durable']:
                self.assertEqual(validate_preference_candidate(item['evidence'],candidate)['value'],item['value'])
            else:
                with self.assertRaises(ValueError): validate_preference_candidate(item['evidence'],candidate)

    def test_fabricated_quote_value_project_and_owner_rejected(self):
        text = 'Please remember: I prefer pytest for my projects.'
        for change in ({'quote':'I like pytest'}, {'value':'unittest'},
                       {'scope':'project','project':'not-in-evidence'}, {'owner':'bob'}):
            candidate={'key':'test_runner','value':'pytest','scope':'user','quote':text,**change}
            with self.assertRaises(ValueError): validate_preference_candidate(text,candidate)

    def test_negation_and_narrow_scope_cannot_be_broadened(self):
        fixture=json.loads((Path(__file__).resolve().parents[1]/'tests/fixtures/service/cloud_preference_language_v1.json').read_text())
        for row in fixture['rejections']:
            with self.subTest(row=row),self.assertRaises(ValueError):
                validate_preference_candidate(row['quote'],{'key':'test_runner',**row})

    def test_project_and_explicit_override_and_org_policy(self):
        rows=[{'key':'test_runner','value':'pytest','scope':'user','project':None,'task':None},
              {'key':'test_runner','value':'unittest','scope':'project','project':'legacy','task':None}]
        self.assertEqual(select_preference_values(rows,project='modern'),{'test_runner':'pytest'})
        self.assertEqual(select_preference_values(rows,project='legacy'),{'test_runner':'unittest'})
        self.assertEqual(select_preference_values(rows,project='legacy',explicit={'test_runner':'nose'}),{'test_runner':'nose'})
        self.assertEqual(select_preference_values(rows,project='legacy',explicit={'test_runner':'nose'},
            required={'test_runner':'pytest'}),{'test_runner':'pytest'})


if __name__ == '__main__': unittest.main()
