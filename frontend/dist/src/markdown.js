/** Small, escaped Markdown view. Raw HTML, images and executable URLs stay inert. */
export const escapeHtml = (value = '') => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));

function safeHref(escaped) {
  const value = escaped.replace(/&amp;/g,'&').replace(/&quot;/g,'"').replace(/&#39;/g,"'").replace(/&lt;/g,'<').replace(/&gt;/g,'>');
  if (/[\u0000-\u0020\u007f]/.test(value)) return null;
  try { const url = new URL(value); return ['https:', 'http:', 'mailto:'].includes(url.protocol) ? escapeHtml(url.href) : null; } catch { return null; }
}
function inline(raw) {
  let value = escapeHtml(String(raw).replace(/\u0000/g,''));
  const tokens = [];
  const keep = html => `\u0000MD${tokens.push(html)-1}\u0000`;
  value = value.replace(/`([^`\n]+)`/g, (_, code) => keep(`<code>${code}</code>`));
  value = value.replace(/(!?)\[([^\]\n]+)]\(([^()\s]+)\)/g, (_, image, label, target) => {
    if (image) return keep(`<span class="md-image-label">图片：${label}</span>`);
    const href = safeHref(target);
    return keep(href ? `<a href="${href}" target="_blank" rel="noopener noreferrer">${label}</a>` : label);
  });
  value = value.replace(/\*\*([^*\n]+)\*\*/g,'<strong>$1</strong>').replace(/__([^_\n]+)__/g,'<strong>$1</strong>')
    .replace(/(^|[^*])\*([^*\n]+)\*(?!\*)/g,'$1<em>$2</em>').replace(/~~([^~\n]+)~~/g,'<del>$1</del>');
  return value.replace(/\u0000MD(\d+)\u0000/g, (_, i) => tokens[Number(i)] || '');
}
function cells(line) {
  const parts = []; let current = '', code = false;
  line = line.trim().replace(/^\|/,'').replace(/(?<!\\)\|$/,'');
  for (let i = 0; i < line.length; i++) {
    const char = line[i];
    if (char === '\\' && line[i+1] === '|') { current += '|'; i++; }
    else if (char === '`') { code = !code; current += char; }
    else if (char === '|' && !code) { parts.push(current.trim()); current = ''; }
    else current += char;
  }
  parts.push(current.trim()); return parts;
}
const divider = line => line?.includes('|') && cells(line).every(cell => /^:?-{3,}:?$/.test(cell));
const listItem = line => /^\s*(?:([-+*])|\d+[.)])\s+(.+)$/.exec(line);
const isBlock = (lines, i) => /^(?:#{1,6}\s|\s*```|\s*~~~|\s*>|\s*(?:---+|___+|\*\*\*+)\s*$)/.test(lines[i]) || listItem(lines[i]) || divider(lines[i+1]);

export function renderMarkdown(source = '') {
  const lines = String(source ?? '').replace(/\r\n?/g,'\n').split('\n');
  const out = [];
  for (let i = 0; i < lines.length;) {
    const line = lines[i]; if (!line.trim()) { i++; continue; }
    const fence = /^\s*(`{3,}|~{3,})([^\s]*)/.exec(line);
    if (fence) {
      const content = [], mark = fence[1][0]; i++;
      while (i < lines.length && !new RegExp(`^\\s*${mark}{${fence[1].length},}\\s*$`).test(lines[i])) content.push(lines[i++]);
      if (i < lines.length) i++;
      out.push(`<pre><code>${escapeHtml(content.join('\n'))}</code></pre>`); continue;
    }
    const heading = /^(#{1,6})\s+(.+?)\s*#*\s*$/.exec(line);
    if (heading) { out.push(`<h${heading[1].length}>${inline(heading[2])}</h${heading[1].length}>`); i++; continue; }
    if (/^\s*(?:---+|___+|\*\*\*+)\s*$/.test(line)) { out.push('<hr>'); i++; continue; }
    if (line.includes('|') && divider(lines[i+1])) {
      const headers = cells(line), widths = cells(lines[i+1]); i += 2;
      const align = widths.map(value => value.startsWith(':') && value.endsWith(':') ? 'center' : value.endsWith(':') ? 'right' : 'left');
      const row = (values, tag) => `<tr>${headers.map((_, index) => `<${tag} class="md-align-${align[index]}">${inline(values[index] || '')}</${tag}>`).join('')}</tr>`;
      const rows = [];
      while (i < lines.length && lines[i].trim() && lines[i].includes('|')) rows.push(row(cells(lines[i++]), 'td'));
      out.push(`<div class="md-table-wrap" tabindex="0" role="region" aria-label="数据表格，可横向滚动"><table><thead>${row(headers,'th')}</thead><tbody>${rows.join('')}</tbody></table></div>`); continue;
    }
    if (/^\s*>/.test(line)) {
      const quotes = []; while (i < lines.length && /^\s*>/.test(lines[i])) quotes.push(lines[i++].replace(/^\s*>\s?/,''));
      out.push(`<blockquote>${quotes.map(inline).join('<br>')}</blockquote>`); continue;
    }
    const item = listItem(line);
    if (item) {
      const tag = item[1] ? 'ul' : 'ol', items = [];
      while (i < lines.length) {
        const next = listItem(lines[i]); if (!next || (next[1] ? 'ul' : 'ol') !== tag) break;
        items.push(`<li>${inline(next[2])}</li>`); i++;
      }
      out.push(`<${tag}>${items.join('')}</${tag}>`); continue;
    }
    const paragraph = [line]; i++;
    while (i < lines.length && lines[i].trim() && !isBlock(lines,i)) paragraph.push(lines[i++]);
    out.push(`<p>${paragraph.map(inline).join('<br>')}</p>`);
  }
  return out.join('\n');
}
