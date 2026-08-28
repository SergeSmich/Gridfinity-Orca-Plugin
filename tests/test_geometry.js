/* Geometry tests for gridfinity_bin.html -- plain node, no deps.
 *
 *   node tests/test_geometry.js
 *
 * The script section of the HTML page is self-contained up to the
 * module.exports line, so it can be evaluated outside a browser.  These
 * tests cover the baseplate stacking feature and general mesh sanity.
 */
"use strict";
const fs = require("fs");
const path = require("path");

const HTML = fs.readFileSync(path.join(__dirname, "..", "gridfinity_bin.html"), "utf8");
const m0 = HTML.indexOf("<script>");
const m1 = HTML.indexOf("</script>", m0);
let src = HTML.slice(m0 + "<script>".length, m1);
const cut = src.indexOf('if (typeof module !== "undefined")');
if (cut < 0) throw new Error("module.exports marker not found");
src = src.slice(0, src.indexOf("\n", cut));

const moduleShim = { exports: {} };
const G = new Function("module", "window", "document",
  src + "\n;return { buildBin, buildPlate, derive, derivePlate, toSTL, DEFAULTS, plateSteps, Mesh };")(moduleShim, undefined, undefined);
// the trailing DOM-free slice still executes the exports line above

let failures = 0;
function check(name, ok, detail) {
  if (ok) console.log("  ok  " + name);
  else { failures++; console.error("FAIL  " + name + (detail !== undefined ? "  --  " + detail : "")); }
}

/* ---- mesh helpers ---- */
function triangles(mesh) {
  const p = mesh.pos, out = [];
  for (let t = 0; t < mesh.count(); t++) {
    const b = t * 9;
    out.push([[p[b], p[b+1], p[b+2]], [p[b+3], p[b+4], p[b+5]], [p[b+6], p[b+7], p[b+8]]]);
  }
  return out;
}
function key(v) { return v.map(x => Math.round(x * 1e4)).join(","); }
/* every directed edge exactly once and its reverse exactly once => closed */
function watertight(mesh) {
  const seen = new Map();
  for (const tri of triangles(mesh)) {
    for (let i = 0; i < 3; i++) {
      const a = key(tri[i]), b = key(tri[(i + 1) % 3]);
      seen.set(a + "|" + b, (seen.get(a + "|" + b) || 0) + 1);
    }
  }
  let bad = 0;
  for (const [k, n] of seen) {
    if (n !== 1) { bad++; continue; }
    const [a, b] = k.split("|");
    if (seen.get(b + "|" + a) !== 1) bad++;
  }
  return bad === 0;
}
function volume(mesh) {
  let v = 0;
  for (const [[ax,ay,az],[bx,by,bz],[cx,cy,cz]] of triangles(mesh))
    v += (ax*(by*cz-bz*cy) - ay*(bx*cz-bz*cx) + az*(bx*cy-by*cx)) / 6;
  return v;
}
function bbox(mesh) {
  let lo = [1e9,1e9,1e9], hi = [-1e9,-1e9,-1e9];
  for (const tri of triangles(mesh)) for (const v of tri)
    for (let i = 0; i < 3; i++) { if (v[i] < lo[i]) lo[i] = v[i]; if (v[i] > hi[i]) hi[i] = v[i]; }
  return { lo, hi };
}
const SEG = { corner: 10, comp: 6, hole: 14 };

/* ---- baseplate stacking ---- */
function plateParams(extra) {
  return Object.assign({}, G.DEFAULTS, {
    mode: "plate", gx: 2, gy: 2, plate_gx: 2, plate_gy: 2,
    plateConnectors: true, plateGap: 0, plateBase: 0,
    plateStack: true, plateStackN: 3, plateStackGap: 0.2
  }, extra || {});
}

