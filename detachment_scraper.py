import sys
import os
import json

import re

from bs4 import BeautifulSoup, Tag
import waha_parse_utils as utils
from playwright.sync_api import sync_playwright

from PIL import Image
from io import BytesIO


# =========================================================
# TEXT CLEANING
# =========================================================

















def extract_subrule_from_table(block):
    title_node = block.select_one(".impact18")
    if not title_node:
        return None

    content_nodes = []

    for node in title_node.next_siblings:
        if utils.is_ignorable_node(node) and not utils.is_br(node):
            continue
        content_nodes.append(node)

    return {
        "displayItem": "subrule",
        "title": utils.clean_text(title_node),
        "content": utils.extract_content_blocks(content_nodes)
    }


def extract_detachment_rules(soup):
    rules = []

    for heading in soup.select("h3"):
        if heading.find_parent(class_="str10Wrap"):
            continue

        block = heading.find_parent("div", class_="BreakInsideAvoid")
        if not block:
            continue

        content_nodes = []

        for node in heading.next_siblings:
            if isinstance(node, Tag) and node.name in ("h2", "h3"):
                break

            if utils.is_ignorable_node(node) and not utils.is_br(node):
                continue

            if isinstance(node, Tag) and node.select_one(".impact18"):
                subrule = extract_subrule_from_table(node)
                if subrule:
                    content_nodes.append(subrule)
                continue

            content_nodes.append(node)

        content = []

        # extract_content_blocks expects DOM nodes, but subrules are already parsed dicts.
        raw_nodes = []
        for node in content_nodes:
            if isinstance(node, dict):
                content.extend(utils.extract_content_blocks(raw_nodes))
                raw_nodes = []
                content.append(node)
            else:
                raw_nodes.append(node)

        content.extend(utils.extract_content_blocks(raw_nodes))

        if content:
            rules.append({
                "name": utils.clean_text(heading),
                "content": content,
                "heading_class": heading.get("class", [])
            })

    return rules


def extract_enhancements(soup):
    enhancements = []

    section = utils.find_section_by_anchor_prefix(soup, "Enhancements")
    if not section:
        return enhancements

    for item in section.select("ul.EnhancementsPts li"):
        spans = item.find_all("span", recursive=False)
        name = utils.clean_text(spans[0]) if spans else utils.clean_text(item)

        container = item.find_parent("div", class_="BreakInsideAvoid")
        if not container:
            continue

        content_nodes = []

        for node in container.find_all("p", recursive=True):
            if utils.is_fluff_node(node):
                continue

            if utils.clean_text(node):
                content_nodes.append(node)

        content = utils.extract_content_blocks(content_nodes)

        enhancements.append({
            "name": name,
            "content": content
        })

    return enhancements


def extract_stratagem_field_block(text_el, label):
    if not text_el:
        return None

    label = label.upper()
    collecting = False
    nodes = []

    labels = {"WHEN", "TARGET", "EFFECT", "RESTRICTIONS"}

    for child in text_el.children:
        if isinstance(child, Tag):
            child_text = utils.clean_text(child).upper().rstrip(":")

            if child.name == "span" and child_text == label:
                collecting = True
                continue

            if (
                collecting
                and child.name == "span"
                and child_text in labels
                and child_text != label
            ):
                break

        if collecting:
            nodes.append(child)

    return utils.paragraph_block_from_nodes(nodes)


def extract_icon_classes(wrap):
    icons = []

    for div in wrap.select(".str10Diamond div[class]"):
        classes = div.get("class", [])

        candidates = [
            c for c in classes
            if c.startswith("str10")
            and not c.startswith("str10Color")
            and c not in {
                "str10Diamond",
                "str10CP",
                "str10Pos2",
                "str10DiamondWrap",
            }
        ]

        for candidate in candidates:
            if candidate not in icons:
                icons.append(candidate)

    return icons


def extract_color_class(wrap):
    for c in wrap.get("class", []):
        if c.startswith("str10Color"):
            return c

    for el in wrap.select("[class]"):
        for c in el.get("class", []):
            if c.startswith("str10Color"):
                return c

    return None


def strip_detachment_name_from_type(value):
    if "–" in value:
        return utils.clean_punctuation_spacing(value.split("–", 1)[1])

    return utils.clean_punctuation_spacing(value)


