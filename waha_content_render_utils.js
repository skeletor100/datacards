function esc(value) {
  return String(value ?? '')
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
    .replaceAll("'", '&#039;');
}

function textRuns(block) {
  if (!block) return '';

  const runs = Array.isArray(block.runs) ? block.runs : [];
  const pieces = [];
  let previousText = '';
  let previousClasses = [];

  const isWordBoundary = (left, right) => {
    if (!left || !right) return false;
    return /[\p{L}\p{N}\])]$/u.test(left) && /^[\p{L}\p{N}[(]/u.test(right);
  };

  for (const run of runs) {
    // A hard line break within the paragraph (e.g. "Channel the
    // Warp<br>(Optional)") — a single <br> that occurred while text was
    // accumulating, preserved as its own run (see waha_scraper_common.py's
    // SOFT_BREAK) instead of being silently dropped, which glued both
    // sides together with no separator at all.
    if (run?.br) {
      pieces.push('<br>');
      previousText = '';
      previousClasses = [];
      continue;
    }

    const rawText = String(run?.text ?? '');
    if (!rawText) continue;

    const sourceClasses = Array.isArray(run.source_classes)
      ? run.source_classes.filter(Boolean)
      : [];

    // Whitespace-only runs are semantic separators between adjacent styled
    // Wahapedia spans, e.g. <span class="kwb">AGENTS</span> <span class="kwb2">OF</span>.
    // Render them as literal text instead of a styled span so they cannot be
    // swallowed by class merging or copied innerHTML simplification.
    if (!rawText.trim() && !sourceClasses.length) {
      pieces.push(' ');
      previousText = ' ';
      previousClasses = [];
      continue;
    }

    // Defensive fallback: if upstream data ever arrives without the separator
    // run, do not glue adjacent word-like styled runs together as AGENTSOF or
    // ADEPTUSASTARTES. Existing explicit whitespace above remains the source of
    // truth; this only covers missing separators.
    if (
      pieces.length &&
      previousText.trim() &&
      rawText.trim() &&
      sourceClasses.length &&
      previousClasses.length &&
      isWordBoundary(previousText, rawText)
    ) {
      pieces.push(' ');
    }

    const classes = esc(sourceClasses.join(' '));
    const text = esc(rawText);
    const classAttr = classes ? ` class="${classes}"` : '';
    // A run's background only ever appears when it genuinely diverged from
    // its surrounding context (see waha_scraper_common._build_run) — e.g.
    // Wahapedia's own <span class="redPad">, a small highlighted pill
    // (dark background + near-white text) that only reads correctly with
    // both pieces together. The small padding/radius here isn't a captured
    // value (the original "redPad" class name itself isn't preserved,
    // only its resolved colours) — just enough breathing room to read as a
    // pill rather than text butting flush against its own background.
    const styleParts = [];
    if (run.color) styleParts.push(`color:${run.color}`);
    if (run.background) styleParts.push(`background-color:${run.background}`, 'padding:0 .2em', 'border-radius:.2em');
    const styleAttr = styleParts.length ? ` style="${esc(styleParts.join(';'))}"` : '';

    pieces.push(`<span${classAttr}${styleAttr}>${text}</span>`);
    previousText = rawText;
    previousClasses = sourceClasses;
  }

  return pieces
    .join('')
    .replace(/\s+([,.;:])/g, '$1');
}

// Real per-instance size/spacing for icon/badge widgets, captured live by
// STAMP_JS (see waha_scraper_common.py) rather than guessed by hand — an
// isolated later CSS sample can't see inline-style overrides or
// flex-derived sizing that only exist on the real page in context.
const MEASURED_CSS_PROPS = {
  width: 'width', height: 'height',
  marginTop: 'margin-top', marginRight: 'margin-right',
  marginBottom: 'margin-bottom', marginLeft: 'margin-left',
  borderRadius: 'border-radius', display: 'display',
};

function measuredCss(measured) {
  if (!measured || typeof measured !== 'object') return '';
  const pairs = [];
  for (const [key, cssName] of Object.entries(MEASURED_CSS_PROPS)) {
    const value = measured[key];
    if (value) pairs.push(`${cssName}:${value}`);
  }
  return pairs.join(';');
}

// Per-widget-family "how big should this look" anchor width, keyed by the
// widget's own outermost class name — the one remaining hand-chosen design
// constant per family. Every nested piece's size, margin, and position is
// then derived from real measured proportions relative to that anchor
// (see scaledMeasuredCss), so scaling a family up or down can never throw
// its internals out of alignment with each other — unlike hand-typing a
// separate cqw guess for every nested piece, which is what produced the
// oval/chip bugs this whole mechanism replaces. A class not listed here
// renders at its real, unscaled native size (e.g. the dsChar family, which
// Wahapedia itself never scales up).
const WIDGET_ANCHOR_WIDTHS = {
  // In em, not cqw: this page (army_rules.html) auto-shrinks a shared
  // --rule-fs variable to fit content in the card (see fitArmyRulesCard),
  // and .rule-body's font-size drives that variable — so a cqw-anchored
  // icon and its paired text fight over the same vertical budget: making
  // the icon bigger shrinks --rule-fs (and thus ALL card text) further,
  // which was silently moving the very thing being measured against.
  // Anchoring in em ties the icon to that same font-size directly, so its
  // size relative to its paired text stays fixed by construction no matter
  // what --rule-fs settles on. 2em/1.8 aspect => height 3.6em, i.e. 3 lines
  // of body text (line-height is 1.2em) tall — most of Aeldari's Agile
  // Manoeuvres pair the icon with 3 lines of TRIGGER:/EFFECT: text.
  redDiamondLeft: '2em',
};

