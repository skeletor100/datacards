import json
import re

from bs4 import BeautifulSoup, NavigableString, Tag


# =========================================================
# EDITION
#
# Single source of truth for which Wahapedia ruleset edition this whole
# pipeline targets. Every scraper/builder script used to hardcode its own
# copy of this (waha_scraper.py's DOMAIN+"/wh40k11ed/...", the core
# stratagems scraper's own "wh40k10ed" default, the CSS builder's own
# "wh40k10ed" default, ...) — which is exactly how the pipeline ended up
# silently scraping stratagems from the wrong edition while the rest of it
# had already moved to wh40k11ed: one copy got updated, the others didn't,
# and nothing caught the mismatch because there was nothing to check it
# against. Importing these three instead of hardcoding a new copy means
# there is now exactly one place to change when the edition moves again.
# =========================================================
WAHAPEDIA_DOMAIN = "https://wahapedia.ru"
WAHAPEDIA_EDITION = "wh40k11ed"
DEFAULT_WAHAPEDIA_BASE = f"{WAHAPEDIA_DOMAIN}/{WAHAPEDIA_EDITION}"


# =========================================================
# FACTION / NAV DISCOVERY
#
# Copied from waha_parse_utils.py (the old parser), not imported — this is
# the last thing waha_scraper.py still depended on that module for. Unlike
# everything else in this file, these were never part of the content-
# scraping rewrite: they normalize faction names and parse the site's own
# faction/sub-faction <select> filter dropdowns, which is navigation/
# discovery bookkeeping, not content extraction. Moving them here (rather
# than leaving waha_scraper.py importing waha_parse_utils just for these
# three functions) lets that whole module become purely legacy, matching
# every other now-superseded old file instead of being a lingering
# exception.
# =========================================================
FACTION_NAME_ALIASES = {
    "Space Marines": "Adeptus Astartes",
    "Chaos Daemons": "Legiones Daemonica",
    "Imperial Agents": "Agents of the Imperium",
}


def normalize_faction_name(name):
    name = str(name or "").strip()
    return FACTION_NAME_ALIASES.get(name, name).upper()


def get_filter_selects(soup):
    return [
        s
        for s in soup.find_all("select")
        if s.get("class") and any("FilterSelect" in c for c in s.get("class", []))
    ]


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


# =========================================================
# LIVE-DOM STYLE RESOLUTION
#
# Instead of recording which Wahapedia CSS classes were on a node (which
# forces every consumer to know what e.g. "kwb" or "bluefont" mean), this
# stamps each element with its own *resolved* computed style while the page
# is still live, then reduces that down to the handful of things a renderer
# actually needs: bold / italic / upper, plus a colour only when it diverges
# from the element's own parent (so ambient/inherited colour never gets
# baked in, only genuine overrides).
# =========================================================

# Icons/badges that aren't styled text at all (see the VISUAL WIDGETS section
# below) — defined here, ahead of STAMP_JS, so the JS stamping pass and the
# Python widget-detection logic share one list instead of two hand-kept
# copies that could drift apart.
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
    "cruD",
    "cruPlus",
    "cruOR",
)

STAMP_JS = """
(root) => {
    const WIDGET_MARKERS = __VISUAL_WIDGET_CLASS_MARKERS__;
    let counter = 0;
    const styles = {};

    const toHex = (rgbStr) => {
        const m = (rgbStr || '').match(/[\\d.]+/g);
        if (!m || m.length < 3) return null;
        const [r, g, b, a] = m.map(Number);
        // Fully/near transparent (the default for most elements' own
        // background) is not a colour worth comparing — without this,
        // "rgba(0,0,0,0)" would resolve to the same hex as solid black.
        if (m.length >= 4 && a <= 0.05) return null;
        return '#' + [r, g, b].map(v =>
            Math.max(0, Math.min(255, Math.round(v))).toString(16).padStart(2, '0')
        ).join('');
    };

    // A "corner-cut" banner gradient (e.g. background-image:
    // linear-gradient(225deg, rgba(0,0,0,0) 14px, rgb(87,12,12) 0px), with
    // background-color left fully transparent) is a newer way Wahapedia
    // draws what used to be a flat background-color — the diagonal cut
    // corner replaces a plain rectangle, but it's still "this banner's own
    // accent colour" visually. Without this, backgroundColor alone reports
    // transparent for these headings and the real accent colour (used to
    // decide which faction/rule a banner belongs to, and what to fill our
    // own — plain, non-cut — banner rectangle with) is lost entirely,
    // silently falling back to a generic default for every such banner.
    const gradientFallbackColor = (bgImage) => {
        if (!bgImage || !bgImage.includes('gradient')) return null;
        const colorTokens = bgImage.match(/rgba?\\([^)]+\\)/g) || [];
        for (let i = colorTokens.length - 1; i >= 0; i--) {
            const hex = toHex(colorTokens[i]);
            if (hex) return hex;
        }
        return null;
    };

    // Icon/badge widgets (see VISUAL_WIDGET_CLASS_MARKERS) can have their
    // real size/spacing set per-instance (an inline style on that specific
    // element, or a flex-computed size derived from a real sibling/parent) —
    // neither of which a later, isolated per-class CSS sample could ever
    // see, since that reconstructs a bare div with just the class name and
    // no real page around it. Capturing this here, on the live element in
    // its real context, is the only way to get it right.
    const hasWidgetMarker = (el) => {
        const classText = el.className && typeof el.className === 'string' ? el.className : '';
        return WIDGET_MARKERS.some(marker => classText.includes(marker));
    };

    const resolve = (el) => {
        const cs = getComputedStyle(el);
        const bold = parseInt(cs.fontWeight, 10) >= 600;
        const italic = cs.fontStyle === 'italic';
        const upper = cs.textTransform === 'uppercase';

        const measured = hasWidgetMarker(el) ? {
            width: cs.width,
            height: cs.height,
            marginTop: cs.marginTop,
            marginRight: cs.marginRight,
            marginBottom: cs.marginBottom,
            marginLeft: cs.marginLeft,
            borderRadius: cs.borderRadius,
            // Unlike width/height/margins (raw px only meaningful relative
            // to THIS element's own real size, hence scaledMeasuredCss's
            // ratio treatment), display is a portable, context-independent
            // layout fact — "does this flow inline or stack as a block" is
            // true regardless of what card it's rendered into. Capturing it
            // here means a brand-new icon-cluster widget family lays itself
            // out correctly the moment it's scraped, instead of silently
            // defaulting to block (vertical stacking) until someone notices
            // and hand-types a display override for that specific class.
            display: cs.display,
        } : null;

        // A native <a> gets its colour from being a hyperlink (an
        // interaction affordance: "this is clickable"), not from any
        // content styling Wahapedia applied to the text. We have no
        // click-through/hover behaviour, so that colour would misrepresent
        // plain linked text as specially emphasised. Substitute the
        // surrounding colour so link text reads as ordinary prose. This is a
        // generic HTML semantics exception (anchor == hyperlink), not a
        // per-site class name, so it doesn't reintroduce the "have to know
        // this class" problem colour resolution was built to avoid.
        //
        // Checked via closest('a'), not just el.tagName === 'A': Wahapedia's
        // markup sometimes wraps a link's own text in an extra <b> (e.g.
        // Hand of the Dynasty's "<a href=...><b>eligible to start an
        // action</b></a>"), and that inner <b> inherits the SAME link
        // colour by ordinary CSS inheritance — resolving el itself (the
        // <b>, not the <a>) missed the exception entirely, capturing the
        // link's blue as if it were deliberate emphasis on that phrase.
        const nearestLink = el.closest('a');
        const colorSource = (nearestLink && nearestLink.parentElement)
            ? getComputedStyle(nearestLink.parentElement).color
            : cs.color;

        // This is the element's own absolute rendered colour, not a diff
        // against its parent — a single element (e.g. a stratagem's whole
        // text container) can legitimately set one colour for all of its
        // body text while still differing from its own wrapper. Diffing at
        // this per-element level would rediscover that same fact on every
        // bit of bare text inside it. The meaningful comparison — "is this
        // colour unusual for the paragraph it's actually in" — is made once
        // per content block, in Python, against that block's own dominant
        // colour (see _relativize_colors).
        return {
            bold, italic, upper,
            color: toHex(colorSource),
            background: toHex(cs.backgroundColor) || gradientFallbackColor(cs.backgroundImage),
            measured,
        };
    };

    const walk = (el) => {
        if (el.tagName === 'SCRIPT' || el.tagName === 'STYLE') return;

        const id = String(counter++);
        el.setAttribute('data-dsid', id);
        styles[id] = resolve(el);

        for (const child of Array.from(el.children)) {
            walk(child);
        }
    };

    const rootId = String(counter);
    walk(root);

    const html = root.innerHTML;

    root.querySelectorAll('[data-dsid]').forEach(e => e.removeAttribute('data-dsid'));
    root.removeAttribute('data-dsid');

    return { html, styles, rootId };
}
""".replace("__VISUAL_WIDGET_CLASS_MARKERS__", json.dumps(list(VISUAL_WIDGET_CLASS_MARKERS)))


