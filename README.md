# Gridfinity Bin, Baseplate & openGrid Generator for OrcaSlicer

A parametric 3D generator plugin for **OrcaSlicer** that configures, previews, and drops custom Gridfinity models directly onto your 3D printer's build plate.

---

## ✨ Features

### 📦 Parametric Bins
* **Standard-Compliant Geometry**: Built strictly to **Zack Freedman’s Gridfinity specification** (42 mm grid pitch, 7 mm unit heights, standard stacking feet and mating lip profile).
* **Advanced Compartment Layouts**:
  * **Uniform Grid**: Divide interior into equal $X \times Y$ compartments.
  * **By Row**: Set total rows and customize the number of columns in each individual row (from front to back).
  * **By Column**: Set total columns and customize the number of rows in each individual column (from left to right).
* **Features & Hardware**:
  * Finger scoops with curved bottom ramps for easy small-part retrieval.
  * 45° angled label tabs designed to print without supports.
  * Blind magnet pockets ($6 \times 2\text{ mm}$) and stepped M3 screw holes in the feet.
  * Configurable wall thickness, floor thickness, and corner fillets.

### 🧩 Interlocking Baseplates
* **Multiple Sizing Modes**:
  * **Grid Units**: Sizing directly in integer $X \times Y$ grid units.
  * **Dimensions (mm)**: Input exact target drawer/tray dimensions in millimeters with automatic integer unit fitting ($\text{units} = \max(1, \lfloor(\text{mm} + 0.5)/42\rfloor)$).
  * **Exact Edge Padding**: Add fractional perimeter borders at the outer edges to hit the exact target millimeter dimensions without disrupting the 42 mm socket grid.
* **Stackable Copies (batch printing)**:
  * Stack 2–10 identical copies straight up in a single STL.
  * Every second copy is laid upside down (sockets facing down), so the alternation is visible on any plate.
  * Configurable vertical air gap between copies (default **0.2 mm**) so the levels never fuse.
* **Large Bed Splitting & Connectors**:
  * Multi-piece interlocking segmentation planning ported from **GridFlock** (by Jonas Konrad).
  * Dove-tail puzzle joints that interlock securely across segments.
  * Staggered seam layout to prevent weak 4-way corner intersections.
  * Auto-detection of active printer bed dimensions from OrcaSlicer.

