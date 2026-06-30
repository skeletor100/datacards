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

  return runs
    .map(run => {
      const classes = esc((run.source_classes || []).join(' '));
      const text = esc(run.text);

      return `<span class="${classes}">${text}</span>`;
    })
    .join('')
    .replace(/\s+([,.;:])/g, '$1');
}

function blockAttrs(block) {
  const classes = Array.isArray(block?.classes) ? block.classes.filter(Boolean) : [];
  const classAttr = classes.length ? ` class="${esc(classes.join(' '))}"` : '';
  const styleAttr = block?.style ? ` style="${esc(block.style)}"` : '';
  return `${classAttr}${styleAttr}`;
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

    const tdAttrs = cell => {
      if (!cell || typeof cell !== 'object' || Array.isArray(cell)) return '';
      const attrs = [];
      const classes = Array.isArray(cell.classes) ? cell.classes.filter(Boolean) : [];
      if (classes.length) attrs.push(`class="${esc(classes.join(' '))}"`);
      if (cell.style) attrs.push(`style="${esc(cell.style)}"`);
      if (cell.colspan) attrs.push(`colspan="${esc(cell.colspan)}"`);
      if (cell.rowspan) attrs.push(`rowspan="${esc(cell.rowspan)}"`);
      return attrs.length ? ' ' + attrs.join(' ') : '';
    };

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

function manifestInlineTextCss(snapshot) {
  if (!snapshot || typeof snapshot !== 'object') return '';

  const pairs = [];

  const add = (prop, value) => {
    if (!value || value === 'normal' || value === 'auto' || value === '0px') return;
    if (prop === 'color' && value === 'rgba(0, 0, 0, 0)') return;
    if (prop === 'background-color' && value === 'rgba(0, 0, 0, 0)') return;
    pairs.push(`${prop}:${value}`);
  };

  // Text-only properties. Do not emit width/height/display/margins/flex/etc.
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
    const css = manifestInlineTextCss(snapshot);
    if (css) rules.push(`.${CSS.escape(className)}{${css}}`);
  }

  if (!rules.length) return;

  const style = document.createElement('style');
  style.id = 'manifest-inline-text-css';
  style.textContent = rules.join('\n');
  document.head.appendChild(style);
}
