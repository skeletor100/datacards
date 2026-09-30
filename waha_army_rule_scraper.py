import json
import argparse

from bs4 import Tag
from playwright.sync_api import sync_playwright

import waha_scraper_common as style_parser


# =========================================================
# PAGE LOCATION
#
# Army Rules sections look like:
#   <a name="Army-Rules"></a>
#   <h2 class="outline_header">Army Rules</h2>
#   <div class="Columns2">...</div>
#
# The original (BeautifulSoup-only) version walked the anchor's siblings
# looking for the first Columns2/BreakInsideAvoid div, stopping early if it
# hit an actual (non-outline) heading first. Style resolution needs a live
# Playwright locator to stamp computed style onto, not just a parsed static
# snapshot, so this is expressed as an XPath equivalent instead: the first
# matching div sibling of the anchor. Verified against real faction pages
# to behave the same in practice (see testing).
# =========================================================

CONTAINER_XPATH_TEMPLATE = (
    "xpath=//a[@name={anchor}]/following-sibling::div"
    "[contains(concat(' ', normalize-space(@class), ' '), ' Columns2 ')"
    " or contains(concat(' ', normalize-space(@class), ' '), ' BreakInsideAvoid ')][1]"
)


def _xpath_string_literal(value):
    """Return a safely quoted XPath string literal."""
    if "'" not in value:
        return f"'{value}'"

    if '"' not in value:
        return f'"{value}"'

    parts = value.split("'")
    return "concat(" + ', "\'", '.join(
        f"'{part}'" for part in parts
    ) + ")"


def locate_army_rules_container(page, section_anchor):
    anchor_value = _xpath_string_literal(section_anchor)
    locator = page.locator(
        CONTAINER_XPATH_TEMPLATE.format(anchor=anchor_value)
    ).first

    if locator.count() == 0:
        raise Exception(
            f"Could not locate Army Rules content for '{section_anchor}'."
        )

    return locator


def _is_rule_heading(node):
    return (
        isinstance(node, Tag)
        and node.name in ("h2", "h3", "h4")
        and "outline_header" not in node.get("class", [])
        and not node.find_parent(class_="str10Wrap")
    )


def _contains_rule_heading(node):
    if not isinstance(node, Tag):
        return False
    return any(_is_rule_heading(h) for h in node.find_all(["h2", "h3", "h4"]))


# =========================================================
# ARMY RULE CONTENT (fresh parsing/style/storage layer)
# =========================================================

def extract_rule_cards(container, styles, root_style):
    rules = []

    headings = [
        h for h in container.find_all(["h2", "h3", "h4"])
        if not (
            "outline_header" in h.get("class", [])
            or h.find_parent(class_="str10Wrap")
        )
    ]

    for heading in headings:
        content_nodes = []

        for node in heading.next_siblings:
            if _is_rule_heading(node) or _contains_rule_heading(node):
                break

            if style_parser.is_ignorable(node) and not style_parser.is_br(node):
                continue

            content_nodes.append(node)

        content = style_parser.extract_content_blocks(content_nodes, styles, root_style)

        if not content:
            continue

        rules.append({
            "name": style_parser.clean_text(heading),
            "content": content,
            "heading": style_parser.resolve_element_style(heading, styles, root_style),
        })

    return rules


# =========================================================
# PIPELINE
# =========================================================

def run(page, section_anchor):
    """
    Extract one Army Rules section from the current Wahapedia faction page.
    Assumes the page has already navigated to the faction page — this
    doesn't do its own navigation, matching the original contract.

    Example:
      run(page, "Army-Rules")
      run(page, "Army-Rules-2")
    """
    container = locate_army_rules_container(page, section_anchor)

    soup, styles, root_style = style_parser.resolve_styled_content(container)

    # Wahapedia now shows a collapsed "FAQ / Errata" spoiler block after
    # many Army Rules sections — a heading plus a "Show" toggle, with the
    # real errata text hidden until clicked. extract_rule_cards has no
    # reason to know about it specifically (it isn't excluded by the
    # outline_header/str10Wrap checks _is_rule_heading already applies),
    # so without this it gets picked up as if it were a genuine rule of
    # its own (name "FAQ / Errata", content ["Show"]). Decomposing it here
    # — before any heading-walking below — also matters more than it might
    # look: merely excluding its heading from _is_rule_heading wouldn't be
    # enough on its own, since extract_rule_cards gathers a rule's content
    # by walking next_siblings until the next rule heading, so the "Show"
    # toggle text would just get silently absorbed into whichever real
    # rule precedes it instead of disappearing.
    for spoiler in soup.select(".faqErrataSpoiler"):
        spoiler.decompose()

    return extract_rule_cards(soup, styles, root_style)


def parse_args():
    parser = argparse.ArgumentParser(description="Wahapedia army rules extractor (v2 parsing)")
    parser.add_argument("--url", required=True, help="Faction page URL")
    parser.add_argument("--anchor", required=True, help="Army Rules anchor name, e.g. Army-Rules")
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(args.url, wait_until="domcontentloaded")
        data = run(page, args.anchor)
        browser.close()

    output_path = f"{args.anchor}.json"

    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=4, ensure_ascii=False)

    print(f"Saved JSON: {output_path}")
