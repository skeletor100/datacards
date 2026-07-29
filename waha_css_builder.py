import argparse
import hashlib
import json
import os
import re
from urllib.parse import urljoin, urlparse

import requests
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

import waha_scraper_common as style_parser

# core-rules, not quick-start-guide: verified it covers everything
# quick-start-guide did (the ae*/cru* widget glyphs used by existing army
# rules content) plus the str11 stratagem icon classes that quick-start-
# guide doesn't load at all — Wahapedia scopes CSS per page/section rather
# than one global stylesheet, so which page this points at matters. If a
# future class turns up that isn't covered by any single page, the real
# fix is tracking each class's own source page from the scrape and
# sampling it there — not chasing an ever-growing single URL.
DEFAULT_WAHAPEDIA_URL = f"{style_parser.DEFAULT_WAHAPEDIA_BASE}/the-rules/core-rules/"


def navigate_with_retries(page, url, attempts=3):
    """Navigate with a longer timeout and retries — Wahapedia is occasionally
    slow enough that a single bare page.goto() at Playwright's default 30s
    timeout isn't reliable. Mirrors waha_scraper.py's navigate_to_required_
    selector; copied rather than imported since this runs as its own
    standalone subprocess.
    """
    last_error = None

    for attempt in range(1, attempts + 1):
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
            return
        except PlaywrightTimeoutError as exc:
            last_error = exc
            print(f"Navigation attempt {attempt}/{attempts} failed for {url}: {exc}")
            if attempt < attempts:
                try:
                    page.goto("about:blank", wait_until="commit", timeout=10000)
                except PlaywrightTimeoutError:
                    pass
                page.wait_for_timeout(2000 * attempt)

    raise last_error

# =========================================================
# What this builds, and why it's smaller than the old manifest builder:
#
# The new parsers (waha_detachment_scraper.py / waha_unit_scraper.py /
# waha_army_rule_scraper.py) resolve ordinary text styling (bold/italic/
# upper/colour) and heading banners live, during the scrape itself — there
# are no raw Wahapedia CSS class names left on runs or headings for a
# manifest to look up. The only things still expressed as raw class names in
# the new schema are compact, class-driven visual glyphs that were never
# text-shaped to begin with (a "BATTLE ROUND" diamond marker, a CP-cost
# icon, a weapon's split-profile marker) — those genuinely need their real
# CSS (background-image/mask-image/etc) resolved from a live page, since
# there's no generic reduction for "draw this specific icon". Stratagem
# colour classes are the one remaining case of a plain colour that isn't
# yet resolved at parse time (kept as a raw class since it lives on the
# outer .str10Wrap card, not a single text run). This builder resolves
# exactly those, plus direct <img> sources, and nothing else.
# =========================================================


def class_key(classes):
    return " ".join(sorted(c for c in classes if c))


# =========================================================
# COLLECTION
# =========================================================

def collect_from_content(blocks, icon_class_sets, direct_image_srcs):
    for block in blocks or []:
        if not block:
            continue

        if isinstance(block, list):
            collect_from_content(block, icon_class_sets, direct_image_srcs)
            continue

        if not isinstance(block, dict):
            continue

        display_item = block.get("displayItem")

        if display_item == "element":
            classes = tuple(sorted(c for c in (block.get("classes") or []) if c))
            if classes:
                icon_class_sets.add(classes)
            collect_from_content(block.get("children"), icon_class_sets, direct_image_srcs)

        elif display_item == "img":
            src = block.get("src")
            if src:
                direct_image_srcs.add(src)

        elif display_item == "table":
            for row in block.get("rows") or []:
                for cell in row or []:
                    collect_from_content(cell.get("content"), icon_class_sets, direct_image_srcs)

        elif display_item in ("ul", "ol"):
            for item in block.get("items") or []:
                collect_from_content(item.get("content"), icon_class_sets, direct_image_srcs)

        elif display_item in ("subrule", "cs_rule"):
            collect_from_content(block.get("content"), icon_class_sets, direct_image_srcs)
            # cs_rule's requirement is usually plain text (e.g. "N/A") but can
            # be a D6-pip icon widget instead (an element block) — collect it
            # the same way as any other icon.
            requirement = block.get("requirement")
            if isinstance(requirement, dict):
                collect_from_content([requirement], icon_class_sets, direct_image_srcs)

        # "p"/"span" blocks carry only already-resolved bold/italic/upper/
        # colour info on their runs now — nothing left there to collect.


