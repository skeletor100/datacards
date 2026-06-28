import json
import argparse
from playwright.sync_api import sync_playwright
from bs4 import BeautifulSoup
from datacard_parser import run as parse_datacard
from detachment_scraper import run as scrape_detachment
import time

import waha_parse_utils as utils

import threading
import queue

job_queue = queue.Queue()
result_queue = queue.Queue()
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

            try:
                faction_name, url, name, screenshots = job

                try:
                    print(f"Picked up job: {name} | URL: {url}")
                    data, sub_faction_name = parse_datacard(page, url, screenshots)
                    result_queue.put({
                        "faction": faction_name,
                        "unit_name": name,
                        "sub_faction_name": sub_faction_name,
                        "data": data
                    })
                    print(f"Processed {name} for faction {sub_faction_name}")
                except Exception as e:
                    failed_units.append((name, url, str(e)))
                    print(f"Failed to process: {name} | URL: {url} | Error: {e}")

            except Exception as e:
                print(f"Failed to parse job: {job}")

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
    parser.add_argument(
        "--output-json",
        default="units.json",
        help="File to output JSON to"
    )
    parser.add_argument(
        "--screenshots",
        action="store_true",
        help="Take Waha screenshots"
    )
    return parser.parse_args()

def load_retry_jobs(path):
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)

    return [(item[1], item[0]) for item in data]



def load_existing_output(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, dict):
                return data
    except FileNotFoundError:
        pass
    except json.JSONDecodeError as e:
        print(f"Warning: could not read existing JSON from {path}: {e}")

    return {}

def merge_with_change_tracking(old_manifest, new_manifest):
    changes = []

    for faction_name, new_faction in new_manifest.items():
        print(f"Merging cards for {faction_name}")
        if faction_name not in old_manifest:
            old_manifest[faction_name] = {}

        for section_name, new_section in new_faction.items():
            if section_name not in old_manifest[faction_name]:
                old_manifest[faction_name][section_name] = new_section
                continue

            if section_name in ("unit_cards", "detachment_cards"):
                section_count = 0

                old_cards = old_manifest[faction_name][section_name]
                new_cards = new_section

                old_only_keys = set(old_cards.keys()) - set(new_cards.keys())

                for card_name in old_only_keys:
                    changes.append({
                        "type": "removed",
                        "path": f"{faction_name}.{section_name}.{card_name}",
                        card_name: old_cards[card_name]
                    })
                    del old_cards[card_name]

                for card_name, new_card in new_cards.items():
                    if card_name in old_cards:
                        if old_cards[card_name] != new_card:
                            changes.append({
                                "type": "modified",
                                "path": f"{faction_name}.{section_name}.{card_name}",
                                card_name: old_cards[card_name]
                            })

                            old_cards[card_name] = new_card
                    else:
                        old_cards[card_name] = new_card
                    
                section_count = section_count + 1
                if section_count % 10 == 0:
                    print(f"Merged {section_count} cards from {section_name}")

            else:
                old_manifest[faction_name][section_name], new_changes = merge_with_change_tracking(old_manifest[faction_name][section_name], new_section)
                changes.extend(new_changes)

    print(f"Merged cards for {faction_name}")

    return old_manifest, changes

def get_dropdown_label(select_element):
    """Dynamically extracts the label from the parent element."""
    parent = select_element.parent
    full_text = parent.get_text()
    select_text = select_element.get_text()
    label = full_text.replace(select_text, "").replace(":", "").strip()
    return label or "SubFilter"

def build_detachment_subfaction_map(faction_name, detachment_select):
    mapping = {}
    current_subfaction = faction_name

    for option in detachment_select.find_all("option"):
        text = option.get_text(strip=True)
        value = option.get("value")
        classes = option.get("class", [])

        if option.has_attr("disabled"):
            if text == "Boarding Actions":
                break

            if "ctrlOptionHeader" in classes and text != "Detachment":
                current_subfaction = text

            continue

        # Skip "No filter"
        if text.lower() == "no filter":
            continue

        mapping[value] = current_subfaction.upper()

    return mapping

def get_detachment_identifier(cls):
    for token in cls.split():
        if len(token) != 4 or token == "clFl":
            continue

        left = token[:2]
        right = token[2:]

        if left != right:
            return right

    return None

def set_default_manifest(manifest, faction_name, sub_faction_name):
    if faction_name == sub_faction_name:
        return manifest.setdefault(
            sub_faction_name,
            {
                "unit_cards": {},
                "detachment_cards": {}
            }
        )
    else:
        print(f"Adding new sub-faction {sub_faction_name}")
        return set_default_manifest(manifest, faction_name, faction_name).setdefault(
            sub_faction_name,
            {
                "unit_cards": {},
                "detachment_cards": {}
            }
        )

def run_retry_pipeline(retry_file):
    jobs = load_retry_jobs(retry_file)

    print(f"Retrying {len(jobs)} failed jobs")

    for job in jobs:
        job_queue.put(job)

    job_queue.join()

