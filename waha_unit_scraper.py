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
    "Imperial Agents": "Agents of the Imperium"
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

        mapping[value] = style_parser.normalize_faction_name(name)

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


def normalized_keyword_set(keywords):
    return {style_parser.normalize_faction_name(k) for k in keywords if k}


def extract_faction_name(soup, data, sub_faction_map):
    """Sub-faction (chapter, craftworld, Chaos god, ...) for a unit, or
    the parent faction if it doesn't belong to one specific sub-faction.

    The parent faction itself comes from the page's own FactionRules
    tooltip — the same lookup this used to fall back to — since it's
    always the parent, never a sub-faction.

    Step 1: whatever remains in Faction Keywords once the parent faction
    itself is removed is the sub-faction. Covers Space Marines (chapter
    keyword sits alongside "ADEPTUS ASTARTES") and Aeldari (just the
    craftworld keyword, no parent keyword at all).

    Step 2: Legiones Daemonica's sub-faction (a Chaos god) isn't a Faction
    Keyword at all — it's in the standard Keywords block, alongside the
    faction's own standard keywords like "CHAOS"/"DAEMON". Checked only
    when step 1 found nothing AND the parent faction is specifically
    Legiones Daemonica — not any faction with a sub-faction list — since a
    generic unit's ordinary standard keywords could otherwise
    coincidentally match some other faction's sub-faction name (e.g. a
    Space Marines chapter) with no such intent.

    Either step falling back covers both "no sub-faction" and "more than
    one candidate" (this shouldn't happen, but is a warning rather than a
    silent guess if it does).
    """
    node = soup.select_one('[data-tooltip-content="#tooltip_contentFactionRules"]')
    parent_faction = style_parser.normalize_faction_name(clean_text(node)) if node else ""

    faction_keywords = normalized_keyword_set(data.get("faction_keywords") or [])
    remaining = sorted(faction_keywords - {parent_faction})

    if len(remaining) == 1:
        return remaining[0]

    if len(remaining) > 1:
        print(
            f"WARNING: {data.get('name', 'unit')} has Faction Keywords "
            f"{sorted(faction_keywords)} leaving more than one candidate "
            f"sub-faction {remaining} after removing the parent faction "
            f"{parent_faction!r} — falling back to the parent faction."
        )
        return parent_faction

    if sub_faction_map and parent_faction == "LEGIONES DAEMONICA":
        standard_keywords = set()

        for group in data.get("keywords") or []:
            standard_keywords.update(group.get("keywords") or [])

        matches = sorted(normalized_keyword_set(standard_keywords) & set(sub_faction_map.values()))

        if len(matches) == 1:
            return matches[0]

        if len(matches) > 1:
            print(
                f"WARNING: {data.get('name', 'unit')} has standard Keywords "
                f"matching more than one sub-faction {matches} — falling "
                f"back to the parent faction."
            )

    return parent_faction


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


def extract_hunter_restriction(row):
    """Handles the newer `dsHunterKwRow` Wahapedia now renders between a
    split weapon profile's two rows, e.g. Beast Snagga Boyz' Choppa, which
    has a plain "Standard" profile and a "Hunter" profile restricted to
    targeting specific keywords:
        <tr class="dsHunterKwRow">...<span class="dsHunterKw">HUNTER:
        <span class="kwb">MONSTER/VEHICLE</span></span>...</tr>
    Returns the restriction keyword(s) (e.g. ["MONSTER/VEHICLE"]) with the
    "HUNTER:" label stripped, ready to attach to the weapon row that
    follows it — the ".kwb" is what actually names the target keyword(s);
    everything else in the row is just the static "HUNTER:" label.
    """
    value_node = row.select_one(".dsHunterKw .kwb")

    if value_node is None:
        return []

    return split_csv_value(clean_text(value_node))


def parse_weapon_profile_row(row, current_hit_key, pending_hunter_restriction):
    """Parses one `dsWeaponRow` into a profile dict. Returns None if the
    row doesn't actually carry weapon data (wrong cell count, blank name)."""
    cells = row.select("td")

    if len(cells) < 8:
        return None

    profile_marker = extract_weapon_profile_marker(cells[0])

    # Some units (e.g. Mek Gunz) assign different weapon options to
    # different models in the unit instead of a split-profile marker, e.g.
    # "1-2" meaning models 1-2 carry this option. That cell holds plain
    # text rather than a .dsPointy marker in that case.
    model_range = clean_text(cells[0]) if profile_marker is None else ""

    name_cell = cells[1]
    name, keywords = extract_weapon_name_and_keywords(name_cell)

    if not name:
        return None

    return {
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
        "hunter_restriction": pending_hunter_restriction,
    }