console.log("baseplate stacking:");
{
  const p1 = plateParams({ plateStack: false });
  const r1 = G.buildPlate(p1, SEG);
  const v1 = volume(r1.mesh);
  const H = r1.derived.H;
  check("single plate is watertight", watertight(r1.mesh));
  check("single plate levels = 1", r1.derived.levels === 1);
  check("single plate height", Math.abs(bbox(r1.mesh).hi[2] - H) < 1e-6, bbox(r1.mesh).hi[2]);

  const p3 = plateParams({});
  const r3 = G.buildPlate(p3, SEG);
  const d = r3.derived;
  const bb = bbox(r3.mesh);
  check("stack levels = 3", d.levels === 3);
  check("stack total height = 3H + 2*0.2", Math.abs(d.HTotal - (3*H + 0.4)) < 1e-9, d.HTotal);
  check("stack mesh z-top matches", Math.abs(bb.hi[2] - d.HTotal) < 1e-6, bb.hi[2]);
  check("stack mesh z-base at 0", Math.abs(bb.lo[2]) < 1e-6, bb.lo[2]);
  check("stacked plate watertight", watertight(r3.mesh));
  const v3 = volume(r3.mesh);
  check("stack volume = 3 x single", Math.abs(v3 - 3*v1) < 1e-6 * Math.abs(v3), (v3/v1).toFixed(6));
  check("stack tri count = 3 x single", r3.mesh.count() === 3 * r1.mesh.count());
  check("gap present between levels", (() => {
    // no triangle may own z values strictly inside (k*H + (k-1)*g, k*H + (k-1)*g + 0.2) bands
    for (const tri of triangles(r3.mesh)) {
      for (let lev = 1; lev < 3; lev++) {
        const lo = lev*H + (lev-1)*0.2, hi = lev*H + lev*0.2;
        for (const v of tri) if (v[2] > lo + 1e-4 && v[2] < hi - 1e-4) return false;
      }
    }
    return true;
  })());
  check("odd levels are flipped upside down", (() => {
    // un-flip each odd level (z -> zoff + H - z); the point set and the
    // per-level volume must then match level 0 exactly (x/y are unchanged)
    function levelSet(lev, unflip) {
      const z0 = lev*(H+0.2), s = new Set();
      for (const tri of triangles(r3.mesh))
        for (const v of tri)
          if (v[2] >= z0 - 1e-6 && v[2] <= z0 + H + 1e-6)
            s.add(key(unflip ? [v[0], v[1], z0 + H - v[2]]
                            : [v[0], v[1], v[2] - z0]));
      return s;
    }
    function levelVolume(lev) {
      const z0 = lev*(H+0.2);
      let v = 0;
      for (const [[ax,ay,az],[bx,by,bz],[cx,cy,cz]] of triangles(r3.mesh)) {
        if (az < z0 - 1e-6 || az > z0 + H + 1e-6) continue;
        v += (ax*(by*cz-bz*cy) - ay*(bx*cz-bz*cx) + az*(bx*cy-by*cx)) / 6;
      }
      return v;
    }
    const s0 = levelSet(0, false), s2 = levelSet(2, false), s1 = levelSet(1, true);
    let same = s0.size === s1.size && s0.size === s2.size;
    if (same) for (const k of s0) if (!s1.has(k) || !s2.has(k)) { same = false; break; }
    if (!same) return false;
    const v0 = levelVolume(0);
    return Math.abs(levelVolume(1) - v0) < 1e-9 && Math.abs(levelVolume(2) - v0) < 1e-9;
  })());

  const pAsym = plateParams({ plateExact: true, plate_size_mode: "mm",
    plate_mm_x: 100, plate_mm_y: 90, buf_x_ratio: 30, buf_y_ratio: 20 });
  const rA = G.buildPlate(pAsym, SEG);
  check("asymmetric pads + stack watertight", watertight(rA.mesh));
  check("asymmetric levels share footprint", (() => {
    const bbA = bbox(rA.mesh);
    return Math.abs(bbA.lo[0] + bbA.hi[0] - (rA.derived.padRight - rA.derived.padLeft)) < 1e-6;
  })());
  check("asymmetric odd level keeps its footprint", (() => {
    const dA = rA.derived, HA = dA.H;
    function levelBBox(lev) {
      const z0 = lev*(HA+0.2);
      let lo = [1e9,1e9], hi = [-1e9,-1e9];
      for (const tri of triangles(rA.mesh))
        for (const v of tri)
          if (v[2] >= z0 - 1e-6 && v[2] <= z0 + HA + 1e-6) {
            if (v[0] < lo[0]) lo[0] = v[0]; if (v[0] > hi[0]) hi[0] = v[0];
            if (v[1] < lo[1]) lo[1] = v[1]; if (v[1] > hi[1]) hi[1] = v[1];
          }
      return { lo, hi };
    }
    const b0 = levelBBox(0), b1 = levelBBox(1);
    // a z-mirror does not move anything in x/y: footprints must be identical
    return Math.abs(b0.lo[0] - b1.lo[0]) < 1e-6 && Math.abs(b0.hi[0] - b1.hi[0]) < 1e-6 &&
           Math.abs(b0.lo[1] - b1.lo[1]) < 1e-6 && Math.abs(b0.hi[1] - b1.hi[1]) < 1e-6;
  })());
}

console.log("regression:");
{
  // The bin is an assembly of touching closed solids, so strict manifoldness
  // does not apply; instead pin volume and triangle count against HEAD.
  const p = Object.assign({}, G.DEFAULTS, {});
  const rb = G.buildBin(p, SEG);
  const vol = volume(rb.mesh), tris = rb.mesh.count();
  const origSrc = require("child_process")
    .execSync("git show HEAD:gridfinity_bin.html", { cwd: path.join(__dirname, ".."), maxBuffer: 1 << 26 })
    .toString();
  const o0 = origSrc.indexOf("<script>"), o1 = origSrc.indexOf("</script>", o0);
  let os = origSrc.slice(o0 + 8, o1);
  const oc = os.indexOf('if (typeof module !== "undefined")');
  os = os.slice(0, os.indexOf("\n", oc));
  const OG = new Function("module", os + "\n;return { buildBin };")(moduleShim);
  const ro = OG.buildBin(Object.assign({}, G.DEFAULTS, {}), SEG);
  check("bin unchanged vs HEAD (tris)", ro.mesh.count() === tris, ro.mesh.count() + " vs " + tris);
  check("bin unchanged vs HEAD (volume)", Math.abs(volume(ro.mesh) - vol) < 1e-9);
  const rl = G.buildPlate(Object.assign({}, G.DEFAULTS, {
    mode: "plate", gx: 2, gy: 2, plate_gx: 2, plate_gy: 2
  }), SEG);
  check("plain plate still watertight", watertight(rl.mesh));
}

console.log(failures ? "\n" + failures + " FAILURE(S)" : "\nall tests passed");
process.exit(failures ? 1 : 0);
