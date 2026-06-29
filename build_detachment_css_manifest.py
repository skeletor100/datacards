import argparse
import json
import os
import re
import hashlib
from urllib.parse import urljoin, urlparse

import requests
from playwright.sync_api import sync_playwright

from bs4 import BeautifulSoup


def class_key(classes):
    return " ".join(sorted(c for c in classes if c))


def style_key(role, style):
    return f"{role}:{style or ''}"


def collect_class_set(classes, target):
    classes = tuple(sorted(c for c in (classes or []) if c))
    if classes:
        target.add(classes)


def add_raw_style(raw_styles, role, style):
    if style:
        raw_styles.add((role, style))


def add_direct_asset(direct_image_assets, src, role="img", classes=None, style=None, alt=None):
    if not src:
        return

    entry = direct_image_assets.setdefault(
        src,
        {
            "src": src,
            "uses": []
        }
    )
    use = {
        "role": role,
        "classes": sorted(c for c in (classes or []) if c),
        "style": style or "",
        "alt": alt or ""
    }
    if use not in entry["uses"]:
        entry["uses"].append(use)


def collect_from_runs(runs, inline_class_sets):
    for run in runs or []:
        collect_class_set(run.get("source_classes", []), inline_class_sets)


def collect_from_requirement_html(html, inline_class_sets, extra_icon_classes, direct_image_assets, raw_styles):
    if not html:
        return

    soup = BeautifulSoup(html, "html.parser")

    for tag in soup.find_all(class_=True):
        classes = tag.get("class", [])
        for cls in classes:
            extra_icon_classes.add(cls)
        collect_class_set(classes, inline_class_sets)

        style = tag.get("style")
        if style:
            add_raw_style(raw_styles, f"html_{tag.name}", style)

    for img in soup.find_all("img"):
        add_direct_asset(
            direct_image_assets,
            img.get("src"),
            role="requirement_html_img",
            classes=img.get("class", []),
            style=img.get("style"),
            alt=img.get("alt")
        )


def collect_from_element_block(
    block,
    inline_class_sets,
    extra_icon_classes,
    direct_image_assets,
    raw_styles,
):
    """Collect classes/assets from generic preserved DOM fragments.

    Generic element blocks are used for class-driven visual clusters. Their
    classes may use background images, masks or pseudo-elements, so collect them
    as both inline class sets and icon/asset candidates.
    """
    classes = block.get("classes", [])
    collect_class_set(classes, inline_class_sets)
    for cls in classes or []:
        extra_icon_classes.add(cls)

    add_raw_style(raw_styles, f"element_{block.get('tag') or 'unknown'}", block.get("style"))

    for child in block.get("children", []) or []:
        if not isinstance(child, dict):
            continue

        display_item = child.get("displayItem")
        if display_item == "element":
            collect_from_element_block(
                child,
                inline_class_sets,
                extra_icon_classes,
                direct_image_assets,
                raw_styles,
            )
        elif display_item == "img":
            collect_class_set(child.get("classes", []), inline_class_sets)
            for cls in child.get("classes", []) or []:
                extra_icon_classes.add(cls)
            add_raw_style(raw_styles, "element_img", child.get("style"))
            add_direct_asset(
                direct_image_assets,
                child.get("src"),
                role="element_img",
                classes=child.get("classes", []),
                style=child.get("style"),
                alt=child.get("alt"),
            )
        else:
            collect_from_runs(child.get("runs", []), inline_class_sets)
            collect_class_set(child.get("classes", []), inline_class_sets)
            add_raw_style(raw_styles, f"element_child_{display_item or 'unknown'}", child.get("style"))