def extract_weapons(ds):
    """Groups weapon rows by their enclosing `<tbody>` — Wahapedia puts
    every profile of a single weapon (e.g. "Gutrippa - Standard" and
    "Gutrippa - Hunter") in one shared `<tbody class="bkg bkgN">`, and
    alternates that N (and so the row's background stripe) per weapon, not
    per profile row. Grouping this way — rather than the previous flat,
    one-entry-per-`dsWeaponRow` list — lets the renderer stripe backgrounds
    per weapon and only draw the "HUNTER: ..." separator between a
    weapon's own profiles, never between two different weapons.
    """
    weapons = []

    current_type = None
    current_hit_key = None

    table = ds.select_one(".wTable")
    if not table:
        return weapons

    for tbody in table.find_all("tbody", recursive=False):
        header_text = clean_text(tbody)

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

        tbody_classes = tbody.get("class", [])

        # Spacer between the ranged/melee sections; carries no data.
        if "dsWeaponsGap" in tbody_classes:
            continue

        # Empty placeholder Wahapedia renders right after each section
        # header purely to seed its background-stripe alternation (its
        # own bkgN value doesn't actually predict the first real weapon's
        # stripe — that always restarts at the "normal" stripe per
        # section) — no weapon data in here, safe to skip.
        if "bkg_reset" in tbody_classes:
            continue

        profiles = []
        pending_hunter_restriction = []

        for row in tbody.select("tr"):
            row_classes = row.get("class", [])

            # Names which keyword(s) the NEXT profile row in this same
            # tbody (a "Hunter" split profile) is restricted to targeting.
            if "dsHunterKwRow" in row_classes:
                pending_hunter_restriction = extract_hunter_restriction(row)
                continue

            # Generic boilerplate ("Before selecting targets for this
            # weapon, select one of its eligible profiles...") — lives in
            # its own trailing tbody once per table, not per weapon, so it
            # never actually reaches here as part of a weapon's profiles;
            # skipped defensively all the same.
            if "dsHunterKwNoteRow" in row_classes:
                continue

            # Duplicate long-name row used for responsive layout
            if "wTable2_long" in row_classes:
                continue

            profile = parse_weapon_profile_row(row, current_hit_key, pending_hunter_restriction)

            if profile is None:
                continue

            pending_hunter_restriction = []
            profiles.append(profile)

        if profiles:
            weapons.append({
                "type": current_type,
                "profiles": profiles,
            })

    return weapons


def extract_keyword_list_from_block(block, prefix, separators=",;"):
    """`separators` is a string of single characters, any of which splits
    one keyword from the next. The live page isn't consistent about using
    "," or ";" between keywords (both standard and Faction Keywords have
    been seen using either), so both are accepted by default rather than
    assuming one.
    """
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
        for part in re.split(f"[{re.escape(separators)}]", text)
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
            keywords = extract_keyword_list_from_block(child, "", ",;")

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
        "FACTION KEYWORDS:",
        ",;"
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

# Wahapedia has renamed these two-column wrapper classes at least once
# (old: Cyrillic "Сol" e.g. ".dsLeftСol" / ".dsRightСol"; current: Latin
# "ColFlat" e.g. ".dsLeftColFlat" / ".dsRightColFlat"). Try current names
# first, fall back to older ones so a partial site migration (or a layout
# variant that never got the rename) doesn't silently yield empty output.
LEFT_COL_SELECTORS = [".dsLeftColFlat", ".dsLeftСol"]
RIGHT_COL_SELECTORS = [".dsRightColFlat", ".dsRightСol"]


def select_first(ds, selectors):
    for selector in selectors:
        node = ds.select_one(selector)
        if node is not None:
            return node
    return None


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


def parse_core_army_table(table):
    """Handles the newer `.dsCoreArmy` badge table Wahapedia now renders
    in place of (or alongside) the old inline 'CORE: ...' / 'FACTION: ...'
    text lines, e.g.:
        CORE ABILITIES | Support
        ARMY RULES     | Waaagh!
    "CORE ABILITIES" rows feed the section's `core` list; anything else
    (currently just "ARMY RULES", but this also covers a "FACTION
    ABILITIES" label if Wahapedia ever renders one this way) feeds
    `faction`.
    """
    core_values = []
    faction_values = []

    for row in table.select("tr"):
        label_cell = row.select_one(".dsCoreArmyLabel")
        value_cell = row.select_one(".dsCoreArmyValue")

        if not label_cell or not value_cell:
            continue

        label = style_parser.clean_text(label_cell).upper()
        values = split_csv_value(style_parser.clean_text(value_cell))

        if "CORE" in label:
            core_values.extend(values)
        else:
            faction_values.extend(values)

    return core_values, faction_values


def extract_sections_from_container(container, styles, root_style):
    sections = []
    current = None
    pending_core = []
    pending_faction = []

    if not container:
        return sections

    for child in container.children:
        if not getattr(child, "get", None):
            continue

        classes = child.get("class", [])

        if child.name == "table" and "dsCoreArmy" in classes:
            core_vals, faction_vals = parse_core_army_table(child)

            if current is not None:
                current["core"].extend(core_vals)
                current["faction"].extend(faction_vals)
            else:
                pending_core.extend(core_vals)
                pending_faction.extend(faction_vals)

            continue

        if "dsHeader" in classes:
            title = style_parser.clean_text(child)

            if not should_keep_section(title):
                current = None
                continue

            current = {
                "title": title,
                "core": pending_core,
                "faction": pending_faction,
                "items": [],
            }
            pending_core = []
            pending_faction = []
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

    for container in (
        select_first(ds, LEFT_COL_SELECTORS),
        select_first(ds, RIGHT_COL_SELECTORS),
    ):
        sections.extend(
            extract_sections_from_container(container, styles, root_style)
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

        # An individual unit's page carries only the Chapter/sub-faction
        # filter select itself — not the extra widgets (Detachments, etc.)
        # a faction's main listing page has — so requiring more than one
        # match here (as before) discarded that one valid select on every
        # unit page and silently fell back to the generic FactionRules
        # tooltip, which just returns the parent faction name (e.g.
        # "Space Marines") regardless of the unit's actual chapter. Any
        # match at all is usable.
        unit_subfaction_map = build_sub_faction_map(selects[0]) if selects else {}

    soup, styles, root_style = style_parser.resolve_styled_content(locator)

    data = extract_all(soup, page, styles, root_style)

    # Resolved after extraction because it works from the unit's own
    # keywords.
    faction_name = extract_faction_name(full_soup, data, unit_subfaction_map)

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
