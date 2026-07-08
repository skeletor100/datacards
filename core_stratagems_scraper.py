import argparse
import json
from pathlib import Path

from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

import waha_parse_utils as utils
from detachment_scraper import (
    extract_stratagem_field_block,
    extract_icon_classes,
    extract_color_class,
    strip_detachment_name_from_type,
)

CORE_RULES_URL = "https://wahapedia.ru/wh40k10ed/the-rules/core-rules/"


def extract_core_stratagems_from_soup(soup):
    """Extract generic/core stratagems from the core rules page.

    Wahapedia currently puts these in a two-column container (`Columns2` in the
    saved HTML, sometimes described as `Column2`) and each stratagem uses the
    same `.str10Wrap` markup as detachment stratagems. Keep this parser focused
    on that independent core-rules container so it cannot accidentally pull
    detachment stratagems from faction pages.
    """
    containers = soup.select(".Columns2, .Column2")
    if not containers:
        containers = [soup]

    stratagems = []
    seen = set()

    for container in containers:
        for wrap in container.select(":scope > .str10Wrap, .str10Wrap"):
            name = utils.clean_text(wrap.select_one(".str10Name"))
            if not name or name in seen:
                continue
            seen.add(name)

            text_el = wrap.select_one(".str10Text")

            stratagems.append({
                "name": name,
                "cp": utils.clean_text(wrap.select_one(".str10CP")),
                "type": strip_detachment_name_from_type(
                    utils.clean_text(wrap.select_one(".str10Type"))
                ),
                "when": extract_stratagem_field_block(text_el, "WHEN"),
                "target": extract_stratagem_field_block(text_el, "TARGET"),
                "effect": extract_stratagem_field_block(text_el, "EFFECT"),
                "restrictions": extract_stratagem_field_block(text_el, "RESTRICTIONS"),
                "icon_classes": extract_icon_classes(wrap),
                "color_class": extract_color_class(wrap),
            })

    return stratagems


def extract_core_stratagems_from_html(html, title="Core Stratagems"):
    soup = BeautifulSoup(html, "html.parser")
    return {
        "name": title,
        "faction": "Core Rules",
        "detachment": "Core Stratagems",
        "stratagems": extract_core_stratagems_from_soup(soup),
    }


def scrape_core_stratagems(page, url=CORE_RULES_URL):
    page.goto(url, wait_until="domcontentloaded")
    page.wait_for_selector(".Columns2 .str10Wrap, .Column2 .str10Wrap, .str10Wrap", timeout=30000)
    return extract_core_stratagems_from_html(page.content())


def parse_args():
    parser = argparse.ArgumentParser(description="Extract Wahapedia generic/core stratagems")
    parser.add_argument("--url", default=CORE_RULES_URL)
    parser.add_argument("--input-html", help="Use a saved core-rules HTML fragment/page instead of fetching Wahapedia")
    parser.add_argument("--output-json", default="core_stratagems.json")
    parser.add_argument("--headed", action="store_true")
    return parser.parse_args()


def main():
    args = parse_args()

    if args.input_html:
        html = Path(args.input_html).read_text(encoding="utf-8")
        data = extract_core_stratagems_from_html(html)
    else:
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
