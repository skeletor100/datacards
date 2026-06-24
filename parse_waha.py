import json
import argparse
from playwright.sync_api import sync_playwright
from bs4 import BeautifulSoup
from datacard_parser import run as parse_datacard
from detachment_scraper import run as scrape_detachment
import time

import threading
import queue

job_queue = queue.Queue(maxsize=10)
workers = []

def worker(failed_units):
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context()
        page = context.new_page()

        while True:
            job = job_queue.get()

            if job is None:
                job_queue.task_done()
                break

            url, name = job

            try:
                print(f"Picked up job: {name} | URL: {url}")
                parse_datacard(page, url)
            except Exception as e:
                failed_units.append((name, url, str(e)))
                print(f"Failed to process: {name} | URL: {url} | Error: {e}")

            job_queue.task_done()

        context.close()
        browser.close()

DOMAIN = "https://wahapedia.ru"

def parse_args():
    parser = argparse.ArgumentParser(description="Wahapedia faction extractor")
    parser.add_argument(
        "--faction",
        help="Only process this faction name"
    )
    parser.add_argument(
        "--no-units",
        action="store_true",
        help="Do not extract unit data cards"
    )
    parser.add_argument(
        "--no-detachments",
        action="store_true",
        help="Do not extract detachment data cards"
    )
    parser.add_argument(
        "--retry",
        help="Retry failed units from a JSON file"
    )
    return parser.parse_args()

def load_retry_jobs(path):
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)

    return [(item[1], item[0]) for item in data]

def get_dropdown_label(select_element):
    """Dynamically extracts the label from the parent element."""
    parent = select_element.parent
    full_text = parent.get_text()
    select_text = select_element.get_text()
    label = full_text.replace(select_text, "").replace(":", "").strip()
    return label or "SubFilter"

def run_retry_pipeline(retry_file):
    jobs = load_retry_jobs(retry_file)

    print(f"Retrying {len(jobs)} failed jobs")

    for job in jobs:
        job_queue.put(job)

    job_queue.join()

