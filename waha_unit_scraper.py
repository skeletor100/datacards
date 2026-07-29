import re
import json
import argparse

from bs4 import BeautifulSoup, NavigableString, Tag
from playwright.sync_api import sync_playwright

import waha_scraper_common as style_parser


# =========================================================
# TEXT HELPERS (copied from waha_parse_utils.py — kept separate from
# style_parser.clean_text/clean_inline, which serve the new run-based
# content model; these serve the plain-data extraction below exactly as
# datacard_parser.py originally used them)
# =========================================================

def clean_text_from_string(value):
    if not value:
        return ""
    return re.sub(r"\s+", " ", str(value)).strip()


def clean_punctuation_spacing(text):
    text = clean_text_from_string(text)
    text = re.sub(r"\s+([,.;:])", r"\1", text)
    text = re.sub(r"([(\[])\s+", r"\1", text)
    text = re.sub(r"\s+([)\]])", r"\1", text)
    return text


def clean_text(element):
    if not element:
        return ""
    text = element.get_text(" ", strip=True)
    return " ".join(text.split())


FACTION_NAME_ALIASES = {
    "Space Marines": "Adeptus Astartes",
    "Chaos Daemons": "Legiones Daemonica",
    "Imperial Agents": "Agents of the Imperium",
}


def normalize_faction_name(name):
    name = str(name or "").strip()
    return FACTION_NAME_ALIASES.get(name, name).upper()


def get_filter_selects(soup):
    return [
        s
        for s in soup.find_all("select")
        if s.get("class") and any("FilterSelect" in c for c in s.get("class", []))
    ]


def build_sub_faction_map(select):
    no_filter_value = None
    mapping = {}

    for opt in select.find_all("option"):
        name = opt.get_text(strip=True)
        value = opt.get("value")

        if not value:
            continue

        name_lower = name.lower()

        if name_lower == "no filter":
            no_filter_value = value
            continue

        if name_lower in ("no supplement", "no supplements"):
            continue

        mapping[value] = normalize_faction_name(name)

    if not no_filter_value:
        return {}

    return {
        f"{no_filter_value}{value}": faction_name
        for value, faction_name in mapping.items()
    }


# =========================================================
# PAGE LOCATION + PURE-DATA EXTRACTION (copied from datacard_parser.py —
# this file doesn't depend on it, which is slated for removal. None of this
# carries raw Wahapedia CSS class names for text styling, so none of it was
# part of the problem the fresh parsing/style layer below addresses.)
# =========================================================

STAT_KEYS = ["M", "T", "Sv", "W", "Ld", "OC"]

# Deliberately a separate list from datacard_parser.EXCLUDED_SECTION_TITLES,
# not just copied verbatim: TRANSPORT capacity/rules are actually useful to
# know during a game, unlike the other excluded sections (composition/
# points/leader-attachment bookkeeping), so this parser keeps it.
EXCLUDED_SECTION_TITLES = {
    "UNIT COMPOSITION",
    "LEADER",
    "ATTACHED UNIT",
    "SUPREME COMMANDER",
    "DEDICATED TRANSPORT",
    "POINTS",
    "WARGEAR OPTIONS",
    "SUPPORT",
    "MASTERS OF THE MAELSTROM",
    "HEROES OF ULTRAMAR"
}

DATASHEET_XPATH = (
    "xpath=//*[contains(concat(' ', normalize-space(@class), ' '), ' datasheet ')]"
    "[.//*[contains(concat(' ', normalize-space(@class), ' '), ' dsH2Header ')]]"
)


def locate_datacard(page):
    locator = page.locator(DATASHEET_XPATH).first
    locator.wait_for(state="visible", timeout=30000)
    return locator


class _ClassOnly:
    """Minimal stand-in so extract_faction_name's `ds.get("class", [])`
    check works against a class list read directly off the live locator,
    without needing the datasheet's own wrapping tag (lost once we only
    have its innerHTML from resolve_styled_content)."""

    def __init__(self, classes):
        self._classes = classes

    def get(self, key, default=None):
        return self._classes if key == "class" else default


def extract_faction_name(soup, ds, sub_faction_map):
    datasheet_classes = set(ds.get("class", []))

    match = None

    for class_name, faction_name in sub_faction_map.items():
        if class_name not in datasheet_classes:
            continue

        if match is not None:
            # Generic unit - belongs to the parent faction.
            match = None
            break

        match = faction_name

    if match is not None:
        return match

    # Fall back to the existing implementation.
    node = soup.select_one('[data-tooltip-content="#tooltip_contentFactionRules"]')

    if not node:
        return ""

    return normalize_faction_name(clean_text(node))


