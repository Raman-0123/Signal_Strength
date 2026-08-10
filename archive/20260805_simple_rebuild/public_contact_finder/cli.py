"""Command-line interface for the public contact finder."""

import argparse
import csv
from pathlib import Path

from public_contact_finder.finder import PublicContactFinder


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Find phone numbers explicitly published on public pages of an "
            "official company domain. No login or access-control bypass."
        ),
    )
    parser.add_argument(
        "--domain",
        required=True,
        help="Official company domain or URL, for example example.com.",
    )
    parser.add_argument(
        "--leader",
        default="",
        help="Optional exact leader name used for context attribution.",
    )
    parser.add_argument(
        "--max-pages",
        type=int,
        default=20,
        help="Maximum public HTML pages to inspect (1-50; default 20).",
    )
    parser.add_argument(
        "--output",
        default="public_contacts.csv",
        help="CSV destination (default public_contacts.csv).",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    finder = PublicContactFinder(
        args.domain,
        max_pages=args.max_pages,
        status=print,
    )
    contacts = finder.find(leader_name=args.leader.strip())
    output_path = Path(args.output).expanduser().resolve()
    fieldnames = [
        "phone", "phone_type", "attribution", "leader_name", "source_url",
        "page_title", "evidence", "confidence",
    ]
    with output_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(contact.to_dict() for contact in contacts)

    leader_count = sum(
        contact.attribution == "leader_context" for contact in contacts
    )
    print(
        f"Saved {len(contacts)} public number(s) to {output_path} "
        f"({leader_count} appeared near the requested leader name)."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