def resolve_styled_content(locator):
    """Stamp computed styles onto a live DOM subtree and parse the result.

    Returns (soup, styles, root_style):
      - soup: BeautifulSoup tree of the block's innerHTML. Every element in
        it carries a `data-dsid` attribute that keys into `styles`.
      - styles: dict of dsid -> {bold, italic, upper, color, background}.
      - root_style: the resolved style of the block's own root element, used
        as the fallback for bare text that is a direct child of the block.
    """
    result = locator.evaluate(STAMP_JS)
    soup = BeautifulSoup(result["html"], "html.parser")
    root_style = result["styles"].get(result["rootId"], _default_style())
    return soup, result["styles"], root_style


def _default_style():
    return {"bold": False, "italic": False, "upper": False, "color": None, "background": None, "measured": None}


# =========================================================
# TEXT / RUN BUILDING
# =========================================================

def clean_text(node):
    if not node:
        return ""
    return _clean_ws(node.get_text(" ", strip=True)) if hasattr(node, "get_text") else _clean_ws(str(node))


def _clean_ws(text):
    return re.sub(r"\s+", " ", str(text or "")).strip()


def clean_inline(text):
    return _clean_inline(text)


def _clean_inline(text):
    text = re.sub(r"\s+", " ", str(text or ""))

    leading = text.startswith(" ")
    trailing = text.endswith(" ")
    core = text.strip()

    if not core:
        return " " if (leading or trailing) else ""

    core = re.sub(r"\s+([,.;:])", r"\1", core)
    core = re.sub(r"([(\[])\s+", r"\1", core)
    core = re.sub(r"\s+([)\]])", r"\1", core)

    return (" " if leading else "") + core + (" " if trailing else "")


def tag_style(tag, styles, root_style):
    if not isinstance(tag, Tag):
        return root_style
    return styles.get(tag.get("data-dsid"), root_style)


def theme_tags(style):
    tags = []
    if style.get("bold"):
        tags.append("bold")
    if style.get("italic"):
        tags.append("italic")
    if style.get("upper"):
        tags.append("upper")
    return tags


def resolve_element_style(tag, styles, root_style):
    """Resolve a standalone styled element's own look (not flowing text).

    For things like a rule/subrule heading — a single element that isn't
    part of a paragraph, so the block-relative colour machinery built for
    prose (see _apply_block_color) doesn't apply. Returns the same
    source_classes/color/background shape used everywhere else, e.g. for a
    per-faction rule heading banner like <h3 class="dsColorBgAM font-white">:
    {"source_classes": ["bold"], "color": "#ffffff", "background": "#324935"}.

    color is only included alongside a genuinely-diverging background, not
    on its own — some Wahapedia headings (e.g. <h2 class="hdrNoColor">) are
    deliberately plain, with no banner fill at all, and their text colour
    was only ever chosen to read against the page's own plain background.
    A renderer that always draws headings as a coloured banner (as this
    project's cards do) has nowhere sensible to put that isolated colour;
    reporting it without its paired background produced unreadable text
    (e.g. black-on-dark) once forced onto a banner it was never designed
    for. Whether a heading gets a banner at all is a real design fact worth
    capturing (own_bg diverging from parent's) — a colour with no
    background to go with it is not.
    """
    own = tag_style(tag, styles, root_style)
    parent = tag_style(tag.parent, styles, root_style) if tag.parent else root_style

    result = {"source_classes": theme_tags(own)}

    own_bg = own.get("background")
    if own_bg and own_bg != parent.get("background"):
        result["background"] = own_bg
        if own.get("color"):
            result["color"] = own["color"]

    return result


