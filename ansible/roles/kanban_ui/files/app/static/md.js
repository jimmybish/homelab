/* Kanban markdown renderer (extracted from app.js so it is unit-testable in
 * plain node). UMD-style: sets window.KanbanMd in the browser, module.exports
 * under node.
 *
 * XSS rules this implementation guarantees (see tests/test_md_renderer.js):
 * - Every payload that survives into the returned HTML was escaped exactly
 *   once. Nothing user-controlled (task body, comment, result, link label)
 *   is ever inserted into preserved HTML before escaping.
 * - Link labels are escaped like any other user text. The previous version
 *   preserved the raw label, which allowed stored XSS via
 *   [<img src=x onerror=...>](https://allowed.example) rendered with
 *   innerHTML.
 * - Only attribute-free whitelisted tags may appear in the output; hrefs are
 *   https/mailto only.
 */
'use strict';

(function (root, factory) {
  const api = factory();
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.KanbanMd = api;
})(typeof self !== "undefined" ? self : globalThis, function () {
  'use strict';

  function esc(s) { return String(s ?? "").replace(/[&<>'"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;" }[c])); }
  const NAMED_ENTITIES = { amp: "&", lt: "<", gt: ">", quot: '"', apos: "'", nbsp: " ", ndash: "\u2013", mdash: "\u2014", hellip: "\u2026", copy: "\u00a9", reg: "\u00ae", deg: "\u00b0", times: "\u00d7", divide: "\u00f7", middot: "\u00b7" };
  function unent(s) {
    // decode HTML entities ONCE (card bodies arrive pre-encoded: &#39; &quot; &lt;b&gt; ...)
    return String(s).replace(/&(#x?[0-9a-f]+|[a-zA-Z]+);/g, (m, e) => {
      try {
        if (/^#x[0-9a-f]+$/i.test(e)) return String.fromCodePoint(parseInt(e.slice(2), 16));
        if (/^#\d+$/.test(e)) return String.fromCodePoint(parseInt(e.slice(1), 10));
        const v = NAMED_ENTITIES[e.toLowerCase()];
        return v !== undefined ? v : m;
      } catch { return m; }
    });
  }
  const SAFE_TAGS = /^(?:br|b|strong|i|em|u|s|del|ins|code|kbd|samp|pre|blockquote|h[1-6]|hr|small|sub|sup|abbr)$/i;
  function safeUrl(u) { return /^(?:https?:|mailto:)/i.test(u) ? u.replace(/"/g, "%22") : null; }
  function textEsc(s) { return esc(unent(s)); } // plain-text fields: decode entities once, then escape

  function md(src) {
    // 1. decode entities once -> raw html/markdown text
    let s = unent(src);
    // 2. protect fenced/inline code (content escaped exactly once), links
    //    (href sanitised, LABEL ALSO ESCAPED -- it is user text)
    const kept = [];
    const keep = html => { kept.push(html); return "\u0000" + (kept.length - 1) + "\u0000"; };
    s = s.replace(/```[^\n]*\n?([\s\S]*?)```/g, (_, c) => keep("<pre>" + esc(c.replace(/^\n|\n$/g, "")) + "</pre>"));
    s = s.replace(/`([^`\n]+)`/g, (_, c) => keep("<code>" + esc(c) + "</code>"));
    s = s.replace(/\[([^\]]+)\]\((https?:[^\)\s]+)\)/g, (_, label, url) => keep('<a href="' + esc(safeUrl(url) || "#") + '" target="_blank" rel="noopener">' + esc(label) + "</a>"));
    // 3. escape everything remaining (kills script/img/attrs)
    s = esc(s);
    // 4. restore whitelisted inline tags (already escaped; no attrs survive)
    s = s.replace(/&lt;(\/?)([a-z][a-z0-9-]*)\s*&gt;/gi, (m, slash, tag) => SAFE_TAGS.test(tag) ? "<" + slash + tag.toLowerCase() + ">" : m);
    // 5. markdown inline: bold, headings, bare urls
    s = s.replace(/\*\*([^*\n]+)\*\*/g, "<b>$1</b>");
    s = s.replace(/^#{1,4} (.*)$/gm, "<b>$1</b>");
    s = s.replace(/(^|[\s(])(https?:\/\/[^\s<)]+)/g, (_, pre, url) => pre + '<a href="' + esc(safeUrl(url) || "#") + '" target="_blank" rel="noopener">' + esc(url) + "</a>");
    // 6. markdown lists: group consecutive bullet/numbered lines (incl. wrapped continuations)
    const lines = s.split("\n");
    const out = [];
    let list = null;
    const flush = () => {
      if (!list) return;
      const tag = list.ol ? "ol" : "ul";
      out.push("<" + tag + ">" + list.items.map(it => "<li>" + it + "</li>").join("") + "</" + tag + ">");
      list = null;
    };
    for (const line of lines) {
      const m = line.match(/^[ \t]*(?:([-*\u2022])|(\d+)[.)])[ \t]+(.*)$/);
      if (m) {
        const ol = !!m[2];
        if (list && list.ol === ol) list.items.push(m[3]);
        else { flush(); list = { ol, items: [m[3]] }; }
      } else if (list && /^[ \t]+\S/.test(line) && line.trim()) {
        list.items[list.items.length - 1] += " " + line.trim();
      } else { flush(); out.push(line); }
    }
    flush();
    s = out.join("\n");
    // 7. unprotect code/link payloads (already fully escaped/sanitised)
    s = s.replace(/\u0000(\d+)\u0000/g, (_, i) => kept[+i] !== undefined ? kept[+i] : "");
    return s;
  }

  return { esc, unent, textEsc, safeUrl, md, SAFE_TAGS, NAMED_ENTITIES };
});
