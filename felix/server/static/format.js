/* Turns Felix's reply text into page content safely. Only *bold* and **bold** are styled;
   every character still goes in as text, never as HTML. Shared by the dashboard and the overlay. */
"use strict";

window.felixRender = function felixRender(node, text) {
  const pattern = /\*\*([^*\n]+)\*\*|(?<![\w*])\*([^*\s][^*\n]*)\*(?![\w*])/g;
  const parts = [];
  let last = 0;
  for (let match = pattern.exec(text); match; match = pattern.exec(text)) {
    if (match.index > last) parts.push(text.slice(last, match.index));
    const strong = document.createElement("strong");
    strong.textContent = match[1] || match[2];
    parts.push(strong);
    last = pattern.lastIndex;
  }
  if (last < text.length) parts.push(text.slice(last));
  node.replaceChildren(...parts);
};