def _build_run(text, style):
    # Carries the run's absolute resolved colour/background for now;
    # merge_runs groups by them, and _apply_block_color (applied once per
    # content block) strips both back down to only the runs that deviate
    # from that block's own dominant colour/background.
    #
    # background used to be compared against just the run's own immediate
    # parent here, to only capture a genuinely highlighted inline phrase
    # (e.g. Wahapedia's own <span class="redPad">Leap to Defend:</span>)
    # rather than every run. That one-level-up comparison broke down for a
    # boxed sub-rule several DOM levels deep (Genestealer Cults' "BROOD
    # BROTHERS" sub-rule, whose .Corner26_in pane has its own solid
    # #eadaec fill): a bare text run sitting directly in the pane compared
    # against a real edge and got flagged, while a keyword span nested one
    # level further in compared two levels that were BOTH already inside
    # the pane and didn't — tagging background on some plain-text runs but
    # not nested keyword spans in the very same sentence, even though none
    # of it was a deliberate highlight, just the box's own uniform fill.
    # Capturing unconditionally and reconciling once per block in
    # _apply_block_color (the same two-tier treatment colour already gets)
    # fixes that inconsistency at its source.
    return {
        "text": text,
        "source_classes": theme_tags(style),
        "color": style.get("color"),
        "background": style.get("background"),
    }


def _theme_key(run):
    return (tuple(run.get("source_classes", [])), run.get("color"), run.get("background"))


def _block_context_color(nodes, styles, root_style):
    """The colour a bare, unstyled span would render as in this block.

    This is the resolved colour of the nodes' shared container (e.g. the
    stratagem's `.str10Text` div, a table cell, a list item) — not a vote
    over what's actually present, so an entirely-recoloured block (every run
    in it sharing one non-default colour) still reports that colour rather
    than mistaking "everything agrees" for "there's nothing to report."
    """
    if not nodes:
        return root_style.get("color")

    parent = getattr(nodes[0], "parent", None)
    return tag_style(parent, styles, root_style).get("color")


def _block_context_background(nodes, styles, root_style):
    """The background a bare, unstyled span would render against in this
    block — same technique as _block_context_color, comparing runs against
    nodes[0]'s own parent rather than root_style directly.

    Needed because a run's own immediate parent isn't always where a boxed
    sub-rule's fill colour actually lives: Genestealer Cults' "BROOD
    BROTHERS" sub-rule wraps its body in a .Corner26_in pane with its own
    solid #eadaec background, several DOM levels deep. A bare text run
    sitting directly in that pane and a bold keyword span nested one level
    further inside it both render against the exact same colour visually,
    but comparing each run only to its own immediate parent (the old
    behaviour) caught the pane's edge for the first and missed it for the
    second — tagging "background" on some plain-text runs in a sentence but
    not nested keyword spans in that very same sentence. Comparing every
    run in the block against this one shared reference point instead keeps
    that judgement consistent across the whole block.
    """
    if not nodes:
        return root_style.get("background")

    parent = getattr(nodes[0], "parent", None)
    return tag_style(parent, styles, root_style).get("background")


def _apply_block_color(nodes, runs, styles, root_style):
    """Split a block's colour into a block-level base and per-run overrides,
    and strip background down to genuine local highlights only.

    Colour: two separate questions, two separate places to answer them:
    - Does this whole block's own colour differ from the page's overall
      default (root_style)? If so that's worth recording once, on the block
      itself — otherwise an all-red paragraph would silently lose its colour
      just because every run inside it agrees with the paragraph's own base.
    - Does an individual run differ from *its own block's* base colour (not
      the page default)? That's genuine local emphasis, e.g. one highlighted
      word inside otherwise-plain prose, and is kept on the run.

    Background gets only the second treatment, not the first: the renderer
    paints any run with a "background" as an individually highlighted pill
    (padding + rounded corners — see textRuns in waha_content_render_utils_v2.js),
    which is correct for a genuine inline highlight (e.g. Wahapedia's own
    <span class="redPad">) but would be wrong for a boxed sub-rule's own
    uniform fill (every run in the box would render as its own separate
    highlighted pill instead of one clean box — the box's own identity is
    already conveyed by it being its own titled sub-rule). So a run's
    background is dropped entirely once it matches the block's own context
    background, with no block-level equivalent recorded anywhere.

    Returns (runs_with_colour/background_stripped_to_local_overrides, block_color).
    """
    block_color = _block_context_color(nodes, styles, root_style)
    block_background = _block_context_background(nodes, styles, root_style)

    result = []
    for run in runs:
        run = dict(run)
        color = run.pop("color", None)
        if color and color != block_color:
            run["color"] = color
        background = run.pop("background", None)
        if background and background != block_background:
            run["background"] = background
        result.append(run)

    recorded_block_color = block_color if block_color != root_style.get("color") else None
    return result, recorded_block_color


class _SoftBreak:
    """Sentinel for a single <br> that turned out to be a soft, in-paragraph
    line break rather than the start of a double-<br> paragraph split (see
    extract_content_blocks) — placed directly into paragraph_nodes so it
    flows through extract_runs/merge_runs at its real position instead of
    being silently dropped, the way a lone <br> previously was."""


SOFT_BREAK = _SoftBreak()


def extract_runs(node, styles, root_style):
    """Walk a node into text runs tagged with resolved style, not raw classes.

    A text node's style is simply its own parent's resolved computed style —
    getComputedStyle already reflects the full cascade/inheritance at that
    point, so there's no need to accumulate a class list up the ancestor
    chain the way a class-based parser would.
    """
    if node is SOFT_BREAK:
        return [{"br": True}]

    if isinstance(node, NavigableString):
        text = _clean_inline(str(node))

        if not text:
            return []

        if not text.strip():
            return [{"text": " ", "source_classes": []}]

        style = tag_style(node.parent, styles, root_style)
        return [_build_run(text, style)]

    if not isinstance(node, Tag) or node.name in ("script", "style", "br"):
        return []

    if is_fluff(node):
        return []

    runs = []
    for child in node.children:
        runs.extend(extract_runs(child, styles, root_style))

    return runs


def _is_word_boundary(left_text, right_text):
    """True when two texts are alnum-adjacent and would glue into one word.

    Guards against upstream node filtering silently dropping the whitespace
    node between two same-styled adjacent spans (e.g. two separate keyword
    spans with a literal space between them in the source markup, but no
    intervening whitespace run reaching merge_runs). Without this, merging
    same-theme runs with no separator would turn "ADEPTUS" + "ASTARTES" into
    "ADEPTUSASTARTES".
    """
    if not left_text or not right_text:
        return False
    if left_text[-1].isspace() or right_text[0].isspace():
        return False
    if not left_text[-1].isalnum() or not right_text[0].isalnum():
        return False
    return True


