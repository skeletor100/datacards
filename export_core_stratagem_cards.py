import argparse
from pathlib import Path
from playwright.sync_api import sync_playwright

BASE_URL = "http://localhost:8000/core_stratagems_index.html"
OUTPUT_DIR = Path("rendered_cards")


def wait_for_render(page):
    page.evaluate("""
      () => new Promise(resolve => {
        requestAnimationFrame(() => requestAnimationFrame(resolve));
      })
    """)


def parse_args():
    parser = argparse.ArgumentParser(description="Screenshot rendered core stratagem cards")
    parser.add_argument("--url", default=BASE_URL)
    parser.add_argument("--output", default=str(OUTPUT_DIR))
    parser.add_argument("--scale", type=float, default=2)
    parser.add_argument("--headed", action="store_true")
    parser.add_argument(
        "--filename",
        default="Core Stratagems.png",
        help="Output filename used when there is only one page",
    )
    parser.add_argument(
        "--filename-prefix",
        default="Core Stratagems",
        help="Output filename prefix used when there are multiple pages",
    )
    return parser.parse_args()


def main():
    args = parse_args()
    output_dir = Path(args.output)
    output_path = output_dir / args.filename

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not args.headed)
        context = browser.new_context(
            viewport={"width": 1400, "height": 2000},
            device_scale_factor=args.scale,
        )
        page = context.new_page()
        page.goto(args.url, wait_until="networkidle")

        # Pagination means this selector may match 2+ cards. Do not use
        # page.locator(".core-stratagem-card").wait_for(), because Playwright
        # strict mode requires a locator to resolve to exactly one element.
        page.wait_for_selector(".core-stratagem-card", state="visible")
        wait_for_render(page)

        output_dir.mkdir(parents=True, exist_ok=True)
        cards = page.locator(".core-stratagem-card")
        count = cards.count()

        if count == 0:
            raise RuntimeError("No .core-stratagem-card elements were rendered")

        print(f"Found {count} core stratagem page(s)")

        if count == 1:
            cards.nth(0).screenshot(path=str(output_path), type="png")
            print(f"Saved {output_path}")
        else:
            for i in range(count):
                card = cards.nth(i)
                page_output_path = output_dir / f"{args.filename_prefix} {i + 1}.png"
                card.screenshot(path=str(page_output_path), type="png")
                print(f"Saved {page_output_path}")

        context.close()
        browser.close()


if __name__ == "__main__":
    main()
