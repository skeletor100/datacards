import re
from bs4 import BeautifulSoup, NavigableString, Tag


INLINE_RENDER_CLASSES = {
    "kwb",
    "kwb2",
    "bluefont",
    "aeText",
    "tt",
    "kwbu",
    "bold",
    "italic",
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


def normalize_inline_text(value):
    """Normalize inline text without stripping meaningful edge spaces.

    Text nodes next to styled inline tags often contain significant leading or
    trailing spaces, e.g. ``If your Army Faction is <span>ADEPTA</span>``.
    Stripping each individual NavigableString loses separators between runs.
    """
    return re.sub(r"\s+", " ", str(value or ""))


def clean_inline_punctuation_spacing(text):
    """Clean punctuation spacing inside a run while preserving edge spaces."""
    text = normalize_inline_text(text)

    leading_space = text.startswith(" ")
    trailing_space = text.endswith(" ")

    core = text.strip()
    if not core:
        return " " if leading_space or trailing_space else ""

    core = re.sub(r"\s+([,.;:])", r"\1", core)
    core = re.sub(r"([(\[])\s+", r"\1", core)
    core = re.sub(r"\s+([)\]])", r"\1", core)

    return (" " if leading_space else "") + core + (" " if trailing_space else "")


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



BLOCK_SOURCE_TAGS = {
    "address", "article", "aside", "blockquote", "dd", "details", "dialog",
    "div", "dl", "dt", "fieldset", "figcaption", "figure", "footer",
    "form", "h1", "h2", "h3", "h4", "h5", "h6", "header", "hr",
    "li", "main", "nav", "ol", "p", "pre", "section", "table", "ul",
}


def is_block_source_tag(node):
    return isinstance(node, Tag) and node.name in BLOCK_SOURCE_TAGS


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
        if c in INLINE_RENDER_CLASSES or c.startswith("tooltip")
    ]


def should_insert_run_separator(left_text, right_text):
    """Return True when merging adjacent runs would glue two words together.

    The parser normally preserves literal whitespace text nodes between inline
    siblings, but some Wahapedia/browser-normalised fragments can still arrive as
    adjacent same-class runs with no separator. Without this guard, merging turns
    markup like ``<span>ADEPTUS</span> <span>ASTARTES</span>`` into
    ``ADEPTUSASTARTES`` if the intervening whitespace node was lost upstream.
    """
    if not left_text or not right_text:
        return False

    if left_text[-1].isspace() or right_text[0].isspace():
        return False

    # Avoid changing punctuation joins such as "(i.e." or "D6+4)."
    if not left_text[-1].isalnum() or not right_text[0].isalnum():
        return False

    return True


def merge_adjacent_runs(runs):
    merged = []

    for run in runs:
        if not run["text"]:
            continue

        if (
            merged
            and merged[-1].get("source_classes", []) == run.get("source_classes", [])
        ):
            # Do not normally inject separators: meaningful spaces between inline
            # tags are represented by real NavigableString nodes. However, guard
            # against accidental word-gluing when two word-like same-class runs
            # arrive adjacent without a preserved whitespace node.
            separator = " " if should_insert_run_separator(
                merged[-1]["text"],
                run["text"],
            ) else ""

            merged[-1]["text"] = clean_inline_punctuation_spacing(
                merged[-1]["text"] + separator + run["text"]
            )
        else:
            merged.append(run)

    return merged


SEMANTIC_INLINE_TAG_CLASSES = {
    "b": "bold",
    "strong": "bold",
    "i": "italic",
    "em": "italic",
}


def extract_text_runs(node, inherited_classes=None):
    inherited_classes = inherited_classes or []

    if isinstance(node, NavigableString):
        text = clean_inline_punctuation_spacing(str(node))

        # Preserve whitespace-only text nodes as a single separating space.
        # Wahapedia uses literal spaces between adjacent styled inline spans,
        # e.g. <span>LEAGUES</span> <span>OF</span> <span>VOTANN</span>.
        # Dropping those nodes turns the rendered text into LEAGUESOFVOTANN.
        if not text.strip():
            if str(node).strip() == "":
                text = " "
            else:
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

    semantic_class = SEMANTIC_INLINE_TAG_CLASSES.get(node.name)
    if semantic_class:
        classes.append(semantic_class)

    combined_classes = inherited_classes + classes

    runs = []

    for child in node.children:
        runs.extend(extract_text_runs(child, combined_classes))

    return merge_adjacent_runs(runs)