def collect_from_content_blocks(
    blocks,
    inline_class_sets,
    heading_classes,
    extra_icon_classes,
    direct_image_assets,
    table_class_sets,
    table_cell_class_sets,
    raw_styles,
):
    for block in blocks or []:
        if not block:
            continue

        # Some scraper fields are a single content block while others are
        # already lists of content blocks. Accept both shapes.
        if isinstance(block, list):
            collect_from_content_blocks(
                block,
                inline_class_sets,
                heading_classes,
                extra_icon_classes,
                direct_image_assets,
                table_class_sets,
                table_cell_class_sets,
                raw_styles,
            )
            continue

        if not isinstance(block, dict):
            continue

        display_item = block.get("displayItem")

        if display_item == "p":
            collect_from_runs(block.get("runs", []), inline_class_sets)
            add_raw_style(raw_styles, "p", block.get("style"))
            collect_class_set(block.get("classes", []), inline_class_sets)

        elif display_item in ("ul", "ol"):
            collect_class_set(block.get("classes", []), inline_class_sets)
            add_raw_style(raw_styles, display_item, block.get("style"))
            for item in block.get("items", []):
                collect_from_runs(item.get("runs", []), inline_class_sets)
                collect_class_set(item.get("classes", []), inline_class_sets)
                add_raw_style(raw_styles, f"{display_item}_item", item.get("style"))
                collect_from_content_blocks(
                    item.get("content", []),
                    inline_class_sets,
                    heading_classes,
                    extra_icon_classes,
                    direct_image_assets,
                    table_class_sets,
                    table_cell_class_sets,
                    raw_styles,
                )

        elif display_item == "cs_rule":
            collect_class_set(block.get("classes", []), inline_class_sets)
            add_raw_style(raw_styles, "cs_rule", block.get("style"))
            collect_from_content_blocks(
                block.get("content", []),
                inline_class_sets,
                heading_classes,
                extra_icon_classes,
                direct_image_assets,
                table_class_sets,
                table_cell_class_sets,
                raw_styles,
            )
            collect_from_requirement_html(
                block.get("requirement_html", ""),
                inline_class_sets,
                extra_icon_classes,
                direct_image_assets,
                raw_styles,
            )

        elif display_item == "subrule":
            collect_class_set(block.get("classes", []), inline_class_sets)
            add_raw_style(raw_styles, "subrule", block.get("style"))
            collect_from_content_blocks(
                block.get("content", []),
                inline_class_sets,
                heading_classes,
                extra_icon_classes,
                direct_image_assets,
                table_class_sets,
                table_cell_class_sets,
                raw_styles,
            )

        elif display_item == "table":
            collect_class_set(block.get("classes", []), table_class_sets)
            add_raw_style(raw_styles, "table", block.get("style"))

            for row in block.get("rows", []) or []:
                for cell in row or []:
                    if not isinstance(cell, dict):
                        continue
                    collect_from_runs(cell.get("runs", []), inline_class_sets)
                    collect_class_set(cell.get("classes", []), table_cell_class_sets)
                    add_raw_style(raw_styles, "table_cell", cell.get("style"))
                    collect_from_content_blocks(
                        cell.get("content", []),
                        inline_class_sets,
                        heading_classes,
                        extra_icon_classes,
                        direct_image_assets,
                        table_class_sets,
                        table_cell_class_sets,
                        raw_styles,
                    )

        elif display_item == "element":
            collect_from_element_block(
                block,
                inline_class_sets,
                extra_icon_classes,
                direct_image_assets,
                raw_styles,
            )

        elif display_item == "img":
            collect_class_set(block.get("classes", []), inline_class_sets)
            add_raw_style(raw_styles, "img", block.get("style"))
            add_direct_asset(
                direct_image_assets,
                block.get("src"),
                role="content_img",
                classes=block.get("classes", []),
                style=block.get("style"),
                alt=block.get("alt")
            )

        elif display_item == "br":
            continue

        else:
            # Be deliberately permissive: if future scraper output adds a new
            # displayItem that contains runs/content/classes/style, we still
            # collect the reusable styling information instead of silently
            # missing it.
            collect_from_runs(block.get("runs", []), inline_class_sets)
            collect_class_set(block.get("classes", []), inline_class_sets)
            add_raw_style(raw_styles, display_item or "unknown", block.get("style"))
            collect_from_content_blocks(
                block.get("content", []),
                inline_class_sets,
                heading_classes,
                extra_icon_classes,
                direct_image_assets,
                table_class_sets,
                table_cell_class_sets,
                raw_styles,
            )