def run_full_pipeline(page, failed_units, failed_detachments, args):
    all_factions_manifest = {}
    exclusion_set = {'sForgeWorld', 'sLegendary', 'datasheetsCollated'}

    # --- STAGE 1: FACTION DISCOVERY ---
    page.goto(f"{DOMAIN}/wh40k10ed/the-rules/quick-start-guide/")
    soup = BeautifulSoup(page.content(), 'html.parser')
    factions_button = soup.find('div', class_='NavBtn_Factions')
    faction_container = factions_button.find_next_sibling('div', class_='NavDropdown-content')
    anchors = faction_container.find_all('a', href=True)
    discovered_factions = [{"name": utils.normalize_faction_name(a.text.strip()), "path": (DOMAIN + a['href'])} 
                            for a in anchors if "/factions/" in a['href']]
    
    if args.faction:
        discovered_factions = [
            f for f in discovered_factions
            if f["name"].upper() == utils.normalize_faction_name(args.faction)
        ]

        if not discovered_factions:
            print(f"No faction found matching: {args.faction}")
            return

    # --- STAGE 2: EXTRACTION ---
    for faction in discovered_factions:
        
        print(f"Processing: {faction['name']} | URL: {faction['path']}")
        page.goto(faction["path"], wait_until="domcontentloaded")
        page.wait_for_selector("#tooltip_contentArmyList", state="attached", timeout=30000)
        
        sm_soup = BeautifulSoup(page.content(), 'html.parser')
        selects = utils.get_filter_selects(sm_soup)
        
        units = []
        detachments = []
        sub_filter_data = []
        sub_filter_key = None

        detachment_subfaction_map = {}

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


            utils.build_sub_faction_map(selects[0])

            detachment_subfaction_map = build_detachment_subfaction_map(faction['name'], selects[1])

            

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
                    identifier = get_detachment_identifier(cls)
                    sub_faction = detachment_subfaction_map.get(identifier, faction['name'])

                    detachments.append({
                        "name": name,
                        "identifier": identifier,
                        "sub_faction": sub_faction
                    })
                name = None


        # Extract Units
        warehouse = sm_soup.find(id="tooltip_contentArmyList")
        if warehouse:
            for anchor in warehouse.find_all('a', href=True):
                name = anchor.text.strip()
                parent_classes = anchor.parent.get('class', []) if anchor.parent else []
                if not any(cls in exclusion_set for cls in parent_classes) and name and not anchor['href'].startswith('#'):
                    units.append({"unit_name": name, "href": anchor['href']})
        else:
            print(f"No units found for faction: {faction['name']}")

        u_len = len(units)
        d_len = len(detachments)
        print(f"Discovered: {faction['name']} | Units: {u_len}, Detachments: {d_len}")

        if u_len > 0 and not args.no_units:
            for unit in units:
                try:
                    job_queue.put((faction['name'], DOMAIN + unit['href'], unit['unit_name'], args.screenshots))
                except Exception as e:
                    failed_units.append({"unit_name": unit['unit_name'], "href": unit['href'], "error": str(e)})
                    print(f"Failed to process: {unit['unit_name']} | URL: {DOMAIN + unit['href']} | Error: {e}")

        if d_len > 0 and not args.no_detachments:
            for detachment in detachments:
                try:
                    faction_name_str = utils.normalize_faction_name(detachment["sub_faction"])

                    print(f"Processing Detachment: {detachment['name']} for faction {faction_name_str}")
                    detachment_data = scrape_detachment(page, faction_name_str, detachment["name"], args.screenshots)

                    set_default_manifest(
                        all_factions_manifest,
                        faction['name'],
                        faction_name_str
                    )["detachment_cards"][detachment["name"]] = detachment_data
                except Exception as e:
                    failed_detachments.append({"detachment_name": detachment['name'], "faction": faction['name'], "faction_path": faction['path'], "error": str(e)})
                    print(f"Failed to process Detachment: {detachment['name']} | Faction: {faction['name']} | Faction Path: {faction['path']} | Error: {e}")

        job_queue.join()

        while not result_queue.empty():
            result = result_queue.get()

            if result["faction"] != faction["name"]:
                # Should not happen if you join per faction, but keeps it safe.
                result_queue.put(result)
                break

            set_default_manifest(
                all_factions_manifest,
                faction['name'],
                result['sub_faction_name']
            )["unit_cards"][result["unit_name"]] = result["data"]
        
        # Logging
        u_len = len(units)
        d_len = len(detachments)
        print(f"Processed: {faction['name']} | Units: {u_len}, Detachments: {d_len}", end="")
        if sub_filter_key and sub_filter_data:
            print(f", {sub_filter_key}: {len(sub_filter_data)}")
        else:
            print() 

    # --- STAGE 3: GENERATION ---
    existing_manifest = load_existing_output(args.output_json)
    merged_manifest, changes = merge_with_change_tracking(existing_manifest, all_factions_manifest)

    with open(args.output_json, "w", encoding="utf-8") as f:
        json.dump(merged_manifest, f, indent=4, ensure_ascii=False)

    with open("changes.json", "w", encoding="utf-8") as f:
        json.dump(changes, f, indent=4, ensure_ascii=False)

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
            run_full_pipeline(page, failed_units, failed_detachments, args)

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
    print(f"Success! '{args.output_json}' generated in {minutes} minutes and {seconds} seconds.")