def runs_to_text(runs):
    return clean_punctuation_spacing(
        " ".join(run["text"] for run in runs)
    )


def source_metadata_from_node(node):
    """Return source markup metadata that may matter to renderers.

    This intentionally keeps presentational Wahapedia classes such as
    ``impact18`` at the block level. The parser does not have to decide that
    impact18 means "title"; it only preserves enough information for a renderer
    or a later semantic pass to make that decision.
    """
    if not isinstance(node, Tag):
        return {}

    metadata = {
        "source_tag": node.name,
        "classes": node.get("class", []),
        "style": node.get("style", ""),
    }

    attrs = {}
    for key in ("id", "name", "title", "colspan", "rowspan"):
        value = node.get(key)
        if value is not None:
            attrs[key] = value

    if attrs:
        metadata["attrs"] = attrs

    return metadata


def paragraph_block_from_nodes(nodes):
    runs = []
    source_node = None

    if len(nodes) == 1 and isinstance(nodes[0], Tag):
        source_node = nodes[0]

    for node in nodes:
        # Let extract_text_runs preserve separator spaces between inline siblings.
        if is_ignorable_node(node) and not isinstance(node, NavigableString):
            continue

        runs.extend(extract_text_runs(node))

    runs = merge_adjacent_runs(runs)

    if not runs:
        return None

    block = {
        "displayItem": "p",
        "runs": runs,
    }

    # Preserve original block-level context for renderers. For example:
    # <p class="impact18">Dacatarai Stance</p>
    # becomes a paragraph with classes ["impact18"], rather than anonymous text.
    if source_node is not None:
        block.update(source_metadata_from_node(source_node))

        # Tell renderers this came from a real block-level source element.
        # This lets table-cell rendering keep blocks like <div class="cruWarpChargeWrap">
        # separate from following inline prose without hard-coding that class.
        if is_block_source_tag(source_node):
            block["is_block"] = True

    return block


def parse_image(img):
    return {
        "displayItem": "img",
        "src": img.get("src"),
        "alt": img.get("alt", ""),
        "classes": img.get("class", []),
        "style": img.get("style", ""),
    }


VISUAL_WIDGET_CLASS_MARKERS = (
    "cruWarpCharge",
    "redDiamond",
    "bluCircle",
    "aeMovement",
    "aeShooting",
    "aeCharge",
    "aeFight",
    "aeCommand",
    "dsCha",
)

LAYOUT_WRAPPER_CLASSES = {
    "BreakInsideAvoid",
    "Columns2",
    "frameLight",
    "Corner16",
    "Corner16_in",
}


def is_visual_widget_node(node):
    """Return True only for compact class-driven visual widgets.

    This deliberately excludes Wahapedia layout containers such as
    BreakInsideAvoid/Corner16. Those wrappers should be parsed through; keeping
    them recreates the source page layout inside the card renderer and causes
    overlap/collapse.
    """
    if not isinstance(node, Tag):
        return False

    classes = node.get("class", []) or []
    class_text = " ".join(classes)

    if any(cls in LAYOUT_WRAPPER_CLASSES for cls in classes):
        return False

    if any(marker in class_text for marker in VISUAL_WIDGET_CLASS_MARKERS):
        return True

    # Inline-block wrappers whose only real purpose is to position dice/plus
    # images should keep their DOM wrapper so spacing remains faithful.
    style = (node.get("style") or "").replace(" ", "").lower()
    if node.name == "div" and "display:inline-block" in style and node.find("img"):
        return True

    return False