def collect_army_rules_from_faction(
    faction_data,
    inline_class_sets,
    heading_classes,
    extra_icon_classes,
    direct_image_assets,
    table_class_sets,
    table_cell_class_sets,
    raw_styles,
):
    """Collect CSS/classes/assets used by army rule cards in a faction/sub-faction node."""
    for rule in (faction_data.get("army_rules") or {}).values():
        for cls in rule.get("heading_class", []):
            heading_classes.add(cls)

        collect_from_content_blocks(
            rule.get("content", []),
            inline_class_sets,
            heading_classes,
            extra_icon_classes,
            direct_image_assets,
            table_class_sets,
            table_cell_class_sets,
            raw_styles,
        )


def collect_unit_card(
    card,
    inline_class_sets,
    heading_classes,
    extra_icon_classes,
    direct_image_assets,
    table_class_sets,
    table_cell_class_sets,
    raw_styles,
):
    """Collect CSS/classes/assets used by a unit datasheet/card."""
    for section in card.get("sections", []) or []:
        for item in section.get("items", []) or []:
            collect_from_content_blocks(
                item.get("content", []),
                inline_class_sets,
                heading_classes,
                extra_icon_classes,
                direct_image_assets,
                table_class_sets,
                table_cell_class_sets,
                raw_styles,
            )


def collect_detachment_card(
    card,
    inline_class_sets,
    heading_classes,
    stratagem_color_classes,
    stratagem_icon_classes,
    extra_icon_classes,
    direct_image_assets,
    table_class_sets,
    table_cell_class_sets,
    raw_styles,
):
    """Collect CSS/classes/assets used by a detachment card."""
    for rule in card.get("rules", []) or []:
        for cls in rule.get("heading_class", []):
            heading_classes.add(cls)

        collect_from_content_blocks(
            rule.get("content", []),
            inline_class_sets,
            heading_classes,
            extra_icon_classes,
            direct_image_assets,
            table_class_sets,
            table_cell_class_sets,
            raw_styles,
        )

    for enhancement in card.get("enhancements", []) or []:
        collect_from_content_blocks(
            enhancement.get("content", []),
            inline_class_sets,
            heading_classes,
            extra_icon_classes,
            direct_image_assets,
            table_class_sets,
            table_cell_class_sets,
            raw_styles,
        )

    for stratagem in card.get("stratagems", []) or []:
        color_class = stratagem.get("color_class")
        if color_class:
            stratagem_color_classes.add(color_class)

        for icon_class in stratagem.get("icon_classes", []) or []:
            stratagem_icon_classes.add(icon_class)

        for key in ("when", "target", "effect", "restrictions"):
            block = stratagem.get(key)
            if block:
                collect_from_content_blocks(
                    [block],
                    inline_class_sets,
                    heading_classes,
                    extra_icon_classes,
                    direct_image_assets,
                    table_class_sets,
                    table_cell_class_sets,
                    raw_styles,
                )


def iter_faction_nodes(units_data):
    """
    Yield every manifest node that can contain unit_cards, detachment_cards or
    army_rules. This includes top-level factions and any nested sub-factions.
    """
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