def merge_runs(runs):
    """Merge adjacent runs that share the same resolved theme.

    Whitespace-only runs never block a merge: they're folded into the
    surrounding text when the runs on either side share a theme (so
    "DEATH" + " " + "COMPANY" with matching bold/upper collapses into one
    run), and preserved standalone only when the two sides actually differ.
    """
    merged = []
    pending_ws = ""

    for run in runs:
        if run.get("br"):
            # A hard line break never merges with neighbouring text (in
            # either direction) — any pending trailing whitespace right
            # before it is visually insignificant, so it's simply dropped
            # rather than turned into a stray space run.
            merged.append(dict(run))
            pending_ws = ""
            continue

        text = run["text"]

        if not text.strip():
            pending_ws += text
            continue

        if merged and _theme_key(merged[-1]) == _theme_key(run):
            separator = pending_ws or (" " if _is_word_boundary(merged[-1]["text"], text) else "")
            merged[-1]["text"] = _clean_inline(merged[-1]["text"] + separator + text)
            pending_ws = ""
        else:
            if pending_ws and merged:
                merged.append({"text": " ", "source_classes": []})
            merged.append(dict(run))
            pending_ws = ""

    return merged


# =========================================================
# BLOCK STRUCTURE (paragraphs / tables / lists / subrules)
# =========================================================

def is_br(node):
    return isinstance(node, Tag) and node.name == "br"


def is_paragraph_separator(node):
    return isinstance(node, Tag) and "dsLineHor" in (node.get("class") or [])


def is_fluff(node):
    if not isinstance(node, Tag):
        return False
    classes = node.get("class") or []
    return "ShowFluff" in classes or "legend" in classes or "legend2" in classes


def is_ignorable(node):
    if isinstance(node, NavigableString):
        return not str(node).strip()

    if not isinstance(node, Tag):
        return True

    if node.name in ("script", "style"):
        return True

    if is_fluff(node):
        return True

    style = (node.get("style") or "").replace(" ", "").lower()
    return "display:none" in style


def find_section_by_anchor_prefix(soup, anchor_prefix):
    anchor = soup.find("a", attrs={"name": lambda v: v and v.startswith(anchor_prefix)})
    if not anchor:
        return None
    return anchor.find_parent("div", class_="BreakInsideAvoid")


def paragraph_block(nodes, styles, root_style):
    runs = []
    for node in nodes:
        runs.extend(extract_runs(node, styles, root_style))

    runs, block_color = _apply_block_color(nodes, merge_runs(runs), styles, root_style)
    if not runs:
        return None

    block = {"displayItem": "p", "runs": runs}
    if block_color:
        block["color"] = block_color

    return block


def parse_list_item(li, styles, root_style):
    # A list item isn't always just text — it can hold a table (e.g. a
    # "Battle Size" reference table nested a few levels deep inside a
    # bullet), a nested sub-list, or an image, so it needs the same general
    # block splitter used everywhere else rather than a text-only run
    # extraction that has no concept of anything but inline text.
    return {
        "content": extract_content_blocks(list(li.children), styles, root_style),
    }


# =========================================================
# VISUAL WIDGETS (round-trackers, action icons, ...)
#
# A handful of Wahapedia elements aren't styled text at all — they're compact
# icons/badges (e.g. a "BATTLE ROUND [N]" diamond marker) whose appearance
# comes entirely from CSS on specific classes (background shapes/colours),
# not from font-weight/style/transform/colour. There's no generic bold/
# italic/upper/color reduction that could represent "draw a red-and-white
# diamond with a number in it", so these need their real class structure
# preserved instead of being run through the text-run model — the same,
# already-proven exception waha_parse_utils.is_visual_widget_node makes for
# the old parser. This is a different kind of exception to the "don't
# hardcode class semantics" rule than e.g. bluefont/aeText would have been:
# those were about ordinary text that happens to carry a semantic class;
# these are icons that were never text-shaped to begin with.
# =========================================================
# (VISUAL_WIDGET_CLASS_MARKERS itself lives above STAMP_JS — shared with the
# JS stamping pass so there's one list, not two hand-kept copies.)

LAYOUT_WRAPPER_CLASSES = {
    "BreakInsideAvoid",
    "Columns2",
    "frameLight",
    "Corner16",
    "Corner16_in",
}


def _has_widget_marker_class(node):
    if not isinstance(node, Tag):
        return False
    class_text = " ".join(node.get("class") or [])
    return any(marker in class_text for marker in VISUAL_WIDGET_CLASS_MARKERS)


def is_visual_widget(node, styles=None, root_style=None):
    if not isinstance(node, Tag):
        return False

    classes = node.get("class") or []

    if any(cls in LAYOUT_WRAPPER_CLASSES for cls in classes):
        return False

    if _has_widget_marker_class(node):
        return True

    style = (node.get("style") or "").replace(" ", "").lower()
    if node.name == "div" and "display:inline-block" in style and node.find("img"):
        return True

    # A div that sets its own real (non-transparent) background, different
    # from its parent's, AND wraps a recognised widget marker somewhere
    # inside it is a banner/badge container for that widget (e.g. Wahapedia's
    # "BATTLE ROUND [N]" banner) — detected generically from the live
    # computed style rather than a hardcoded class name, the same principle
    # used for the <a> exception above. Requiring both signals (not just a
    # divergent background alone) keeps this from also swallowing an
    # unrelated highlighted-but-plain-text callout box.
    if styles is not None and node.name == "div":
        own_bg = tag_style(node, styles, root_style).get("background")
        parent_bg = tag_style(node.parent, styles, root_style).get("background") if node.parent else None

        if own_bg and own_bg != parent_bg and node.find(_has_widget_marker_class):
            return True

    return False


