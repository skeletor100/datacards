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
      const text = esc(run.text).trim();

      return `<span class="${classes}">${text}</span>`;
    })
    .join(' ')
    .replace(/\s+([,.;:])/g, '$1');
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
    return `
      <table class="content-table">
        <tbody>
          ${(block.rows || []).map(row => `
            <tr>
              ${row.map(cell => `<td>${textRuns(cell)}</td>`).join('')}
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

  return `<${safeTag}>${textRuns(block)}</${safeTag}>`;
}