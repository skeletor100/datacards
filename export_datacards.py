import argparse
import os
import re
from pathlib import Path
from playwright.sync_api import sync_playwright

BASE_URL = "http://localhost:8000/index.html"
OUTPUT_DIR = Path("rendered_cards")
NONE_LABELS = {"None", "__NONE__"}

def safe_file_name(value):
    return re.sub(r'[<>:"/\\|?*\x00-\x1F]', "", str(value)).strip()

def safe_dir_name(value):
    value = safe_file_name(value)
    value = re.sub(r"\s+", "_", value)
    return value

def wait_for_render(page):
    page.evaluate("""
      () => new Promise(resolve => {
        requestAnimationFrame(() => requestAnimationFrame(resolve));
      })
    """)

def screenshot_current_card(page, output_path):
    card = page.locator(".datasheet-card")
    card.wait_for(state="visible")
    wait_for_render(page)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    card.screenshot(path=str(output_path), type="png")
    print(f"Saved {output_path}")

def selected_text(page, selector):
    return page.locator(f"{selector} option:checked").inner_text()

def select_by_text(page, selector, wanted):
    options = page.locator(f"{selector} option")
    wanted_upper = wanted.strip().upper()

    for i in range(options.count()):
        text = options.nth(i).inner_text().strip()
        value = options.nth(i).get_attribute("value")

        if text.upper() == wanted_upper or str(value).strip().upper() == wanted_upper:
            page.select_option(selector, index=i)
            wait_for_render(page)
            return

    raise ValueError(f"Could not find {wanted!r} in {selector}")

def get_output_path(page):
    faction = selected_text(page, "#faction-primary")
    unit = selected_text(page, "#unit-select")

    secondary = page.locator("#faction-secondary")
    parts = [safe_dir_name(faction)]

    if secondary.is_visible():
        subfaction = selected_text(page, "#faction-secondary")
        if subfaction not in NONE_LABELS:
            parts.append(safe_dir_name(subfaction))

    return OUTPUT_DIR.joinpath(*parts) / f"{safe_file_name(unit)}.png"

def export_current_unit(page):
    screenshot_current_card(page, get_output_path(page))

def export_selected_subfaction(page):
    unit_options = page.locator("#unit-select option")
    for i in range(unit_options.count()):
        page.select_option("#unit-select", index=i)
        wait_for_render(page)
        export_current_unit(page)

def export_selected_faction(page):
    secondary = page.locator("#faction-secondary")

    if secondary.is_visible():
        options = secondary.locator("option")
        for i in range(options.count()):
            page.select_option("#faction-secondary", index=i)
            wait_for_render(page)
            export_selected_subfaction(page)
    else:
        export_selected_subfaction(page)

def export_all_factions(page):
    primary_options = page.locator("#faction-primary option")

    for i in range(primary_options.count()):
        page.select_option("#faction-primary", index=i)
        wait_for_render(page)
        export_selected_faction(page)

def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default=BASE_URL)
    parser.add_argument("--output", default=str(OUTPUT_DIR))
    parser.add_argument("--faction")
    parser.add_argument("--subFaction")
    parser.add_argument("--unit")
    parser.add_argument("--scale", type=float, default=2)
    parser.add_argument("--headed", action="store_true")
    return parser.parse_args()

def validate_args(args):
    if args.subFaction and not args.faction:
        raise SystemExit("--subFaction requires --faction")

    if args.unit and not args.subFaction:
        raise SystemExit("--unit requires --subFaction")

def main():
    args = parse_args()
    validate_args(args)

    global OUTPUT_DIR
    OUTPUT_DIR = Path(args.output)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not args.headed)
        page = browser.new_page(
            viewport={"width": 1800, "height": 1200},
            device_scale_factor=args.scale
        )

        page.goto(args.url, wait_until="networkidle")
        page.wait_for_selector(".datasheet-card")
        wait_for_render(page)

        if args.faction:
            select_by_text(page, "#faction-primary", args.faction)

        if args.subFaction:
            secondary = page.locator("#faction-secondary")
            if not secondary.is_visible():
                raise SystemExit("Selected faction has no visible sub-faction dropdown")
            select_by_text(page, "#faction-secondary", args.subFaction)

        if args.unit:
            select_by_text(page, "#unit-select", args.unit)
            export_current_unit(page)
        elif args.subFaction:
            export_selected_subfaction(page)
        elif args.faction:
            export_selected_faction(page)
        else:
            export_all_factions(page)

        browser.close()

if __name__ == "__main__":
    main()