def extract_datacard(soup):
    candidates = soup.find_all(class_=lambda c: c and "datasheet" in c)

    for ds in candidates:
        if ds.select_one(".dsH2Header"):
            return ds

    raise Exception("Could not find valid unit datacard")


def extract_name(ds):
    node = ds.select_one(".dsH2Header div")
    return clean_text(node)


def extract_profiles(ds):
    profiles = []

    profile_blocks = ds.select(".dsProfileBaseWrap")

    for index, block in enumerate(profile_blocks):
        values = [
            clean_text(x)
            for x in block.select(".dsCharValue")
        ]

        if len(values) < 6:
            continue

        name_node = block.select_one(".dsModelName")
        profile_name = clean_text(name_node)

        if not profile_name:
            profile_name = extract_name(ds)

        stats = {
            key: values[i]
            for i, key in enumerate(STAT_KEYS)
            if i < len(values)
        }

        invuln = ""
        invulnComment = ""

        next_node = block.find_next_sibling()

        while next_node:
            if getattr(next_node, "get", None):
                classes = next_node.get("class", [])

                if "dsInvulWrap" in classes:
                    invuln = clean_text(
                        next_node.select_one(".dsCharInvulValue")
                    )

                if "dsInvulComment" in classes:
                    invulnComment = clean_text(
                        next_node
                    )

                if "dsProfileBaseWrap" in classes:
                    break

            next_node = next_node.find_next_sibling()

        profiles.append({
            "name": profile_name,
            "stats": stats,
            "invulnerable_save": invuln,
            "invulnerable_save_comment": invulnComment
        })

    return profiles


def extract_weapon_name_and_keywords(name_cell):
    # .kwbw, not the older .kwb2 — Wahapedia renamed this class site-wide
    # for wh40k11ed (confirmed: .kwb2 appears zero times anywhere on a real
    # unit datasheet page now, while .kwbw carries exactly the same role —
    # a weapon-ability annotation like "blast"/"pistol"/"devastating
    # wounds" next to the weapon's name). Keeping the old class name here
    # meant every weapon's keyword list silently came back empty and its
    # name included the un-stripped annotation text.
    keyword_nodes = name_cell.select(".kwbw")

    keywords = [
        clean_text(node)
        for node in keyword_nodes
        if clean_text(node)
    ]

    # Remove keyword nodes so only the weapon name remains
    cell_copy = BeautifulSoup(str(name_cell), "html.parser")
    for node in cell_copy.select(".kwbw"):
        node.decompose()

    name = clean_text(cell_copy)

    return name, keywords


PROFILE_MARKER_IGNORED_CLASSES = {
    "tooltip",
    "tooltip_",
    "tooltipstered",
    "showShort2",
    "hideShort2",
}


def extract_weapon_profile_marker(marker_cell):
    marker = marker_cell.select_one(".dsPointy")
    if not marker:
        return None

    classes = [
        cls
        for cls in marker.get("class", [])
        if cls not in PROFILE_MARKER_IGNORED_CLASSES
    ]

    return {
        "source_tag": marker.name,
        "classes": classes,
        "style": marker.get("style", ""),
    }


def extract_weapons(ds):
    weapons = []

    current_type = None
    current_hit_key = None

    table = ds.select_one(".wTable")
    if not table:
        return weapons

    for row in table.select("tr"):
        header_text = clean_text(row)

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

        # Ignore the duplicate long-name rows used for responsive layout
        if "wTable2_long" in row.get("class", []):
            continue

        cells = row.select("td")

        if len(cells) < 8:
            continue

        profile_marker = extract_weapon_profile_marker(cells[0])

        # Some units (e.g. Mek Gunz) assign different weapon options to
        # different models in the unit instead of a split-profile marker,
        # e.g. "1-2" meaning models 1-2 carry this option. That cell holds
        # plain text rather than a .dsPointy marker in that case.
        model_range = clean_text(cells[0]) if profile_marker is None else ""

        name_cell = cells[1]
        name, keywords = extract_weapon_name_and_keywords(name_cell)

        if not name:
            continue

        weapon = {
            "type": current_type,
            "name": name,
            "keywords": keywords,
            "is_profile": profile_marker is not None,
            "profile_marker": profile_marker,
            "models": model_range,
            "range": clean_text(cells[2]),
            "A": clean_text(cells[3]),
            current_hit_key: clean_text(cells[4]),
            "S": clean_text(cells[5]),
            "AP": clean_text(cells[6]),
            "D": clean_text(cells[7]),
        }

        weapons.append(weapon)

    return weapons