def collect_stratagems(stratagems, icon_class_sets, stratagem_color_classes, direct_image_srcs):
    for stratagem in stratagems or []:
        color_class = stratagem.get("color_class")
        if color_class:
            stratagem_color_classes.add(color_class)

        for icon_class in stratagem.get("icon_classes") or []:
            icon_class_sets.add((icon_class,))

        # The icon column's own frame texture (e.g. str11StratBg) — a
        # separate field from icon_classes since it names the WRAPPING div
        # around the glyphs, not one of the glyphs itself (see
        # waha_scraper_common._str11_stratagem_bg_class).
        bg_class = stratagem.get("bg_class")
        if bg_class:
            icon_class_sets.add((bg_class,))

        for key in ("when", "target", "effect", "restrictions"):
            block = stratagem.get(key)
            if block:
                collect_from_content([block], icon_class_sets, direct_image_srcs)


def collect_unit_card(card, icon_class_sets, direct_image_srcs):
    for section in card.get("sections") or []:
        collect_from_content(section.get("items"), icon_class_sets, direct_image_srcs)

    collect_from_content(card.get("weapon_abilities"), icon_class_sets, direct_image_srcs)

    for weapon in card.get("weapons") or []:
        marker = weapon.get("profile_marker")
        if isinstance(marker, dict):
            classes = tuple(sorted(c for c in (marker.get("classes") or []) if c))
            if classes:
                icon_class_sets.add(classes)


def collect_detachment_card(card, icon_class_sets, stratagem_color_classes, direct_image_srcs):
    for rule in card.get("rules") or []:
        collect_from_content(rule.get("content"), icon_class_sets, direct_image_srcs)

    for enhancement in card.get("enhancements") or []:
        collect_from_content(enhancement.get("content"), icon_class_sets, direct_image_srcs)

    collect_stratagems(card.get("stratagems"), icon_class_sets, stratagem_color_classes, direct_image_srcs)


def iter_faction_nodes(units_data):
    """Yield every manifest node that can contain unit_cards, detachment_cards
    or army_rules — top-level factions and any nested sub-factions."""
    reserved_keys = {"unit_cards", "detachment_cards", "army_rules"}

    def walk(node):
        if not isinstance(node, dict):
            return

        if any(key in node for key in reserved_keys):
            yield node

        for key, value in node.items():
            if key in reserved_keys:
                continue
            if isinstance(value, dict):
                yield from walk(value)

    for faction_data in units_data.values():
        yield from walk(faction_data)


def collect_all(units_data):
    icon_class_sets = set()
    stratagem_color_classes = set()
    direct_image_srcs = set()

    for faction_data in iter_faction_nodes(units_data):
        for rule in (faction_data.get("army_rules") or {}).values():
            collect_from_content(rule.get("content"), icon_class_sets, direct_image_srcs)

        for card in (faction_data.get("unit_cards") or {}).values():
            collect_unit_card(card, icon_class_sets, direct_image_srcs)

        for card in (faction_data.get("detachment_cards") or {}).values():
            collect_detachment_card(card, icon_class_sets, stratagem_color_classes, direct_image_srcs)

    return icon_class_sets, stratagem_color_classes, direct_image_srcs


# =========================================================
# LIVE COMPUTED STYLE
# =========================================================

