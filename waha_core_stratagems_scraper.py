import json
import argparse
from pathlib import Path

from playwright.sync_api import sync_playwright

import waha_scraper_common as style_parser

CORE_RULES_URL = f"{style_parser.DEFAULT_WAHAPEDIA_BASE}/the-rules/core-rules/"


# =========================================================
# STRATAGEM CONTENT (fresh parsing/style/storage layer — same wrapper
# markup as detachment stratagems, so this shares
# style_parser.extract_stratagem_from_wrap rather than reinventing it)
# =========================================================

def extract_core_stratagems_from_soup(soup, styles, root_style):
    """Extract generic/core stratagems from the (already style-stamped)
    core rules page content.

    Wahapedia currently puts these in a two-column container (`Columns2` in
    the saved HTML, sometimes described as `Column2`) and each stratagem
    uses the same wrapper markup as detachment stratagems — `.str10Wrap` on
    wh40k10ed, or the newer `.str11Wrap` on wh40k11ed (see
    style_parser.extract_stratagem_from_wrap). Keep this parser focused on
    that independent core-rules container so it cannot accidentally pull
    detachment stratagems from faction pages.
    """
    containers = soup.select(".Columns2, .Column2")
    if not containers:
        containers = [soup]

    stratagems = []
    seen = set()

    for container in containers:
        for wrap in container.select(":scope > .str10Wrap, .str10Wrap, :scope > .str11Wrap, .str11Wrap"):
            stratagem = style_parser.extract_stratagem_from_wrap(wrap, styles, root_style)
            if not stratagem or stratagem["name"] in seen:
                continue
            seen.add(stratagem["name"])
            stratagems.append(stratagem)

    return stratagems


# =========================================================
# PIPELINE
# =========================================================

def scrape_core_stratagems(page, url=CORE_RULES_URL):
    page.goto(url, wait_until="domcontentloaded")
    page.wait_for_selector(
        ".Columns2 .str10Wrap, .Column2 .str10Wrap, .str10Wrap, "
        ".Columns2 .str11Wrap, .Column2 .str11Wrap, .str11Wrap",
        timeout=30000,
    )

    body = page.locator("body")
    soup, styles, root_style = style_parser.resolve_styled_content(body)

    return {
        "name": "Core Stratagems",
        "faction": "Core Rules",
        "detachment": "Core Stratagems",
        "stratagems": extract_core_stratagems_from_soup(soup, styles, root_style),
    }


def parse_args():
    parser = argparse.ArgumentParser(description="Extract Wahapedia generic/core stratagems (v2 parsing)")
    parser.add_argument("--url", default=CORE_RULES_URL)
    parser.add_argument("--output-json", default="core_stratagems.json")
    parser.add_argument("--headed", action="store_true")
    return parser.parse_args()


def main():
    args = parse_args()

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not args.headed)
        page = browser.new_page()
        data = scrape_core_stratagems(page, args.url)
        browser.close()

    with open(args.output_json, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    print(f"Wrote {args.output_json} with {len(data['stratagems'])} core stratagems")


if __name__ == "__main__":
    main()