def extract_keyword_list_from_block(block, prefix):
    if not block:
        return []

    block_copy = BeautifulSoup(str(block), "html.parser")

    for hidden in block_copy.find_all(style=lambda s: s and "display:none" in s.replace(" ", "").lower()):
        hidden.decompose()

    text = clean_punctuation_spacing(block_copy.get_text(" ", strip=True))

    if text.upper().startswith(prefix):
        text = text[len(prefix):].strip()

    return [
        clean_punctuation_spacing(part)
        for part in text.split(",")
        if clean_punctuation_spacing(part)
    ]


def extract_keywords(ds):
    block = ds.select_one(".dsLeftСolKW")

    if not block:
        return []

    sections = []

    current = {
        "applies_to": None,
        "keywords": []
    }

    pending_label = ""

    for child in block.children:

        if isinstance(child, NavigableString):
            text = clean_punctuation_spacing(str(child))

            if not text:
                continue

            if ":" in text:
                pending_label = text.split(":", 1)[0].replace("KEYWORDS", "").replace("–", "").strip()

        elif getattr(child, "name", None) == "span":

            # separator between keyword groups
            if "dsVertLine" in child.get("class", []):
                if current["keywords"]:
                    sections.append(current)

                current = {
                    "applies_to": None,
                    "keywords": []
                }
                continue

            # actual keyword span
            keywords = extract_keyword_list_from_block(child, "")

            if pending_label:
                current["applies_to"] = pending_label
                pending_label = ""

            current["keywords"].extend(keywords)

    if current["keywords"]:
        sections.append(current)

    return sections


def extract_faction_keywords(ds):
    return extract_keyword_list_from_block(
        ds.select_one(".dsRightСolKW"),
        "FACTION KEYWORDS:"
    )


def split_csv_value(value):
    value = clean_punctuation_spacing(value)
    return [
        item.strip()
        for item in value.split(",")
        if item.strip()
    ]


def extract_colour_classes(ds):
    found = set()

    for el in ds.find_all(class_=True):
        for cls in el.get("class", []):
            if cls.startswith("dsColor"):
                found.add(cls)

    return sorted(found)


def extract_theme(ds, page):
    colour_classes = extract_colour_classes(ds)

    raw = page.evaluate(
        """
        (classes) => {
            const result = {};

            for (const cls of classes) {
                const el = document.createElement("div");
                el.className = cls;
                document.body.appendChild(el);

                const style = window.getComputedStyle(el);

                result[cls] = {
                    color: style.color,
                    background: style.backgroundColor,
                    border: style.borderColor
                };

                el.remove();
            }

            return result;
        }
        """,
        colour_classes
    )

    theme = {}

    for cls, values in raw.items():
        if cls.startswith("dsColorBan"):
            theme["banner"] = values["background"]

        elif cls.startswith("dsColorBg"):
            theme["background"] = values["background"]

        elif cls.startswith("dsColorFr"):
            theme["frame"] = values["border"]

        elif cls.startswith("dsColor"):
            theme["text"] = values["color"]

    return theme


# =========================================================
# ABILITY / SECTION CONTENT (fresh parsing/style/storage layer)
# =========================================================

def should_keep_section(title):
    return title.upper() not in EXCLUDED_SECTION_TITLES


def parse_ability_section_item(node, styles, root_style):
    text = style_parser.clean_text(node)

    if text.upper().startswith("CORE:"):
        return {"kind": "core", "values": split_csv_value(text.split(":", 1)[1])}

    if text.upper().startswith("FACTION:"):
        return {"kind": "faction", "values": split_csv_value(text.split(":", 1)[1])}

    return {
        "kind": "items",
        "blocks": style_parser.extract_content_blocks([node], styles, root_style),
    }


