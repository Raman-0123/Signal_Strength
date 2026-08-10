from __future__ import annotations

import argparse
from datetime import datetime
from pathlib import Path

from speedy_scraper.config import load_job_config, preset_config
from speedy_scraper.exports import write_result
from speedy_scraper.pipeline import LeadScraper


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="speedy-scraper")
    subcommands = parser.add_subparsers(dest="command", required=True)

    run_parser = subcommands.add_parser("run", help="run a lead scrape")
    run_parser.add_argument("--config", type=Path, help="YAML job config")
    run_parser.add_argument("--preset", default=None, help="catalog preset name")
    run_parser.add_argument("--target", type=int, default=None, help="verified lead target")
    run_parser.add_argument("--output", type=Path, default=None, help="CSV/XLSX export path")
    run_parser.add_argument("--headful", action="store_true", help="show browser windows for browser sources")

    query_parser = subcommands.add_parser("queries", help="print generated queries")
    query_parser.add_argument("--config", type=Path)
    query_parser.add_argument("--preset", default=None)

    args = parser.parse_args(argv)
    if args.command == "run":
        config = load_job_config(args.config) if args.config else preset_config(args.preset)
        if args.target:
            config.target_count = args.target
        if args.headful:
            config.browser_headless = False
        output = args.output or Path("exports") / f"leads_{datetime.now().strftime('%Y%m%d_%H%M')}.xlsx"

        def progress(event: dict[str, object]) -> None:
            name = event.get("event")
            if name in {"searching", "verified", "finished"}:
                print(event)

        result = LeadScraper().run(config, progress=progress)
        saved = write_result(result, output)
        print(f"verified={len(result.leads)} rejected={len(result.rejections)} output={saved}")
        if result.source_errors:
            print("source_errors:")
            for error in result.source_errors:
                print(f"- {error}")
    elif args.command == "queries":
        from speedy_scraper.query import build_queries

        config = load_job_config(args.config) if args.config else preset_config(args.preset)
        for query in build_queries(config):
            print(query)

