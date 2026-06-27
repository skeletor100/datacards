import argparse
import json
import os
import re
import hashlib
from urllib.parse import urljoin, urlparse

import requests
from playwright.sync_api import sync_playwright


def class_key(classes):
    return " ".join(sorted(c for c in classes if c))


def collect_from_runs(runs, inline_class_sets):
    for run in runs or []:
        classes = run.get("source_classes", [])
        if classes:
            inline_class_sets.add(tuple(sorted(classes)))


def collect_from_content_blocks(blocks, inline_class_sets, heading_classes):
    for block in blocks or []:
        if not block:
            continue

        if block.get("displayItem") == "p":
            collect_from_runs(block.get("runs", []), inline_class_sets)

        elif block.get("displayItem") in ("ul", "ol"):
            for item in block.get("items", []):
                collect_from_runs(item.get("runs", []), inline_class_sets)

        elif block.get("displayItem") == "subrule":
            collect_from_content_blocks(
                block.get("content", []),
                inline_class_sets,
                heading_classes
            )


def collect_css_classes(units_data):
    inline_class_sets = set()
    heading_classes = set()
    stratagem_color_classes = set()
    stratagem_icon_classes = set()

    for faction_data in units_data.values():
        for card in faction_data.get("detachment_cards", {}).values():
            for rule in card.get("rules", []):
                for cls in rule.get("heading_class", []):
                    heading_classes.add(cls)

                collect_from_content_blocks(
                    rule.get("content", []),
                    inline_class_sets,
                    heading_classes
                )

            for enhancement in card.get("enhancements", []):
                collect_from_content_blocks(
                    enhancement.get("content", []),
                    inline_class_sets,
                    heading_classes
                )

            for stratagem in card.get("stratagems", []):
                color_class = stratagem.get("color_class")
                if color_class:
                    stratagem_color_classes.add(color_class)

                for icon_class in stratagem.get("icon_classes", []):
                    stratagem_icon_classes.add(icon_class)

                for key in ("when", "target", "effect", "restrictions"):
                    block = stratagem.get(key)
                    if block:
                        collect_from_content_blocks(
                            [block],
                            inline_class_sets,
                            heading_classes
                        )

    return {
        "inline_class_sets": sorted(
            [list(classes) for classes in inline_class_sets],
            key=lambda x: class_key(x)
        ),
        "heading_classes": sorted(heading_classes),
        "stratagem_color_classes": sorted(stratagem_color_classes),
        "stratagem_icon_classes": sorted(stratagem_icon_classes),
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


def extract_css_url(value):
    if not value or value == "none":
        return None

    match = re.search(r'url\((.*?)\)', value)
    if not match:
        return None

    return match.group(1).strip('\'"')


def safe_asset_name(class_name, url):
    parsed = urlparse(url)
    base = os.path.basename(parsed.path) or "asset"
    _, ext = os.path.splitext(base)

    if not ext:
        ext = ".bin"

    digest = hashlib.sha1(url.encode("utf-8")).hexdigest()[:8]
    return f"{class_name}_{digest}{ext}"


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


def localize_icon_assets(icon_styles, base_url, asset_dir):
    localized = {}

    for class_name, styles in icon_styles.items():
        entry = dict(styles)

        for field in ("backgroundImage", "maskImage", "webkitMaskImage"):
            raw_url = extract_css_url(styles.get(field))

            if not raw_url:
                continue

            absolute_url = urljoin(base_url, raw_url)
            local_path = download_asset(
                absolute_url,
                asset_dir,
                class_name
            )

            entry[f"{field}Url"] = absolute_url
            entry[f"{field}Asset"] = local_path.replace("\\", "/")

        localized[class_name] = entry

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

        browser.close()

    stratagem_icon_styles = localize_icon_assets(
        stratagem_icon_styles,
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
        },
        "inline_classes": inline_styles,
        "heading_classes": heading_styles,
        "stratagem_colors": stratagem_color_styles,
        "stratagem_icons": stratagem_icon_styles
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