import os
import json
import argparse

from bs4 import BeautifulSoup, NavigableString, Tag
from playwright.sync_api import sync_playwright

import waha_scraper_common as style_parser
from waha_unit_scraper import extract_weapon_name_and_keywords


# =========================================================
# PAGE LOCATION (tested infra, copied rather than imported — this file
# doesn't depend on detachment_scraper.py, which is slated for removal)
# =========================================================

def xpath_string_literal(value):
    """Return a safely quoted XPath string literal."""
    if "'" not in value:
        return f"'{value}'"

    if '"' not in value:
        return f'"{value}"'

    parts = value.split("'")
    return "concat(" + ', "\'", '.join(
        f"'{part}'" for part in parts
    ) + ")"


def extract_detachment_block(page, detachment_anchor):
    anchor_value = xpath_string_literal(detachment_anchor)

    anchor = page.locator(
        f"xpath=//a[@name={anchor_value}]"
    ).first

    anchor.wait_for(state="attached", timeout=30000)

    block = anchor.locator(
        "xpath=ancestor::div[contains("
        "concat(' ', normalize-space(@class), ' '), "
        "' clFl '"
        ")][1]"
    )

    if block.count() == 0:
        raise Exception(
            f"Containing detachment block not found: {detachment_anchor}"
        )

    return block


# =========================================================
# DETACHMENT CONTENT (fresh parsing/style/storage layer)
# =========================================================

def extract_detachment_rules(soup, styles, root_style):
    rules = []

    for heading in soup.select("h3"):
        if heading.find_parent(class_="str10Wrap") or heading.find_parent(class_="str11Wrap"):
            continue

        block = heading.find_parent("div", class_="BreakInsideAvoid")
        if not block:
            continue

        content_nodes = []

        for node in heading.next_siblings:
            if isinstance(node, Tag) and node.name in ("h2", "h3"):
                break

            # Whitespace-only text nodes must never be dropped here: they are
            # the only thing separating adjacent same-styled spans (e.g.
            # <span class="kwb">ADEPTUS</span> <span class="kwb">ASTARTES</span>),
            # and merge_runs relies on seeing them to know a space belongs
            # between two runs it's about to fold together.
            if style_parser.is_ignorable(node) and not isinstance(node, NavigableString):
                continue

            if isinstance(node, Tag) and node.select_one(".impact18"):
                subrule = style_parser.extract_subrule_from_table(node, styles, root_style)
                if subrule:
                    content_nodes.append(subrule)
                continue

            content_nodes.append(node)

        # extract_content_blocks expects DOM nodes, but subrules are already
        # parsed dicts by the time they reach here.
        content = []
        raw_nodes = []

        for node in content_nodes:
            if isinstance(node, dict):
                content.extend(style_parser.extract_content_blocks(raw_nodes, styles, root_style))
                raw_nodes = []
                content.append(node)
            else:
                raw_nodes.append(node)

        content.extend(style_parser.extract_content_blocks(raw_nodes, styles, root_style))

        if content:
            rules.append({
                "name": style_parser.clean_text(heading),
                "content": content,
                "heading": style_parser.resolve_element_style(heading, styles, root_style),
            })

    return rules


def extract_enhancement_weapon_profile(container):
    """New this edition: an enhancement can grant a specific weapon profile
    (e.g. Space Marines' "Orksbane" in the Vengeful Hosts detachment,
    Detachment-Rule-46), marked up with the same .wTable structure a unit
    datasheet's own weapon table uses — RANGED/MELEE WEAPONS header row,
    then a row per profile — just without the leading split-profile marker
    column every datasheet row has, since there's only ever the one
    profile here. Returns None if this enhancement has no such table
    (the normal case).
    """
    table = container.select_one(".wTable")
    if not table:
        return None

    current_type = None
    current_hit_key = None

    for row in table.select("tr"):
        header_text = style_parser.clean_text(row).upper()

        if "RANGED WEAPONS" in header_text:
            current_type = "ranged"
            current_hit_key = "BS"
            continue

        if "MELEE WEAPONS" in header_text:
            current_type = "melee"
            current_hit_key = "WS"
            continue

        if not current_type:
            continue

        # Same duplicate responsive-layout row unit datasheets have.
        if "wTable2_long" in (row.get("class") or []):
            continue

        cells = row.select("td")
        if len(cells) < 7:
            continue

        name, keywords = extract_weapon_name_and_keywords(cells[0])
        if not name:
            continue

        return {
            "type": current_type,
            "name": name,
            "keywords": keywords,
            "range": style_parser.clean_text(cells[1]),
            "A": style_parser.clean_text(cells[2]),
            current_hit_key: style_parser.clean_text(cells[3]),
            "S": style_parser.clean_text(cells[4]),
            "AP": style_parser.clean_text(cells[5]),
            "D": style_parser.clean_text(cells[6]),
        }

    return None


def extract_enhancement_content_nodes(container):
    """The enhancement's ability text, in source order — deliberately NOT
    just container.find_all("p"). An enhancement's bullet list (e.g.
    Celerity's two advance/fall-back-move bullets) is real markup written as
    <p>...text...<ul><li>...</li></ul></p>, but a <ul> isn't valid content
    inside a <p> per HTML5, so the browser auto-closes the <p> right before
    it — by the time resolve_styled_content's browser-parsed DOM reaches
    here, the <ul> is a SIBLING of the <p>, not its child, and searching only
    for <p> tags silently dropped it entirely. Walking the table cell's
    direct children instead (skipping the name/points line, fluff text, and
    any weapon-profile table already handled separately) picks up whatever
    block types are actually present, matching how extract_content_blocks
    handles sibling <ul>/<ol>/<table> elsewhere.
    """
    td = container.select_one("td.td_w") or container

    nodes = []
    for child in td.children:
        if isinstance(child, Tag):
            if "EnhancementsPts" in (child.get("class") or []):
                continue
            if style_parser.is_fluff(child):
                continue
            if child.select_one(".wTable") or "wTable" in (child.get("class") or []):
                continue
        nodes.append(child)

    return nodes


