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

function richBlockSequenceHtml(blocks) {
  const out = [];
  let inline = '';

  const flushInline = () => {
    if (!inline) return;
    out.push(`<p>${inline}</p>`);
    inline = '';
  };

  for (const block of (blocks || [])) {
    if (!block) continue;

    if (block.displayItem === 'br') {
      flushInline();
      out.push('<br>');
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
    return renderImageBlock(block);
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
