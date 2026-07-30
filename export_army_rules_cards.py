import argparse
import re
from pathlib import Path
from playwright.sync_api import sync_playwright

# army_rules.html is now the merged Army Rules / Detachment Rules / Unit
# Rules page (see the "Rule Type" dropdown) — this script selects "Army
# Rules" mode and otherwise works the same way it always did.
BASE_URL = "http://localhost:8000/army_rules.html"
OUTPUT_DIR = Path("rendered_cards")
NONE_LABELS = {"None", "__NONE__", "__parent__"}


def safe_file_name(value):
    return re.sub(r'[<>:"/\\|?*\x00-\x1F]', "", str(value)).strip()


def safe_dir_name(value):
    value = safe_file_name(value)
    return re.sub(r"\s+", "_", value)


def wait_for_render(page, stable_frames=5):
    page.evaluate(
        """async (stableFrames) => {
            if (document.fonts)
                await document.fonts.ready;

            let last = "";
            let stable = 0;

            function snapshot() {
                const cards = [...document.querySelectorAll('.army-rules-card')];
                return JSON.stringify({
                    bodyWidth: document.body.scrollWidth,
                    bodyHeight: document.body.scrollHeight,
                    cardCount: cards.length,
                    cards: cards.map(card => {
                        const fit = card.querySelector('.army-rules-fit');
                        const content = card.querySelector('.content');
                        return {
                            textLength: card.innerText.length,
                            htmlLength: card.innerHTML.length,
                            contentHeight: content ? content.clientHeight : 0,
                            fitHeight: fit ? fit.scrollHeight : 0,
                            ruleFs: getComputedStyle(card).getPropertyValue('--rule-fs')
                        };
                    })
                });
            }

            return new Promise(resolve => {
                function tick() {
                    const current = snapshot();
                    if (current === last) {
                        stable++;
                        if (stable >= stableFrames) {
                            resolve();
                            return;
                        }
                    } else {
                        stable = 0;
                        last = current;
                    }
                    requestAnimationFrame(tick);
                }
                requestAnimationFrame(tick);
            });
        }""",
        stable_frames,
    )


def selected_text(page, selector):
    return page.locator(f"{selector} option:checked").inner_text().strip()


def selected_value(page, selector):
    return (page.locator(f"{selector} option:checked").get_attribute("value") or "").strip()


def select_by_text(page, selector, wanted):
    options = page.locator(f"{selector} option")
    wanted_upper = wanted.strip().upper()

    for i in range(options.count()):
        text = options.nth(i).inner_text().strip()
        value = options.nth(i).get_attribute("value") or ""

        if text.upper() == wanted_upper or value.strip().upper() == wanted_upper:
            page.select_option(selector, index=i)
            wait_for_render(page)
            return

    raise SystemExit(f"Could not find {wanted!r} in {selector}")


def select_rule_type(page):
    page.select_option("#rule-type", "army")
    wait_for_render(page)


def get_output_path(page, outdir):
    faction = selected_text(page, "#faction-primary")
    parts = [safe_dir_name(faction)]

    secondary = page.locator("#sub-faction")
    secondary_row = page.locator("#sub-faction-row")

    if secondary.count() and secondary_row.count() and not secondary_row.evaluate("el => el.classList.contains('hidden')"):
        subfaction = selected_text(page, "#sub-faction")
        sub_value = selected_value(page, "#sub-faction")
        if subfaction and subfaction != faction and sub_value not in NONE_LABELS:
            parts.append(safe_dir_name(subfaction))

    return outdir.joinpath(*parts) / "army_rules.png"


def screenshot_current_army_rule_card(page, outdir):
    wait_for_render(page)

    card = page.locator(".army-rules-card").first
    if card.count() == 0:
        # Strict lookup (see renderArmyRulesPage): this faction/sub-faction
        # combo has no army rules of its own, so the page rendered nothing
        # rather than falling back to some other faction's content. Skip
        # instead of erroring or saving a stale/duplicate screenshot.
        print(f"Skipping {selected_text(page, '#faction-primary')} "
              f"/ {selected_text(page, '#sub-faction') if page.locator('#sub-faction').count() else 'None'}: no army rules")
        return

    card.wait_for(state="visible")

    output_path = get_output_path(page, outdir)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    card.screenshot(path=str(output_path), type="png")
    print(f"Saved {output_path}")


def export_selected_faction(page, outdir):
    secondary = page.locator("#sub-faction")
    secondary_row = page.locator("#sub-faction-row")

    if secondary.count() and secondary_row.count() and not secondary_row.evaluate("el => el.classList.contains('hidden')"):
        options = secondary.locator("option")
        if options.count() == 0:
            print(f"Skipping {selected_text(page, '#faction-primary')}: no sub-faction options for Army Rules")
            return
        for i in range(options.count()):
            page.select_option("#sub-faction", index=i)
            wait_for_render(page)
            screenshot_current_army_rule_card(page, outdir)
    else:
        screenshot_current_army_rule_card(page, outdir)


def export_all_factions(page, outdir):
    options = page.locator("#faction-primary option")
    for i in range(options.count()):
        page.select_option("#faction-primary", index=i)
        wait_for_render(page)
        export_selected_faction(page, outdir)


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default=BASE_URL)
    parser.add_argument("--output", default=str(OUTPUT_DIR))
    parser.add_argument("--faction")
    parser.add_argument("--subFaction")
    parser.add_argument("--scale", type=float, default=2)
    parser.add_argument("--headed", action="store_true")
    return parser.parse_args()


def validate_args(args):
    if args.subFaction and not args.faction:
        raise SystemExit("--subFaction requires --faction")


def main():
    args = parse_args()
    validate_args(args)
    outdir = Path(args.output)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not args.headed)
        page = browser.new_page(
            viewport={"width": 1300, "height": 1900},
            device_scale_factor=args.scale,
        )

        page.goto(args.url, wait_until="networkidle")
        page.wait_for_selector("#faction-primary option", state="attached")
        select_rule_type(page)

        if args.faction:
            select_by_text(page, "#faction-primary", args.faction)

        if args.subFaction:
            secondary_row = page.locator("#sub-faction-row")
            secondary = page.locator("#sub-faction")
            if not secondary.count() or secondary_row.evaluate("el => el.classList.contains('hidden')"):
                raise SystemExit("Selected faction has no visible sub-faction dropdown")
            select_by_text(page, "#sub-faction", args.subFaction)
            screenshot_current_army_rule_card(page, outdir)
        elif args.faction:
            export_selected_faction(page, outdir)
        else:
            export_all_factions(page, outdir)

        browser.close()


if __name__ == "__main__":
    main()