def parse_visual_element(node, styles, root_style):
    children = []

    for child in node.children:
        if isinstance(child, NavigableString):
            # Resolved the same way as any other text — a widget's own label
            # (e.g. "BATTLE ROUND") can carry real bold/colour just like
            # ordinary prose; there's no reason to hardcode it away just
            # because it's a bare string sitting inside a widget. Routed
            # through _apply_block_color (like paragraph_block) rather than
            # kept as _build_run's raw absolute colour/background: without
            # it, extract_runs's own background would always be reported
            # unconditionally, painting this bare label text as its own
            # highlighted pill any time it happens to sit against a real
            # background (i.e. almost any widget) instead of only when it
            # genuinely diverges from its surroundings.
            runs, _ = _apply_block_color([child], merge_runs(extract_runs(child, styles, root_style)), styles, root_style)
            if runs:
                children.append({"displayItem": "span", "runs": runs})
            continue

        if not isinstance(child, Tag) or is_ignorable(child):
            continue

        if child.name == "img":
            children.append({
                "displayItem": "img",
                "src": child.get("src"),
                "alt": child.get("alt", ""),
            })
            continue

        if is_visual_widget(child, styles, root_style) or child.name in ("div", "span", "i", "b", "em", "strong", "small", "a"):
            if child.find(True) or child.get("class") or child.get("style"):
                children.append(parse_visual_element(child, styles, root_style))
                continue

        runs, _ = _apply_block_color([child], merge_runs(extract_runs(child, styles, root_style)), styles, root_style)
        if runs:
            children.append({"displayItem": "span", "runs": runs})

    element = {
        "displayItem": "element",
        "tag": node.name,
        "classes": node.get("class", []),
        "style": node.get("style", ""),
        "children": children,
    }

    # The background divergence that qualified this node as a widget/cluster
    # root in the first place (see is_visual_widget) was already resolved
    # live during the scrape — store that real value directly rather than
    # discarding it and making a renderer re-derive it later from the class
    # name via the CSS manifest. A solid banner fill like whiteDiamondLine's
    # doesn't need an external asset lookup at all; only classes whose
    # appearance is an actual image/mask (not a plain colour) still do.
    own_bg = tag_style(node, styles, root_style).get("background")
    parent_bg = tag_style(node.parent, styles, root_style).get("background") if node.parent else None
    if own_bg and own_bg != parent_bg:
        element["background"] = own_bg

    # Real size/spacing for this specific instance (see STAMP_JS's
    # hasWidgetMarker/measured) — captured live, not diffed against the
    # parent, since geometry is inherently the element's own rather than
    # something ambient/inherited like colour.
    own_measured = tag_style(node, styles, root_style).get("measured")
    if own_measured:
        element["measured"] = own_measured

    return element


def _inline_style_property(style, prop):
    """Read one property out of a raw inline `style="..."` string.

    Wahapedia's hand-built data tables (e.g. Thousand Sons' Psychic Test
    Sequence) set per-cell text-align/vertical-align directly as inline
    styles — there's no computed-style divergence to resolve here (unlike
    background), just a literal declaration to read straight off the tag.
    """
    for part in (style or "").split(";"):
        if ":" not in part:
            continue
        key, _, value = part.partition(":")
        if key.strip().lower() == prop:
            return value.strip()
    return None


def parse_table(table, styles, root_style):
    inner = table.find("table")
    if inner:
        table = inner

    rows = []
    for tr in table.find_all("tr"):
        cells = []
        for cell in tr.find_all(["td", "th"], recursive=False):
            if cell.find("table"):
                continue

            # A cell isn't always just text (e.g. a D6-result column can hold
            # bare <img> icons interspersed with text), so it needs the same
            # general block splitter used everywhere else, not a text-only
            # run extraction that silently drops anything without text.
            cell_entry = {
                "content": extract_content_blocks(list(cell.children), styles, root_style),
                "colspan": cell.get("colspan"),
                "rowspan": cell.get("rowspan"),
            }

            # A header row (or a striped alternating row) can set its own
            # background directly on the <td>, same as a rule heading or a
            # widget banner — resolved live and kept only when it genuinely
            # diverges from the row it sits in, not stored as a raw class/
            # style dump.
            own_bg = tag_style(cell, styles, root_style).get("background")
            parent_bg = tag_style(cell.parent, styles, root_style).get("background") if cell.parent else None
            if own_bg and own_bg != parent_bg:
                cell_entry["background"] = own_bg

            cell_style = cell.get("style") or ""
            text_align = _inline_style_property(cell_style, "text-align")
            vertical_align = _inline_style_property(cell_style, "vertical-align")
            width = _inline_style_property(cell_style, "width")
            if text_align:
                cell_entry["align"] = text_align
            if vertical_align:
                cell_entry["valign"] = vertical_align
            if width:
                cell_entry["width"] = width

            cells.append(cell_entry)

        if cells:
            rows.append(cells)

    return {"displayItem": "table", "rows": rows}


def extract_subrule_from_table(block, styles, root_style):
    title_node = block.select_one(".impact18")
    if not title_node:
        return None

    content_nodes = [
        node for node in title_node.next_siblings
        if not is_ignorable(node) or is_br(node)
    ]

    return {
        "displayItem": "subrule",
        "title": clean_text(title_node),
        "content": extract_content_blocks(content_nodes, styles, root_style),
    }


def parse_cs_rule_wrapper(node, styles, root_style):
    """A named sub-rule with its own separate requirement badge, e.g.:

        <div class="stratWrapper_CS">
          <div class="stratName_CS stratReq">
            <span>DEATHLY TERROR (AURA)</span>
            <span><div class="cruD6wrap">N/A</div></span>
          </div>
          <div class="stratText_CS">...</div>
        </div>

    The requirement (a D6 threshold, or "N/A") is a genuinely separate piece
    of structured data, not just a bold prefix on flowing text — unlike a
    plain named-ability title, so it keeps its own field rather than being
    folded into a run.
    """
    name_el = node.select_one(".stratName_CS")
    text_el = node.select_one(".stratText_CS")

    if not name_el or not text_el:
        return None

    title_span = name_el.find("span")
    title = clean_text(title_span or name_el)

    req_el = name_el.select_one(".cruD6wrap")

    # The requirement is sometimes plain text ("N/A") and sometimes a D6-pip
    # icon (<div class="cruD6wrap"><div class="cruD3"></div></div>) with no
    # text in it at all — clean_text() on that silently returns "", losing
    # the die pip entirely. Preserve it as a real widget element (same as
    # any other icon) whenever one is actually present, instead of just
    # taking whatever text happens to be there.
    requirement = ""
    if req_el:
        widget = req_el.find(_has_widget_marker_class)
        requirement = parse_visual_element(req_el, styles, root_style) if widget else clean_text(req_el)

    return {
        "displayItem": "cs_rule",
        "title": title,
        "requirement": requirement,
        "content": extract_content_blocks(list(text_el.children), styles, root_style),
    }


def _attach_content_to_custom_subrules(blocks):
    """Fold trailing content into an h_custom/hi_custom subrule heading.

    Unlike an `.impact18`-table subrule (whose content is scoped by the table
    itself), a bare `<span class="hi_custom">` heading is just inline text
    with no natural container — everything after it belongs to it until the
    next subrule heading. extract_content_blocks emits it with empty content
    since it can't know that boundary node-by-node; this stitches the
    following blocks back in afterward.
    """
    out = []
    i = 0

    while i < len(blocks):
        block = blocks[i]

        if (
            block.get("displayItem") == "subrule"
            and block.get("content") == []
            and block.get("source") == "h_custom"
        ):
            block = dict(block)
            content = []
            j = i + 1

            # A <br> immediately after the heading itself (e.g.
            # <span class="hi_custom">TITLE</span><br>body text...) is just
            # the heading ending its own line, the same job the heading's own
            # block-level styling already does visually — not a meaningful
            # break to preserve inside the folded-in body content.
            if j < len(blocks) and blocks[j].get("displayItem") == "br":
                j += 1

            while j < len(blocks):
                # A battleRoundBanner marks the START of whatever comes next
                # (e.g. Adeptus Mechanicus' Rad-bombardment: a "BATTLE ROUND
                # 2 ONWARDS" banner sits right before "FALLOUT", announcing
                # that section — not trailing content of the PRECEDING
                # "BOMBARDMENT" heading), so it's a boundary like a subrule
                # itself, not content to fold in.
                if blocks[j].get("displayItem") in ("subrule", "battleRoundBanner"):
                    break
                content.append(blocks[j])
                j += 1

            if content:
                block["content"] = content
                out.append(block)
                i = j
                continue

        out.append(block)
        i += 1

    return out


