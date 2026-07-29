import json
import argparse
import subprocess
import sys
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError
from bs4 import BeautifulSoup
from waha_unit_scraper import run as parse_datacard
from waha_detachment_scraper import run as scrape_detachment
from waha_army_rule_scraper import run as scrape_army_rules
import time
from pathlib import Path

import waha_scraper_common as style_parser

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
                faction_name, url, name, subfaction_map = job

                try:
                    print(f"Picked up job: {name} | URL: {url}")
                    data, sub_faction_name = parse_datacard(page, url, subfaction_map)
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

# DOMAIN/EDITION come from waha_scraper_common (see EDITION section there)
# rather than being redefined here — waha_core_stratagems_scraper.py is
# launched as its own separate subprocess (see the --url pass-through
# below) rather than imported, so it has no visibility into this file's own
# variables otherwise; without passing the edition through explicitly, that
# subprocess used to silently fall back to its own hardcoded default
# (wh40k10ed) while the rest of the pipeline had already moved to
# wh40k11ed. A single shared constant is what makes that impossible now.
DOMAIN = style_parser.WAHAPEDIA_DOMAIN
EDITION = style_parser.WAHAPEDIA_EDITION


def navigate_to_required_selector(page, url, selector, attempts=3):
    """Navigate without waiting for every page resource, then wait for required content."""
    last_error = None

    for attempt in range(1, attempts + 1):
        try:
            page.goto(
                url,
                wait_until="commit",
                timeout=60000,
            )
            page.wait_for_selector(
                selector,
                state="attached",
                timeout=60000,
            )
            return
        except PlaywrightTimeoutError as exc:
            last_error = exc
            print(
                f"Navigation attempt {attempt}/{attempts} failed for {url}: {exc}"
            )
            if attempt < attempts:
                try:
                    page.goto(
                        "about:blank",
                        wait_until="commit",
                        timeout=10000,
                    )
                except PlaywrightTimeoutError:
                    pass
                page.wait_for_timeout(2000 * attempt)

    raise last_error

