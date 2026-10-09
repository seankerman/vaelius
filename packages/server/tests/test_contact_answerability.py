"""A relevant project passage cannot supply a missing contact attribute."""
import unittest
from agenthub.cloud_retrieval import supported_answer


class ContactAnswerability(unittest.TestCase):
    def test_communication_mechanisms_do_not_require_contact_addresses(self):
        for question, text in [
            ('How does Project Maple send email notifications?',
             'Project Maple sends email notifications using SMTP over TLS.'),
            ('How does Project Maple record phone calls?',
             'Project Maple records phone calls in encrypted local storage.')]:
            with self.subTest(question=question):
                self.assertTrue(supported_answer(question, {'title': 'Project Maple', 'lesson': text},
                    policy='facets_v2', semantic_score=.9))

    def test_phone_requires_a_phone_value(self):
        question = 'What is the customer support phone number for Project Maple?'
        for text, expected in [
            ('Project Maple stores copies across three zones.', False),
            ('Project Maple customer support can be contacted; its phone number is unknown.', False),
            ('Project Maple customer support phone is +1 555 010 1234.', True)]:
            with self.subTest(text=text):
                self.assertEqual(supported_answer(question, {'title': 'Project Maple', 'lesson': text},
                    policy='facets_v2', semantic_score=.9), expected)

    def test_email_requires_an_email_value(self):
        question = 'What is the support email address for Project Maple?'
        for text, expected in [
            ('Project Maple supports email notifications.', False),
            ('Project Maple support email is help@example.test.', True)]:
            with self.subTest(text=text):
                self.assertEqual(supported_answer(question, {'title': 'Project Maple', 'lesson': text},
                    policy='facets_v2', semantic_score=.9), expected)


if __name__ == '__main__':
    unittest.main()