def _parse_battle_round_banner(node, styles, root_style):
    """A "BATTLE ROUND [N]" banner (e.g. Adeptus Mechanicus' Rad-bombardment
    detachment rule, which only triggers "At the start of the first battle
    round") is built from a rotated-square diamond badge — .redDiamondSpan
    and .whiteDiamondSpan each carry a real transform:rotate(45deg), with
    the round number counter-rotated back level via .whiteDiamondText —
    absolutely positioned over a solid accent-coloured bar (.whiteDiamondLine
    itself). Our generic style capture only ever resolves bold/italic/upper/
    colour/background/box-geometry (see resolve() in STAMP_JS) — never CSS
    transform or absolute-position offsets — so passing this straight
    through the generic div/is_visual_widget path left the badge as a stack
    of plain, unrotated, out-of-place squares instead of a diamond sitting at
    the end of the bar. Extracting just the real content (the bar's label
    text either side of the badge, the round number, and the handful of
    colours involved) and handing them to a purpose-built renderer
    sidesteps needing to capture transform/position generically at all —
    the same approach already used for the CP-cost badge and the stratagem
    icon bar.

    The badge sits INLINE in the source, not pinned to the bar's far edge —
    "BATTLE ROUND [1]" has nothing after it, but "BATTLE ROUND [2] ONWARDS"
    has the badge in between the two words, not at the end. beforeLabel/
    afterLabel (rather than a single joined label) preserve that position
    instead of concatenating "BATTLE ROUND" and "ONWARDS" into one string
    and losing where the badge actually belongs.
    """
    wrap_el = node.select_one('[class*="DiamondWrap"]')
    outer_el = wrap_el.select_one('[class*="DiamondSpan"]') if wrap_el else None
    inner_el = outer_el.select_one('[class*="DiamondSpan"]') if outer_el else None
    round_el = (inner_el or outer_el).select_one('[class*="DiamondText"]') if (inner_el or outer_el) else None

    before_nodes = []
    after_nodes = []
    seen_wrap = False
    for child in node.children:
        if child is wrap_el:
            seen_wrap = True
            continue
        (after_nodes if seen_wrap else before_nodes).append(child)

    before_label = clean_text(BeautifulSoup("".join(str(n) for n in before_nodes), "html.parser"))
    after_label = clean_text(BeautifulSoup("".join(str(n) for n in after_nodes), "html.parser"))
    if not before_label and not after_label and not round_el:
        return None

    bar_style = tag_style(node, styles, root_style)
    outer_style = tag_style(outer_el, styles, root_style) if outer_el else None
    inner_style = tag_style(inner_el, styles, root_style) if inner_el else None
    round_style = tag_style(round_el, styles, root_style) if round_el else None

    return {
        "displayItem": "battleRoundBanner",
        "beforeLabel": before_label,
        "afterLabel": after_label,
        "round": clean_text(round_el) if round_el else "",
        "background": bar_style.get("background"),
        "color": bar_style.get("color"),
        "badgeBackground": outer_style.get("background") if outer_style else None,
        "badgeFill": inner_style.get("background") if inner_style else None,
        "badgeColor": round_style.get("color") if round_style else None,
    }


