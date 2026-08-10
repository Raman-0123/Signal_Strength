import unittest

from public_contact_finder.finder import (
    _is_allowed_url,
    extract_contacts_from_html,
)


class PublicContactFinderTests(unittest.TestCase):
    def test_tel_link_near_leader_is_high_confidence(self):
        html = """
        <html>
          <head><title>Leadership</title></head>
          <body>
            <section>
              <h2>Jane Doe</h2>
              <p>Chief Executive Officer</p>
              <a href="tel:+91 98765 43210">Call Jane's office</a>
            </section>
          </body>
        </html>
        """

        contacts = extract_contacts_from_html(
            html, "https://example.com/leadership", leader_name="Jane Doe",
        )

        self.assertEqual(len(contacts), 1)
        self.assertEqual(contacts[0].phone, "+919876543210")
        self.assertEqual(contacts[0].attribution, "leader_context")
        self.assertEqual(contacts[0].confidence, "high")
        self.assertEqual(contacts[0].source_url, "https://example.com/leadership")

    def test_footer_phone_is_not_claimed_as_leader_direct_number(self):
        html = """
        <html><body>
          <h1>Jane Doe, CEO</h1>
          <div>Company headquarters</div>
          <footer>Contact our reception: <a href="tel:+1-212-555-0199">Call</a></footer>
        </body></html>
        """

        contacts = extract_contacts_from_html(
            html, "https://example.com", leader_name="Jane Doe",
        )

        self.assertEqual(len(contacts), 1)
        self.assertEqual(contacts[0].attribution, "company_general")
        self.assertEqual(contacts[0].leader_name, "")

    def test_number_like_financial_text_without_phone_cue_is_ignored(self):
        html = """
        <html><body>
          <p>Revenue increased from 20231001 to 20241001.</p>
        </body></html>
        """

        contacts = extract_contacts_from_html(
            html, "https://example.com/investors",
        )

        self.assertEqual(contacts, [])

    def test_url_encoded_tel_spaces_do_not_add_twenty_to_number(self):
        html = """
        <html><body>
          <a href="tel:+91%2098765%2043210">Phone</a>
        </body></html>
        """

        contacts = extract_contacts_from_html(
            html, "https://example.com/contact",
        )

        self.assertEqual([contact.phone for contact in contacts], ["+919876543210"])

    def test_scope_rejects_login_and_external_domains(self):
        self.assertTrue(_is_allowed_url(
            "https://www.example.com/contact", "example.com",
        ))
        self.assertFalse(_is_allowed_url(
            "https://www.example.com/login", "example.com",
        ))
        self.assertFalse(_is_allowed_url(
            "https://example.net/contact", "example.com",
        ))


if __name__ == "__main__":
    unittest.main()
