import sys
import json
import re
import os

from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright


# =========================================================
# TEXT CLEANING (UNCHANGED)
# =========================================================
def clean_text(element):
    if not element:
        return ""

    text = element.get_text(" ", strip=True)
    text = re.sub(r"\s+", " ", text)
    return text.strip()

def extract_faction_name(soup):
    node = soup.select_one('[data-tooltip-content="#tooltip_contentFactionRules"]')

    if not node:
        return ""

    return clean_text(node).upper()

def extract_datacard(soup):
    candidates = soup.find_all(class_=lambda c: c and "datasheet" in c)

    for ds in candidates:
        if ds.select_one(".dsH2Header"):
            return ds

    raise Exception("Could not find valid unit datacard")

def extract_name(ds):
    node = ds.select_one(".dsH2Header div")
    return clean_text(node)


def extract_faction_keywords(ds):
    block = ds.find(string=lambda x: x and x.strip() == "FACTION KEYWORDS:")

    if not block:
        return []

    return [
        x.strip()
        for x in block.parent.stripped_strings
        if x.strip() != "FACTION KEYWORDS:"
    ]


def extract_all(ds):
    return {
        "name": extract_name(ds),
        "faction_keywords": extract_faction_keywords(ds)
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
def run(page, url):
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

    faction_name = extract_faction_name(soup)
    ds = extract_datacard(soup)
    data = extract_all(ds)

    faction_name_str = faction_name.replace(" ", "_").upper()
    # Why do some factions have to be so fucking weird?
    if (faction_name_str == "SPACE_MARINES"):
        faction_name_str = "ADEPTUS_ASTARTES"
    if (faction_name_str == "CHAOS_DAEMONS"):
        faction_name_str = "LEGIONES_DAEMONICA"

    faction_keywords_str = "_".join(data["faction_keywords"]).replace("_,_", "/")

    # If a faction is made of sub-factions order them by faction then sub-faction
    if (faction_name_str not in faction_keywords_str):
        faction_keywords_str = f"{faction_name_str}/{faction_keywords_str}"

    print(f"Faction: {faction_name_str} | Keywords: {faction_keywords_str} | Unit: {data['name']}")

    # ONLY NEW ADDITION (post-extraction safe zone)
    screenshot_datacard(page, url, data["name"], faction_keywords_str)

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

# =========================================================
# ENTRY
# =========================================================
if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python script.py <wahapedia_url>")
        sys.exit(1)

    url = sys.argv[1]

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        run(page, url)