function widgetAnchorWidth(classes) {
  for (const cls of classes || []) {
    if (WIDGET_ANCHOR_WIDTHS[cls]) return WIDGET_ANCHOR_WIDTHS[cls];
  }
  return null;
}

// Scales every measured field by the same ratio (derived once, from the
// widget root's own real width vs. its chosen anchor width) so proportions
// AND relative positions (margins) survive the scale-up/down together —
// the same reason CSS transform:scale() preserves a subtree's internal
// layout, but expressed as plain lengths instead, so each element keeps
// real per-instance layout (flex/grid participation) rather than being
// pulled out of flow into a paint-only transform.
function scaledMeasuredCss(measured, ratioExpr) {
  if (!measured || typeof measured !== 'object') return '';
  const scale = (raw) => (raw ? `calc(${ratioExpr} * ${raw})` : null);
  const pairs = [];

  const width = scale(measured.width);
  if (width) pairs.push(`width:${width}`);
  const height = scale(measured.height);
  if (height) pairs.push(`height:${height}`);
  const mt = scale(measured.marginTop);
  if (mt) pairs.push(`margin-top:${mt}`);
  const mr = scale(measured.marginRight);
  if (mr) pairs.push(`margin-right:${mr}`);
  const mb = scale(measured.marginBottom);
  if (mb) pairs.push(`margin-bottom:${mb}`);
  const ml = scale(measured.marginLeft);
  if (ml) pairs.push(`margin-left:${ml}`);

  if (measured.borderRadius) {
    // A percentage radius is already scale-invariant; only a pixel radius
    // needs the same ratio treatment as width/height.
    const isPercent = measured.borderRadius.trim().endsWith('%');
    pairs.push(`border-radius:${isPercent ? measured.borderRadius : scale(measured.borderRadius)}`);
  }

  // display is a keyword, not a length — never run it through scale().
  if (measured.display) pairs.push(`display:${measured.display}`);

  return pairs.join(';');
}

function blockAttrs(block, ratioExpr) {
  const classes = Array.isArray(block?.classes) ? block.classes.filter(Boolean) : [];
  const classAttr = classes.length ? ` class="${esc(classes.join(' '))}"` : '';

  const styleParts = [];
  if (block?.style) styleParts.push(String(block.style).replace(/;\s*$/, '') + ';');
  if (block?.color) styleParts.push(`color:${block.color}`);
  if (block?.background) styleParts.push(`background-color:${block.background}`);
  const mCss = ratioExpr ? scaledMeasuredCss(block?.measured, ratioExpr) : measuredCss(block?.measured);
  if (mCss) styleParts.push(mCss);
  const styleAttr = styleParts.length ? ` style="${esc(styleParts.join(';'))}"` : '';

  return `${classAttr}${styleAttr}`;
}


function parseConcreteCssColor(value) {
  value = String(value || '').trim().toLowerCase();

  // CSS variables and keywords require browser cascade/context. Do not guess.
  if (!value || value.includes('var(') || value === 'transparent' || value === 'inherit' || value === 'initial' || value === 'unset' || value === 'currentcolor') {
    return null;
  }

  const namedColors = {
    black: '#000000',
    white: '#ffffff',
    red: '#ff0000',
    green: '#008000',
    blue: '#0000ff',
    yellow: '#ffff00',
    cyan: '#00ffff',
    aqua: '#00ffff',
    magenta: '#ff00ff',
    fuchsia: '#ff00ff',
    gray: '#808080',
    grey: '#808080',
    silver: '#c0c0c0',
    maroon: '#800000',
    olive: '#808000',
    purple: '#800080',
    teal: '#008080',
    navy: '#000080',
    orange: '#ffa500',
  };

  if (namedColors[value]) value = namedColors[value];

  let match = value.match(/^#([0-9a-f]{3}|[0-9a-f]{4}|[0-9a-f]{6}|[0-9a-f]{8})$/i);
  if (match) {
    let hex = match[1];
    if (hex.length === 3 || hex.length === 4) {
      hex = hex.split('').map(ch => ch + ch).join('');
    }

    const r = parseInt(hex.slice(0, 2), 16);
    const g = parseInt(hex.slice(2, 4), 16);
    const b = parseInt(hex.slice(4, 6), 16);
    const a = hex.length >= 8 ? parseInt(hex.slice(6, 8), 16) / 255 : 1;
    if (a <= 0.05) return null;
    return { r, g, b, a };
  }

  match = value.match(/^rgba?\(([^)]+)\)$/i);
  if (match) {
    const parts = match[1]
      .split(',')
      .map(part => part.trim())
      .filter(Boolean);

    if (parts.length >= 3) {
      const toChannel = part => {
        if (part.endsWith('%')) return Math.round(Math.max(0, Math.min(100, parseFloat(part))) * 2.55);
        return Math.max(0, Math.min(255, parseFloat(part)));
      };

      const r = toChannel(parts[0]);
      const g = toChannel(parts[1]);
      const b = toChannel(parts[2]);
      const a = parts.length >= 4 ? Math.max(0, Math.min(1, parseFloat(parts[3]))) : 1;

      if ([r, g, b, a].some(n => Number.isNaN(n)) || a <= 0.05) return null;
      return { r, g, b, a };
    }
  }

  return null;
}

