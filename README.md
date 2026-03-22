# BB SVG Layers — Blender Addon

A Blender 4.2+ extension that automates the full pipeline for converting imported SVG layers into game-ready 3D paper cutout meshes. It handles geometry processing, UV projection, material creation from a master material, automatic export to the asset library, and intelligent layer stacking — including multi-character scenes with automatic collection sorting.

---

## What It Does

This addon is designed for **paper cutout 3D scenes** where SVG layers become individual mesh pieces stacked along the Y axis, simulating physical depth between paper layers.

---

## Installation

1. Download `bb_svg_layers.zip` from the [Releases](../../releases) page
2. In Blender: **Edit → Preferences → Add-ons**
3. Drag and drop the `.zip` into the Preferences window, or use **▾ → Install from Disk**
4. Enable **BB SVG Layers** in the addon list

> Requires **Blender 4.2 or later** (uses the extension manifest format).

---

## Panel Location

`3D Viewport → N-Panel (N key) → SVG Layer tab`

---

## Controls

### Slider

| Slider | Description |
|---|---|
| **Tiny Object Threshold** | Objects with surface area below this value (in Blender units²) are always placed in the frontmost layer. Use this to keep whiskers, thin lines and small details on top. Default: `500` |

---

### Buttons

#### Load SVG
Imports an SVG file and automatically runs the full pipeline in one step:

1. **Reads layer order** from the SVG XML before importing, so document order is preserved regardless of Blender's alphabetical import behaviour
2. **Imports the SVG** via Blender's built-in importer
3. **Selects the new collection** created by the importer
4. **Runs Apply & Sort** — full geometry pipeline on every object in the collection, then sorts them into sub-collections by name prefix:
   - `BG_` objects → **BG** collection
   - Character objects (`Wes_`, `Dad_`, etc.) → one collection per character
   - `FG_` objects → **FG** collection

   Steps performed on each object:
   - Rotate +90° on X, Scale ×850, Convert curves to mesh, Apply all transforms
   - Merge by Distance, Solidify (thickness `1`, applied), Offset back faces (`-2` X, `+2` Z)
   - UV projection from Y onto 1920×1920 px canvas; back/side faces pinned to `(0, 0)`
   - **Create material** by copying the **Master** material, injecting the fill color read from the SVG, and assigning it to the object
   - **Export all created materials** to the **Paper** catalog in the User asset library

5. **Runs Auto Stack** — stacks all objects using a greedy layer-packing algorithm:
   - Objects below the **Tiny Object Threshold** go to the frontmost layer of their group
   - Each remaining object is placed in the earliest layer where it doesn't overlap anything already there (overlap detected via SAT on XZ-plane polygons, with a bounding-box pre-check)
   - Collections are processed in outliner order — reorder them in the outliner before loading if needed
   - Y offset between every layer is `1` unit

---

#### − / + Buttons
Move all selected objects along +Y or -Y by `1` unit. Useful for fine-tuning individual layers after Auto Stack.

#### Snap
Snaps all selected objects to the **highest Y value** among them — useful for aligning pieces that should be on the same layer.

#### Override Single
Makes a **single-user copy** of the assigned material for each selected object, then creates a **library override** so it can be edited independently without affecting other objects. The overridden material is named `<prefix>_override` (e.g. `Wes_body_override`).

#### Override Same
Same as Override Single, but after creating the override it **reassigns it to every object in the scene** that was using the same original material. Use this when multiple objects share one material and you want them all to switch to the same editable override in one click.

---

## Master Material Setup

Materials are created automatically at import time by copying a local material named **`Master`** and injecting the fill color read from the SVG file.

### Steps to set up

1. In your `.blend` file, create a material named exactly **`Master`**
2. Set up its node tree however you like — the addon will find the first `RGB` node, node named `Color`, Principled BSDF Base Color input, or any `RGBA` input named `Color`, and write the SVG fill color into it
3. If your Master material has a **Mapping** node, its Z rotation will be randomised on each copy for natural texture variation

All created materials are automatically marked as assets and written to a **Paper** catalog in your configured User asset library.

### Name Matching

The addon derives the material name from the object name by stripping Blender's duplicate suffix:

| Object name | Material created |
|---|---|
| `Wes_body` | `Wes_body` |
| `Wes_body.001` | `Wes_body` |
| `BG_sky.014` | `BG_sky` |

If a material named `<prefix>` already exists locally it is reused and its color updated rather than creating a duplicate.

---

## Naming Convention

For multi-character scenes, name your SVG layers with prefixes:

| Prefix | Goes into | Stacked |
|---|---|---|
| `BG_` | **BG** collection | Furthest back |
| `Wes_`, `Dad_`, etc. | Per-character collection | Middle, in outliner order |
| `FG_` | **FG** collection | Closest to camera |

---

## Typical Workflow

1. Create a material named **`Master`** in your `.blend` file and set up its node tree
2. *(Optional)* Reorder collections in the outliner to set the desired BG → characters → FG depth order before loading
3. Click **Load SVG** and select your file — geometry processing, material creation, collection sorting, and layer stacking all run automatically
4. Fine-tune with **− / +**, **Snap**, **Override Single**, and **Override Same** as needed

---

## File Structure

```
bb_svg_layers/
├── __init__.py            # Addon code
└── blender_manifest.toml  # Extension manifest (Blender 4.2+)
```

---

## Requirements

- Blender 4.2 or later
- A local material named **`Master`** in the current `.blend` file (used as the template for all created materials)
- A configured asset library in **Preferences → File Paths → Asset Libraries** (for exporting the generated materials)

---

## License

GPL-2.0-or-later — see [Blender's extension licensing guidelines](https://extensions.blender.org/about/licenses/).
