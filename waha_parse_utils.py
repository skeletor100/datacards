import re
from bs4 import BeautifulSoup, NavigableString, Tag


INLINE_RENDER_CLASSES = {
    "kwb",
    "kwb2",
    "bluefont",
}


def clean_text(element):
    if not element:
        return ""

    text = element.get_text(" ", strip=True)
    return " ".join(text.split())


def clean_text_from_string(value):
    if not value:
        return ""

    return re.sub(r"\s+", " ", str(value)).strip()


def clean_punctuation_spacing(text):
    text = clean_text_from_string(text)
    text = re.sub(r"\s+([,.;:])", r"\1", text)
    text = re.sub(r"([(\[])\s+", r"\1", text)
    text = re.sub(r"\s+([)\]])", r"\1", text)
    return text


def norm(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.strip().lower()).strip("-")


def normalise_anchor_name(name):
    return (
        name.strip()
        .replace(" ", "-")
        .replace("(", "")
        .replace(")", "")
        .replace("’", "-")
        .replace("!", "-")
        .replace("--", "-")
    )


def is_br(node):
    return isinstance(node, Tag) and node.name == "br"


def is_paragraph_separator(node):
    return (
        isinstance(node, Tag)
        and "dsLineHor" in node.get("class", [])
    )


def is_fluff_node(node):
    if not isinstance(node, Tag):
        return False

    classes = node.get("class", [])

    return (
        "ShowFluff" in classes
        or "legend" in classes
        or "legend2" in classes
    )


def is_ignorable_node(node):
    if isinstance(node, NavigableString):
        return not clean_punctuation_spacing(str(node))

    if not isinstance(node, Tag):
        return True

    if node.name in ("script", "style", "br"):
        return True

    if is_fluff_node(node):
        return True

    style = (node.get("style") or "").replace(" ", "").lower()
    if "display:none" in style:
        return True

    return False


def filtered_inline_classes(classes):
    return [
        c for c in classes
        if c in INLINE_RENDER_CLASSES
    ]


def merge_adjacent_runs(runs):
    merged = []

    for run in runs:
        if not run["text"]:
            continue

        if (
            merged
            and merged[-1].get("source_classes", []) == run.get("source_classes", [])
        ):
            merged[-1]["text"] = clean_punctuation_spacing(
                merged[-1]["text"] + " " + run["text"]
            )
        else:
            merged.append(run)

    return merged


def extract_text_runs(node, inherited_classes=None):
    inherited_classes = inherited_classes or []

    if isinstance(node, NavigableString):
        text = clean_punctuation_spacing(str(node))
        if not text:
            return []

        source_classes = filtered_inline_classes(inherited_classes)

        return [{
            "text": text,
            "source_classes": sorted(set(source_classes)),
        }]

    if not isinstance(node, Tag):
        return []

    if node.name in ("script", "style", "br"):
        return []

    if is_fluff_node(node):
        return []

    classes = filtered_inline_classes(node.get("class", []))
    combined_classes = inherited_classes + classes

    runs = []

    for child in node.children:
        runs.extend(extract_text_runs(child, combined_classes))

    return merge_adjacent_runs(runs)


def runs_to_text(runs):
    return clean_punctuation_spacing(
        " ".join(run["text"] for run in runs)
    )


def paragraph_block_from_nodes(nodes):
    runs = []

    for node in nodes:
        if is_ignorable_node(node):
            continue

        runs.extend(extract_text_runs(node))

    runs = merge_adjacent_runs(runs)

    if not runs:
        return None

    return {
        "displayItem": "p",
        "runs": runs,
    }


def parse_table(table):
    inner = table.find("table")
    if inner:
        table = inner

    rows = []

    for tr in table.find_all("tr", recursive=False):
        cells = []

        for cell in tr.find_all(["td", "th"], recursive=False):
            if cell.find("table"):
                continue

            runs = extract_text_runs(cell)
            if runs:
                cells.append({"runs": runs})

        if cells:
            rows.append(cells)

    if not rows:
        tbody = table.find("tbody", recursive=False)
        if tbody:
            for tr in tbody.find_all("tr", recursive=False):
                cells = []

                for cell in tr.find_all(["td", "th"], recursive=False):
                    if cell.find("table"):
                        continue

                    runs = extract_text_runs(cell)
                    if runs:
                        cells.append({"runs": runs})

                if cells:
                    rows.append(cells)

    return {
        "displayItem": "table",
        "rows": rows,
    }