def collect_css_classes(units_data):
    inline_class_sets = set()
    heading_classes = set()
    stratagem_color_classes = set()
    stratagem_icon_classes = set()
    extra_icon_classes = set()
    direct_image_assets = {}
    table_class_sets = set()
    table_cell_class_sets = set()
    raw_styles = set()

    for faction_data in iter_faction_nodes(units_data):
        collect_army_rules_from_faction(
            faction_data,
            inline_class_sets,
            heading_classes,
            extra_icon_classes,
            direct_image_assets,
            table_class_sets,
            table_cell_class_sets,
            raw_styles,
        )

        for card in (faction_data.get("unit_cards") or {}).values():
            collect_unit_card(
                card,
                inline_class_sets,
                heading_classes,
                extra_icon_classes,
                direct_image_assets,
                table_class_sets,
                table_cell_class_sets,
                raw_styles,
            )

        for card in (faction_data.get("detachment_cards") or {}).values():
            collect_detachment_card(
                card,
                inline_class_sets,
                heading_classes,
                stratagem_color_classes,
                stratagem_icon_classes,
                extra_icon_classes,
                direct_image_assets,
                table_class_sets,
                table_cell_class_sets,
                raw_styles,
            )

    return {
        "inline_class_sets": sorted(
            [list(classes) for classes in inline_class_sets],
            key=lambda x: class_key(x)
        ),
        "heading_classes": sorted(heading_classes),
        "stratagem_color_classes": sorted(stratagem_color_classes),
        "stratagem_icon_classes": sorted(stratagem_icon_classes),
        "extra_icon_classes": sorted(extra_icon_classes),
        "table_class_sets": sorted(
            [list(classes) for classes in table_class_sets],
            key=lambda x: class_key(x)
        ),
        "table_cell_class_sets": sorted(
            [list(classes) for classes in table_cell_class_sets],
            key=lambda x: class_key(x)
        ),
        "raw_styles": sorted(
            [{"role": role, "style": style} for role, style in raw_styles],
            key=lambda item: style_key(item["role"], item["style"])
        ),
        "direct_image_assets": sorted(
            direct_image_assets.values(),
            key=lambda item: item["src"]
        ),
    }


def read_computed_styles(page, class_sets):
    if not class_sets:
        return {}

    payload = [
        {
            "key": class_key(classes),
            "classes": classes
        }
        for classes in class_sets
    ]

    return page.evaluate(
        """
        (items) => {
            const result = {};

            for (const item of items) {
                const el = document.createElement("span");
                el.className = item.classes.join(" ");
                el.textContent = "Sample";
                document.body.appendChild(el);

                const style = window.getComputedStyle(el);

                result[item.key] = {
                    color: style.color,
                    backgroundColor: style.backgroundColor,
                    borderColor: style.borderColor,
                    fontWeight: style.fontWeight,
                    fontStyle: style.fontStyle,
                    textTransform: style.textTransform,
                    textDecoration: style.textDecorationLine
                };

                el.remove();
            }

            return result;
        }
        """,
        payload
    )


def read_single_class_styles(page, classes):
    return read_computed_styles(page, [[cls] for cls in classes])


