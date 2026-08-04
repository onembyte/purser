"use strict";
/* A small, safe markdown renderer.
 *
 * Two constraints drive the design:
 *   1. The page runs under a strict CSP with no network — so no library.
 *   2. Every input is untrusted transcript text — so the output is built from DOM
 *      nodes and text nodes. There is no innerHTML anywhere in this file, which means
 *      markup in a transcript can never become markup on the page.
 *
 * Supported: fenced code, ATX headings, unordered/ordered lists, blockquotes, rules,
 * tables, and inline code/bold/italic/strike/links. Deliberately not a full CommonMark
 * implementation — it covers what Claude actually emits.
 */

const MD = (() => {
  // Inline: code first so its contents are never re-parsed as emphasis.
  //
  // Emphasis follows CommonMark's flanking rule: an opener may not be followed by
  // whitespace, a closer may not be preceded by one. Without it `2 * 3 = 6 and **x`
  // italicises " 3 = 6 and " — which would mangle `SELECT * FROM`, `a * b`, and every
  // glob in a transcript. The alnum lookarounds additionally keep snake_case and
  // a*b*c intact.
  const INLINE = new RegExp(
    [
      "(`+)([\\s\\S]*?)\\1",                    // 1,2  `code`
      "\\[([^\\]]*)\\]\\(([^)\\s]+)[^)]*\\)",   // 3,4  [text](url)
      "\\*\\*(?!\\s)([\\s\\S]+?)(?<!\\s)\\*\\*",   // 5   **bold**
      "~~(?!\\s)([\\s\\S]+?)(?<!\\s)~~",           // 6   ~~strike~~
      "(?<![A-Za-z0-9*])\\*(?!\\s)([^*\\n]+?)(?<!\\s)\\*(?![A-Za-z0-9*])",  // 7 *italic*
      "(?<![A-Za-z0-9_])_(?!\\s)([^_\\n]+?)(?<!\\s)_(?![A-Za-z0-9_])",      // 8 _italic_
      "(https?://[^\\s<>()]+)",                 // 9    bare url
    ].join("|"),
    "g"
  );

  function el(tag, cls, text) {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    return n;
  }

  function link(text, href) {
    // Only http(s) becomes clickable, so a transcript can never produce a
    // javascript:/data: target; the webview's navigation delegate then routes real
    // clicks to the default browser.
    if (/^https?:\/\//i.test(href)) {
      const a = el("a", "md-link", text || href);
      a.href = href;
      a.title = href;
      return a;
    }
    // Everything else (Claude writes file refs as `[db.py](src/db.py)` constantly) is
    // inert — but the target is kept as a tooltip rather than discarded. Dropping it
    // silently loses the only copy of the path.
    const s = el("span", "md-path", text || href);
    if (href && href !== text) s.title = href;
    return s;
  }

  /** Parse inline spans into `parent`. */
  function inline(text, parent) {
    let last = 0;
    text.replace(INLINE, (m, tick, code, ltext, lhref, bold, strike, i1, i2, url,
                          offset) => {
      if (offset > last) parent.append(document.createTextNode(text.slice(last, offset)));
      const wrap = (tag, body) => {
        const s = el(tag);
        inline(body, s);          // emphasis can nest
        parent.append(s);
      };
      if (code !== undefined) parent.append(el("code", "md-code", code.trim()));
      else if (lhref !== undefined) parent.append(link(ltext, lhref));
      else if (bold !== undefined) wrap("strong", bold);
      else if (strike !== undefined) wrap("s", strike);
      else if (i1 !== undefined || i2 !== undefined) wrap("em", i1 !== undefined ? i1 : i2);
      else if (url !== undefined) parent.append(link(url, url));
      last = offset + m.length;
      return m;
    });
    if (last < text.length) parent.append(document.createTextNode(text.slice(last)));
    return parent;
  }

  const RE = {
    fence: /^(```|~~~)\s*([\w+-]*)\s*$/,
    heading: /^(#{1,6})\s+(.*)$/,
    ul: /^[ \t]*[-*+]\s+(.*)$/,
    ol: /^[ \t]*(\d+)[.)]\s+(.*)$/,
    quote: /^>\s?(.*)$/,
    rule: /^ {0,3}([-*_])(?:\s*\1){2,}\s*$/,
    tableSep: /^\s*\|?[\s:|-]+\|[\s:|-]*$/,
  };

  const cells = (line) =>
    line.replace(/^\s*\|/, "").replace(/\|\s*$/, "").split("|").map((c) => c.trim());

  /** Render markdown `src` into a fragment. */
  function render(src) {
    const frag = document.createDocumentFragment();
    const lines = String(src || "").split("\n");
    let i = 0;

    const flushPara = (buf) => {
      if (!buf.length) return;
      frag.append(inline(buf.join("\n"), el("p", "md-p")));
      buf.length = 0;
    };
    const para = [];

    while (i < lines.length) {
      const line = lines[i];

      // ---- fenced code
      const fence = line.match(RE.fence);
      if (fence) {
        flushPara(para);
        const marker = fence[1];
        const body = [];
        i++;
        while (i < lines.length && !lines[i].startsWith(marker)) body.push(lines[i++]);
        i++;                                     // closing fence (or EOF)
        const pre = el("pre", "md-pre");
        const code = el("code", null, body.join("\n"));
        if (fence[2]) pre.dataset.lang = fence[2];
        pre.append(code);
        frag.append(pre);
        continue;
      }

      if (!line.trim()) { flushPara(para); i++; continue; }

      if (RE.rule.test(line)) { flushPara(para); frag.append(el("hr", "md-hr")); i++; continue; }

      const h = line.match(RE.heading);
      if (h) {
        flushPara(para);
        frag.append(inline(h[2], el("h" + Math.min(h[1].length + 2, 6), "md-h")));
        i++;
        continue;
      }

      // ---- table: header row followed by a separator row
      if (line.includes("|") && i + 1 < lines.length && RE.tableSep.test(lines[i + 1])) {
        flushPara(para);
        const table = el("table", "md-table");
        const thead = el("thead");
        const hr = el("tr");
        cells(line).forEach((c) => hr.append(inline(c, el("th"))));
        thead.append(hr);
        table.append(thead);
        i += 2;
        const tbody = el("tbody");
        while (i < lines.length && lines[i].includes("|") && lines[i].trim()) {
          const tr = el("tr");
          cells(lines[i]).forEach((c) => tr.append(inline(c, el("td"))));
          tbody.append(tr);
          i++;
        }
        table.append(tbody);
        frag.append(table);
        continue;
      }

      // ---- blockquote
      if (RE.quote.test(line)) {
        flushPara(para);
        const body = [];
        while (i < lines.length && RE.quote.test(lines[i])) {
          body.push(lines[i].match(RE.quote)[1]);
          i++;
        }
        const bq = el("blockquote", "md-quote");
        bq.append(render(body.join("\n")));
        frag.append(bq);
        continue;
      }

      // ---- lists (one level; nesting is rare in transcripts and folds into the item)
      if (RE.ul.test(line) || RE.ol.test(line)) {
        flushPara(para);
        const ordered = RE.ol.test(line);
        const list = el(ordered ? "ol" : "ul", "md-list");
        while (i < lines.length && (RE.ul.test(lines[i]) || RE.ol.test(lines[i]))) {
          const m = lines[i].match(ordered ? RE.ol : RE.ul);
          if (!m) break;
          const li = el("li");
          inline(ordered ? m[2] : m[1], li);
          i++;
          // continuation lines indented under the item
          const cont = [];
          while (i < lines.length && /^ {2,}\S/.test(lines[i]) &&
                 !RE.ul.test(lines[i]) && !RE.ol.test(lines[i])) {
            cont.push(lines[i].trim());
            i++;
          }
          if (cont.length) inline(" " + cont.join(" "), li);
          list.append(li);
        }
        frag.append(list);
        continue;
      }

      para.push(line);
      i++;
    }
    flushPara(para);
    return frag;
  }

  return { render, inline };
})();

window.MD = MD;
