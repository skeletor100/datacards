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

    pieces.push(classes ? `<span class="${classes}">${text}</span>` : `<span>${text}</span>`);
    previousText = rawText;
    previousClasses = sourceClasses;
  }

  return pieces
    .join('')
    .replace(/\s+([,.;:])/g, '$1');
}

function blockAttrs(block) {
  const classes = Array.isArray(block?.classes) ? block.classes.filter(Boolean) : [];
  const classAttr = classes.length ? ` class="${esc(classes.join(' '))}"` : '';
  const styleAttr = block?.style ? ` style="${esc(block.style)}"` : '';
  return `${classAttr}${styleAttr}`;
}


function parseInlineStyleDeclarations(style) {
  const declarations = [];

  for (const raw of String(style || '').split(';')) {
    if (!raw || !raw.includes(':')) continue;
    const [propRaw, ...valueParts] = raw.split(':');
    const prop = propRaw.trim().toLowerCase();
    const value = valueParts.join(':').trim();
    if (!prop || !value) continue;
    declarations.push([prop, value]);
  }

  return declarations;
}

function styleHasProperty(style, propertyName) {
  const wanted = String(propertyName || '').trim().toLowerCase();
  return parseInlineStyleDeclarations(style).some(([prop]) => prop === wanted);
}