def extract_sections_from_container(container, styles, root_style):
    sections = []
    current = None

    if not container:
        return sections

    for child in container.children:
        if not getattr(child, "get", None):
            continue

        classes = child.get("class", [])

        if "dsHeader" in classes:
            title = style_parser.clean_text(child)

            if not should_keep_section(title):
                current = None
                continue

            current = {
                "title": title,
                "core": [],
                "faction": [],
                "items": [],
            }
            sections.append(current)

        elif "dsAbility" in classes:
            if current is None:
                continue

            parsed = parse_ability_section_item(child, styles, root_style)

            if parsed["kind"] == "core":
                current["core"].extend(parsed["values"])
            elif parsed["kind"] == "faction":
                current["faction"].extend(parsed["values"])
            elif parsed["kind"] == "items":
                current["items"].extend(parsed["blocks"])

        elif child.name == "ul":
            if current is None:
                continue

            current["items"].extend(
                style_parser.extract_content_blocks([child], styles, root_style)
            )

    return sections


def extract_sections(ds, styles, root_style):
    sections = []

    for selector in [".dsLeftСol", ".dsRightСol"]:
        sections.extend(
            extract_sections_from_container(ds.select_one(selector), styles, root_style)
        )

    return [
        section for section in sections
        if section["items"] or section["core"] or section["faction"]
    ]


def extract_weapon_abilities(ds, styles, root_style):
    table = ds.select_one(".dsLeftСol .wTable")
    if not table:
        return []

    nodes = []
    sibling = table.find_next_sibling()

    while sibling:
        if isinstance(sibling, Tag):
            classes = sibling.get("class", [])

            # Weapon notes live immediately after the table and its separator.
            # Stop once a conventional headed section or another table begins.
            if "dsHeader" in classes or sibling.name == "table":
                break

            nodes.append(sibling)

        sibling = sibling.find_next_sibling()

    return style_parser.extract_content_blocks(nodes, styles, root_style)


def extract_all(ds, page, styles, root_style):
    return {
        "name": extract_name(ds),
        "profiles": extract_profiles(ds),
        "weapons": extract_weapons(ds),
        "weapon_abilities": extract_weapon_abilities(ds, styles, root_style),
        "sections": extract_sections(ds, styles, root_style),
        "keywords": extract_keywords(ds),
        "faction_keywords": extract_faction_keywords(ds),
        "theme": extract_theme(ds, page),
    }


# =========================================================
# PIPELINE
# =========================================================

def run(page, url, unit_subfaction_map=None):
    page.set_viewport_size({"width": 1600, "height": 2000})
    page.goto(url, wait_until="domcontentloaded")

    locator = locate_datacard(page)

    page.wait_for_selector(".dsRightСolKW")
    locator.locator(".dsRightСolKW").last.wait_for(state="visible")

    page.wait_for_function("""
    () => {
        const first = document.querySelector('.dsH2Header');
        const last = document.querySelector('.dsRightСolKW');
        if (!first || !last) return false;

        const r1 = first.getBoundingClientRect();
        const r2 = last.getBoundingClientRect();

        window.__crop_state = window.__crop_state || [];

        const snapshot = [r1.x, r1.y, r2.x, r2.y, r2.width, r2.height].join(',');

        window.__crop_state.push(snapshot);

        if (window.__crop_state.length > 3) window.__crop_state.shift();

        return window.__crop_state.length === 3 &&
            window.__crop_state.every(s => s === snapshot);
    }
    """)

    page.add_style_tag(content="""
    *   {
            animation: none !important;
            transition: none !important;
        }
    """)

    # Page-level concerns (sub-faction filter dropdowns, the faction-name
    # tooltip fallback) need the whole page, not just the datasheet subtree.
    full_soup = BeautifulSoup(page.content(), "html.parser")

    if not unit_subfaction_map:
        selects = get_filter_selects(full_soup)

        if len(selects) > 1:
            unit_subfaction_map = build_sub_faction_map(selects[0])
        else:
            unit_subfaction_map = {}

    ds_classes = locator.evaluate("el => Array.from(el.classList)")
    faction_name = extract_faction_name(full_soup, _ClassOnly(ds_classes), unit_subfaction_map)

    soup, styles, root_style = style_parser.resolve_styled_content(locator)

    data = extract_all(soup, page, styles, root_style)

    return data, faction_name


def parse_args():
    parser = argparse.ArgumentParser(description="Wahapedia datacard extractor (v2 parsing)")
    parser.add_argument("--url", help="The url for the unit")
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        data, faction_name = run(page, args.url)
        browser.close()

    output_path = f"{faction_name}_{data.get('name')}.json"

    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=4, ensure_ascii=False)

    print(f"Saved JSON: {output_path}")