def parse_args():
    parser = argparse.ArgumentParser(description="Wahapedia faction extractor")
    parser.add_argument(
        "--faction",
        help="Only process this faction name"
    )
    parser.add_argument(
        "--no-core-rules",
        action="store_true",
        help="Do not extract core rules"
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
        "--remerge",
        help="Merge an existing manifest into the output JSON"
    )
    parser.add_argument(
        "--reverse-changes",
        help="Reapply the previous values stored in a changes JSON file"
    )
    parser.add_argument(
        "--output-json",
        default="waha_data.json",
        help="File to output JSON to"
    )
    parser.add_argument(
        "--forgeworld",
        action="store_true",
        help="Include ForgeWorld units"
    )
    parser.add_argument(
        "--legends",
        action="store_true",
        help="Include Legends units"
    )
    parser.add_argument(
        "--no-css-manifest",
        action="store_true",
        help="Do not build the icon/asset CSS manifest after scraping"
    )
    parser.add_argument(
        "--css-manifest-output",
        default="waha_css_manifest.json",
        help="Output path for the icon/asset CSS manifest"
    )
    parser.add_argument(
        "--asset-dir",
        default="assets",
        help="Directory where downloaded Wahapedia image assets are stored"
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

def merge_with_change_tracking(old_manifest, new_manifest, sections_to_merge=None):
    changes = []
    tracked_sections = {"unit_cards", "detachment_cards", "army_rules"}


    for faction_name, new_faction in new_manifest.items():
        print(f"Merging cards for {faction_name}")
        if faction_name not in old_manifest:
            old_manifest[faction_name] = {}

        for section_name, new_section in new_faction.items():
            if sections_to_merge is not None and section_name in tracked_sections and section_name not in sections_to_merge:
                print(f"Skipping merge for {faction_name}.{section_name}; section was not gathered in this run")
                continue

            if section_name not in old_manifest[faction_name]:
                old_manifest[faction_name][section_name] = new_section
                continue

            if section_name in tracked_sections:
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
                merged_section, new_changes = merge_with_change_tracking(
                    {section_name: old_manifest[faction_name][section_name]},
                    {section_name: new_section},
                    sections_to_merge=sections_to_merge
                )

                old_manifest[faction_name][section_name] = merged_section[section_name]

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


def resolve_sub_faction_from_heading(heading, faction_name, sub_faction_map):
    heading_norm = style_parser.normalize_faction_name(heading)

    candidates = {
        style_parser.normalize_faction_name(faction_name),
        *[
            style_parser.normalize_faction_name(name)
            for name in sub_faction_map.values()
        ],
    }

    for candidate in sorted(candidates, key=len, reverse=True):
        if candidate in heading_norm:
            return candidate

    return style_parser.normalize_faction_name(faction_name)


def discover_army_rules_from_contents(container_soup, faction_name, sub_faction_map):
    sections = []

    for link in container_soup.select(
        "div.i10 > a[href^='#Army-Rules'], "
        "div.i30 > a[href^='#Army-Rules']"
    ):
        row = link.find_parent("div")
        classes = row.get("class", [])
        anchor = link["href"].lstrip("#")

        if "i10" in classes:
            sub_faction = style_parser.normalize_faction_name(faction_name)

        elif "i30" in classes:
            heading_row = row.find_previous(
                lambda tag: (
                    tag.name == "div"
                    and "i10" in tag.get("class", [])
                    and "clFl" in tag.get("class", [])
                )
            )

            if not heading_row:
                continue

            sub_faction = resolve_sub_faction_from_heading(
                heading_row.get_text(" ", strip=True),
                faction_name,
                sub_faction_map
            )

        else:
            continue

        sections.append({
            "sub_faction": sub_faction,
            "anchor": anchor,
        })

    return sections


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

def discover_detachments_from_contents(container_soup, faction_name, detachment_subfaction_map):
    detachments = []
    seen_anchors = set()

    for link in container_soup.select("div.i30 > a[href^='#Detachment-Rule']"):
        row = link.find_parent("div")
        classes = row.get("class", [])

        # Wahapedia's Contents nav renders twice per page (an "mw1" and an
        # "mw3" copy — presumably two responsive-breakpoint variants both
        # present in the DOM at once), so every real detachment link shows
        # up here twice. Processing both isn't wrong (results land in a
        # dict keyed by name, so the second pass just overwrites the
        # first) but it silently doubles scrape work for every faction.
        anchor = link["href"].lstrip("#")
        if anchor in seen_anchors:
            continue

        # Also seen (Chaos Space Marines' "Cabal of Chaos"): a stray,
        # contentless "Detachment Rule" nav entry with no `clFl`
        # sub-faction wrapper class on its row at all — the anchor it
        # points to is real but is just an empty "Detachment Rule" heading
        # with no rule content and no clFl ancestor, so it can only fail
        # downstream with "Containing detachment block not found." Every
        # genuine row carries clFl; skip anything that doesn't.
        if "clFl" not in classes:
            continue

        cls = " ".join(classes)

        identifier = get_detachment_identifier(cls)
        sub_faction = detachment_subfaction_map.get(identifier, faction_name)

        heading_row = row.find_previous(
            lambda tag: (
                tag.name == "div"
                and "i10" in tag.get("class", [])
                and "clFl" in tag.get("class", [])
            )
        )

        if not heading_row:
            continue

        seen_anchors.add(anchor)
        detachments.append({
            "name": heading_row.get_text(" ", strip=True),
            "identifier": identifier,
            "sub_faction": sub_faction,
            "anchor": anchor,
        })

    return detachments

def set_default_manifest(manifest, faction_name, sub_faction_name):
    if faction_name == sub_faction_name:
        return manifest.setdefault(
            sub_faction_name,
            {
                "unit_cards": {},
                "detachment_cards": {},
                "army_rules": {}
            }
        )
    else:
        return set_default_manifest(manifest, faction_name, faction_name).setdefault(
            sub_faction_name,
            {
                "unit_cards": {},
                "detachment_cards": {},
                "army_rules": {}
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
    if args.forgeworld:
        exclusion_set.discard('sForgeWorld')
    if args.legends:
        exclusion_set.discard('sLegendary')

    # --- STAGE 1: FACTION DISCOVERY ---
    navigate_to_required_selector(
        page,
        f"{DOMAIN}/{EDITION}/the-rules/",
        "div.NavBtn_Factions",
    )
    soup = BeautifulSoup(page.content(), 'html.parser')
    factions_button = soup.find('div', class_='NavBtn_Factions')
    faction_container = factions_button.find_next_sibling('div', class_='NavDropdown-content')
    anchors = faction_container.find_all('a', href=True)
    discovered_factions = [{"name": style_parser.normalize_faction_name(a.text.strip()), "path": (DOMAIN + a['href'])} 
                            for a in anchors if "/factions/" in a['href']]
    
    if args.faction:
        discovered_factions = [
            f for f in discovered_factions
            if f["name"].upper() == style_parser.normalize_faction_name(args.faction)
        ]

        if not discovered_factions:
            print(f"No faction found matching: {args.faction}")
            return

    # --- STAGE 2: EXTRACTION ---
    for faction in discovered_factions:
        
        print(f"Processing: {faction['name']} | URL: {faction['path']}")
        navigate_to_required_selector(
            page,
            faction["path"],
            "#tooltip_contentArmyList",
        )
        
        sm_soup = BeautifulSoup(page.content(), 'html.parser')
        selects = style_parser.get_filter_selects(sm_soup)
        
        units = []
        detachments = []
        sub_filter_data = []
        sub_filter_key = None

        subfaction_map = None
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


            subfaction_map = style_parser.build_sub_faction_map(selects[0])

            detachment_subfaction_map = build_detachment_subfaction_map(faction['name'], selects[1])

            

        # Extract Detachments
        contents = sm_soup.select_one("div.contents_header")

        container_soup = contents.find_parent(
            "div",
            class_=lambda c: c and "BreakInsideAvoid" in c
        )

        detachments = discover_detachments_from_contents(
            container_soup,
            faction["name"],
            detachment_subfaction_map
        )

        army_rule_sections = discover_army_rules_from_contents(
            container_soup,
            faction["name"],
            subfaction_map
        )

        for section in army_rule_sections:
            try:
                faction_name_str = section["sub_faction"]

                rules = scrape_army_rules(
                    page,
                    section["anchor"]
                )

                army_rules = set_default_manifest(
                    all_factions_manifest,
                    faction["name"],
                    faction_name_str
                )["army_rules"]

                for rule in rules:
                    army_rules[rule["name"]] = rule

            except Exception as e:
                print(
                    f"Failed to process Army Rules: "
                    f"Faction: {faction['name']} | "
                    f"Sub-faction: {section['sub_faction']} | "
                    f"Anchor: {section['anchor']} | "
                    f"Error: {e}"
                )

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
                    job_queue.put((faction['name'], DOMAIN + unit['href'], unit['unit_name'], subfaction_map))
                except Exception as e:
                    failed_units.append({"unit_name": unit['unit_name'], "href": unit['href'], "error": str(e)})
                    print(f"Failed to process: {unit['unit_name']} | URL: {DOMAIN + unit['href']} | Error: {e}")

        if d_len > 0 and not args.no_detachments:
            for detachment in detachments:
                try:
                    faction_name_str = style_parser.normalize_faction_name(detachment["sub_faction"])

                    print(f"Processing Detachment: {detachment['name']} for faction {faction_name_str}")
                    detachment_data = scrape_detachment(
                        page=page,
                        faction_name=faction_name_str,
                        detachment_name=detachment["name"],
                        detachment_anchor=detachment["anchor"],
                    )

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

    with open("tmp.json", "w", encoding="utf-8") as f:
        json.dump(all_factions_manifest, f, indent=4, ensure_ascii=False)

    # --- STAGE 3: GENERATION ---
    sections_to_merge = {"army_rules"}
    if not args.no_units:
        sections_to_merge.add("unit_cards")
    if not args.no_detachments:
        sections_to_merge.add("detachment_cards")

    merge_and_write_json(args.output_json, all_factions_manifest, sections_to_merge=sections_to_merge)


def get_change_payload(change, item_name):
    """Return the stored previous value from a change record."""
    if item_name in change:
        return change[item_name]

    payload_keys = [
        key for key in change
        if key not in {"type", "path"}
    ]

    if len(payload_keys) == 1:
        return change[payload_keys[0]]

    raise ValueError(
        f"Could not identify stored value for change path: {change.get('path')}"
    )


def reverse_changes(changes_file, output_json):
    """Restore values recorded in a changes file into the output manifest."""
    manifest = load_existing_output(output_json)

    if not manifest and not Path(output_json).exists():
        raise FileNotFoundError(
            f"Output JSON does not exist: {output_json}"
        )

    with open(changes_file, "r", encoding="utf-8") as f:
        changes = json.load(f)

    if not isinstance(changes, list):
        raise ValueError(
            f"Changes file must contain a JSON list: {changes_file}"
        )

    restored = 0
    skipped = 0

    for index, change in enumerate(changes, start=1):
        if not isinstance(change, dict):
            raise ValueError(
                f"Change #{index} is not a JSON object"
            )

        change_type = change.get("type")
        path = change.get("path")

        if change_type not in {"removed", "modified"}:
            print(
                f"Skipping unsupported change type at entry {index}: "
                f"{change_type}"
            )
            skipped += 1
            continue

        if not isinstance(path, str):
            raise ValueError(
                f"Change #{index} has no valid path"
            )

        path_parts = path.split(".", 2)
        if len(path_parts) != 3:
            raise ValueError(
                f"Change #{index} has an invalid path: {path}"
            )

        faction_name, section_name, item_name = path_parts
        previous_value = get_change_payload(change, item_name)

        faction = manifest.setdefault(faction_name, {})
        section = faction.setdefault(section_name, {})

        if not isinstance(section, dict):
            raise ValueError(
                f"Target section is not an object: "
                f"{faction_name}.{section_name}"
            )

        section[item_name] = previous_value
        restored += 1

        action = (
            "Restored removed"
            if change_type == "removed"
            else "Reverted modified"
        )
        print(f"{action}: {path}")

    with open(output_json, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=4, ensure_ascii=False)

    print(
        f"Reverse changes complete: {restored} restored, "
        f"{skipped} skipped. Updated '{output_json}'."
    )

def merge_and_write_json(output_json, new_json, sections_to_merge=None):
    existing_manifest = load_existing_output(output_json)
    merged_manifest, changes = merge_with_change_tracking(
        existing_manifest,
        new_json,
        sections_to_merge=sections_to_merge
    )

    with open(output_json, "w", encoding="utf-8") as f:
        json.dump(merged_manifest, f, indent=4, ensure_ascii=False)

    with open("changes.json", "w", encoding="utf-8") as f:
        json.dump(changes, f, indent=4, ensure_ascii=False)

if __name__ == "__main__":
    args = parse_args()

    start_time = time.perf_counter()

    if args.reverse_changes:
        reverse_changes(args.reverse_changes, args.output_json)
        elapsed = time.perf_counter() - start_time
        minutes = int(elapsed // 60)
        seconds = int(elapsed % 60)
        print(
            f"Success! Reverse changes applied in "
            f"{minutes} minutes and {seconds} seconds."
        )
        raise SystemExit(0)

    if not args.no_core_rules:
        core_rules_url = f"{DOMAIN}/{EDITION}/the-rules/core-rules/"
        process = subprocess.Popen(
            [sys.executable, "waha_core_stratagems_scraper.py", "--url", core_rules_url]
        )
        exit_code = process.wait()
        if exit_code != 0:
            raise SystemExit(f"waha_core_stratagems_scraper.py failed with exit code {exit_code}")

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
        elif (args.remerge):
            new_manifest = load_existing_output(args.remerge)
            merge_and_write_json(args.output_json, new_manifest)
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

    if not args.no_css_manifest:
        process = subprocess.Popen([
            sys.executable, "waha_css_builder.py",
            "--data-json", args.output_json,
            "--output", args.css_manifest_output,
            "--asset-dir", args.asset_dir,
        ])
        exit_code = process.wait()
        if exit_code != 0:
            raise SystemExit(f"waha_css_builder.py failed with exit code {exit_code}")

    elapsed = time.perf_counter() - start_time
    minutes = int(elapsed // 60)
    seconds = int(elapsed % 60)
    print(f"Success! '{args.output_json}' generated in {minutes} minutes and {seconds} seconds.")