def read_icon_styles(page, class_sets):
    """Resolve an icon/widget class combination's real appearance.

    Reconstructs the same ancestor scaffold (datasheet > left column >
    weapon table > row > cell) real datasheet icons sit inside, since some
    of these classes are only styled through context-dependent selectors
    (e.g. ".wTable .dsPointy"). Harmless for classes that don't need it —
    they resolve the same regardless of the scaffold.
    """
    if not class_sets:
        return {}

    payload = [
        {"key": class_key(classes), "classes": list(classes)}
        for classes in class_sets
    ]

    return page.evaluate(
        """
        (items) => {
            const fields = [
                "backgroundImage", "maskImage", "webkitMaskImage",
                "content", "listStyleImage", "borderImageSource"
            ];

            function snapshot(style) {
                const out = {
                    boxSizing: style.boxSizing,
                    width: style.width, height: style.height,
                    minWidth: style.minWidth, minHeight: style.minHeight,
                    maxWidth: style.maxWidth, maxHeight: style.maxHeight,
                    padding: style.padding,
                    color: style.color, backgroundColor: style.backgroundColor,
                    backgroundSize: style.backgroundSize,
                    backgroundRepeat: style.backgroundRepeat,
                    backgroundPosition: style.backgroundPosition,
                    filter: style.filter,
                    borderRadius: style.borderRadius,
                    borderWidth: style.borderWidth,
                    borderStyle: style.borderStyle,
                    borderColor: style.borderColor,
                    // dsPointy and similar markers are drawn entirely via
                    // clip-path (an arrow/chevron shape), not an image — this
                    // was previously missing, so those markers silently
                    // rendered as plain rectangles.
                    clipPath: style.clipPath,
                    transform: style.transform,
                    overflow: style.overflow,
                };
                for (const field of fields) {
                    out[field] = style[field] || "";
                }
                return out;
            }

            const result = {};

            for (const item of items) {
                const datasheet = document.createElement("div");
                datasheet.className = "datasheet";
                const leftCol = document.createElement("div");
                leftCol.className = "dsLeftСol";
                const table = document.createElement("table");
                table.className = "wTable";
                const tbody = document.createElement("tbody");
                const row = document.createElement("tr");
                const cell = document.createElement("td");
                const el = document.createElement("div");
                el.className = item.classes.join(" ");

                cell.appendChild(el);
                row.appendChild(cell);
                tbody.appendChild(row);
                table.appendChild(tbody);
                leftCol.appendChild(table);
                datasheet.appendChild(leftCol);
                document.body.appendChild(datasheet);

                result[item.key] = {
                    element: snapshot(window.getComputedStyle(el)),
                    before: snapshot(window.getComputedStyle(el, "::before")),
                    after: snapshot(window.getComputedStyle(el, "::after")),
                };

                datasheet.remove();
            }

            return result;
        }
        """,
        payload,
    )


def read_stratagem_colors(page, classes):
    if not classes:
        return {}

    return page.evaluate(
        """
        (classes) => {
            const result = {};
            for (const className of classes) {
                const el = document.createElement("div");
                el.className = className;
                document.body.appendChild(el);
                const style = window.getComputedStyle(el);
                result[className] = {
                    color: style.color,
                    backgroundColor: style.backgroundColor,
                    borderColor: style.borderColor,
                };
                el.remove();
            }
            return result;
        }
        """,
        sorted(classes),
    )


# =========================================================
# ASSET DOWNLOAD (skips anything already present in the asset directory —
# filenames are a deterministic hash of the source URL, so re-running this
# never re-downloads what's already there)
# =========================================================

def extract_css_urls(value):
    if not value or value == "none":
        return []
    return [
        match.strip("'\"")
        for match in re.findall(r"url\((.*?)\)", value)
        if match.strip("'\"")
    ]


def safe_asset_name(class_name, url):
    parsed = urlparse(url)
    base = os.path.basename(parsed.path) or "asset"
    _, ext = os.path.splitext(base)
    if not ext:
        ext = ".bin"

    safe_class = re.sub(r"[^A-Za-z0-9_.-]+", "_", class_name or "asset").strip("_") or "asset"
    digest = hashlib.sha1(url.encode("utf-8")).hexdigest()[:8]
    return f"{safe_class}_{digest}{ext}"


def direct_asset_name(src):
    parsed = urlparse(src)
    base = os.path.basename(parsed.path) or "image"
    stem, _ = os.path.splitext(base)
    stem = re.sub(r"[^A-Za-z0-9_.-]+", "_", stem).strip("_") or "image"
    return f"direct_{stem}"


def download_asset(url, output_dir, name_hint):
    os.makedirs(output_dir, exist_ok=True)

    filename = safe_asset_name(name_hint, url)
    output_path = os.path.join(output_dir, filename)

    if os.path.exists(output_path):
        return output_path, False

    response = requests.get(url, timeout=30)
    response.raise_for_status()

    with open(output_path, "wb") as f:
        f.write(response.content)

    return output_path, True


