import sys
import os

import re

from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

from PIL import Image
from io import BytesIO


# =========================================================
# TEXT CLEANING
# =========================================================

def clean_text(element):
    if not element:
        return ""

    text = element.get_text(" ", strip=True)

    return " ".join(text.split())


# =========================================================
# DETACHMENT EXTRACTION
# =========================================================

def norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.strip().lower()).strip("-")

def extract_detachment_block(page, detachment_anchor):

    target = norm(detachment_anchor)

    anchors = page.locator("a[name]")

    count = anchors.count()

    for i in range(count):
        a = anchors.nth(i)
        name = a.get_attribute("name") or ""

        if norm(name) == target:
            return a.locator(
                "xpath=ancestor::div[contains(@class,'clFl')]"
            ).first

    raise Exception(f"Detachment not found: {detachment_anchor}")


def normalise_anchor_name(name):

    return (
        name
        .strip()
        .replace(" ", "-")
        .replace("(", "")
        .replace(")", "")
        .replace("’","-")
        .replace("!","-")
        .replace("--","-")
    )

# =========================================================
# PIPELINE (FETCH + IDENTIFICATION ONLY)
# =========================================================

def run(page, faction_name, detachment_name):

    detachment_anchor = normalise_anchor_name(
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


    print(
        f"Faction: {faction_name} | Detachment: {detachment_anchor}"
    )


    screenshot_detachment(
        page,
        detachment_block,
        faction_name,
        detachment_anchor,
        detachment_rule_names,
        has_enhancements,
        has_stratagems
    )



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
        detachment_name
    )

    for rule in detachment_rule_names:
        ruleAnchors = detachment_block.locator(f"a[name^='{normalise_anchor_name(rule)}']")
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

    enhancementAnchors = detachment_block.locator("a[name^='Enhancements']")
    count = enhancementAnchors.count()

    for i in range(count):
        anchor = enhancementAnchors.nth(i)

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

    stratagemAnchors = detachment_block.locator("a[name^='Stratagems']")
    count = stratagemAnchors.count()

    for i in range(count):
        anchor = stratagemAnchors.nth(i)

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

        run(
            page,
            args.faction,
            args.detachment
        )


        browser.close()