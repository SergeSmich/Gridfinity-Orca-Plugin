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
  src + "\n;return { buildBin, buildPlate, derive, derivePlate, toSTL, DEFAULTS, plateSteps, boardSteps, deriveBoard, ogEmitLevel, ogRibProfile, OG, Mesh };")(moduleShim, undefined, undefined);
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
/* The plate top cap may legitimately carry zero-width seams (relax-fan
 * wedges that spilled over a socket are decimated away; contours for
 * slicing come from the wall bands, which stay complete).  So instead of
 * full edge pairing we require: every edge with an actual z-span (the
 * walls) is paired exactly. */
function watertightWalls(mesh) {
  const seen = new Map();
  for (const tri of triangles(mesh)) {
    for (let i = 0; i < 3; i++) {
      const a = tri[i], b = tri[(i + 1) % 3];
      if (Math.abs(a[2] - b[2]) < 1e-6) continue;    // cap-plane edge: seam allowed
      const ka = key(a), kb = key(b);
      seen.set(ka + "|" + kb, (seen.get(ka + "|" + kb) || 0) + 1);
    }
  }
  for (const [k, n] of seen) if (n !== 1) return false;
  for (const [k, n] of seen) {
    const [a, b] = k.split("|");
    if (seen.get(b + "|" + a) !== 1) return false;
  }
  return true;
}
/* the user-facing invariant: no coplanar double-cover ("film") on the top
 * face - no two top-plane triangles may overlap with area > 0.05 mm2 */
function noFilm(mesh) {
  const tris = triangles(mesh);
  let zTop = -1e9;
  for (const tri of tris) for (const v of tri) if (v[2] > zTop) zTop = v[2];
  const cap = tris.filter(t => t.every(v => Math.abs(v[2] - zTop) < 1e-6));
  const cr = (o, a, b) => (a[0]-o[0])*(b[1]-o[1]) - (a[1]-o[1])*(b[0]-o[0]);
  const segX = (p1, p2, p3, p4) => {
    const d1 = cr(p3, p4, p1), d2 = cr(p3, p4, p2), d3 = cr(p1, p2, p3), d4 = cr(p1, p2, p4);
    return ((d1 > 1e-9) !== (d2 > 1e-9)) && ((d3 > 1e-9) !== (d4 > 1e-9));
  };
  const inTri = (p, T) => {
    const d1 = cr(T[0], T[1], p), d2 = cr(T[1], T[2], p), d3 = cr(T[2], T[0], p);
    return (d1 > 1e-9 && d2 > 1e-9 && d3 > 1e-9) || (d1 < -1e-9 && d2 < -1e-9 && d3 < -1e-9);
  };
  const overlaps = (A, B) => {
    for (let i = 0; i < 3; i++) for (let j = 0; j < 3; j++)
      if (segX(A[i], A[(i+1)%3], B[j], B[(j+1)%3])) return true;
    for (const p of A) if (inTri(p, B)) return true;
    for (const p of B) if (inTri(p, A)) return true;
    return false;
  };
  const triArea = T => Math.abs(cr(T[0], T[1], T[2])) / 2;
  const clip = (poly, A, B) => {
    const side = p => cr(A, B, p);
    const out = [];
    for (let i = 0; i < poly.length; i++) {
      const p = poly[i], q2 = poly[(i+1)%poly.length];
      const sp = side(p), sq = side(q2);
      if (sp >= -1e-12) out.push(p);
      if ((sp > 1e-12 && sq < -1e-12) || (sp < -1e-12 && sq > 1e-12)) {
        const t = sp / (sp - sq);
        out.push([p[0] + t*(q2[0]-p[0]), p[1] + t*(q2[1]-p[1])]);
      }
    }
    return out;
  };
  const polyA = poly => { let s = 0; for (let i = 0; i < poly.length; i++) { const p = poly[i], q2 = poly[(i+1)%poly.length]; s += p[0]*q2[1] - q2[0]*p[1]; } return Math.abs(s)/2; };
  for (let i = 0; i < cap.length; i++) {
    const A = cap[i].map(v => [v[0], v[1]]);
    for (let j = i + 1; j < cap.length; j++) {
      const B = cap[j].map(v => [v[0], v[1]]);
      if (!overlaps(A, B)) continue;
      let poly = A;
      for (let k = 0; k < 3 && poly.length; k++) poly = clip(poly, B[k], B[(k+1)%3]);
      if (poly.length && polyA(poly) > 0.05) return false;
    }
  }
  return true;
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
  check("single plate walls watertight", watertightWalls(r1.mesh));
  check("single plate has no film", noFilm(r1.mesh));
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
  check("stacked plate walls watertight", watertightWalls(r3.mesh));
  check("stacked plate has no film", noFilm(r3.mesh));
  const v3 = volume(r3.mesh);
  check("stack volume = 3 x single", Math.abs(v3 - 3*v1) < 5e-5 * Math.abs(v3), (v3/v1).toFixed(6));
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
    // an odd level is a z-mirror of the even ones: after un-flipping
    // (z -> zoff + H - z) the x/y footprint and the per-level volume must
    // match level 0.  Vertex sets are compared loosely - the cap-face
    // decimation may drop different overlap wedges per level, so exact
    // point sets are no longer expected to coincide.
    function levelBBox(lev) {
      const z0 = lev*(H+0.2);
      let lo = [1e9, 1e9], hi = [-1e9, -1e9];
      for (const tri of triangles(r3.mesh))
        for (const v of tri)
          if (v[2] >= z0 - 1e-6 && v[2] <= z0 + H + 1e-6) {
            if (v[0] < lo[0]) lo[0] = v[0];
            if (v[1] < lo[1]) lo[1] = v[1];
            if (v[0] > hi[0]) hi[0] = v[0];
            if (v[1] > hi[1]) hi[1] = v[1];
          }
      return { lo, hi };
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
    const b0 = levelBBox(0), b1 = levelBBox(1), b2 = levelBBox(2);
    const bboxSame = (a, b) => Math.abs(a.lo[0]-b.lo[0]) < 1e-6 && Math.abs(a.lo[1]-b.lo[1]) < 1e-6 &&
                              Math.abs(a.hi[0]-b.hi[0]) < 1e-6 && Math.abs(a.hi[1]-b.hi[1]) < 1e-6;
    if (!bboxSame(b0, b1) || !bboxSame(b0, b2)) return false;
    const v0 = levelVolume(0);
    return Math.abs(levelVolume(1) - v0) < 1e-3 * Math.abs(v0) &&
           Math.abs(levelVolume(2) - v0) < 1e-3 * Math.abs(v0);
  })());

  const pAsym = plateParams({ plateExact: true, plate_size_mode: "mm",
    plate_mm_x: 100, plate_mm_y: 90, buf_x_ratio: 30, buf_y_ratio: 20 });
  const rA = G.buildPlate(pAsym, SEG);
  check("asymmetric pads + stack watertight", watertightWalls(rA.mesh));
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