def localize_icon_assets(icon_styles, base_url, asset_dir, stats):
    url_fields = (
        "backgroundImage", "maskImage", "webkitMaskImage",
        "content", "listStyleImage", "borderImageSource",
    )

    def localize(target, name_prefix):
        for field in url_fields:
            urls = extract_css_urls(target.get(field))
            if not urls:
                continue

            target[f"{field}Urls"] = []
            target[f"{field}Assets"] = []

            for index, raw_url in enumerate(urls):
                absolute_url = urljoin(base_url, raw_url)
                local_path, downloaded = download_asset(
                    absolute_url, asset_dir, f"{name_prefix}_{field}_{index}"
                )
                stats["downloaded" if downloaded else "skipped"] += 1

                target[f"{field}Urls"].append(absolute_url)
                target[f"{field}Assets"].append(local_path.replace("\\", "/"))

            if len(urls) == 1:
                target[f"{field}Url"] = target[f"{field}Urls"][0]
                target[f"{field}Asset"] = target[f"{field}Assets"][0]

    localized = {}
    for class_key_str, styles in icon_styles.items():
        entry = dict(styles)
        for nested_key in ("element", "before", "after"):
            nested = entry.get(nested_key)
            if isinstance(nested, dict):
                localize(nested, f"{class_key_str}_{nested_key}")
        localized[class_key_str] = entry

    return localized


def localize_direct_images(srcs, base_url, asset_dir, stats):
    localized = {}

    for src in srcs:
        absolute_url = urljoin(base_url, src)
        local_path, downloaded = download_asset(absolute_url, asset_dir, direct_asset_name(src))
        stats["downloaded" if downloaded else "skipped"] += 1

        localized[src] = {
            "url": absolute_url,
            "asset": local_path.replace("\\", "/"),
        }

    return localized


# =========================================================
# PIPELINE
# =========================================================

def build_manifest(data_json, wahapedia_url, asset_dir, core_stratagems_json=None):
    with open(data_json, "r", encoding="utf-8") as f:
        units_data = json.load(f)

    icon_class_sets, stratagem_color_classes, direct_image_srcs = collect_all(units_data)

    # Core stratagems (waha_core_stratagems_scraper.py's output) live in
    # their own separate JSON file, not nested under any faction in
    # waha_data.json — collect_all above walks faction data exclusively
    # (army_rules/unit_cards/detachment_cards per faction), so it has no
    # path to ever see this file's contents. Without this, core stratagems'
    # icon classes (str11Any, str11Movement, ...) would never be collected
    # at all, no matter what the scraped data actually contains — which is
    # exactly the gap that left their icons unresolved in the manifest.
    if core_stratagems_json and os.path.exists(core_stratagems_json):
        with open(core_stratagems_json, "r", encoding="utf-8") as f:
            core_stratagems_data = json.load(f)
        collect_stratagems(
            core_stratagems_data.get("stratagems"),
            icon_class_sets,
            stratagem_color_classes,
            direct_image_srcs,
        )

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        navigate_with_retries(page, wahapedia_url)

        icon_styles = read_icon_styles(page, icon_class_sets)
        stratagem_colors = read_stratagem_colors(page, stratagem_color_classes)

        browser.close()

    stats = {"downloaded": 0, "skipped": 0}
    icon_styles = localize_icon_assets(icon_styles, wahapedia_url, asset_dir, stats)
    direct_image_assets = localize_direct_images(direct_image_srcs, wahapedia_url, asset_dir, stats)

    print(f"Assets: {stats['downloaded']} downloaded, {stats['skipped']} already present")

    return {
        "source": wahapedia_url,
        "asset_dir": asset_dir.replace("\\", "/"),
        "icons": icon_styles,
        "stratagem_colors": stratagem_colors,
        "direct_image_assets": direct_image_assets,
    }


def parse_args():
    parser = argparse.ArgumentParser(
        description="Build the icon/asset CSS manifest for the new (waha_*) parser schema"
    )
    parser.add_argument("--data-json", default="waha_data.json", help="Path to waha_data.json")
    parser.add_argument("--core-stratagems-json", default="core_stratagems.json", help="Path to waha_core_stratagems_scraper.py's output (skipped if missing)")
    parser.add_argument("--wahapedia-url", default=DEFAULT_WAHAPEDIA_URL, help="A Wahapedia page URL with the relevant CSS loaded")
    parser.add_argument("--output", default="waha_css_manifest.json", help="Output manifest path")
    parser.add_argument("--asset-dir", default="assets", help="Directory where downloaded Wahapedia image assets are stored")
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()

    manifest = build_manifest(args.data_json, args.wahapedia_url, args.asset_dir, args.core_stratagems_json)

    os.makedirs(os.path.dirname(args.output) or ".", exist_ok=True)

    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2, ensure_ascii=False)

    print(f"Wrote CSS manifest: {args.output}")