function relativeLuminance({ r, g, b }) {
  const channel = value => {
    value = value / 255;
    return value <= 0.03928
      ? value / 12.92
      : Math.pow((value + 0.055) / 1.055, 2.4);
  };

  return 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b);
}

function readableTextColorForBackground(backgroundColor) {
  const color = parseConcreteCssColor(backgroundColor);
  if (!color) return '';
  return relativeLuminance(color) <= 0.42 ? '#fff' : '#111';
}

function isWhitespaceOnlyRuns(runs) {
  return (Array.isArray(runs) ? runs : []).every(run => !String(run?.text ?? '').trim());
}

// True when a cell's content is nothing but image icons (optionally with
// incidental whitespace between them, e.g. a D6-result column's dice). Used
// to keep that content from wrapping onto multiple lines — see tableCellAttrs.
function isImageOnlyCellContent(content) {
  if (!Array.isArray(content) || !content.length) return false;
  if (!content.some(block => block?.displayItem === 'img')) return false;

  return content.every(block => {
    if (!block) return true;
    if (block.displayItem === 'img') return true;
    if (block.displayItem === 'p') return isWhitespaceOnlyRuns(block.runs);
    return false;
  });
}

function tableCellAttrs(cell, extraClasses = []) {
  if (!cell || typeof cell !== 'object' || Array.isArray(cell)) {
    return extraClasses.length ? ` class="${esc(extraClasses.join(' '))}"` : '';
  }

  const attrs = [];
  const classes = [...extraClasses];

  const backgroundColor = cell.background || '';
  const hasConcreteBackground = !!parseConcreteCssColor(backgroundColor);
  const textColor = hasConcreteBackground ? readableTextColorForBackground(backgroundColor) : '';

  if (hasConcreteBackground && !classes.includes('readable-bg-cell')) {
    classes.push('readable-bg-cell');
  }

  const styleParts = [];
  if (backgroundColor) styleParts.push(`background-color:${backgroundColor}`);
  if (textColor) styleParts.push(`color:${textColor}`);
  if (cell.align) styleParts.push(`text-align:${cell.align}`);
  if (cell.valign) styleParts.push(`vertical-align:${cell.valign}`);
  if (cell.width) styleParts.push(`width:${cell.width}`);

  if (classes.length) attrs.push(`class="${esc(classes.join(' '))}"`);
  if (styleParts.length) attrs.push(`style="${esc(styleParts.join(';'))}"`);
  if (cell.colspan) attrs.push(`colspan="${esc(cell.colspan)}"`);
  if (cell.rowspan) attrs.push(`rowspan="${esc(cell.rowspan)}"`);

  return attrs.length ? ' ' + attrs.join(' ') : '';
}

// Set once per page load, right after the detachment CSS manifest is
// fetched (see setActiveManifest calls in each page's own script). Kept as
// module state rather than threaded through every render function's
// parameters, since blockHtml/richBlockSequenceHtml/contentItemHtml are
// invoked from many places across every page and only image rendering
// actually needs this.
let ACTIVE_MANIFEST = null;

function setActiveManifest(manifest) {
  ACTIVE_MANIFEST = manifest || null;
}

const WAHAPEDIA_ORIGIN = 'https://wahapedia.ru';

function resolveImageSrc(src) {
  if (!src) return '';

  // Prefer the manifest's localized copy (downloaded by
  // waha_css_builder.py) so the page doesn't depend on
  // wahapedia.ru being reachable at render time.
  const localAsset = ACTIVE_MANIFEST?.direct_image_assets?.[src]?.asset;
  if (localAsset) return localAsset;

  // Wahapedia emits image paths relative to its own domain (e.g.
  // "/wh40k10ed/img/d1.png"). Without a manifest entry, resolving that
  // against whatever origin serves this page (not wahapedia.ru) 404s
  // silently, so fall back to loading it directly from the source site.
  if (src.startsWith('/')) return `${WAHAPEDIA_ORIGIN}${src}`;

  return src;
}

function renderImageBlock(block) {
  const src = resolveImageSrc(block?.src || '');
  if (!src) return '';

  const attrs = [
    `src="${esc(src)}"`,
    `alt="${esc(block.alt || '')}"`,
  ];

  const classes = Array.isArray(block.classes) ? block.classes.filter(Boolean) : [];
  if (classes.length) attrs.push(`class="${esc(classes.join(' '))}"`);
  if (block.style) attrs.push(`style="${esc(block.style)}"`);

  return `<img ${attrs.join(' ')}>`;
}

function renderContentImageBlock(block) {
  const image = renderImageBlock(block);
  if (!image) return '';

  // block.style can carry a source wrapper's float (e.g. Drukhari's
  // <div class="img-inv" style="float:left;"><img></div>, which is how
  // Wahapedia makes the following text wrap beside an image instead of
  // stacking under it — see extract_content_blocks in
  // waha_scraper_common.py). A float only affects surrounding layout when
  // it's on the box that sits among those siblings, i.e. this wrapper div,
  // not (only) the <img> inside it — applying it here as well as on the
  // image itself is redundant but harmless.
  const wrapperStyle = block.style ? ` style="${esc(block.style)}"` : '';
  return `<div class="content-image"${wrapperStyle}>${image}</div>`;
}