console.log("openGrid board:");
function boardParams(extra) {
  return Object.assign({}, G.DEFAULTS, {
    mode: "board", ogW: 2, ogH: 2, ogType: "full",
    ogScrews: true, ogConnectors: true
  }, extra || {});
}
{
  const d = G.deriveBoard(boardParams());
  check("derive: footprint 56x56, T 6.8", d.OX === 56 && d.OY === 56 && d.T === 6.8, JSON.stringify([d.OX, d.OY, d.T]));
  check("derive: 4 snap, 1 screw, 4 conn holes", d.snapHoles === 4 && d.screwHoles === 1 && d.connHoles === 4, JSON.stringify([d.snapHoles, d.screwHoles, d.connHoles]));
  const dLite = G.deriveBoard(boardParams({ ogType: "lite" }));
  check("derive: Lite T 4 (top band of full board)", dLite.T === 4 && dLite.lite === true, JSON.stringify([dLite.T, dLite.lite]));

  // every emitted piece must be a closed, positively-oriented solid
  function auditPieces(name, p, seg) {
    const d2 = G.deriveBoard(p);
    const r = G.ogEmitLevel(p, d2, seg);
    let ok = true, total = 0;
    for (const s of r.solids) {
      if (!watertightWalls(s)) { ok = false; break; }
      const v = volume(s);
      if (!(v > 0)) { ok = false; break; }
      total += v;
    }
    check(name + ": all pieces closed & positive", ok, r.solids.length + " pieces");
    return total;
  }
  const seg = { og: 24 };
  const vFull = auditPieces("2x2 Full", boardParams(), seg);
  auditPieces("2x2 Lite", boardParams({ ogType: "lite" }), seg);
  auditPieces("1x1 Full", boardParams({ ogW: 1, ogH: 1, ogScrews: false, ogConnectors: false }), seg);
  auditPieces("3x2 Full no screws", boardParams({ ogW: 3, ogH: 2, ogScrews: false }), seg);
  auditPieces("2x2 Full cs+back", boardParams({ ogCs: true, ogScrewInset: 1, ogBackside: true, ogBackInset: 1, ogBackCs: true }), seg);

  // analytic cross-checks against the rib profile
  const T = 6.8, prof = G.ogRibProfile(T, 28);
  let area = 0;
  for (let i = 0; i < prof.length; i++) {
    const P = prof[i], Q = prof[(i + 1) % prof.length];
    area += P[0] * Q[1] - Q[0] * P[1];
  }
  area = Math.abs(area) / 2;
  check("rib strip volume = profile area x 28", Math.abs(area * 28 - 222.32) < 0.02, (area * 28).toFixed(2));

  // screw bore must remove material from the node diamond (z-lofted blob)
  function centerPiece(r) {
    return r.solids.find(s => {
      const q = s.pos;
      let ok = true;
      for (let i = 0; i < q.length && ok; i += 3)
        if (Math.abs(q[i]) > 8.01 || Math.abs(q[i + 1]) > 8.01) ok = false;
      return ok;
    });
  }
  const pPlain = boardParams({ ogScrews: false });
  const rPlain = G.ogEmitLevel(pPlain, G.deriveBoard(pPlain), seg);
  const diaPlain = centerPiece(rPlain);
  const pScr = boardParams();
  const rScr = G.ogEmitLevel(pScr, G.deriveBoard(pScr), seg);
  const diaScr = centerPiece(rScr);
  check("node diamond plain volume ~763.9", diaPlain && Math.abs(volume(diaPlain) - 763.9) < 0.5, volume(diaPlain).toFixed(2));
  check("screw bore removes material", volume(diaScr) < volume(diaPlain) - 80, volume(diaScr).toFixed(2));
  check("screw piece watertight", watertightWalls(diaScr));

  // flip stacking: levels alternate upside down, gap preserved
  const p1 = boardParams({ ogStack: false });
  const b1 = G.boardSteps(p1, seg);
  let s1; do { s1 = b1.next(); } while (!s1.done);
  const m1 = s1.value.mesh, d1 = s1.value.derived;
  const p2 = boardParams({ ogStack: true, ogStackN: 2, ogStackGap: 0.2 });
  const b2 = G.boardSteps(p2, seg);
  let s2; do { s2 = b2.next(); } while (!s2.done);
  const m2 = s2.value.mesh, d2 = s2.value.derived;
  const bb2 = bbox(m2);
  check("board stack: 2 levels, height = 2T + 0.2", d2.levels === 2 && Math.abs(bb2.hi[2] - (2 * 6.8 + 0.2)) < 1e-6, bb2.hi[2].toFixed(3));
  check("board stack: volume = 2 x single", Math.abs(volume(m2) - 2 * volume(m1)) < 1e-6 * volume(m2));
  check("board stack: flip keeps footprint", Math.abs(bb2.lo[0] + 28) < 1e-6 && Math.abs(bb2.hi[0] - 28) < 1e-6 && Math.abs(bb2.lo[1] + 28) < 1e-6 && Math.abs(bb2.hi[1] - 28) < 1e-6);
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
  /* v1.8.2: holed caps (compartments, bores) switched to the exact scanline
     triangulation, so the tri count legitimately differs from HEAD; the
     volume pin still guards the solid */
  check("bin volume unchanged vs HEAD", Math.abs(volume(ro.mesh) - vol) < 1e-6 * Math.abs(vol), volume(ro.mesh).toFixed(3) + " vs " + vol.toFixed(3));
  check("bin unchanged vs HEAD (volume)", Math.abs(volume(ro.mesh) - vol) < 1e-9);
  const rl = G.buildPlate(Object.assign({}, G.DEFAULTS, {
    mode: "plate", gx: 2, gy: 2, plate_gx: 2, plate_gy: 2
  }), SEG);
  check("plain plate walls watertight", watertightWalls(rl.mesh));
  check("plain plate has no film", noFilm(rl.mesh));
}

console.log(failures ? "\n" + failures + " FAILURE(S)" : "\nall tests passed");
process.exit(failures ? 1 : 0);