def extract_stratagems(soup):
    stratagems = []

    for wrap in soup.select(".str10Wrap"):
        text_el = wrap.select_one(".str10Text")

        stratagems.append({
            "name": utils.clean_text(wrap.select_one(".str10Name")),
            "cp": utils.clean_text(wrap.select_one(".str10CP")),
            "type": strip_detachment_name_from_type(
                utils.clean_text(wrap.select_one(".str10Type"))
            ),
            "when": extract_stratagem_field_block(text_el, "WHEN"),
            "target": extract_stratagem_field_block(text_el, "TARGET"),
            "effect": extract_stratagem_field_block(text_el, "EFFECT"),
            "restrictions": extract_stratagem_field_block(text_el, "RESTRICTIONS"),
            "icon_classes": extract_icon_classes(wrap),
            "color_class": extract_color_class(wrap)
        })

    return stratagems

# =========================================================
# DETACHMENT EXTRACTION
# =========================================================


def extract_detachment_block(page, detachment_anchor):

    target = utils.norm(detachment_anchor)

    anchors = page.locator("a[name]")

    count = anchors.count()

    for i in range(count):
        a = anchors.nth(i)
        name = a.get_attribute("name") or ""

        if utils.norm(name) == target:
            return a.locator(
                "xpath=ancestor::div[contains(@class,'clFl')]"
            ).first

    raise Exception(f"Detachment not found: {detachment_anchor}")


def extract_detachment_data(detachment_block, faction_name, detachment_name):
    soup = BeautifulSoup(detachment_block.inner_html(), "html.parser")

    return {
        "faction": faction_name,
        "detachment": detachment_name,
        "rules": extract_detachment_rules(soup),
        "enhancements": extract_enhancements(soup),
        "stratagems": extract_stratagems(soup)
    }



# =========================================================
# PIPELINE (FETCH + IDENTIFICATION ONLY)
# =========================================================

def run(page, faction_name, detachment_name, take_screenshot):

    detachment_anchor = utils.normalise_anchor_name(
        detachment_name
    )

    page.set_viewport_size({
        "width": 1600,
        "height": 2000
    })


    page.wait_for_function(f"""
    () => {{
        const target = "{detachment_anchor}".toLowerCase().replace(/[^a-z0-9]+/g, "-");

        const anchors = Array.from(document.querySelectorAll("a[name]"));

        const el = anchors.find(a => {{
            const name = (a.getAttribute("name") || "")
                .toLowerCase()
                .replace(/[^a-z0-9]+/g, "-");

            return name === target;
        }});

        if (!el) return false;

        const section = el.closest('div.clFl');
        if (!section) return false;

        const rect = section.getBoundingClientRect();
        return rect.width > 0 && rect.height > 0;
    }}
    """)


    page.add_style_tag(content="""
        * {
            animation: none !important;
            transition: none !important;
        }
    """)

    # Extract Detachments
    contents = page.locator("div.contents_header").first

    container = contents.locator(
        "xpath=ancestor::div[contains(@class,'BreakInsideAvoid')]"
    ).first

    headers = container.locator(
        "div.i10 a[href^='#'], div.i30 a[href^='#'], div.i50 a[href^='#']"
    )
    count = headers.count()

    detachmentDetails = []
    detachment_found = False
    detachment_rule_names = []
    has_enhancements = False
    has_stratagems = False

    for i in range(count):
        h = headers.nth(i)

        text = h.inner_text().strip()
        cls = h.locator("..").get_attribute("class") or ""

        if "i10" in cls:
            if detachment_found:
                break
            else:
                if text == detachment_name:
                    detachment_found = True
            continue

        if detachment_found and "i30" in cls:
            if text == "Enhancements":
                has_enhancements = True
            if text == "Stratagems":
                has_stratagems = True
            continue

        if detachment_found and "i50" in cls:
            detachment_rule_names.append(text)
            continue

    html = page.content()

    soup = BeautifulSoup(
        html,
        "html.parser"
    )


    detachment_block = extract_detachment_block(
        page,
        detachment_anchor
    )


    data = []
    

    data = extract_detachment_data(
        detachment_block,
        faction_name,
        detachment_name
    )


    print(
        f"Faction: {faction_name} | Detachment: {detachment_anchor}"
    )


    if take_screenshot:
        screenshot_detachment(
            page,
            detachment_block,
            faction_name,
            detachment_anchor,
            detachment_rule_names,
            has_enhancements,
            has_stratagems
        )

    return data