function inlineStylePropertyValue(style, propertyName) {
  const wanted = String(propertyName || '').trim().toLowerCase();
  const declarations = parseInlineStyleDeclarations(style);

  for (let i = declarations.length - 1; i >= 0; i--) {
    const [prop, value] = declarations[i];
    if (prop === wanted) return value;
  }

  return '';
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

function readableTableCellPresentation(style) {
  style = String(style || '').trim();

  const backgroundColor = inlineStylePropertyValue(style, 'background-color')
    || inlineStylePropertyValue(style, 'background');

  const hasConcreteBackground = !!parseConcreteCssColor(backgroundColor);
  const hasExplicitColor = styleHasProperty(style, 'color');
  const textColor = hasConcreteBackground && !hasExplicitColor
    ? readableTextColorForBackground(backgroundColor)
    : '';

  let resolvedStyle = style;
  if (textColor) {
    const separator = resolvedStyle && !resolvedStyle.trim().endsWith(';') ? ';' : '';
    resolvedStyle = `${resolvedStyle}${separator}color:${textColor}`;
  }

  return {
    style: resolvedStyle,
    readableBgCell: hasConcreteBackground && (hasExplicitColor || !!textColor),
  };
}

function tableCellAttrs(cell) {
  if (!cell || typeof cell !== 'object' || Array.isArray(cell)) return '';

  const attrs = [];
  const classes = Array.isArray(cell.classes) ? cell.classes.filter(Boolean) : [];
  const presentation = readableTableCellPresentation(cell.style || '');
  const style = presentation.style;

  if (presentation.readableBgCell && !classes.includes('readable-bg-cell')) {
    classes.push('readable-bg-cell');
  }

  if (classes.length) attrs.push(`class="${esc(classes.join(' '))}"`);
  if (style) attrs.push(`style="${esc(style)}"`);
  if (cell.colspan) attrs.push(`colspan="${esc(cell.colspan)}"`);
  if (cell.rowspan) attrs.push(`rowspan="${esc(cell.rowspan)}"`);

  return attrs.length ? ' ' + attrs.join(' ') : '';
}

function renderImageBlock(block) {
  const src = block?.src || '';
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
  return `<div class="content-image">${image}</div>`;
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

function renderElementBlock(block) {
  const tag = safeElementTag(block.tag || block.source_tag || 'div');
  const children = Array.isArray(block.children) ? block.children : [];
  const inner = children.map(blockHtml).join('') || textRuns(block);
  return `<${tag}${blockAttrs(block)}${attrHtml(block.attrs)}>${inner}</${tag}>`;
}

function shouldKeepOwnBlock(block) {
  if (!block || Array.isArray(block)) return true;
  if (block.displayItem === 'br') return true;
  if (block.is_block) return true;
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

    if (shouldKeepOwnBlock(block)) {
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
  const title = item.title
    ? `<span class="li-title">${esc(item.title)}</span> `
    : '';

  const nested = Array.isArray(item.content)
    ? item.content.map(blockHtml).join('')
    : '';

  return `<li>${title}${textRuns(item)}${nested}</li>`;
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

  if (block.displayItem === 'subrule') {
    return `
      <div class="subrule-title">${esc(block.title)}</div>
      ${(block.content || []).map(blockHtml).join('')}
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

    const tdAttrs = tableCellAttrs;

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

/* Emit only safe Wahapedia inline/text classes from detachment_css_manifest.json.
   This deliberately excludes layout/widget classes and computed geometry. */
const MANIFEST_INLINE_TEXT_CLASS_WHITELIST = new Set([
  // Order matters: generic tooltip-link classes must be emitted before
  // stronger semantic classes such as .kwb/.kwb2, and .bluefont must be
  // emitted after .kwb2 so explicit blue keyword spans keep their colour.
  'kwbu',
  'tt',
  'kwb',
  'kwb2',
  'bluefont',
  'aeText'
]);

function manifestInlineTextSnapshot(manifest, className) {
  const inline = manifest?.inline_classes || {};

  if (inline[className]) return inline[className];

  // Some manifest entries are recorded as full class sets rather than as
  // standalone classes, e.g. "bluefont kwb2" or "kwbu tooltip00001 tt".
  // Only use a combined snapshot when the requested class is the leading
  // class in that set. This prevents .kwbu/.tt from inheriting the blue,
  // bold, uppercase styling from entries such as
  // "bluefont kwb2 kwbu tooltip00002 tt".
  for (const [key, snapshot] of Object.entries(inline)) {
    const classes = String(key || '').trim().split(/\s+/).filter(Boolean);
    if (classes[0] === className) return snapshot;
  }

  // Fallback for manifests that only have richer asset_candidate_styles.
  const entry = manifest?.asset_candidate_styles?.[className];
  if (entry?.element) return entry.element;
  if (entry && typeof entry === 'object') return entry;

  return null;
}

const MANIFEST_INLINE_TEXT_COLOR_CLASSES = new Set([
  // These classes are explicitly colour-semantic. Other safe inline classes
  // such as .kwb/.kwb2/.kwbu/.tt should inherit the renderer context colour.
  'bluefont',
  'aeText'
]);

function manifestInlineTextCss(snapshot, className) {
  if (!snapshot || typeof snapshot !== 'object') return '';

  const pairs = [];
  const allowColor = MANIFEST_INLINE_TEXT_COLOR_CLASSES.has(className);

  const add = (prop, value) => {
    if (!value || value === 'normal' || value === 'auto' || value === '0px') return;
    if (prop === 'color' && !allowColor) return;
    if (prop === 'color' && value === 'rgba(0, 0, 0, 0)') return;
    if (prop === 'background-color' && value === 'rgba(0, 0, 0, 0)') return;
    pairs.push(`${prop}:${value}`);
  };

  // Text-only properties. Do not emit width/height/display/margins/flex/etc.
  // Colour is only emitted for explicit colour classes; normal keyword classes
  // inherit card/stratagem/table-cell colour from their renderer context.
  add('color', snapshot.color);
  add('font-weight', snapshot.fontWeight);
  add('font-style', snapshot.fontStyle);
  add('text-transform', snapshot.textTransform);
  add('text-decoration', snapshot.textDecoration);
  add('text-decoration-style', snapshot.textDecorationStyle);
  add('text-underline-offset', snapshot.textUnderlineOffset);

  return pairs.join(';');
}

function applyManifestInlineTextCss(manifest) {
  const old = document.getElementById('manifest-inline-text-css');
  if (old) old.remove();

  const rules = [];

  for (const className of MANIFEST_INLINE_TEXT_CLASS_WHITELIST) {
    const snapshot = manifestInlineTextSnapshot(manifest, className);
    const css = manifestInlineTextCss(snapshot, className);
    if (css) rules.push(`.${CSS.escape(className)}{${css}}`);
  }

  if (!rules.length) return;

  const style = document.createElement('style');
  style.id = 'manifest-inline-text-css';
  style.textContent = rules.join('\n');
  document.head.appendChild(style);
}