def extract_content_blocks(nodes, styles, root_style):
    blocks = []
    paragraph_nodes = []
    br_count = 0

    def flush():
        nonlocal paragraph_nodes
        block = paragraph_block(paragraph_nodes, styles, root_style)
        if block:
            blocks.append(block)
        paragraph_nodes = []

    for node in nodes:
        if is_paragraph_separator(node):
            flush()
            br_count = 0
            continue

        if is_br(node):
            if not paragraph_nodes and blocks and blocks[-1].get("displayItem") != "br":
                # A <br> sitting between two already-distinct blocks (e.g.
                # three separate <div><img></div> siblings with a <br>
                # between each, no wrapping paragraph at all) is a real,
                # intentional line break in the source — not an instance of
                # the double-<br>-means-new-paragraph convention below, which
                # only makes sense while text is actually accumulating.
                # Without this, a <br> here was silently dropped (br_count
                # was only ever used to flush flowing text), so images meant
                # to sit on separate lines rendered squashed onto one.
                #
                # Guarded on `blocks` being non-empty (not just paragraph_nodes
                # being empty) so a <br> that's the very first node — e.g.
                # right after a heading, before any body text has
                # accumulated — doesn't fabricate a leading blank line that
                # wasn't visually there before. Also collapse repeats so a
                # trailing <br><br> doesn't turn into two stacked blank
                # lines.
                blocks.append({"displayItem": "br"})
                br_count = 0
                continue

            br_count += 1
            if br_count >= 2:
                flush()
                br_count = 0
            continue

        if br_count == 1:
            # Exactly one <br> occurred while text was accumulating, and it
            # did NOT turn out to be the first half of a double-<br>
            # paragraph split (that already flushed above) — it's a soft,
            # in-paragraph line break instead (e.g. "Channel the
            # Warp<br>(Optional)"), preserved as a real break in the run
            # sequence rather than silently dropped, which glued the two
            # sides together with no separator at all.
            paragraph_nodes.append(SOFT_BREAK)

        br_count = 0

        if is_ignorable(node) and not isinstance(node, NavigableString):
            continue

        if (
            isinstance(node, Tag)
            and node.name == "span"
            and ("h_custom" in (node.get("class") or []) or "hi_custom" in (node.get("class") or []))
        ):
            flush()
            blocks.append({
                "displayItem": "subrule",
                "title": clean_text(node),
                "content": [],
                "source": "h_custom",
            })
            continue

        if (
            isinstance(node, Tag)
            and node.name == "div"
            and any("DiamondLine" in c for c in (node.get("class") or []))
        ):
            flush()
            banner = _parse_battle_round_banner(node, styles, root_style)
            if banner:
                blocks.append(banner)
            continue

        if isinstance(node, Tag) and node.name == "table":
            flush()
            subrule = extract_subrule_from_table(node, styles, root_style)
            blocks.append(subrule if subrule else parse_table(node, styles, root_style))
            continue

        if isinstance(node, Tag) and node.name in ("ul", "ol"):
            flush()
            blocks.append({
                "displayItem": node.name,
                "items": [
                    parse_list_item(li, styles, root_style)
                    for li in node.find_all("li", recursive=False)
                ],
            })
            continue

        if isinstance(node, Tag) and node.name == "img":
            flush()
            img_block = {
                "displayItem": "img",
                "src": node.get("src"),
                "alt": node.get("alt", ""),
            }
            if node.get("style"):
                img_block["style"] = node.get("style")
            blocks.append(img_block)
            continue

        if isinstance(node, Tag) and "stratWrapper_CS" in (node.get("class") or []):
            flush()
            parsed = parse_cs_rule_wrapper(node, styles, root_style)
            if parsed:
                blocks.append(parsed)
            continue

        if isinstance(node, Tag) and node.name == "div":
            flush()

            if is_visual_widget(node, styles, root_style):
                blocks.append(parse_visual_element(node, styles, root_style))
            else:
                # An alternate-mode stratagem effect can carry its own extra
                # CP cost as a badge buried inside the mode's own text (e.g.
                # Heroic Intervention's "Into the Fray" mode: <div
                # class="str11Text2"><div class="str11CP2">+1CP</div><ul>...
                # </ul></div>). Pull the badge's text out and extract() it
                # from the tree before the generic recursion below runs, so
                # it lands as a distinct `extraCost` field on the mode's own
                # block instead of getting flattened into the middle of that
                # block's flowing text (which is what happened before this
                # existed — a "+1CP" appearing mid-sentence with no
                # indication of what it actually costs extra for).
                cp2_el = node.select_one(".str10CP2, .str11CP2")
                extra_cost = clean_text(cp2_el) if cp2_el else None
                if cp2_el:
                    cp2_el.extract()

                child_blocks = extract_content_blocks(list(node.children), styles, root_style)

                if extra_cost and child_blocks:
                    child_blocks[0]["extraCost"] = extra_cost

                # A wrapping div with its own float (e.g. Drukhari's
                # <div class="img-inv" style="float:left;"><img ...></div>)
                # is how Wahapedia makes an image sit beside the text that
                # follows it, with that text wrapping around it — not
                # stacked as its own separate block. Unwrapping the div
                # (immediately above) already discarded that float; carry
                # it onto the img block(s) actually found inside so the
                # renderer can let the browser's own float/wrap layout
                # reproduce the same live-page positioning.
                div_style = node.get("style") or ""
                if "float" in div_style.replace(" ", "").lower():
                    for child_block in child_blocks:
                        if child_block.get("displayItem") != "img":
                            continue
                        own_style = child_block.get("style") or ""
                        combined = f"{div_style.rstrip(';')}; {own_style}".strip("; ")
                        child_block["style"] = combined

                # A div holding nothing but its own inline presentation style
                # (e.g. Aeldari's centered "AGILE MANOEUVRES" section title:
                # <div style="text-align:center;font-size:2em;...">) is how
                # Wahapedia sets off a subheading using ad hoc inline style
                # instead of a named class. Unwrapping already flattened it
                # to a plain 'p' block above, discarding that style; carry it
                # onto the p block(s) so the renderer's existing blockAttrs
                # style passthrough reproduces the same alignment/sizing.
                # Scoped to 'p' blocks only (not table/img/subrule) so a
                # style on a div that wraps richer structure — e.g. the
                # unstyled Columns2 wrapper around the Battle Focus table —
                # never leaks onto content it wasn't actually set on.
                if "text-align" in div_style.replace(" ", "").lower():
                    for child_block in child_blocks:
                        if child_block.get("displayItem") != "p":
                            continue
                        own_style = child_block.get("style") or ""
                        combined = f"{div_style.rstrip(';')}; {own_style}".strip("; ")
                        child_block["style"] = combined

                blocks.extend(child_blocks)

            continue

        # A plain inline tag (e.g. <b>) can still wrap an <img> directly —
        # e.g. two dice icons grouped in one <b> with a literal nbsp between
        # them, no real bold text involved. extract_runs only ever produces
        # text runs, so an image nested this way would silently vanish if
        # this fell through to flowing paragraph content below. Unwrap it
        # the same way a <div> already is, so the image is found by the
        # ordinary img handling above instead of being flattened away.
        if isinstance(node, Tag) and node.find("img"):
            flush()
            blocks.extend(extract_content_blocks(list(node.children), styles, root_style))
            continue

        # <p> is a block-level element in its own right — adjacent sibling
        # <p> tags with no <br>/dsLineHor separator between them (e.g. a
        # title paragraph immediately followed by a body paragraph) must
        # still land in separate blocks, not get silently flattened into one
        # shared run of text just because nothing marked the boundary.
        #
        # A <p> isn't always pure flowing text either — e.g. an action-icon
        # widget div sitting directly inside a <p>, with a double <br>
        # further splitting TRIGGER:/EFFECT: into two paragraphs of their
        # own. Recursing through extract_content_blocks (rather than a single
        # paragraph_block() call over all of the <p>'s children) lets the
        # existing table/div/img/br-pair handling apply inside the <p> too,
        # instead of silently dropping the widget (extract_runs only ever
        # produces text) and gluing TRIGGER:/EFFECT: together.
        if isinstance(node, Tag) and node.name == "p":
            flush()
            blocks.extend(extract_content_blocks(list(node.children), styles, root_style))
            continue

        paragraph_nodes.append(node)

    flush()
    return _attach_content_to_custom_subrules(blocks)


# =========================================================
# STRATAGEM CONTENT (shared by core-stratagems and per-detachment
# stratagem extraction — both use the same wrapper markup, so this is the
# one place that knows about it instead of two scrapers each keeping their
# own copy. Wahapedia has begun rolling out a restructured version of this
# markup (`.str11Wrap`, seen so far on the wh40k11ed preview edition)
# alongside the older `.str10Wrap` still used on wh40k10ed pages — auto
# detected per wrap so callers don't need to know or care which edition a
# given page is serving.)
# =========================================================

STRATAGEM_FIELD_LABELS = {"WHEN", "TARGET", "EFFECT", "RESTRICTIONS"}