def read_icon_styles(page, icon_classes):
    if not icon_classes:
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
                    width: style.width,
                    height: style.height,
                    color: style.color,
                    backgroundColor: style.backgroundColor,
                    backgroundImage: style.backgroundImage,
                    maskImage: style.maskImage,
                    webkitMaskImage: style.webkitMaskImage,
                    filter: style.filter,
                    backgroundSize: style.backgroundSize,
                    backgroundRepeat: style.backgroundRepeat,
                    backgroundPosition: style.backgroundPosition
                };

                el.remove();
            }

            return result;
        }
        """,
        icon_classes
    )



def read_asset_candidate_styles(page, class_sets):
    """Read URL-bearing CSS from elements and their ::before/::after pseudo-elements.

    This is intentionally broader than read_icon_styles: some army-rule tables use
    cell classes (for example td_w) whose visual assets are attached through CSS
    pseudo-elements rather than direct <img> blocks.
    """
    if not class_sets:
        return {}

    payload = [
        {
            "key": class_key(classes),
            "classes": classes
        }
        for classes in class_sets
    ]

    return page.evaluate(
        """
        (items) => {
            const fields = [
                "backgroundImage",
                "maskImage",
                "webkitMaskImage",
                "content",
                "listStyleImage",
                "borderImageSource"
            ];

            function snapshot(style) {
                const out = {
                    width: style.width,
                    height: style.height,
                    color: style.color,
                    backgroundColor: style.backgroundColor,
                    backgroundSize: style.backgroundSize,
                    backgroundRepeat: style.backgroundRepeat,
                    backgroundPosition: style.backgroundPosition,
                    filter: style.filter
                };

                for (const field of fields) {
                    out[field] = style[field] || "";
                }

                return out;
            }

            const result = {};

            for (const item of items) {
                const el = document.createElement("div");
                el.className = item.classes.join(" ");
                el.textContent = "Sample";
                document.body.appendChild(el);

                result[item.key] = {
                    element: snapshot(window.getComputedStyle(el)),
                    before: snapshot(window.getComputedStyle(el, "::before")),
                    after: snapshot(window.getComputedStyle(el, "::after"))
                };

                el.remove();
            }

            return result;
        }
        """,
        payload
    )


def extract_css_urls(value):
    if not value or value == "none":
        return []

    return [
        match.strip('\'"')
        for match in re.findall(r'url\((.*?)\)', value)
        if match.strip('\'"')
    ]


def extract_css_url(value):
    urls = extract_css_urls(value)
    return urls[0] if urls else None


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


def download_asset(url, output_dir, class_name):
    os.makedirs(output_dir, exist_ok=True)

    filename = safe_asset_name(class_name, url)
    output_path = os.path.join(output_dir, filename)

    if os.path.exists(output_path):
        return output_path

    response = requests.get(url, timeout=30)
    response.raise_for_status()

    with open(output_path, "wb") as f:
        f.write(response.content)

    return output_path


def localize_style_asset_fields(entry, base_url, asset_dir, asset_name_prefix):
    """Add URL/asset fields next to any CSS value containing url(...).

    Handles plain style dicts and nested pseudo-element style dicts such as
    {"element": {...}, "before": {...}, "after": {...}}.
    """
    url_fields = (
        "backgroundImage",
        "maskImage",
        "webkitMaskImage",
        "content",
        "listStyleImage",
        "borderImageSource",
    )

    def localize_dict(target, name_prefix):
        for field in url_fields:
            urls = extract_css_urls(target.get(field))
            if not urls:
                continue

            target[f"{field}Urls"] = []
            target[f"{field}Assets"] = []

            # Keep backwards-compatible singular fields for the common one-URL case.
            for index, raw_url in enumerate(urls):
                absolute_url = urljoin(base_url, raw_url)
                local_path = download_asset(
                    absolute_url,
                    asset_dir,
                    f"{name_prefix}_{field}_{index}"
                )

                target[f"{field}Urls"].append(absolute_url)
                target[f"{field}Assets"].append(local_path.replace("\\", "/"))

            if len(urls) == 1:
                target[f"{field}Url"] = target[f"{field}Urls"][0]
                target[f"{field}Asset"] = target[f"{field}Assets"][0]

    localize_dict(entry, asset_name_prefix)

    for nested_key in ("element", "before", "after"):
        nested = entry.get(nested_key)
        if isinstance(nested, dict):
            localize_dict(nested, f"{asset_name_prefix}_{nested_key}")

    return entry


def localize_icon_assets(icon_styles, base_url, asset_dir):
    localized = {}

    for class_name, styles in icon_styles.items():
        entry = dict(styles)
        localized[class_name] = localize_style_asset_fields(
            entry,
            base_url,
            asset_dir,
            class_name
        )

    return localized


def localize_direct_image_assets(image_assets, base_url, asset_dir):
    localized = {}

    for asset in image_assets:
        src = asset.get("src")
        if not src:
            continue

        absolute_url = urljoin(base_url, src)
        local_path = download_asset(
            absolute_url,
            asset_dir,
            direct_asset_name(src)
        )

        localized[src] = {
            **asset,
            "url": absolute_url,
            "asset": local_path.replace("\\", "/")
        }

    return localized


def build_manifest(units_json, wahapedia_url, asset_dir):
    with open(units_json, "r", encoding="utf-8") as f:
        units_data = json.load(f)

    classes = collect_css_classes(units_data)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()

        page.goto(wahapedia_url, wait_until="domcontentloaded")

        inline_styles = read_computed_styles(
            page,
            classes["inline_class_sets"]
        )

        heading_styles = read_single_class_styles(
            page,
            classes["heading_classes"]
        )

        stratagem_color_styles = read_single_class_styles(
            page,
            classes["stratagem_color_classes"]
        )

        stratagem_icon_styles = read_icon_styles(
            page,
            classes["stratagem_icon_classes"]
        )

        extra_icon_styles = read_icon_styles(
            page,
            classes["extra_icon_classes"]
        )

        table_styles = read_computed_styles(
            page,
            classes["table_class_sets"]
        )

        table_cell_styles = read_computed_styles(
            page,
            classes["table_cell_class_sets"]
        )

        asset_candidate_class_sets = []
        seen_asset_candidate_keys = set()

        for source in (
            classes["stratagem_icon_classes"],
            classes["extra_icon_classes"],
            classes["table_class_sets"],
            classes["table_cell_class_sets"],
            classes["inline_class_sets"],
        ):
            for item in source:
                classes_for_item = [item] if isinstance(item, str) else list(item)
                key = class_key(classes_for_item)
                if key and key not in seen_asset_candidate_keys:
                    seen_asset_candidate_keys.add(key)
                    asset_candidate_class_sets.append(classes_for_item)

        asset_candidate_styles = read_asset_candidate_styles(
            page,
            asset_candidate_class_sets
        )

        browser.close()

    stratagem_icon_styles = localize_icon_assets(
        stratagem_icon_styles,
        wahapedia_url,
        asset_dir
    )

    extra_icon_styles = localize_icon_assets(
        extra_icon_styles,
        wahapedia_url,
        asset_dir
    )

    direct_image_assets = localize_direct_image_assets(
        classes["direct_image_assets"],
        wahapedia_url,
        asset_dir
    )

    asset_candidate_styles = localize_icon_assets(
        asset_candidate_styles,
        wahapedia_url,
        asset_dir
    )

    return {
        "source": wahapedia_url,
        "asset_dir": asset_dir.replace("\\", "/"),
        "classes_found": {
            "inline_class_sets": [
                class_key(classes)
                for classes in classes["inline_class_sets"]
            ],
            "heading_classes": classes["heading_classes"],
            "stratagem_color_classes": classes["stratagem_color_classes"],
            "stratagem_icon_classes": classes["stratagem_icon_classes"],
            "extra_icon_classes": classes["extra_icon_classes"],
            "table_class_sets": [
                class_key(classes)
                for classes in classes["table_class_sets"]
            ],
            "table_cell_class_sets": [
                class_key(classes)
                for classes in classes["table_cell_class_sets"]
            ],
            "direct_image_srcs": [
                asset["src"]
                for asset in classes["direct_image_assets"]
            ],
            "asset_candidate_class_sets": sorted(asset_candidate_styles.keys()),
        },
        "inline_classes": inline_styles,
        "heading_classes": heading_styles,
        "stratagem_colors": stratagem_color_styles,
        "stratagem_icons": stratagem_icon_styles,
        "extra_icons": extra_icon_styles,
        "tables": table_styles,
        "table_cells": table_cell_styles,
        "raw_styles": classes["raw_styles"],
        "direct_image_assets": direct_image_assets,
        "asset_candidate_styles": asset_candidate_styles,
    }


def parse_args():
    parser = argparse.ArgumentParser(
        description="Build renderer CSS manifest from detachment card JSON"
    )

    parser.add_argument(
        "--units-json",
        default="units.json",
        help="Path to units.json"
    )

    parser.add_argument(
        "--wahapedia-url",
        default="https://wahapedia.ru/wh40k10ed/the-rules/quick-start-guide/",
        help="A Wahapedia page URL with the relevant CSS loaded"
    )

    parser.add_argument(
        "--output",
        default="detachment_css_manifest.json",
        help="Output manifest path"
    )

    parser.add_argument(
        "--asset-dir",
        default="assets",
        help="Directory where downloaded Wahapedia image assets are stored"
    )

    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()

    manifest = build_manifest(
        args.units_json,
        args.wahapedia_url,
        args.asset_dir
    )

    os.makedirs(
        os.path.dirname(args.output) or ".",
        exist_ok=True
    )

    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2, ensure_ascii=False)

    print(f"Wrote CSS manifest: {args.output}")
