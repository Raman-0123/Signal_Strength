# Public Contact Finder

This folder finds phone numbers explicitly published on public pages of an
official company domain. It is a free, lower-coverage alternative to licensed
contact databases.

It does:

- stay on the company domain supplied with `--domain`;
- check `robots.txt`;
- inspect public HTML pages and public search-result URLs;
- record the source URL, surrounding evidence, and confidence;
- label a number `leader_context` only when the exact leader name appears near
  that number. Everything else is `company_general`.

It does not:

- create accounts or temporary identities;
- sign in, submit forms, solve CAPTCHAs, or bypass paywalls/access controls;
- claim a switchboard or footer number belongs personally to a leader;
- guarantee coverage or that a publicly posted number remains current.

## Run

From the `Speedy-Scraper` directory:

```bash
conda activate speedy-scraper

python -m public_contact_finder.cli \
  --domain example.com \
  --leader "Jane Doe" \
  --max-pages 20 \
  --output jane_doe_public_contacts.csv
```

For company-level public numbers, omit `--leader`:

```bash
python -m public_contact_finder.cli \
  --domain example.com \
  --output example_public_contacts.csv
```

Always inspect `source_url`, `evidence`, and `attribution` before using a result.
Finding a public number is not permission to send unsolicited marketing calls
or messages; follow applicable consent, DND, privacy, and opt-out requirements.