def extract_stratagem_field_block(text_el, label, styles, root_style):
    """Pull the node range for one WHEN/TARGET/EFFECT/RESTRICTIONS field out
    of a stratagem's body text. The label itself can be carried by either a
    <span> (str10: `<span class="str10ColorEither"><b>WHEN:</b></span>`) or
    a bare <b> (str11: `<b>WHEN:</b>`, no wrapping span at all) — matching
    on the tag's text content rather than requiring a specific tag name is
    what lets one function handle both eras without caring which produced
    the label. Coincidental unrelated <b> text (e.g. a bolded tooltip
    phrase) never collides with this since only an exact WHEN/TARGET/
    EFFECT/RESTRICTIONS match (case-insensitive, trailing colon stripped)
    is treated as a label.

    Returns a LIST of content blocks (via extract_content_blocks), not a
    single flat paragraph — some fields hold more than flowing text (e.g.
    Heroic Intervention's EFFECT lists two alternate modes, one with its own
    extra CP cost), and only the general block pipeline already used for
    army/detachment rule content actually preserves that structure instead
    of flattening it into a single run of text.
    """
    if not text_el:
        return None

    label = label.upper()
    collecting = False
    nodes = []

    for child in text_el.children:
        if isinstance(child, Tag):
            child_text = clean_text(child).upper().rstrip(":")

            if child.name in ("span", "b") and child_text == label:
                collecting = True
                continue

            if (
                collecting
                and child.name in ("span", "b")
                and child_text in STRATAGEM_FIELD_LABELS
                and child_text != label
            ):
                break

        if collecting:
            nodes.append(child)

    return extract_content_blocks(nodes, styles, root_style) or None


def strip_detachment_name_from_type(value):
    if "–" in value:
        return clean_inline(value.split("–", 1)[1]).strip()
    return value.strip()


def _str10_stratagem_icon_classes(wrap):
    # These identify which icon graphic to render (e.g. a CP-cost diamond),
    # not text formatting — collapsing them into bold/italic/upper wouldn't
    # make sense, so they're kept as the raw identifiers they already are.
    icons = []

    for div in wrap.select(".str10Diamond div[class]"):
        classes = div.get("class", [])

        candidates = [
            c for c in classes
            if c.startswith("str10")
            and not c.startswith("str10Color")
            and c not in {
                "str10Diamond",
                "str10CP",
                "str10Pos2",
                "str10DiamondWrap",
            }
        ]

        for candidate in candidates:
            if candidate not in icons:
                icons.append(candidate)

    return icons


def _str10_stratagem_color_class(wrap):
    for c in wrap.get("class", []):
        if c.startswith("str10Color"):
            return c

    for el in wrap.select("[class]"):
        for c in el.get("class", []):
            if c.startswith("str10Color"):
                return c

    return None


def _str11_stratagem_icon_classes(wrap):
    # Each `.str11Icon` div carries exactly one extra class identifying its
    # own glyph (e.g. str11Movement, str11Any, str11Either) — unlike str10,
    # which packed a phase glyph AND a colour-eligibility class onto the
    # SAME div and recoloured one shared icon via a CSS filter, str11 just
    # gives every glyph its own already-correctly-coloured PNG, so there's
    # no colour class to strip back out here the way str10 needs.
    icons = []

    for div in wrap.select(".str11Icon"):
        for c in div.get("class", []):
            if c != "str11Icon" and c.startswith("str11") and c not in icons:
                icons.append(c)

    return icons


def _str11_stratagem_color_class(wrap):
    # str11 puts the colour-eligibility class on .str11Type, not on the
    # wrap itself or on an icon div — it's the one place that's always
    # present on a real stratagem (see the None-return guard below for the
    # action-glossary cards that share this markup but have no Type row).
    type_el = wrap.select_one(".str11Type")
    if not type_el:
        return None

    for c in type_el.get("class", []):
        if c.startswith("str11Color"):
            return c

    return None


def _str11_stratagem_bg_class(wrap):
    # The icon column's own frame texture (e.g. <div class="str11Bg
    # str11StratBg">) lives on the WRAPPING div around the .str11Icon
    # glyphs, not on any of the glyphs themselves — _str11_stratagem_icon_
    # classes only ever looks at .str11Icon children, so without this the
    # wrapper's own class (the only thing that actually names which frame
    # texture to use) was never recorded anywhere at all. Nothing
    # downstream (css builder, renderer) can download or reference an
    # asset for a class that was never even captured in the first place.
    bg_el = wrap.select_one(".str11Bg")
    if not bg_el:
        return None

    for c in bg_el.get("class", []):
        if c != "str11Bg" and c.startswith("str11"):
            return c

    return None


def extract_stratagem_from_wrap(wrap, styles, root_style):
    """Extract one stratagem card's fields from its wrapper element,
    auto-detecting str10 vs str11 markup from the wrap's own class. Returns
    None for a wrap that isn't actually a stratagem — Wahapedia's str11
    markup is also reused for core-rules action-glossary entries (e.g.
    "NORMAL MOVE", "FALL-BACK MOVE"), which have no CP cost or Type row at
    all, unlike every real stratagem.
    """
    is_str11 = "str11Wrap" in (wrap.get("class") or [])
    prefix = "str11" if is_str11 else "str10"

    name = clean_text(wrap.select_one(f".{prefix}Name"))
    if not name:
        return None

    type_el = wrap.select_one(f".{prefix}Type")
    if is_str11 and not type_el:
        return None

    text_el = wrap.select_one(f".{prefix}Text")

    return {
        "name": name,
        "cp": clean_text(wrap.select_one(f".{prefix}CP")),
        "type": strip_detachment_name_from_type(clean_text(type_el)),
        # Deliberately not extracting .{prefix}Legend — it's Wahapedia's own
        # ShowFluff-tagged flavour text (<div class="str11Legend ShowFluff">),
        # and ShowFluff content is excluded everywhere else in this parser
        # (see is_fluff/is_ignorable, used throughout extract_content_blocks/
        # extract_runs) — this field would have been the one place fluff
        # text slipped through, bypassing that filter entirely by reading
        # the element directly instead of going through the general pipeline.
        "when": extract_stratagem_field_block(text_el, "WHEN", styles, root_style),
        "target": extract_stratagem_field_block(text_el, "TARGET", styles, root_style),
        "effect": extract_stratagem_field_block(text_el, "EFFECT", styles, root_style),
        "restrictions": extract_stratagem_field_block(text_el, "RESTRICTIONS", styles, root_style),
        "icon_classes": (
            _str11_stratagem_icon_classes(wrap) if is_str11
            else _str10_stratagem_icon_classes(wrap)
        ),
        "color_class": (
            _str11_stratagem_color_class(wrap) if is_str11
            else _str10_stratagem_color_class(wrap)
        ),
        # str10 has no equivalent concept (its icon column is our own drawn
        # diamond shape, not a downloaded frame texture) — None there.
        "bg_class": _str11_stratagem_bg_class(wrap) if is_str11 else None,
    }