def extract_enhancements(soup, styles, root_style):
    # soup is already scoped to a single detachment block (see
    # extract_detachment_data), so querying directly is safe. We used to
    # first narrow down to the <a name="Enhancements-N"> anchor's
    # BreakInsideAvoid ancestor, but that wrapper isn't consistently present
    # site-wide (e.g. Space Marines' Librarius Conclave has no
    # BreakInsideAvoid around the anchor at all, even though each
    # ul.EnhancementsPts item still has its own further down) — that extra
    # scoping step could only fail, never actually helped.
    enhancements = []

    for item in soup.select("ul.EnhancementsPts li"):
        spans = item.find_all("span", recursive=False)
        name_span = spans[0] if spans else item

        # New this edition: some enhancements carry a small "UPGRADE" badge
        # right next to their name (<span>Symphonic Payload<span
        # class="EnhUpgrade">UPGRADE</span></span>) — a fixed style (not a
        # per-faction colour like the dsColorBgXX/dsColorGradXX headings),
        # so a hardcoded CSS class for it is fine, unlike those. Without
        # extracting it separately, clean_text() below would just fold its
        # text into the plain name string, indistinguishable from the rest
        # once rendered inside the (already bold/uppercase) enhancement
        # name — which is exactly what "thrown away, shown as plain bold"
        # looked like: the text survived, but its own distinct label/badge
        # styling didn't.
        upgrade_el = name_span.select_one(".EnhUpgrade")
        upgrade_label = style_parser.clean_text(upgrade_el) if upgrade_el else None

        name_copy = BeautifulSoup(str(name_span), "html.parser")
        for node in name_copy.select(".EnhUpgrade"):
            node.decompose()
        name = style_parser.clean_text(name_copy)

        container = item.find_parent("div", class_="BreakInsideAvoid")
        if not container:
            continue

        content_nodes = extract_enhancement_content_nodes(container)

        content = style_parser.extract_content_blocks(content_nodes, styles, root_style)
        weapon_profile = extract_enhancement_weapon_profile(container)

        enhancement = {
            "name": name,
            "upgrade_label": upgrade_label,
            "content": content,
        }
        if weapon_profile:
            enhancement["weapon_profile"] = weapon_profile

        enhancements.append(enhancement)

    return enhancements


def extract_stratagems(soup, styles, root_style):
    # See style_parser.extract_stratagem_from_wrap: handles both the older
    # .str10Wrap markup (wh40k10ed) and the newer .str11Wrap markup
    # (wh40k11ed), auto-detected per wrap.
    stratagems = []

    for wrap in soup.select(".str10Wrap, .str11Wrap"):
        stratagem = style_parser.extract_stratagem_from_wrap(wrap, styles, root_style)
        if stratagem:
            stratagems.append(stratagem)

    return stratagems


def extract_detachment_data(detachment_block, faction_name, detachment_name):
    soup, styles, root_style = style_parser.resolve_styled_content(detachment_block)

    # Wahapedia markup mistake (seen on Death Guard): "Paragons of
    # Putrescence"'s own div.clFl block is nested INSIDE "Flyblown Host"'s
    # div.clFl block instead of sitting beside it, so Flyblown Host's
    # otherwise-correctly-scoped block also contains Paragons' entire
    # subtree — rules/enhancements/stratagems extraction below just query
    # this whole soup, so without this they'd silently absorb Paragons'
    # content into Flyblown Host's own results. A genuine detachment block
    # never contains another div.clFl (verified against several
    # well-formed ones), so any found here can only be this kind of
    # erroneous nesting — safe to drop before extracting anything.
    # Paragons of Putrescence itself is scraped independently via its own
    # anchor, so removing it here doesn't lose it, just this duplicate.
    for nested in soup.select("div.clFl"):
        nested.decompose()

    return {
        "faction": faction_name,
        "detachment": detachment_name,
        "rules": extract_detachment_rules(soup, styles, root_style),
        "enhancements": extract_enhancements(soup, styles, root_style),
        "stratagems": extract_stratagems(soup, styles, root_style),
    }


# =========================================================
# PIPELINE
# =========================================================

def run(page, faction_name, detachment_name, detachment_anchor):
    detachment_block = extract_detachment_block(page, detachment_anchor)

    detachment_block.wait_for(state="visible", timeout=30000)

    data = extract_detachment_data(detachment_block, faction_name, detachment_name)

    print(f"Faction: {faction_name} | Detachment: {detachment_name}")

    return data


# =========================================================
# ENTRY
# =========================================================

def parse_args():
    parser = argparse.ArgumentParser(description="Scrape Wahapedia detachment data (v2 parsing)")

    parser.add_argument("--url", required=True, help="Wahapedia page URL")
    parser.add_argument("--faction", required=True, help="Faction name used for output directory")
    parser.add_argument("--detachment", required=True, help="Detachment anchor name (eg Gladius-Task-Force)")

    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()

        page.goto(args.url, wait_until="domcontentloaded")

        data = run(
            page=page,
            faction_name=args.faction,
            detachment_name=args.detachment,
            detachment_anchor=args.detachment,
        )

        output_path = f"./{args.faction}/{args.detachment}.json"

        os.makedirs(os.path.dirname(output_path), exist_ok=True)
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)

        print(f"Wrote detachment data to {output_path}")

        browser.close()