def parse_visual_element(node):
    children = []

    for child in node.children:
        if isinstance(child, NavigableString):
            text = clean_inline_punctuation_spacing(str(child))
            if text.strip():
                children.append({
                    "displayItem": "span",
                    "runs": [{"text": text, "source_classes": []}],
                })
            continue

        if not isinstance(child, Tag) or is_ignorable_node(child):
            continue

        if child.name == "img":
            children.append(parse_image(child))
            continue

        if is_visual_widget_node(child) or child.name in ("div", "span", "i", "b", "em", "strong", "small", "a"):
            if child.find(True) or child.get("class") or child.get("style"):
                children.append(parse_visual_element(child))
                continue

        runs = extract_text_runs(child)
        if runs:
            block = {
                "displayItem": "span",
                "runs": runs,
            }
            block.update(source_metadata_from_node(child))
            children.append(block)

    block = {
        "displayItem": "element",
        "tag": node.name,
        "classes": node.get("class", []),
        "style": node.get("style", ""),
        "children": children,
        "is_block": is_block_source_tag(node),
    }

    attrs = {}
    for key in ("id", "name", "title", "aria-label", "role"):
        value = node.get(key)
        if value is not None:
            attrs[key] = value
    if attrs:
        block["attrs"] = attrs

    return block


def sanitize_style(style, *, allow_layout=True):
    if not style:
        return ""

    blocked = {
        "font-size",
        "font-family",
        "line-height",
        "width",
        "height",
    }

    parts = []
    for decl in style.split(";"):
        if ":" not in decl:
            continue
        prop, value = decl.split(":", 1)
        prop = prop.strip().lower()
        value = value.strip()

        if prop in blocked:
            continue

        parts.append(f"{prop}:{value}")

    return ";".join(parts)


def parse_table(table):
    inner = table.find("table")
    if inner:
        table = inner

    rows = []

    def parse_row(tr):
        cells = []

        for cell in tr.find_all(["td", "th"], recursive=False):
            # Some Wahapedia layout tables wrap a real content table. Those
            # wrappers are handled by the `inner = table.find("table")` logic
            # above, so nested-table cells here should not become empty cells.
            if cell.find("table"):
                continue

            cells.append({
                # existing renderer compatibility
                "runs": extract_text_runs(cell),

                # richer future renderer data
                "content": extract_cell_blocks(cell),
                "colspan": cell.get("colspan"),
                "rowspan": cell.get("rowspan"),
                "classes": cell.get("class", []),
                "style": sanitize_style(cell.get("style", "")),
            })

        return cells

    for tr in table.find_all("tr", recursive=False):
        cells = parse_row(tr)
        if cells:
            rows.append(cells)

    # Wahapedia often emits multiple direct <tbody> elements, and the first
    # can be empty. Parse all direct tbodies instead of only table.find(...).
    for tbody in table.find_all("tbody", recursive=False):
        for tr in tbody.find_all("tr", recursive=False):
            cells = parse_row(tr)
            if cells:
                rows.append(cells)

    return {
        "displayItem": "table",
        "classes": table.get("class", []),
        "style": sanitize_style(table.get("style", "")),
        "attrs": {
            "border": table.get("border"),
            "bordercolor": table.get("bordercolor"),
            "cellpadding": table.get("cellpadding"),
            "cellspacing": table.get("cellspacing"),
            "width": table.get("width"),
            "max-width": table.get("max-width"),
        },
        "rows": rows,
    }


def parse_cs_rule_wrapper(node):
    name_el = node.select_one(".stratName_CS")
    text_el = node.select_one(".stratText_CS")

    if not name_el or not text_el:
        return None

    title_span = name_el.find("span")
    title = clean_text(title_span or name_el)

    req_el = name_el.select_one(".cruD6wrap")

    return {
        "displayItem": "cs_rule",
        "title": title,
        "requirement": clean_text(req_el) if req_el else "",
        "requirement_classes": req_el.get("class", []) if req_el else [],
        "requirement_html": str(req_el) if req_el else "",
        "content": extract_content_blocks(list(text_el.children)),
        "classes": node.get("class", []),
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
        # Let extract_text_runs preserve separator spaces between inline siblings.
        if is_ignorable_node(child) and not isinstance(child, NavigableString):
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
        "source_tag": li.name,
        "classes": li.get("class", []),
        "style": li.get("style", ""),
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
        ):
            block = dict(block)
            content = []
            j = i + 1

            # Wahapedia often uses an inline custom heading followed by multiple
            # sibling blocks: prose, then direct image wrappers such as
            # <div class="img-opa"><img ...></div>. Keep those siblings inside
            # the custom subrule until the next parsed subrule boundary.
            while j < len(blocks):
                next_block = blocks[j]

                if next_block.get("displayItem") == "subrule":
                    break

                content.append(next_block)
                j += 1

            if content:
                block["content"] = content
                out.append(block)
                i = j
                continue

        out.append(block)
        i += 1

    return out


