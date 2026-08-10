import unittest

from core.competitor_intel import extract_event_participants
from core.signal_intelligence import assess_business_model, assess_gcc, assess_signal
from lead_generator_cli import get_event_probability_score


class SignalIntelligenceTests(unittest.TestCase):
    def test_contextual_keynote_is_verified(self):
        result = assess_signal(
            "Jane Doe - CMO at Acme | LinkedIn",
            "Delhi · Keynote speaker at the SaaS Growth Summit 2025.",
            person_name="Jane Doe",
            selected_signals=["speaker", "keynote speaker", "panelist"],
        )
        self.assertTrue(result["selected_match"])
        self.assertEqual(result["name"], "Past Speaker")
        self.assertGreaterEqual(result["score"], 90)
        self.assertTrue(result["evidence"])

    def test_audio_speaker_does_not_become_a_person_signal(self):
        result = assess_signal(
            "Jane Doe - CMO at Acme | LinkedIn",
            "Led the launch of an award-winning smart speaker system for homes.",
            person_name="Jane Doe",
            selected_signals=["speaker", "award winner"],
        )
        self.assertFalse(result["selected_match"])
        self.assertEqual(result["name"], "No Verified Signal")

    def test_late_signal_about_another_person_is_rejected(self):
        prefix = "Jane leads marketing at Acme. " + ("product growth strategy " * 12)
        result = assess_signal(
            "Jane Doe - CMO at Acme | LinkedIn",
            prefix + "Ravi Kumar was the keynote speaker at the annual summit.",
            person_name="Jane Doe",
            selected_signals=["keynote speaker"],
        )
        self.assertFalse(result["selected_match"])

    def test_authority_is_not_mistaken_for_author(self):
        score, name = get_event_probability_score(
            "Owns budget authority for the marketing team.",
            "Jane Doe - CMO at Acme | LinkedIn",
        )
        self.assertEqual(score, 35)
        self.assertEqual(name, "No Verified Signal")

    def test_b2b_classifier_requires_real_evidence(self):
        b2b = assess_business_model(
            "Acme enterprise software",
            "B2B SaaS platform serving corporate clients.",
            "B2B only",
        )
        unknown = assess_business_model(
            "Jane Doe - CMO",
            "Marketing leader focused on growth and brand strategy.",
            "B2B only",
        )
        self.assertTrue(b2b["matched"])
        self.assertIn(b2b["detected"], {"B2B", "Hybrid"})
        self.assertFalse(unknown["matched"])

    def test_gcc_roundtable_and_company_evidence_are_detected(self):
        signal = assess_signal(
            "Jane Doe - Chief Digital Officer | LinkedIn",
            "Bengaluru · Hosted the GCC roundtable for capability-centre leaders.",
            person_name="Jane Doe",
            selected_signals=["GCC Roundtable"],
        )
        gcc = assess_gcc(
            "Jane Doe - Chief Digital Officer | LinkedIn",
            "India GCC leader at a Global Capability Centre in Bengaluru.",
            enabled=True,
        )
        self.assertTrue(signal["selected_match"])
        self.assertEqual(signal["name"], "Roundtable Participant")
        self.assertTrue(gcc["matched"])

    def test_competitor_posts_only_keep_names_tied_to_event_evidence(self):
        participants = extract_event_participants(
            "Jane Doe announced a product update. Conference speakers include Ravi Kumar and Meera Shah.",
            ["speaker", "conference speaker"],
        )
        names = [item["name"] for item in participants]
        self.assertNotIn("Jane Doe", names)
        self.assertIn("Ravi Kumar", names)
        self.assertIn("Meera Shah", names)


if __name__ == "__main__":
    unittest.main()