function inlineBlockHtml(block) {
  if (!block) return '';
  if (block.displayItem === 'img') return renderImageBlock(block);
  if (block.displayItem === 'element') return renderElementBlock(block);

  const classes = Array.isArray(block.classes) ? block.classes.filter(Boolean) : [];
  const classAttr = classes.length ? ` class="${esc(classes.join(' '))}"` : '';
  return `<span${classAttr}>${textRuns(block)}</span>`;
}

function safeElementTag(tag) {
  const allowed = new Set(['div','span','i','b','em','strong','small','a']);
  tag = String(tag || 'div').toLowerCase();
  return allowed.has(tag) ? tag : 'div';
}

function attrHtml(attrs) {
  if (!attrs || typeof attrs !== 'object') return '';
  const allowed = new Set(['id','name','title','aria-label','role']);
  return Object.entries(attrs)
    .filter(([key, value]) => allowed.has(String(key).toLowerCase()) && value != null)
    .map(([key, value]) => ` ${esc(key)}="${esc(value)}"`)
    .join('');
}

function renderElementBlock(block, ratioExpr) {
  const tag = safeElementTag(block.tag || block.source_tag || 'div');

  // Establish this widget's scale ratio once, at whichever element first
  // has both real measured data and a chosen anchor — then thread that
  // same ratio down to every descendant so the whole subtree scales
  // together, preserving real proportions and relative positions.
  let ownRatioExpr = ratioExpr;
  if (!ownRatioExpr && block?.measured?.width) {
    const anchor = widgetAnchorWidth(block.classes);
    if (anchor) ownRatioExpr = `(${anchor} / ${block.measured.width})`;
  }

  const children = Array.isArray(block.children) ? block.children : [];
  const inner = children
    .map(child => (child?.displayItem === 'element' ? renderElementBlock(child, ownRatioExpr) : blockHtml(child)))
    .join('') || textRuns(block);

  return `<${tag}${blockAttrs(block, ownRatioExpr)}${attrHtml(block.attrs)}>${inner}</${tag}>`;
}

// A "BATTLE ROUND [N]" banner (see _parse_battle_round_banner in
// waha_scraper_common.py) is reproduced with hand-written CSS rather than
// the generic element/measured pipeline: the live page builds its diamond
// badge from a real CSS transform:rotate(45deg), which our style capture
// never records (it only ever resolves bold/italic/upper/colour/background/
// box-geometry), so preserving the raw nested-div tree would just render a
// stack of plain, unrotated, out-of-place squares. Rendering our own
// rotated-square badge from the extracted label/round/colours instead gives
// the same diamond-on-a-bar look without needing to capture transform or
// absolute-position offsets generically.
function renderBattleRoundBanner(block) {
  const bg = block.background || '#a31317';
  const color = block.color || '#fff';
  const badgeBg = block.badgeBackground || bg;
  const badgeFill = block.badgeFill || '#fff';
  const badgeColor = block.badgeColor || bg;

  // beforeLabel/afterLabel, not a single joined label: the badge sits
  // inline in the source, immediately after "BATTLE ROUND" — sometimes
  // with nothing following it, sometimes with a further word like
  // "ONWARDS" after the badge — not pinned to the bar's far edge.
  const after = block.afterLabel
    ? `<span class="battle-round-label">${esc(block.afterLabel)}</span>`
    : '';

  return `
    <div class="battle-round-banner" style="background:${esc(bg)};color:${esc(color)}">
      <span class="battle-round-label">${esc(block.beforeLabel || '')}</span>
      <span class="battle-round-badge" style="background:${esc(badgeBg)}">
        <span class="battle-round-badge-fill" style="background:${esc(badgeFill)}">
          <span class="battle-round-badge-text" style="color:${esc(badgeColor)}">${esc(block.round || '')}</span>
        </span>
      </span>
      ${after}
    </div>
  `;
}

function shouldKeepOwnBlock(block) {
  if (!block || Array.isArray(block)) return true;
  if (block.displayItem === 'br') return true;
  if (block.is_block) return true;

  // <img> is inline by nature — two icons that were adjacent siblings in
  // the source markup (e.g. a D6-result column's dice) should flow side by
  // side like the source does, not each get forced onto its own line.
  // block.is_block (checked above) still lets a genuinely standalone image
  // claim its own block when that's actually true of the source markup.
  if (block.displayItem === 'img') return false;

  if (block.displayItem && block.displayItem !== 'p' && block.displayItem !== 'span') return true;

  const classes = Array.isArray(block.classes) ? block.classes : [];
  const style = String(block.style || '').toLowerCase().replace(/\s+/g, '');

  if (classes.includes('impact18')) return true;
  if (block.source_tag === 'p' && style.includes('display:block')) return true;

  return false;
}


function isActionIconBlock(block) {
  if (!block || block.displayItem !== 'element') return false;
  const classes = Array.isArray(block.classes) ? block.classes : [];
  return classes.includes('redDiamondLeft');
}

function renderActionEntry(iconBlocks, textBlocks) {
  const iconsHtml = iconBlocks.map(blockHtml).join('');
  const textHtml = textBlocks.map(block => {
    if (!block) return '';
    if (block.displayItem === 'br') return '';
    if (block.displayItem === 'p') return blockHtml(block);
    if (block.displayItem === 'span') return `<p>${inlineBlockHtml(block)}</p>`;
    return blockHtml(block);
  }).join('');

  if (!textHtml) {
    return `<div class="action-icons action-icons-standalone">${iconsHtml}</div>`;
  }

  return `
    <div class="action-entry">
      <div class="action-icons">${iconsHtml}</div>
      <div class="action-text">${textHtml}</div>
    </div>
  `;
}

