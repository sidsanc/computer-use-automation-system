({
  norm(t) {
    return (t || "").replace(/\s+/g, " ").trim();
  },

  cellText(cell) {
    return this.norm(cell.innerText);
  },

  // Caption for an unlabelled control: nearest non-empty, control-free cell to its left.
  neighborLabel(el) {
    const cell = el.closest("td,th");
    if (!cell) return "";
    for (let c = cell.previousElementSibling; c; c = c.previousElementSibling) {
      if (c.querySelector("input,select,textarea,button")) continue;
      const text = this.norm(c.innerText).replace(/:$/, "").trim();
      if (text) return text;
    }
    return "";
  },

  // Legacy tables rarely use <th>; header rows are recognisable by being rendered bold.
  isHeaderRow(row) {
    const cells = [...row.cells];
    if (cells.length < 2) return false;
    return cells.every(
      (c) => c.tagName === "TH" || parseInt(getComputedStyle(c).fontWeight, 10) >= 600
    );
  },

  headerRowFor(row) {
    const rows = [...row.closest("table").rows];
    for (let i = rows.indexOf(row) - 1; i >= 0; i--) {
      if (rows[i].cells.length === row.cells.length && this.isHeaderRow(rows[i])) return rows[i];
    }
    return null;
  },

  tableContext(cell) {
    const row = cell.parentElement;
    const table = row.closest("table");
    const ctx = {
      table: [...table.ownerDocument.querySelectorAll("table")].indexOf(table),
      row: [...table.rows].indexOf(row),
      col: cell.cellIndex,
      column: null,
      row_values: {},
    };
    const header = this.headerRowFor(row);
    if (header) {
      ctx.column = this.cellText(header.cells[cell.cellIndex]);
      [...row.cells].forEach((c, i) => {
        ctx.row_values[this.cellText(header.cells[i])] = this.cellText(c);
      });
    }
    return ctx;
  },

  documentOrder() {
    const order = new Map();
    const walker = document.createTreeWalker(
      document.documentElement,
      NodeFilter.SHOW_ELEMENT | NodeFilter.SHOW_TEXT
    );
    let i = 0;
    for (let n = walker.currentNode; n; n = walker.nextNode()) order.set(n, i++);
    return order;
  },

  describe(node) {
    const el = node.nodeType === 1 ? node : node.parentElement;
    const out = {
      in_dialog: !!el.closest("[role=dialog],[role=alertdialog],dialog"),
      neighbor_label: null,
      value: null,
      table: null,
      has_control: false,
    };
    if (node.nodeType !== 1) return out;
    if (["INPUT", "SELECT", "TEXTAREA"].includes(el.tagName)) {
      out.neighbor_label = this.neighborLabel(el);
      if (el.tagName === "SELECT") {
        out.value = el.selectedOptions[0] ? this.norm(el.selectedOptions[0].text) : "";
      } else if (el.type !== "password") {
        out.value = el.value;
      }
    }
    const cell = el.closest("td,th");
    if (cell) out.table = this.tableContext(cell);
    if (el.matches("td,th")) out.has_control = !!el.querySelector("a,input,select,textarea,button");
    return out;
  },

  cssPath(el) {
    const parts = [];
    for (; el && el.nodeType === 1 && el.tagName !== "HTML"; el = el.parentElement) {
      let part = el.tagName.toLowerCase();
      const same = [...el.parentElement.children].filter((s) => s.tagName === el.tagName);
      if (same.length > 1) part += `:nth-of-type(${same.indexOf(el) + 1})`;
      parts.unshift(part);
    }
    return parts.join(" > ");
  },

  findTableCells(column, rowColumn, equals) {
    const found = [];
    for (const table of document.querySelectorAll("table")) {
      for (const row of table.rows) {
        const header = this.headerRowFor(row);
        if (!header) continue;
        const names = [...header.cells].map((c) => this.cellText(c));
        const ci = names.indexOf(column);
        const ki = names.indexOf(rowColumn);
        if (ci < 0 || ki < 0) continue;
        if (this.cellText(row.cells[ki]) === equals) found.push(row.cells[ci]);
      }
    }
    return found;
  },
})