def extract_cell_blocks(cell):
    blocks = []
    inline_nodes = []
    br_count = 0

    def flush_inline():
        nonlocal inline_nodes

        paragraph = paragraph_block_from_nodes(inline_nodes)
        if paragraph:
            blocks.append(paragraph)

        inline_nodes = []

    def append_block_paragraph(node):
        paragraph = paragraph_block_from_nodes([node])
        if paragraph:
            blocks.append(paragraph)

    for child in cell.children:
        # Preserve whitespace-only text nodes between inline siblings, e.g.
        # <span>LEAGUES</span> <span>OF</span> <span>VOTANN</span>.
        if (
            is_ignorable_node(child)
            and not is_br(child)
            and not isinstance(child, NavigableString)
        ):
            continue

        if is_br(child):
            br_count += 1

            # Wahapedia uses <br><br> inside cells to separate logical
            # paragraphs (for example TRIGGER and EFFECT text). A single
            # <br> is usually only a soft line break, so do not split there.
            if br_count >= 2:
                flush_inline()
                br_count = 0

            continue

        br_count = 0

        if isinstance(child, Tag) and is_visual_widget_node(child):
            flush_inline()
            blocks.append(parse_visual_element(child))
            continue

        if isinstance(child, Tag) and child.name == "img":
            flush_inline()
            blocks.append(parse_image(child))
            continue

        if isinstance(child, Tag) and child.find("img"):
            flush_inline()
            imgs = child.find_all("img")
            for img in imgs:
                blocks.append(parse_image(img))
            continue

        if isinstance(child, Tag) and child.name == "table":
            flush_inline()
            blocks.append(parse_table(child))
            continue

        if isinstance(child, Tag) and child.name in ("ul", "ol"):
            flush_inline()
            blocks.append({
                "displayItem": child.name,
                "items": [
                    parse_list_item(li)
                    for li in child.find_all("li", recursive=False)
                ],
            })
            continue

        # Real block tags should remain separate blocks, but inline tags and
        # text nodes that are direct children of the cell belong to the same
        # paragraph until a block element or <br><br> separates them.
        if isinstance(child, Tag) and child.name in ("p", "div"):
            flush_inline()
            append_block_paragraph(child)
            continue

        inline_nodes.append(child)

    flush_inline()

    return blocks


def should_preserve_styled_block(node):
    if not isinstance(node, Tag):
        return False

    classes = node.get("class", []) or []

    # Never preserve layout wrappers
    if any(cls in LAYOUT_WRAPPER_CLASSES for cls in classes):
        return False

    style = node.get("style") or ""

    # Preserve intentionally styled headings/blocks
    return bool(style.strip())


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

        # Preserve whitespace-only text nodes between inline siblings.
        # These are significant separators in Wahapedia's inline markup.
        if is_ignorable_node(node) and not isinstance(node, NavigableString):
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

        if isinstance(node, Tag) and node.name == "img":
            flush_paragraph()
            blocks.append(parse_image(node))
            continue

        if isinstance(node, Tag) and node.name == "table":
            flush_paragraph()
            blocks.append(parse_table(node))
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

        if (
            isinstance(node, Tag)
            and "stratWrapper_CS" in node.get("class", [])
        ):
            flush_paragraph()

            parsed = parse_cs_rule_wrapper(node)
            if parsed:
                blocks.append(parsed)

            continue

        if isinstance(node, Tag) and node.name == "div":
            flush_paragraph()

            if is_visual_widget_node(node):
                blocks.append(parse_visual_element(node))
            elif should_preserve_styled_block(node):
                paragraph = paragraph_block_from_nodes([node])
                if paragraph:
                    blocks.append(paragraph)
            else:
                blocks.extend(extract_content_blocks(list(node.children)))

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