### 🟩 openGrid Boards (v1.7.0)
* **Standard-Compliant Geometry**: Boards are generated to the **openGrid specification** (28 mm grid, 6.8 mm full thickness, snap-compatible sockets) — the same geometry as the [openGrid Studio](https://ogstudio.sudomaker.com/) web generator, verified volume-for-volume against its manifold pipeline.
* **Board Types**:
  * **Full Board**: 6.8 mm thick with the standard capture-chamfer socket grid.
  * **Lite Board**: The top 4 mm band of the full board (front screw pockets kept, backside pockets trimmed away) — exactly as openGrid Studio builds it.
* **Screw Options**:
  * Screw shaft diameter, head diameter, and head inset (front pockets).
  * Optional **countersinks** with configurable angle (front side).
  * Optional **backside pockets** with shrink/inset/countersink controls.
* **Connector Cutouts**: Side connector notches on all border nodes, ready for standard openGrid connectors.
* **Flip-Stacked Copies**: Stack 2–10 boards in one STL using the same upside-down alternation + air gap mechanism as the baseplates (no Interface Layer or ironing required).
* **Live Stats & Naming**: Board statistics (tiles, holes, volume) and descriptive file names (`opengrid_board_2x2_lite.stl`, …).

### 🚀 Slicer Integration & Experience
* **Direct Build Plate Injection**: Exports binary STL files and automatically drops them into your active OrcaSlicer plate via single-instance IPC (D-Bus on Linux, `WM_COPYDATA` on Windows).
* **Interactive 3D WebGL Viewport**: Orbit, zoom, and pan directly within the OrcaSlicer UI with dark/light theme matching and zero external web dependencies.
* **Bilingual Interface (EN/RU)**: Full English and Russian UI with a language toggle at the top of the panel; the choice is remembered and defaults to the browser language.
* **OpenSCAD Parity**: Matching parametric OpenSCAD script (`gridfinity_bin.scad`) supporting all features including custom `row_divisions` and `col_divisions` array parameters.

---

## 📥 Installation

### Method 1: Subscribe via Orca Cloud
Search for **Gridfinity Bin & Baseplate Generator** in Orca Cloud's **Plugin Hub** and click **Subscribe** to receive automatic updates directly in OrcaSlicer.

### Method 2: Automatic Install via Build Script
Run the builder with `--install` to compile and copy the target-tagged plugin into OrcaSlicer's plugin directory:

```bash
python3 build_orca_plugin.py --install
```

### Method 3: Manual Installation
1. Clone or download this repository.
2. Build target plugins:
   ```bash
   python3 build_orca_plugin.py --all-targets
   ```
3. Copy the generated `.py` file matching your operating system and architecture into an `orca_plugins/` subfolder:
   * **Linux**: `~/.config/OrcaSlicer/plugins/gridfinity_bin/gridfinity_bin_plugin_linux_x86_64.py`
   * **Windows**: `%APPDATA%\OrcaSlicer\plugins\gridfinity_bin\gridfinity_bin_plugin_win_x86_64.py`
   * **macOS**: `~/Library/Application Support/OrcaSlicer/plugins/gridfinity_bin/gridfinity_bin_plugin_macosx_arm64.py`
4. Restart OrcaSlicer. You will see a new **Gridfinity** tab in the top navigation bar.

---

## 🛠️ Supported Targets

| Platform | Architecture | Plugin Filename |
| :--- | :--- | :--- |
| **Linux** | x86_64 | `gridfinity_bin_plugin_linux_x86_64.py` |
| **Linux** | ARM64 | `gridfinity_bin_plugin_linux_arm64.py` |
| **Windows** | x86_64 | `gridfinity_bin_plugin_win_x86_64.py` |
| **Windows** | ARM64 | `gridfinity_bin_plugin_win_arm64.py` |
| **macOS** | Apple Silicon (ARM64) | `gridfinity_bin_plugin_macosx_arm64.py` |
| **macOS** | Intel (x86_64) | `gridfinity_bin_plugin_macosx_x86_64.py` |

---

## 📖 Usage Guide

### 1. Generating Bins
1. Select **Model > Bin**.
2. Adjust **Bin Size**:
   * **Width / Depth**: Multiples of 42 mm grid pitch.
   * **Height**: Multiples of 7 mm Gridfinity unit height (default $6\text{ units} = 42\text{ mm}$).
3. Configure **Compartments**:
   * **Uniform**: Select divisions along X and Y.
   * **By Row**: Set total rows, then choose compartments per row.
   * **By Column**: Set total columns, then choose compartments per column.
4. Toggle **Features** (Stacking lip, Finger scoop, Label tab, Magnet/Screw holes).
5. Click **Export STL** to save or drop onto the build plate.

### 2. Generating Baseplates
1. Select **Model > Baseplate**.
2. Choose **Grid Units** or **Dimensions (mm)**:
   * When using **Dimensions (mm)**, enter your drawer or enclosure size (e.g. `250 × 210 mm`).
   * Click **`Add edge padding for exact size`** if you want symmetrical outer rim padding to fill the drawer completely.
3. Configure puzzle connectors and optional solid base thickness.
4. If the plate exceeds your printer bed, the plugin automatically splits it into puzzle-joint segments.
5. Click **Export STL**.

### 3. Generating openGrid Boards
1. Select **Model > openGrid Board**.
2. Set the board size in **tile units** ($W \times H$, 28 mm pitch).
3. Choose **Full** (6.8 mm) or **Lite** (4 mm) board type.
4. Configure screws (diameter, head, inset, countersink, backside pocket) and side connector cutouts.
5. Optionally enable **Stack copies** — flipped levels with an air gap, like the baseplates.
6. Click **Export STL**.

> The 28 mm openGrid grid is a superset of the 42 mm Gridfinity grid: 3 openGrid tiles = 2 Gridfinity units = 84 mm.

---

## 📐 Gridfinity Dimensions Quick Reference

| Parameter | Dimension | Notes |
| :--- | :--- | :--- |
| **Grid Pitch** | $42.0\text{ mm}$ | Center-to-center cell spacing |
| **Unit Height ($Z$)** | $7.0\text{ mm}$ | Standard height unit ($1U = 7\text{ mm}$) |
| **Foot Height** | $4.75\text{ mm}$ | Base stacking profile height |
| **Foot Top Dimension** | $41.5\text{ mm}$ | $42.0\text{ mm} - 0.5\text{ mm}$ grid clearance gap |
| **Top Lip Height** | $3.30\text{ mm}$ | Male stacking rim |
| **Magnet Recess** | $\varnothing 6.5\text{ mm} \times 2.4\text{ mm}$ | Fits standard $6 \times 2\text{ mm}$ neodymium magnets |
| **Screw Holes** | $\varnothing 3.0\text{ mm}$ | Fits standard M3 socket head screws |

---

## 📜 References & Acknowledgments
* [Gridfinity Standard](https://gridfinity.xyz/) by Zack Freedman ([Voidstar Lab](https://www.youtube.com/c/ZackFreedman)).
* [GridFlock](https://github.com/jkonrad/gridflock) segmentation planning and puzzle connector profiles by Jonas Konrad.
* [openGrid](https://opengrid.world/) standard by David D. (CC-BY 4.0); board geometry verified against [openGrid Studio](https://github.com/ClassicOldSong/openGrid-Studio) by Yukino Song / SudoMaker (Apache-2.0).
* [OrcaSlicer](https://github.com/SoftFever/OrcaSlicer) plugin architecture and slicing engine.
* [OpenSCAD](https://openscad.org/) parametric CAD framework.