function richBlockSequenceHtml(blocks) {
  const out = [];
  let inline = '';

  const flushInline = () => {
    if (!inline) return;
    out.push(`<p>${inline}</p>`);
    inline = '';
  };

  const list = blocks || [];

  for (let i = 0; i < list.length; i++) {
    const block = list[i];
    if (!block) continue;

    if (block.displayItem === 'br') {
      flushInline();
      out.push('<br>');
      continue;
    }

    // Wahapedia action cards use one or more redDiamondLeft icon widgets as a
    // left rail for the following TRIGGER/EFFECT text. Render those together so
    // the icon rail reserves horizontal space instead of overlapping prose.
    if (isActionIconBlock(block)) {
      flushInline();

      const icons = [];
      while (i < list.length && isActionIconBlock(list[i])) {
        icons.push(list[i]);
        i++;
      }

      const textBlocks = [];
      while (i < list.length) {
        const next = list[i];
        if (!next) {
          i++;
          continue;
        }

        if (next.displayItem === 'br') {
          i++;
          continue;
        }

        if (isActionIconBlock(next)) break;

        const isInlineParagraph = next.displayItem === 'p' && !shouldKeepOwnBlock(next);
        const isInlineSpan = next.displayItem === 'span' && !shouldKeepOwnBlock(next);

        if (!isInlineParagraph && !isInlineSpan) break;

        textBlocks.push(next);
        i++;
      }

      i--;
      out.push(renderActionEntry(icons, textBlocks));
      continue;
    }

    // A 'p' block reaching here (not consumed by the action-icon-rail
    // lookahead above, which deliberately wants the opposite) is always a
    // complete, already-decided paragraph from the parser — unlike the old
    // schema's small inline fragments, there's no more ambiguity left for
    // the renderer to resolve by merging it with whatever follows.
    if (block.displayItem === 'p' || shouldKeepOwnBlock(block)) {
      flushInline();
      out.push(blockHtml(block));
      continue;
    }

    inline += inlineBlockHtml(block);
  }

  flushInline();
  return out.join('');
}

function contentItemHtml(item) {
  const content = Array.isArray(item?.content) ? item.content : [];
  return `<li>${content.map(blockHtml).join('')}</li>`;
}

function blockHtml(block) {
  if (!block) return '';

  if (Array.isArray(block)) {
    return block.map(blockHtml).join('');
  }

  if (block.displayItem === 'img') {
    return renderContentImageBlock(block);
  }

  if (block.displayItem === 'element') {
    return renderElementBlock(block);
  }

  if (block.displayItem === 'battleRoundBanner') {
    return renderBattleRoundBanner(block);
  }

  if (block.displayItem === 'subrule') {
    return `
      <div class="subrule-title">${esc(block.title)}</div>
      ${richBlockSequenceHtml(block.content || [])}
    `;
  }

  if (block.displayItem === 'cs_rule') {
    // requirement is usually plain text (e.g. "N/A") but can be a D6-pip
    // icon widget instead (an element block with no text at all).
    const isWidgetRequirement = block.requirement && typeof block.requirement === 'object';
    const requirement = isWidgetRequirement
      ? `<span class="cs-rule-requirement">${blockHtml(block.requirement)}</span>`
      : (block.requirement ? `<span class="cs-rule-requirement">${esc(block.requirement)}</span>` : '');

    return `
      <div class="subrule-title">${esc(block.title)} ${requirement}</div>
      ${richBlockSequenceHtml(block.content || [])}
    `;
  }

  if (block.displayItem === 'ul' || block.displayItem === 'ol') {
    const tag = block.displayItem;

    return `
      <${tag} class="content-list">
        ${(block.items || []).map(contentItemHtml).join('')}
      </${tag}>
    `;
  }

  if (block.displayItem === 'table') {
    const cellHtml = cell => {
      if (!cell) return '';

      if (Array.isArray(cell.content)) {
        return richBlockSequenceHtml(cell.content);
      }

      if (Array.isArray(cell)) {
        return cell.map(blockHtml).join('');
      }

      return textRuns(cell);
    };

    const tdAttrs = cell => tableCellAttrs(
      cell,
      Array.isArray(cell?.content) && isImageOnlyCellContent(cell.content) ? ['icon-cell'] : []
    );

    return `
      <table class="content-table">
        <tbody>
          ${(block.rows || []).map(row => `
            <tr>
              ${row.map(cell => `<td${tdAttrs(cell)}>${cellHtml(cell)}</td>`).join('')}
            </tr>
          `).join('')}
        </tbody>
      </table>
    `;
  }

  if (block.displayItem === 'br') {
    return '<br>';
  }

  const allowed = new Set([
    'p',
    'span',
    'strong',
    'b',
    'em',
    'i',
    'u',
    'small',
  ]);

  const tag = String(block.displayItem || 'span').toLowerCase();
  const safeTag = allowed.has(tag) ? tag : 'span';

  return `<${safeTag}${blockAttrs(block)}>${textRuns(block)}</${safeTag}>`;
}

