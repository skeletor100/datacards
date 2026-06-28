import argparse, re
from pathlib import Path
from playwright.sync_api import sync_playwright
BASE_URL='http://localhost:8000/detachment_index.html'
OUTPUT_DIR=Path('rendered_cards')
def safe(v): return re.sub(r'[<>:"/\\|?*\x00-\x1F]','',str(v)).strip()
def safe_dir(v): return re.sub(r'\s+','_',safe(v))
def wait(page, stable_frames=5):
    page.evaluate(
        """async (stableFrames) => {
            if (document.fonts)
                await document.fonts.ready;

            let last = "";
            let stable = 0;

            function snapshot() {
                const body = document.body;

                return JSON.stringify({
                    width: body.scrollWidth,
                    height: body.scrollHeight,
                    textLength: body.innerText.length,
                    htmlLength: body.innerHTML.length
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
        stable_frames
    )
def selected(page, sel): return page.locator(f'{sel} option:checked').inner_text().strip()
def select_text(page, sel, wanted):
    opts=page.locator(f'{sel} option'); w=wanted.strip().upper()
    for i in range(opts.count()):
        t=opts.nth(i).inner_text().strip(); val=opts.nth(i).get_attribute('value') or ''
        if t.upper()==w or val.strip().upper()==w:
            page.select_option(sel,index=i); wait(page); return
    raise SystemExit(f'Could not find {wanted!r} in {sel}')
def screenshot_visible_cards(page, outdir):
    faction = selected(page, "#faction-primary")
    secondary = page.locator("#sub-faction")
    subfaction = selected(page, "#sub-faction") if secondary.is_visible() else "None"
    det = selected(page, "#detachment")

    parts = [safe_dir(faction)]
    if subfaction != "None":
        parts.append(safe_dir(subfaction))

    base = outdir.joinpath(*parts) / safe_dir(det)
    base.mkdir(parents=True, exist_ok=True)

    for name in ("rules", "stratagems"):
        page.select_option("#show", name)
        wait(page)

        card = page.locator(f'.detachment-card[data-card="{name}"]')

        if card.count() == 0:
            if name == "stratagems":
                continue
            raise RuntimeError("Rules card was not rendered")

        box = card.bounding_box()

        page.screenshot(
            path=str(base / f"{name}.png"),
            clip={
                "x": box["x"],
                "y": box["y"],
                "width": 1200,
                "height": 1800,
            }
        )
        print(f"Saved {base / f'{name}.png'}")
def export_detachment(page,outdir): screenshot_visible_cards(page,outdir)
def export_selected_subfaction(page, outdir):
    opts = page.locator("#detachment option")
    for i in range(opts.count()):
        page.select_option("#detachment", index=i)
        wait(page)
        export_detachment(page, outdir)

def export_faction(page, outdir):
    secondary = page.locator("#sub-faction")

    if secondary.is_visible():
        opts = secondary.locator("option")
        for i in range(opts.count()):
            page.select_option("#sub-faction", index=i)
            wait(page)
            export_selected_subfaction(page, outdir)
    else:
        export_selected_subfaction(page, outdir)

def export_all(page, outdir):
    opts = page.locator("#faction-primary option")
    for i in range(opts.count()):
        page.select_option("#faction-primary", index=i)
        wait(page)
        export_faction(page, outdir)
def main():
    ap=argparse.ArgumentParser(); ap.add_argument('--url',default=BASE_URL); ap.add_argument('--output',default=str(OUTPUT_DIR)); ap.add_argument('--faction'); ap.add_argument("--subFaction"); ap.add_argument('--detachment'); ap.add_argument('--scale',type=float,default=2); ap.add_argument('--headed',action='store_true')
    a=ap.parse_args()
    if a.subFaction and not a.faction:
        raise SystemExit("--subFaction requires --faction")

    if a.detachment and not a.faction:
        raise SystemExit("--detachment requires --faction")
    out=Path(a.output)
    with sync_playwright() as p:
        browser=p.chromium.launch(headless=not a.headed)
        page = browser.new_page(
            viewport={"width": 1300, "height": 1900},
            device_scale_factor=a.scale
        )
        page.goto(a.url, wait_until='networkidle'); page.wait_for_selector('.detachment-card'); wait(page)
        if a.faction:
            select_text(page, "#faction-primary", a.faction)

        if a.subFaction:
            secondary = page.locator("#sub-faction")
            if not secondary.is_visible():
                raise SystemExit("Selected faction has no visible sub-faction dropdown")
            select_text(page, "#sub-faction", a.subFaction)

        if a.detachment:
            select_text(page, "#detachment", a.detachment)
            export_detachment(page, out)
        elif a.subFaction:
            export_selected_subfaction(page, out)
        elif a.faction:
            export_faction(page, out)
        else:
            export_all(page, out)
        browser.close()
if __name__=='__main__': main()
