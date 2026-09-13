"""Hardware-free regressions for OLED power-event presentation."""
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'oled'))
from power_overlay import PowerOverlay


class PowerOverlayTests(unittest.TestCase):
    @staticmethod
    def status(mains='UP', supervisor=None, voltage=4.1):
        targets = {}
        if mains is not None:
            targets['mains'] = {'state': mains, 'voltage_v': voltage}
        return {'targets': targets, 'ups_supervisor': supervisor or {}}

    def test_mains_transitions_replace_pages_for_three_seconds(self):
        now = [0.0]
        overlay = PowerOverlay(clock=lambda: now[0])
        self.assertIsNone(overlay.update(self.status('UP')))

        event = overlay.update(self.status('DOWN', voltage=4.12))
        self.assertEqual((event['title'], event['message']),
                         ('MAINS POWER', 'DISCONNECTED'))
        self.assertEqual(event['voltage_v'], 4.12)
        now[0] = 2.9
        self.assertEqual(overlay.update(None)['message'], 'DISCONNECTED')
        now[0] = 3.0
        self.assertIsNone(overlay.update(self.status('DOWN')))

        restored = overlay.update(self.status('UP'))
        self.assertEqual(restored['message'], 'RESTORED')

    def test_initial_outage_is_shown_but_initial_healthy_state_is_quiet(self):
        self.assertIsNone(PowerOverlay().update(self.status('UP')))
        self.assertEqual(
            PowerOverlay().update(self.status('DOWN'))['message'], 'DISCONNECTED')

    def test_shutdown_countdown_and_display_off_take_priority(self):
        overlay = PowerOverlay()
        countdown = overlay.update(self.status('DOWN', {
            'state': 'QUALIFYING', 'remaining_seconds': 9.2,
            'battery_voltage_v': 3.05,
        }))
        self.assertEqual(countdown['message'], 'IN 10 SEC')
        self.assertEqual(countdown['voltage_v'], 3.05)

        test_countdown = overlay.update(self.status('DOWN', {
            'state': 'TEST_COUNTDOWN', 'final_countdown_seconds': 5.1,
            'battery_voltage_v': 4.0,
        }))
        self.assertEqual(test_countdown['message'], 'IN 6 SEC')

        self.assertEqual(overlay.update(self.status('DOWN', {
            'state': 'DISPLAY_OFF'})), {'display_off': True})
        self.assertEqual(overlay.update(None), {'display_off': True})
        failed = overlay.update(self.status('DOWN', {'state': 'POWEROFF_FAILED'}))
        self.assertEqual(failed['message'], 'FAILED')


if __name__ == '__main__':
    unittest.main()