def run_full_pipeline(page, failed_units, failed_detachments, faction_filter=None, extract_units=True, extract_detachments=True):
    all_factions_manifest = {}
    exclusion_set = {'sForgeWorld', 'sLegendary', 'datasheetsCollated'}

    # --- STAGE 1: FACTION DISCOVERY ---
    page.goto(f"{DOMAIN}/wh40k10ed/the-rules/quick-start-guide/")
    soup = BeautifulSoup(page.content(), 'html.parser')
    factions_button = soup.find('div', class_='NavBtn_Factions')
    faction_container = factions_button.find_next_sibling('div', class_='NavDropdown-content')
    anchors = faction_container.find_all('a', href=True)
    discovered_factions = [{"name": a.text.strip(), "path": (DOMAIN + a['href'])} 
                            for a in anchors if "/factions/" in a['href']]
    
    if faction_filter:
        discovered_factions = [
            f for f in discovered_factions
            if f["name"].lower() == faction_filter.lower()
        ]

        if not discovered_factions:
            print(f"No faction found matching: {faction_filter}")
            return

    # --- STAGE 2: EXTRACTION ---
    for faction in discovered_factions:
        
        print(f"Processing: {faction['name']} | URL: {faction['path']}")
        page.goto(faction["path"], wait_until="domcontentloaded")
        page.wait_for_selector("#tooltip_contentArmyList", state="attached", timeout=30000)
        
        sm_soup = BeautifulSoup(page.content(), 'html.parser')
        selects = [s for s in sm_soup.find_all('select') 
                    if s.get('class') and any('FilterSelect' in c for c in s['class'])]
        
        manifest_entry = {"units": [], "detachments": []}
        sub_filter_data = []
        sub_filter_key = None

        # Handle Sub-Filter ONLY if multiple dropdowns exist
        if len(selects) >= 2:
            sub_filter_key = get_dropdown_label(selects[0])
            
            target_val = next((o.get('value')
                                for o in selects[0].find_all('option') 
                                if "no filter" in o.text.lower()
                                ), None)
            
            if target_val:
                try:
                    page.locator("select[class*='FilterSelect']").nth(0).select_option(target_val)
                    
                    page.wait_for_function("""
                    () => {
                        const el = document.querySelector('#tooltip_contentArmyList');
                        if (!el) return false;

                        const now = Date.now();

                        window.__last_sample = window.__last_sample || 0;
                        if (now - window.__last_sample < 100) return false;

                        window.__last_sample = now;

                        const count = el.querySelectorAll('a[href]').length;

                        window.__unit_counts = window.__unit_counts || [];
                        window.__unit_counts.push(count);

                        if (window.__unit_counts.length > 5) {
                            window.__unit_counts.shift();
                        }

                        return window.__unit_counts.length === 5 &&
                            window.__unit_counts.every(x => x === count);
                    }
                    """)

                    sm_soup = BeautifulSoup(page.content(), 'html.parser')
                    selects = [s for s in sm_soup.find_all('select')
                                if s.get('class') and any('FilterSelect' in c for c in s['class'])]
                except: pass

            for opt in selects[0].find_all('option'):
                if opt.get('value') and 'no filter' not in opt.text.lower() and "no supplements" not in opt.text.lower():
                    sub_filter_data.append({"id": opt['value'], "name": opt.text.strip()})
            
            if sub_filter_data:
                manifest_entry[sub_filter_key] = sub_filter_data

        # Extract Detachments
        contents = page.locator("div.contents_header").first

        container = contents.locator(
            "xpath=ancestor::div[contains(@class,'BreakInsideAvoid')]"
        ).first

        headers = container.locator(
            "div.i10 a[href^='#'], div.i30 a[href^='#']"
        )
        count = headers.count()

        detachments = []
        name = None

        for i in range(count):
            h = headers.nth(i)

            text = h.inner_text().strip()
            cls = h.locator("..").get_attribute("class") or ""

            if "i10" in cls:
                name = text
                continue

            if "i30" in cls:
                if name and text == "Detachment Rule":
                    manifest_entry["detachments"].append({"name": name})
                name = None


        # Extract Units
        warehouse = sm_soup.find(id="tooltip_contentArmyList")
        if warehouse:
            for anchor in warehouse.find_all('a', href=True):
                name = anchor.text.strip()
                parent_classes = anchor.parent.get('class', []) if anchor.parent else []
                if not any(cls in exclusion_set for cls in parent_classes) and name and not anchor['href'].startswith('#'):
                    manifest_entry["units"].append({"unit_name": name, "href": anchor['href']})
        else:
            print(f"No units found for faction: {faction['name']}")

        u_len = len(manifest_entry["units"])
        d_len = len(manifest_entry["detachments"])
        print(f"Discovered: {faction['name']} | Units: {u_len}, Detachments: {d_len}")



        if d_len > 0 and extract_detachments:
            for detachment in manifest_entry["detachments"]:
                print(f"Processing Detachment: {detachment['name']}")
                try:
                    # Why do some factions have to be so fucking weird?
                    faction_name_str = faction["name"]
                    if (faction["name"] == "Space Marines"):
                        faction_name_str = "Adeptus Astartes"
                    if (faction["name"] == "Chaos Daemons"):
                        faction_name_str = "Legiones_Daemonica"
                    scrape_detachment(page, faction_name_str, detachment["name"])
                except Exception as e:
                    failed_detachments.append({"detachment_name": detachment['name'], "faction": faction['name'], "faction_path": faction['path'], "error": str(e)})
                    print(f"Failed to process Detachment: {detachment['name']} | Faction: {faction['name']} | Faction Path: {faction['path']} | Error: {e}")



        if u_len > 0 and extract_units:
            for unit in manifest_entry["units"]:
                print(f"Processing: {unit['unit_name']} | URL: {DOMAIN + unit['href']}")
                try:
                    job_queue.put((DOMAIN + unit['href'], unit['unit_name']))
                except Exception as e:
                    failed_units.append({"unit_name": unit['unit_name'], "href": unit['href'], "error": str(e)})
                    print(f"Failed to process: {unit['unit_name']} | URL: {DOMAIN + unit['href']} | Error: {e}")
            job_queue.join()

        all_factions_manifest[faction["name"]] = manifest_entry
        
        # Logging
        u_len = len(manifest_entry["units"])
        d_len = len(manifest_entry["detachments"])
        print(f"Processed: {faction['name']} | Units: {u_len}, Detachments: {d_len}", end="")
        if sub_filter_key and sub_filter_data:
            print(f", {sub_filter_key}: {len(sub_filter_data)}")
        else:
            print() 

    # --- STAGE 3: GENERATION ---
    with open("units.js", "w", encoding="utf-8") as f:
        f.write(f"const unitsData = {json.dumps(all_factions_manifest, indent=4, ensure_ascii=False)};\n")

if __name__ == "__main__":
    args = parse_args()

    start_time = time.perf_counter()

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(viewport={"width": 1920, "height": 1080})
        page = context.new_page()

        failed_units = []
        failed_detachments = []

        for _ in range(3):
            t = threading.Thread(
                target=worker,
                args=(failed_units,),
                daemon=True
            )
            t.start()
            workers.append(t)

        if (args.retry):
            run_retry_pipeline(args.retry)
        else:
            run_full_pipeline(page, failed_units, failed_detachments, args.faction, not args.no_units, not args.no_detachments)

        browser.close()

        for _ in workers:
            job_queue.put(None)

        for t in workers:
            t.join()

        with open("failed_units.json", "w", encoding="utf-8") as f:
            json.dump(failed_units, f, indent=4, ensure_ascii=False)

        with open("failed_detachments.json", "w", encoding="utf-8") as f:
            json.dump(failed_detachments, f, indent=4, ensure_ascii=False)

    elapsed = time.perf_counter() - start_time
    minutes = int(elapsed // 60)
    seconds = int(elapsed % 60)
    print(f"Success! 'units.js' generated in {minutes} minutes and {seconds} seconds.")