/* ============================================================
   Card image export — "Download PNG" buttons
   Lets a user download the currently rendered card(s) as PNG
   images straight from the page, without an external screenshot
   script. Uses html2canvas, loaded lazily from a CDN, to walk the
   DOM and paint it onto a canvas.

   html2canvas doesn't implement CSS `filter` at all (that's how
   this app tints its phase-icon diamonds blue/red/green), so
   filtered elements would render in their untinted original
   color. We fix that ourselves: before capture, every element
   with both a computed `filter` and a `background-image` gets its
   background pre-baked into a plain, filter-free PNG data URL
   using a real <canvas> 2D context (which DOES implement `filter`
   natively, with the same syntax as CSS) via bakeFilteredIconDataUrl,
   then the original inline styles are restored afterward regardless
   of success or failure.

   (An earlier version of this tried rendering through an SVG
   <foreignObject> + canvas, which uses the browser's actual paint
   engine and does support `filter` — but browsers unconditionally
   taint any canvas drawn from an SVG image containing a
   foreignObject, regardless of whether embedded resources are
   inlined. That's a deliberate, unfixable-from-JS security
   restriction, not a bug, so that approach was removed rather than
   left in as a nonfunctional dead end.)

   No fallback: if capture fails, the download fails loudly (alert +
   console.error with the real cause) instead of silently producing
   a wrong-looking image.
   ============================================================ */

const CARD_EXPORT_SCALE = 2; // 2x resolution for crisper output
const HTML2CANVAS_SRC = 'https://cdnjs.cloudflare.com/ajax/libs/html2canvas/1.4.1/html2canvas.min.js';

let html2canvasLoadPromise = null;

function ensureHtml2Canvas() {
  if (window.html2canvas) return Promise.resolve(window.html2canvas);
  if (html2canvasLoadPromise) return html2canvasLoadPromise;

  html2canvasLoadPromise = new Promise((resolve, reject) => {
    const script = document.createElement('script');
    script.src = HTML2CANVAS_SRC;
    script.onload = () => resolve(window.html2canvas);
    script.onerror = () => reject(new Error('Could not load html2canvas from CDN'));
    document.head.appendChild(script);
  });

  return html2canvasLoadPromise;
}

function slugifyForFilename(text) {
  const slug = String(text ?? '')
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/^-+|-+$/g, '');
  return slug || 'card';
}

function injectCardExportStyles() {
  if (document.getElementById('card-export-ui-css')) return;

  const style = document.createElement('style');
  style.id = 'card-export-ui-css';
  style.textContent = `
    .card-download-all-btn.card-download-all-btn{
      padding:8px 14px; cursor:pointer;
      border:1px solid #6fae9e; border-radius:5px;
      background:#1f6f5c; color:#fff;
      font:600 14px Arial,sans-serif;
    }
    .card-download-all-btn.card-download-all-btn:hover{ background:#278470; }
  `;
  document.head.appendChild(style);
}

function loadImageElement(src) {
  return new Promise((resolve, reject) => {
    const img = new Image();
    img.onload = () => resolve(img);
    img.onerror = () => reject(new Error(`Failed to load image: ${src}`));
    img.src = src;
  });
}

const bakedFilterIconCache = new Map();

/* Draws `iconUrl` onto an offscreen canvas with `filterValue` applied
   (native Canvas2D `ctx.filter`, same syntax as CSS filter), returning a
   plain unfiltered-looking PNG data URL that already has the tint baked
   into its pixels. Cached per (url, filter) pair since a card typically
   reuses a handful of icons across multiple stratagems. */
function bakeFilteredIconDataUrl(iconUrl, filterValue) {
  const key = `${iconUrl}\u0000${filterValue}`;
  if (!bakedFilterIconCache.has(key)) {
    const promise = loadImageElement(iconUrl).then(img => {
      const canvas = document.createElement('canvas');
      canvas.width = img.naturalWidth || img.width || 1;
      canvas.height = img.naturalHeight || img.height || 1;
      const ctx = canvas.getContext('2d');
      ctx.filter = filterValue;
      ctx.drawImage(img, 0, 0);
      return canvas.toDataURL('image/png');
    }).catch(err => {
      bakedFilterIconCache.delete(key); // allow a retry on the next export
      throw err;
    });
    bakedFilterIconCache.set(key, promise);
  }
  return bakedFilterIconCache.get(key);
}

/* Temporarily replaces every filtered background-image under `cardEl`
   with its pre-baked, filter-free equivalent, runs `fn`, then restores
   the original inline styles afterward — whether `fn` succeeds or throws. */