def parse_list_item(li):
    bold = li.find("b", recursive=False)
    title = ""

    if bold:
        title = clean_punctuation_spacing(
            bold.get_text(" ", strip=True)
        ).rstrip(":")
        bold.extract()

    runs = []
    content = []

    for child in li.children:
        if is_ignorable_node(child):
            continue

        if isinstance(child, Tag) and child.name in ("ul", "ol"):
            content.append({
                "displayItem": child.name,
                "items": [
                    parse_list_item(sub_li)
                    for sub_li in child.find_all("li", recursive=False)
                ],
            })
        else:
            runs.extend(extract_text_runs(child))

    item = {
        "title": title,
        "runs": merge_adjacent_runs(runs),
    }

    if content:
        item["content"] = content

    return item


def attach_paragraphs_to_custom_subrules(blocks):
    out = []
    i = 0

    while i < len(blocks):
        block = blocks[i]

        if (
            block.get("displayItem") == "subrule"
            and block.get("content") == []
            and (
                block.get("source") == "h_custom"
                or block.get("source") == "hi_custom"
            )
            and i + 1 < len(blocks)
            and blocks[i + 1].get("displayItem") == "p"
        ):
            block = dict(block)
            block["content"] = [blocks[i + 1]]
            out.append(block)
            i += 2
            continue

        out.append(block)
        i += 1

    return out


def extract_content_blocks(nodes):
    blocks = []
    paragraph_nodes = []
    br_count = 0

    def flush_paragraph():
        nonlocal paragraph_nodes

        block = paragraph_block_from_nodes(paragraph_nodes)
        if block:
            blocks.append(block)

        paragraph_nodes = []

    for node in nodes:
        if is_paragraph_separator(node):
            flush_paragraph()
            br_count = 0
            continue

        if is_br(node):
            br_count += 1

            if br_count >= 2:
                flush_paragraph()
                br_count = 0

            continue

        br_count = 0

        if is_ignorable_node(node):
            continue

        if (
            isinstance(node, Tag)
            and node.name == "span"
            and (
                "h_custom" in node.get("class", [])
                or "hi_custom" in node.get("class", [])
            )
        ):
            flush_paragraph()

            blocks.append({
                "displayItem": "subrule",
                "title": clean_punctuation_spacing(node.get_text(" ", strip=True)),
                "content": [],
                "source": "h_custom",
            })

            continue

        if isinstance(node, Tag):
            table = node if node.name == "table" else node.find("table")

            if table:
                flush_paragraph()
                blocks.append(parse_table(table))
                continue

        if isinstance(node, Tag) and node.name in ("ul", "ol"):
            flush_paragraph()

            blocks.append({
                "displayItem": node.name,
                "items": [
                    parse_list_item(li)
                    for li in node.find_all("li", recursive=False)
                ],
            })

            continue

        paragraph_nodes.append(node)

    flush_paragraph()

    return attach_paragraphs_to_custom_subrules(blocks)


def find_anchor(soup, anchor_name):
    return (
        soup.find(id=anchor_name)
        or soup.find("a", attrs={"name": anchor_name})
    )


def find_section_by_anchor_prefix(soup, anchor_prefix):
    anchor = soup.find(
        "a",
        attrs={"name": lambda v: v and v.startswith(anchor_prefix)}
    )

    if not anchor:
        return None

    return anchor.find_parent("div", class_="BreakInsideAvoid")

def get_filter_selects(soup):
    return [
        s
        for s in soup.find_all("select")
        if s.get("class") and any("FilterSelect" in c for c in s.get("class", []))
    ]

FACTION_NAME_ALIASES = {
    "Space Marines": "Adeptus Astartes",
    "Chaos Daemons": "Legiones Daemonica",
    "Imperial Agents": "Agents of the Imperium",
}

def normalize_faction_name(name):
    name = str(name or "").strip()
    return FACTION_NAME_ALIASES.get(name, name).upper()

def build_sub_faction_map(select):
    no_filter_value = None
    mapping = {}

    for opt in select.find_all("option"):
        name = opt.get_text(strip=True)
        value = opt.get("value")

        if not value:
            continue

        name_lower = name.lower()

        if name_lower == "no filter":
            no_filter_value = value
            continue

        if name_lower in ("no supplement", "no supplements"):
            continue

        mapping[value] = normalize_faction_name(name)

    if not no_filter_value:
        return {}

    return {
        f"{no_filter_value}{value}": faction_name
        for value, faction_name in mapping.items()
    }