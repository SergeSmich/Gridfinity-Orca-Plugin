/* i18n consistency tests for gridfinity_bin.html -- plain node, no deps.
 *
 *   node tests/test_i18n.js
 *
 * Checks that every data-i key in the markup and every t("...") key used in
 * the script exists in BOTH language dictionaries, and that the dictionaries
 * cover the same key sets.
 */
"use strict";
const fs = require("fs");
const path = require("path");

const HTML = fs.readFileSync(path.join(__dirname, "..", "gridfinity_bin.html"), "utf8");
const s0 = HTML.indexOf("<script>"), s1 = HTML.indexOf("</script>", s0);
const src = HTML.slice(s0 + 8, s1);
/* markup only, so data-i mentions inside comments/code do not count */
const markup = HTML.slice(0, s0);

let failures = 0;
function check(name, ok, detail) {
  if (ok) console.log("  ok  " + name);
  else { failures++; console.error("FAIL  " + name + (detail ? "  --  " + detail : "")); }
}

/* pull the two dictionary literals out of the page source */
function dictOf(lang) {
  const m = src.match(new RegExp(lang + ":\\s*\\{"));
  if (!m) throw new Error(lang + " dict not found");
  let i = m.index + m[0].length, depth = 1, inStr = null;
  while (depth > 0 && i < src.length) {
    const c = src[i];
    if (inStr) {
      if (c === "\\") i++;
      else if (c === inStr) inStr = null;
    } else if (c === '"' || c === "'") inStr = c;
    else if (c === "{") depth++;
    else if (c === "}") depth--;
    i++;
  }
  const body = "{" + src.slice(m.index + m[0].length, i);
  return new Function("return " + body)();
}
const en = dictOf("en"), ru = dictOf("ru");

console.log("i18n dictionaries:");
const enKeys = Object.keys(en), ruKeys = Object.keys(ru);
check("en has keys", enKeys.length > 80, enKeys.length);
check("key sets match", enKeys.length === ruKeys.length &&
  enKeys.every(k => ru.hasOwnProperty(k)) &&
  ruKeys.every(k => en.hasOwnProperty(k)),
  "en=" + enKeys.length + " ru=" + ruKeys.length);
check("no empty values", enKeys.every(k => en[k]) && ruKeys.every(k => ru[k]));

console.log("markup coverage:");
const dataKeys = new Set();
const reDI = /data-i="([a-z0-9_]+)"/g;
let mm; while ((mm = reDI.exec(markup))) dataKeys.add(mm[1]);
const missingData = [...dataKeys].filter(k => !(k in en));
check("all data-i keys exist in both dicts (" + dataKeys.size + " keys)",
  missingData.length === 0, missingData.join(", "));

console.log("script coverage:");
const used = new Set();
const reT = /\bt\("([a-z0-9_]+)"/g;
let mt; while ((mt = reT.exec(src))) used.add(mt[1]);
const missingT = [...used].filter(k => !(k in en));
check("all t() keys exist in both dicts (" + used.size + " keys)",
  missingT.length === 0, missingT.join(", "));

/* no leftover literal UI strings outside the dictionaries */
console.log("leftovers:");
const dictStart = src.indexOf("var I18N"), dictEnd = src.indexOf("var LANG");
const uiCode = src.slice(0, dictStart) + src.slice(dictEnd);
const leftovers = ["\"Building\"", '"Writing STL"', '"Handing to OrcaSlicer"',
  '"0 compartments"', '"Export failed: "', '"Use printer bed"', '" (Front)"'];
const stillThere = leftovers.filter(l => uiCode.includes(l));
check("no untranslated hotspots", stillThere.length === 0, stillThere.join(" | "));

console.log(failures ? "\n" + failures + " FAILURE(S)" : "\nall tests passed");
process.exit(failures ? 1 : 0);