# =========================================================
# SCREENSHOT STAGE
# =========================================================

def screenshot_detachment(
    page,
    detachment_block,
    faction_name,
    detachment_name,
    detachment_rule_names,
    has_enhancements,
    has_stratagems
):

    faction_dir = (
        faction_name
        .upper()
        .replace(" ", "_")
    )


    output_dir = os.path.join(
        faction_dir,
        detachment_name.upper()
    )

    for rule in detachment_rule_names:
        ruleAnchors = detachment_block.locator(f"a[name^='{utils.normalise_anchor_name(rule)}']")
        anchor = ruleAnchors.first

        section = anchor.locator(
            "xpath=ancestor::div[contains(@class,'BreakInsideAvoid')]"
        ).first

        capture_section(
            page,
            section,
            rule,
            os.path.join(
                output_dir,
                f"{rule}.png"
            )
        )

    if has_enhancements:
        enhancementAnchors = detachment_block.locator("a[name^='Enhancements']")

        anchor = enhancementAnchors.first

        section = anchor.locator(
            "xpath=ancestor::div[contains(@class,'BreakInsideAvoid')]"
        ).first

        capture_section(
            page,
            section,
            "Enhancements",
            os.path.join(
                output_dir,
                "enhancements.png"
            )
        )

    if has_stratagems:
        stratagemAnchors = detachment_block.locator("a[name^='Stratagems']")

        anchor = stratagemAnchors.first

        section = anchor.locator(
            "xpath=ancestor::div[contains(@class,'BreakInsideAvoid')]"
        ).first

        capture_section(
            page,
            section,
            "Stratagems",
            os.path.join(
                output_dir,
                "stratagems.png"
            )
        )



def capture_section(
    page,
    section,
    anchor_name,
    output_path
):

    page.evaluate("""
    () => {
        document.querySelectorAll(
            '.modal, .overlay, .popup, .blur, .tooltip_, .picLegend, .picSearch, .altModels'
        ).forEach(e => e.remove());

        document.body.style.overflow = 'auto';
    }
    """)

    box = section.bounding_box()

    if not box:
        raise Exception(
            f"Could not calculate bounds: {anchor_name}"
        )

    # Guard against invalid geometry (THIS is your crash)
    if box["width"] < 5 or box["height"] < 5:
        raise Exception(
            f"Invalid bounding box for {anchor_name}: {box}"
        )

    clean_page_for_screenshot(page)

    wait_for_render_stable(page)

    # IMPORTANT CHANGE: ensure element is in viewport before screenshot
    section.scroll_into_view_if_needed()

    img_bytes = section.screenshot()


    img = Image.open(
        BytesIO(img_bytes)
    ).convert("RGB")


    os.makedirs(
        os.path.dirname(output_path),
        exist_ok=True
    )


    img.save(
        output_path,
        optimize=True,
        quality=85
    )


    print(
        f"Saved detachment screenshot: {output_path}"
    )


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

# =========================================================
# ENTRY
# =========================================================

# =========================================================
# ENTRY
# =========================================================

def parse_args():

    import argparse

    parser = argparse.ArgumentParser(
        description="Capture Wahapedia detachment screenshots"
    )


    parser.add_argument(
        "--url",
        required=True,
        help="Wahapedia page URL"
    )


    parser.add_argument(
        "--faction",
        required=True,
        help="Faction name used for output directory"
    )


    parser.add_argument(
        "--detachment",
        required=True,
        help="Detachment anchor name (eg Gladius-Task-Force)"
    )

    parser.add_argument(
        "--screenshot",
        action="store_true",
        help="Screenshot detachment"
    )

    return parser.parse_args()



if __name__ == "__main__":

    args = parse_args()


    with sync_playwright() as p:

        browser = p.chromium.launch(
            headless=True
        )


        page = browser.new_page()

        page.goto(
            args.url,
            wait_until="domcontentloaded"
        )

        data = run(
            page,
            args.faction,
            args.detachment,
            args.screenshot
        )

        output_path = f"./{args.faction}/{args.detachment}.json"

        os.makedirs(os.path.dirname(output_path), exist_ok=True)
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)

        print(f"Wrote detachment data to {output_path}")

        browser.close()