async function withBakedFilterIcons(cardEl, fn) {
  const restore = [];
  const candidates = [cardEl, ...cardEl.querySelectorAll('*')];

  for (const el of candidates) {
    const computed = getComputedStyle(el);
    const filterValue = computed.filter;
    const bgImage = computed.backgroundImage;
    if (!filterValue || filterValue === 'none') continue;
    if (!bgImage || bgImage === 'none') continue;

    const match = bgImage.match(/url\((['"]?)([^'")]+)\1\)/);
    if (!match) continue;

    try {
      const dataUrl = await bakeFilteredIconDataUrl(match[2], filterValue);
      restore.push({
        el,
        prevBackgroundImage: el.style.backgroundImage,
        prevFilter: el.style.filter,
      });
      el.style.backgroundImage = `url("${dataUrl}")`;
      el.style.filter = 'none';
    } catch (err) {
      console.warn(`Card export: could not pre-bake filtered icon "${match[2]}" — it may render untinted in the download.`, err);
    }
  }

  try {
    return await fn();
  } finally {
    for (const { el, prevBackgroundImage, prevFilter } of restore) {
      el.style.backgroundImage = prevBackgroundImage;
      el.style.filter = prevFilter;
    }
  }
}

async function captureCardViaHtml2Canvas(cardEl) {
  const html2canvas = await ensureHtml2Canvas();
  return html2canvas(cardEl, {
    backgroundColor: null,
    scale: CARD_EXPORT_SCALE,
    useCORS: true,
  });
}

/* Renders a single card element to a PNG and triggers a browser download.
   `filename` is slugified automatically, so raw display names are fine.
   Throws if capture fails — callers should let that surface, not swallow
   it, since a silently-substituted lower-fidelity render is worse than a
   visibly failed download. */
async function downloadCardAsPng(cardEl, filename) {
  if (!cardEl) return;

  let canvas;
  try {
    canvas = await withBakedFilterIcons(cardEl, () => captureCardViaHtml2Canvas(cardEl));
  } catch (err) {
    console.error('Card export failed:', err);
    alert(`Card download failed: ${err.message || err}\n\nSee the browser console for details.`);
    throw err;
  }

  const link = document.createElement('a');
  link.download = `${slugifyForFilename(filename)}.png`;
  link.href = canvas.toDataURL('image/png');
  document.body.appendChild(link);
  link.click();
  link.remove();
}

/* Downloads several cards in sequence, one PNG per card. filenameFn(el, i)
   should return the (un-slugified) name for card i. */
async function downloadCardsAsPng(cardEls, filenameFn) {
  const cards = Array.from(cardEls || []);
  for (let i = 0; i < cards.length; i++) {
    const name = filenameFn ? filenameFn(cards[i], i) : `card-${i + 1}`;
    await downloadCardAsPng(cards[i], name);
    // Stagger downloads: browsers can silently drop several downloads
    // triggered in the same tick.
    await new Promise(resolve => setTimeout(resolve, 200));
  }
}

/* Adds (or rewires) a "Download all as PNG" button inside `toolbarEl`.
   getCardsFn() is called at click time so it can reflect whichever cards
   are currently visible/rendered. */
function addDownloadAllButton(toolbarEl, getCardsFn, filenameFn, label = 'Download all as PNG') {
  if (!toolbarEl) return;
  injectCardExportStyles();

  let btn = toolbarEl.querySelector('.card-download-all-btn');
  if (!btn) {
    btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'card-download-all-btn';
    toolbarEl.appendChild(btn);
  }

  btn.textContent = label;
  btn.onclick = () => downloadCardsAsPng(getCardsFn(), filenameFn);
}

/* ============================================================
   STRATAGEM CARDS (shared by core_stratagems.html and
   detachments.html — the per-stratagem entry component's own render
   logic is identical between the two pages; only the surrounding page/card
   container differs, so that stays local to each HTML file. See
   waha_common.css's matching "STRATAGEM CARDS" section for the CSS.)
   ============================================================ */
function colourClass(cls){ return cls || 'str10ColorEither'; }

// Every icon (str10 or str11) resolves the same way: through the manifest,
// which waha_css_builder.py populates by downloading the real asset to
// assets/ locally — never a live wahapedia.ru URL baked into the page.
// That's what keeps the rendered output usable with zero dependency on
// wahapedia.ru being reachable, and it holds for str11 icons too: they're
// plain, unfiltered PNGs (no per-instance recolouring needed the way
// str10's shared-glyph-plus-CSS-filter trick required), so the existing
// generic icon-class collection in waha_css_builder.py already picks them
// up — the manifest just needs to be rebuilt against a wh40k11ed page for
// this manifest key to exist.
function iconStyle(icon){
  if (!icon) return '';
  const item = MANIFEST.icons?.[icon]?.element;
  const path = item?.backgroundImageAsset || '';
  return path ? `style="background-image:url('${esc(path)}')"` : '';
}

// A field (when/target/effect/restrictions) is always a LIST of content
// blocks (see waha_scraper_common.extract_stratagem_field_block), not a
// single flat paragraph — some fields hold more than flowing text (e.g.
// Heroic Intervention's EFFECT lists two alternate modes as their own
// <ul>, one carrying its own extra CP cost). extraCost is a stratagem-only
// concept blockHtml itself has no idea about, so it's handled here rather
// than in the general-purpose renderer.
function fieldBlockHtml(block){
  if (!block) return '';
  if (Array.isArray(block)) return block.map(fieldBlockHtml).join('');
  if (block.extraCost) {
    return `<div class="extra-cost-wrap">${blockHtml(block)}<span class="extra-cost-badge">${esc(block.extraCost)}</span></div>`;
  }
  return blockHtml(block);
}
function fieldHtml(val){ return fieldBlockHtml(val); }

// Height only, deliberately — both stratagem cards are fixed-width poster
// designs (text already wraps within its own fixed column), so vertical
// fit is the only thing their shrink-to-fit loops need to solve. A width
// check here caused a real bug: the extra-cost badge intentionally bleeds
// out past its own column into the icon bar (see .extra-cost-badge in
// waha_common.css), which inflates scrollWidth at every font size —
// shrinking text never shrinks that fixed cqw offset — so checking it
// would report "still overflowing" forever and shrink a page all the way
// to its floor for no real reason.
function overflowing(el){ return el && el.scrollHeight > el.clientHeight + 1; }

// name-fs and type-fs track body-fs at a fixed ratio (matching their
// original starting proportions: name 6.0/4.0=1.5x, type 4.0/4.0=1x)
// instead of each having their own independent shrink loop. Body text,
// the header, and the type bar all need to shrink (or stay large) as ONE
// unit — --strat-name-fs also drives .strat-head's own height, so if it
// were sized independently (e.g. by its own "does the name text overflow
// its box" check) an oversized header would eat into the vertical budget
// the body text has to work with, forcing it to shrink far more than the
// content actually needs. A single fixed ratio sidesteps that entirely.
const STRAT_NAME_TO_TEXT_RATIO = 6.0 / 4.0;
const STRAT_TYPE_TO_TEXT_RATIO = 1.0;
const STRAT_FS_FLOOR = 0.86;
// Since the binary search below finds the largest size that still fits
// (not "whatever the starting/ceiling value happens to be"), the ceiling
// only actually matters for a page with room to spare — one with little
// enough content that it never needs to shrink at all, so it just stays
// at the ceiling. 4.0 was chosen generously for the densest realistic
// core-stratagems page; a detachment with very few stratagems (e.g.
// Adeptus Astartes' Fulguris Task Force, only 3) never has to shrink from
// that at all and rendered visibly oversized (~3.3cqw) as a result.
// Reasonable-looking detachments with normal stratagem counts (Aeldari's
// Aspect Host, Chaos Knights' Helhunt Lance, 6 each) settle at ~2.15-2.2cqw
// on their own regardless of ceiling, since that's below either value —
// 2.4 leaves those unchanged while capping the sparse ones sensibly.
const STRAT_FS_CEILING = 2.4;

// Shared by core_stratagems.html (gridSelector='.core-stratagem-grid')
// and detachments.html (gridSelector='.strat-grid') — identical fit
// logic, only which page-level grid element bounds the available space
// differs.
function fitStratagemGrid(card, gridSelector){
  const grid = card.querySelector(gridSelector);
  const content = card.querySelector('.content');
  const strats = [...card.querySelectorAll('.strat')];

  const applyFs = (fs) => {
    strats.forEach(strat=>{
      strat.style.setProperty('--strat-fs', fs + 'cqw');
      strat.style.setProperty('--strat-name-fs', (fs * STRAT_NAME_TO_TEXT_RATIO) + 'cqw');
      strat.style.setProperty('--strat-type-fs', (fs * STRAT_TYPE_TO_TEXT_RATIO) + 'cqw');
    });
  };
  const fits = () => !overflowing(grid) && !overflowing(content);

  // Binary search instead of a linear decrement loop: checking every
  // 0.035cqw step from a starting size clearly larger than anything that
  // could plausibly fit took 100+ forced-layout reflows per fit — cheap
  // once, but core_stratagems.html's page-split search calls this
  // once per candidate split, multiplying that cost by however many
  // candidates there are. Halving the search range each step finds the
  // same answer (to within `precision`) in ~20 reflows regardless of the
  // starting range, which is what actually fixed the page becoming
  // noticeably laggy to load.
  applyFs(STRAT_FS_CEILING);
  if (!fits()) {
    let lo = STRAT_FS_FLOOR, hi = STRAT_FS_CEILING;
    applyFs(lo);
    if (fits()) {
      const precision = 0.02;
      for (let i = 0; i < 24 && (hi - lo) > precision; i++) {
        const mid = (lo + hi) / 2;
        applyFs(mid);
        if (fits()) lo = mid; else hi = mid;
      }
      applyFs(lo);
    }
    // else: doesn't fit even at the floor — leave it at the floor, the
    // best this content can do.
  }

  // Secondary, rarely-triggered safety net: an exceptionally long name can
  // still overflow its own header box horizontally even at the
  // proportional size above — shrink further ONLY in that case, never
  // growing it back past what was just set.
  strats.forEach(strat=>{
    const name = strat.querySelector('.strat-name');
    for(let i=0; i<40 && overflowing(name); i++){
      const ns = parseFloat(getComputedStyle(strat).getPropertyValue('--strat-name-fs')) || 6;
      if(ns <= 1.3) break;
      strat.style.setProperty('--strat-name-fs', (ns - .055) + 'cqw');
    }
  });
  positionExtraCostBadges(card);
}

// Positions each extra-cost badge (see .extra-cost-badge in
// waha_common.css) to match the live site: left-aligned with the icon
// bar, stretching down to the bottom of the stratagem, starting level with
// whichever mode it's attached to. That start point varies per instance
// and only exists once layout has settled (font size affects where each
// mode falls), so this can't be pure CSS — call it once text has finished
// shrinking to fit, not before. offsetTop is measured against .strat-body
// specifically because that's the badge's actual positioning context
// (position:relative in CSS) once reparented there; .extra-cost-wrap
// itself is left in place as a plain marker so this can find "which mode
// does this badge belong to", not moved along with the badge.
function positionExtraCostBadges(card){
  card.querySelectorAll('.strat').forEach(strat=>{
    const body = strat.querySelector('.strat-body');
    if (!body) return;
    strat.querySelectorAll('.extra-cost-wrap').forEach(wrap=>{
      const badge = wrap.querySelector(':scope > .extra-cost-badge');
      if (!badge) return;
      badge.style.top = `${wrap.offsetTop}px`;
      body.appendChild(badge);
    });
  });
}
