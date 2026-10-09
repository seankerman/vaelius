"""Authored native temporal parse failures cannot gate original ingestion."""
import unittest


class NativeValidityTests(unittest.TestCase):
    def test_explicit_source_dates_are_independent_of_capture(self):
        from agenthub.processing.temporal import _native_validity
        value=_native_validity('From 2026-09-01 the rotor pressure is 12 kPa.')
        self.assertEqual(value['from'],'2026-09-01');self.assertEqual(value['basis'],'explicit_source')
        self.assertEqual(value['to_status'],'ongoing')

    def test_absent_malformed_and_reversed_dates_stay_unknown(self):
        from agenthub.processing.temporal import _native_validity
        for body in ('The rotor pressure is 12 kPa.','From 2026-99-01 the rotor pressure is 12 kPa.',
                     'From 2026-09-12 until 2026-09-01 the rotor pressure is 12 kPa.',
                     'From 2026-09-01 until 2026-99-12 the rotor pressure is 12 kPa.'):
            with self.subTest(body=body):self.assertIsNone(_native_validity(body))

    def test_explicit_half_open_end_and_unknown_end_are_distinct(self):
        from agenthub.processing.temporal import _native_validity
        bounded=_native_validity('From 2026-09-01 until 2026-09-12 the rotor pressure is 12 kPa.')
        self.assertEqual((bounded['to_status'],bounded['to']),('bounded','2026-09-12'))
        unknown=_native_validity('From 2026-09-01 until the next inspection the rotor pressure is 12 kPa.')
        self.assertEqual(unknown['to_status'],'unknown');self.assertNotIn('to',unknown)
        inclusive=_native_validity('From 2026-09-01 through 2026-09-12 the rotor pressure is 12 kPa.')
        self.assertEqual(inclusive['to_status'],'unknown')


if __name__=='__main__':unittest.main()
