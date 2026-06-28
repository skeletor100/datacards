import sys
import json
import re
import os
import argparse

from bs4 import BeautifulSoup, NavigableString, Tag
import waha_parse_utils as utils
from playwright.sync_api import sync_playwright


STAT_KEYS = ["M", "T", "Sv", "W", "Ld", "OC"]

EXCLUDED_SECTION_TITLES = {
    "UNIT COMPOSITION",
    "LEADER",
    "ATTACHED UNIT",
    "SUPREME COMMANDER",
    "DEDICATED TRANSPORT",
    "TRANSPORT",
    "POINTS",
    "WARGEAR OPTIONS",
}

# =========================================================
# TEXT CLEANING (UNCHANGED)
# =========================================================


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

    return utils.normalize_faction_name(utils.clean_text(node))

def extract_datacard(soup):
    candidates = soup.find_all(class_=lambda c: c and "datasheet" in c)

    for ds in candidates:
        if ds.select_one(".dsH2Header"):
            return ds

    raise Exception("Could not find valid unit datacard")

def extract_name(ds):
    node = ds.select_one(".dsH2Header div")
    return utils.clean_text(node)


def extract_profiles(ds):
    profiles = []

    profile_blocks = ds.select(".dsProfileBaseWrap")

    for index, block in enumerate(profile_blocks):
        values = [
            utils.clean_text(x)
            for x in block.select(".dsCharValue")
        ]

        if len(values) < 6:
            continue

        name_node = block.select_one(".dsModelName")
        profile_name = utils.clean_text(name_node)

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
                    invuln = utils.clean_text(
                        next_node.select_one(".dsCharInvulValue")
                    )

                if "dsInvulComment" in classes:
                    invulnComment = utils.clean_text(
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
    keyword_nodes = name_cell.select(".kwb2")

    keywords = [
        utils.clean_text(node)
        for node in keyword_nodes
        if utils.clean_text(node)
    ]

    # Remove keyword nodes so only the weapon name remains
    cell_copy = BeautifulSoup(str(name_cell), "html.parser")
    for node in cell_copy.select(".kwb2"):
        node.decompose()

    name = utils.clean_text(cell_copy)

    return name, keywords


def extract_weapons(ds):
    weapons = []

    current_type = None
    current_hit_key = None

    table = ds.select_one(".wTable")
    if not table:
        return weapons

    for row in table.select("tr"):
        header_text = utils.clean_text(row)

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

        name_cell = cells[1]
        name, keywords = extract_weapon_name_and_keywords(name_cell)

        if not name:
            continue

        weapon = {
            "type": current_type,
            "name": name,   
            "keywords": keywords,
            "range": utils.clean_text(cells[2]),
            "A": utils.clean_text(cells[3]),
            current_hit_key: utils.clean_text(cells[4]),
            "S": utils.clean_text(cells[5]),
            "AP": utils.clean_text(cells[6]),
            "D": utils.clean_text(cells[7]),
        }

        weapons.append(weapon)

    return weapons


def extract_sectioned_blocks(container):
    sections = []
    current = None

    if not container:
        return sections

    for child in container.children:
        if not getattr(child, "get", None):
            continue

        classes = child.get("class", [])

        if "dsHeader" in classes:
            current = {
                "title": utils.clean_text(child),
                "items": []
            }
            sections.append(current)

        elif "dsAbility" in classes:
            if current is None:
                current = {
                    "title": "",
                    "items": []
                }
                sections.append(current)

            current["items"].append(utils.clean_text(child))

        elif child.name == "ul":
            if current is None:
                current = {
                    "title": "",
                    "items": []
                }
                sections.append(current)

            current["items"].append(utils.clean_text(child))

    return sections


def is_hidden(node):
    style = (node.get("style") or "").lower()
    return "display:none" in style.replace(" ", "")


def extract_keyword_list_from_block(block, prefix):
    if not block:
        return []

    block_copy = BeautifulSoup(str(block), "html.parser")

    for hidden in block_copy.find_all(style=lambda s: s and "display:none" in s.replace(" ", "").lower()):
        hidden.decompose()

    text = utils.clean_punctuation_spacing(block_copy.get_text(" ", strip=True))

    if text.upper().startswith(prefix):
        text = text[len(prefix):].strip()

    return [
        utils.clean_punctuation_spacing(part)
        for part in text.split(",")
        if utils.clean_punctuation_spacing(part)
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
            text = utils.clean_punctuation_spacing(str(child))

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


def should_keep_section(title):
    return title.upper() not in EXCLUDED_SECTION_TITLES




def split_csv_value(value):
    value = utils.clean_punctuation_spacing(value)
    return [
        item.strip()
        for item in value.split(",")
        if item.strip()
    ]








def extract_named_abilities(node):

    abilities = []

    current_title = None
    current_nodes = []

    def flush():
        nonlocal current_title, current_nodes

        if current_title:

            abilities.append({
                "title": current_title.rstrip(":"),
                "content": utils.extract_content_blocks(current_nodes)
            })

        current_title = None
        current_nodes = []

    for child in node.children:

        if isinstance(child, Tag) and child.name == "b":
            flush()
            current_title = utils.clean_punctuation_spacing(
                child.get_text(" ", strip=True)
            )
            continue

        if (
            isinstance(child, Tag)
            and "dsLineHor" in child.get("class", [])
        ):
            flush()
            continue

        current_nodes.append(child)

    flush()

    return abilities


def parse_ability_section_item(node):
    text = utils.clean_punctuation_spacing(node.get_text(" ", strip=True))

    if text.upper().startswith("CORE:"):
        return {
            "kind": "core",
            "values": split_csv_value(text.split(":", 1)[1])
        }

    if text.upper().startswith("FACTION:"):
        return {
            "kind": "faction",
            "values": split_csv_value(text.split(":", 1)[1])
        }

    named = extract_named_abilities(node)

    if named:
        return {
            "kind": "items",
            "items": named
        }

    return {
        "kind": "items",
        "items": [{
            "content": utils.extract_content_blocks([node])
        }]
    }


def extract_sections_from_container(container):
    sections = []
    current = None

    if not container:
        return sections

    for child in container.children:
        if not getattr(child, "get", None):
            continue

        classes = child.get("class", [])

        if "dsHeader" in classes:
            title = utils.clean_text(child)

            if not should_keep_section(title):
                current = None
                continue

            current = {
                "title": title,
                "core": [],
                "faction": [],
                "items": []
            }

            sections.append(current)

        elif "dsAbility" in classes:
            if current is None:
                continue

            parsed = parse_ability_section_item(child)

            if parsed["kind"] == "core":
                current["core"].extend(parsed["values"])

            elif parsed["kind"] == "faction":
                current["faction"].extend(parsed["values"])

            elif parsed["kind"] == "items":
                current["items"].extend(parsed["items"])

        elif child.name == "ul":
            if current is None:
                continue

            text = utils.clean_punctuation_spacing(child.get_text(" ", strip=True))

            if text:
                current["items"].append({
                    "text": text
                })

    return sections


def extract_sections(ds):
    sections = []

    for selector in [".dsLeftСol", ".dsRightСol"]:
        sections.extend(
            extract_sections_from_container(ds.select_one(selector))
        )

    return [
        section for section in sections
        if section["items"]
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


def extract_all(ds, page):
    return {
        "name": extract_name(ds),
        "profiles": extract_profiles(ds),
        "weapons": extract_weapons(ds),
        "sections": extract_sections(ds),
        "keywords": extract_keywords(ds),
        "faction_keywords": extract_faction_keywords(ds),
        "theme": extract_theme(ds, page)
    }


def preprocess(img, max_width=1200, max_height=2000):
    w, h = img.size
    scale = min(max_width / w, max_height / h, 1.0)

    if scale < 1.0:
        img = img.resize(
            (int(w * scale), int(h * scale)),
            Image.LANCZOS
        )

    return img

# =========================================================
# PIPELINE (PURE FETCH + PARSE ONLY)
# =========================================================
def run(page, url, screenshot, unit_subfaction_map=None):
    page.set_viewport_size({"width": 1600, "height": 2000})

    page.goto(url, wait_until="domcontentloaded")

    page.wait_for_selector(".dsH2Header")
    first = page.locator(".dsH2Header").first
    
    page.wait_for_selector(".dsRightСolKW")
    last = page.locator(".dsRightСolKW").last

    first.wait_for(state="visible")
    last.wait_for(state="visible")

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

    html = page.content()

    soup = BeautifulSoup(html, "html.parser")

    if not unit_subfaction_map:
        selects = utils.get_filter_selects(soup)

        if len(selects) > 1:
            unit_subfaction_map = utils.build_sub_faction_map(selects[0])
        else:
            unit_subfaction_map = {}

    ds = extract_datacard(soup)
    faction_name = extract_faction_name(soup, ds, unit_subfaction_map)
    data = extract_all(ds, page)

    if screenshot:
        # Why do some factions have to be so fucking weird?
        if (faction_name == "SPACE MARINES"):
            faction_name = "ADEPTUS ASTARTES"
        if (faction_name == "CHAOS DAEMONS"):
            faction_name = "LEGIONES DAEMONICA"
        if (faction_name == "IMPERIAL AGENTS"):
            faction_name = "AGENTS OF THE IMPERIUM"

        faction_keywords_str = ""

        # If a faction is made of sub-factions order them by faction then sub-faction
        if (faction_name not in data["faction_keywords"]):
            faction_keywords_str = f"{faction_name}/{"/".join(data["faction_keywords"])}"
        else:
            faction_keywords_str = "/".join(data["faction_keywords"])

        faction_keywords_str = faction_keywords_str.replace(" ", "_")

        print(f"Faction: {faction_name} | Keywords: {faction_keywords_str} | Unit: {data['name']}")

        # ONLY NEW ADDITION (post-extraction safe zone)
        screenshot_datacard(page, url, data["name"], faction_keywords_str)


    return data, faction_name

# =========================================================
# SCREENSHOT STAGE (POST-EXTRACTION ONLY)
# =========================================================
def screenshot_datacard(page, url, unit_name, faction_name):
    from playwright.sync_api import sync_playwright

    # -------------------------------------------------
    # CLEAN UI (tooltips only)
    # -------------------------------------------------
    page.evaluate("""
    () => {
        document.querySelectorAll(
            ".picLegend.tooltip_, .picSearch.tooltip_, .altModels.tooltip_"
        ).forEach(e => e.remove());
    }
    """)

    # -------------------------------------------------
    # STEP 1: FIRST ANCHOR (top of datacard)
    # -------------------------------------------------
    first = page.locator(".dsH2Header").first

    # -------------------------------------------------
    # STEP 2: LAST ANCHOR (bottom of datacard)
    # -------------------------------------------------
    last = page.locator(
        ".dsRightСolKW"
    ).last

    if first.count() == 0 or last.count() == 0:
        raise Exception("Datacard anchors not found")
    
    left = 0
    top = 0
    width = 0
    height = 0

    first_box = first.bounding_box()
    last_box = last.bounding_box()

    if not first_box or not last_box:
        raise Exception("Could not compute bounding boxes")


    # -------------------------------------------------
    # STEP 3: COMPUTE UNION RECTANGLE
    # -------------------------------------------------
    left = first_box["x"]
    top = first_box["y"]

    right = last_box["x"] + last_box["width"]

    bottom = last_box["y"] + last_box["height"]

    width = right - left
    height = bottom - top

    output_path = f"{faction_name}/{unit_name}.png"

    page.evaluate("""
    () =>   {
        document.querySelectorAll('*').forEach(el =>
        {
            const cls = el.getAttribute("class") || "";

            if (
                cls.includes("picLegend") ||
                cls.includes("picSearch") ||
                cls.includes("altModels")
            ) {
                el.remove();
            }       
        });
    }
    """)

    clean_page_for_screenshot(page)

    wait_for_render_stable(page)

    # -------------------------------------------------
    # STEP 4: SCREENSHOT CLIPPED REGION
    # -------------------------------------------------
    img_bytes = page.screenshot(clip={
        "x": left,
        "y": top,
        "width": width,
        "height": height
    })

    from PIL import Image
    from io import BytesIO

    img = Image.open(BytesIO(img_bytes)).convert("RGB")

    img = preprocess(img)

    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    img.save(output_path, optimize=True, quality=85)

    print(f"Saved datacard screenshot: {output_path}")


def wait_for_render_stable(page):

    page.wait_for_function("""
    () => {

        return new Promise(resolve => {

            requestAnimationFrame(() => {

                requestAnimationFrame(() => {

                    resolve(true);

                });

            });

        });

    }
    """)


def clean_page_for_screenshot(page):

    page.evaluate("""
    () => {

        // Remove common popup/overlay elements
        const selectors = [
            '[class*="modal"]',
            '[class*="popup"]',
            '[class*="overlay"]',
            '[class*="cookie"]',
            '[class*="consent"]',
            '[class*="banner"]',
            '[class*="dialog"]',
            '[role="dialog"]',
            '[aria-modal="true"]'
        ];


        selectors.forEach(sel => {
            document.querySelectorAll(sel).forEach(el => {
                el.remove();
            });
        });


        // Kill blur effects everywhere
        document.querySelectorAll("*").forEach(el => {

            const style = window.getComputedStyle(el);

            if (
                style.filter.includes("blur") ||
                style.backdropFilter.includes("blur")
            ){
                el.style.filter = "none";
                el.style.backdropFilter = "none";
                el.style.webkitBackdropFilter = "none";
            }

        });


        // Remove fixed floating elements
        document.querySelectorAll("*").forEach(el => {

            const style = window.getComputedStyle(el);

            if(style.position === "fixed") {

                const rect = el.getBoundingClientRect();

                if(rect.width > 100 && rect.height > 50) {
                    el.remove();
                }
            }

        });


        // Disable all animations/transitions
        const css = document.createElement("style");

        css.innerHTML = `
            *,
            *::before,
            *::after {

                animation:none !important;
                transition:none !important;
                caret-color:transparent !important;

            }
        `;

        document.head.appendChild(css);


        // Reset scrolling
        document.body.style.overflow = "visible";

    }
    """)


def parse_args():
    parser = argparse.ArgumentParser(description="Wahapedia datacard extractor")
    parser.add_argument(
        "--url",
        help="The url for the unit"
    )
    parser.add_argument(
        "--screenshot",
        action="store_true",
        help="Don't take a screenshot of the datacard"
    )

    return parser.parse_args()


# =========================================================
# ENTRY
# =========================================================
if __name__ == "__main__":

    args = parse_args()

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        data, faction_name  = run(page, args.url, args.screenshot)

        browser.close()

    output_path = f"{faction_name}_{data.get("name")}.json"

    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=4, ensure_ascii=False)

    print(f"Saved JSON: {output_path}")