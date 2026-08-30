# BB Poly Draw

A Blender extension for fast, interactive **polyline / polygon and Bézier** drawing directly in the 3D viewport — with roll-over (hover) point editing, boolean hole cutting, polyline & curve trimming, 2D polygon union, view-aware offsetting, full snap support, angle-constrained drawing, single-step undo, and a built-in curve editing mode.

**Authors:** Blender Bob & Claude.ai

---

## Features

- **Poly Draw** — click to place points; **RMB/Enter** commits an open polyline, **Alt+RMB** closes the loop and fills a polygon (N-Gon face)
- **Bézier** — click for corner points, click-drag for smooth points with live handle pulling
- **Roll-over editing (no modifier)** — just hover over any point, vertex, or Bézier handle (it lights up green) and **click-drag** to move it — while drawing *or* while editing committed geometry. No Ctrl+Shift required.
- **Append** — Shift+LMB after committing to union a new shape into the previous one using clean 2D polygon math (no Boolean modifier, no leftover geometry). Appending onto a polygon auto-closes the new shape on plain RMB.
- **Holes / Cut** — Ctrl+LMB after committing to cut into the previous shape; Boolean Difference for solid meshes, 2D point-in-polygon trimming for polylines, direct control-point removal with boundary splitting for Bézier curves
- **Post-commit close** — after committing, the shape stays highlighted; **Alt+RMB** closes/opens it (curve cyclic toggle, or fill/clear a polygon face) after the fact, not only mid-draw
- **Pick mode (Q)** — hover any curve or mesh in the viewport to highlight it, then click to edit it
- **View-aware offset** — auto-detects the correct axis from your viewport; scales from the camera in perspective
- **Angle snap (Ctrl)** — constrain the next segment to a configurable angle increment (default 5°), adjustable live with Ctrl+Scroll. With Bézier, Ctrl also snaps the handle direction to that increment.
- **Alignment guides** — while drawing, the preview point snaps to line up (vertically/horizontally) with existing points and a guide line pops up, so you can square off rectangles and align corners. Toggle **All / Current shape / Off** live with the **G** key.
- **Axis-lock numeric entry** — press an axis key, type a distance, hit Enter to add the next point that far along the axis from the last one (e.g. `X` `2` `2` `Enter` = 22 units along +X). Default keys are **X / Y / Z**, rebindable (with optional Ctrl/Shift/Alt) in preferences.
- **Full Blender snap support** — Vertex, Edge, Edge Midpoint, Face, and adaptive Grid, with quick exclusive on/off toggles for Vertex / Edge / Grid on the **V / C / X** keys
- **Viewport shading shortcuts** — **4** for Wireframe, **5** for Solid, without leaving the tool
- **Single-step undo (Ctrl+Z)** — removes the last placed point while drawing, or reverts the last committed shape / append / hole / close during editing
- **Configurable preferences** — default offset distance, roll-over (hover) tolerance, and alignment-guide scope
- Flat drawing plane locked on the first click so all points stay coplanar in perspective
- Live rubber-band preview, green hover dots, yellow snap indicator, cyan edge-insert indicator

---

## Requirements

- Blender **4.2.0** or newer (extension platform)

---

## Installation

1. Download `bb_poly_draw-x.y.z.zip` from the [Releases](https://github.com/riouxr/BB-Poly-Draw/releases) page
2. In Blender: **Edit → Preferences → Add-ons** (or **Get Extensions**)
3. Click the **▾** dropdown (top right) → **Install from Disk**
4. Select the `.zip`
5. Enable the extension
6. The tools appear in the **Toolbar** (press **T**) in the 3D Viewport, in Object Mode:
   **Poly Draw (P)**, **Bézier (B)**

---

## The Tools

All three live in the viewport Toolbar. Pick a tool, then click in the viewport to start drawing. The tool's **Offset Value** is shown in its tool settings.

### Poly Draw  (P)
1. **LMB** to place each point
2. **Hold Ctrl** to constrain a segment to the current angle increment (**Ctrl+Scroll** to change it)
3. **Enter / RMB** — commit as an **open polyline** (edge chain)
4. **Alt+RMB** — **close the loop and fill a polygon** (N-Gon face)
5. **Ctrl+Z** to remove the last placed point · **Esc** to cancel

### Bézier  (B)
1. **LMB click** to place a corner point; **LMB click-drag** to place a smooth point and pull its handles
2. **Enter / RMB** — commit the curve **open**
3. **Alt+RMB** — close with a **sharp** corner at the seam
4. **Shift+Alt+RMB** — close with a **smooth** tangent at the seam
5. **Ctrl+Z** / **Esc** as above

> The very first point (the click that activates the tool) is placed as a corner. Place subsequent points with click-drag for smooth handles, or adjust any point's handle afterward by hovering and dragging it.
>
> **Ctrl while click-dragging a Bézier point** snaps the handle direction to the angle increment (default 5°, change live with Ctrl+Scroll).

---

## Alignment Guides

While drawing **or editing**, when the moving point lines up — **vertically or horizontally on screen** — with an existing point, it **snaps** onto that alignment and a **magenta guide line** is drawn back through the point you're aligning with. Both axes can engage at once, so the last corner of a rectangle locks onto the first point's column *and* the previous point's row — then **Alt+RMB** closes a perfect rectangle. Dragging a vertex/anchor of a committed shape aligns the same way (Bézier handles are excluded — they follow the curve).

It cooperates with **Ctrl angle-snap**: Ctrl keeps your segment axis-aligned, and the guide tells you when you've reached an existing point's height/width.

Press **G** while drawing to cycle the guide mode (shown in the header):

| Mode | Aligns with |
|------|-------------|
| **All shapes** (default) | The current shape **and** any other visible mesh / curve in the scene |
| **Current only** | Only points on the shape being drawn |
| **Off** | No alignment guides |

The default mode is set by the **Alignment Guides** preference; **G** changes it live.

---

## Editing — Roll Over and Drag

The primary way to edit is **hover-to-grab**, with **no modifier keys**:

1. Move the cursor over any **point, vertex, or Bézier handle** — it highlights with a **green dot**
2. **Click-drag** it to move
3. **Release** to drop

This works on **in-progress points while drawing** and on **committed geometry** (after a commit, or after picking a shape with **Q**). Clicking on empty space instead places a new point / starts a new shape.

The hover radius is set by the **Edit Roll-Over Tolerance** preference (default 4 px).

---

## After Committing (Edit Phase)

After you commit a shape it stays selected and highlighted, ready to edit. From here:

| Action | Result |
|--------|--------|
| Hover a point/handle + **drag** | Move it (roll-over editing) |
| **Scroll** | Offset / scale the shape (view-aware) |
| **Alt+Scroll** | Adjust the offset value (±1 mm; **Shift+Alt** ±10 mm) |
| **LMB** on empty space | Start a new shape |
| **Shift+LMB** | Draw a shape to union into this one (append) |
| **Ctrl+LMB** | Draw a hole / cut into this shape |
| **Alt+RMB** | Close/open the committed curve (sharp) or fill/clear a polygon |
| **Shift+Alt+RMB** | Close/open a curve with a smooth seam |
| **Q** | Pick a different curve/mesh to edit |
| **Ctrl+Z** | Undo the last committed operation (incl. a post-commit close) |
| **Esc** | Exit |

You can also start a tool with a mesh or curve already selected — it enters the edit phase immediately, ready for an append, hole, or roll-over edit.

---

## Pick Mode (Q)

Press **Q** at any time (it's a global Object-Mode shortcut, so it works even before the tool has been used):

- **Move the mouse** over any curve or mesh — it highlights in **green**
- **Click** it to select and edit it immediately
- **Click empty space** or **Esc** — cancels the pick

Q works repeatedly — you can hop from editing one curve to picking and editing another.

---

## Holes & Cutting

After committing, in the edit phase, **Ctrl+LMB** starts a cut targeting the previous shape:

- **Solid mesh** → a Boolean Difference prism is built and applied
- **Edge-only polyline** → vertices inside the drawn shape are deleted; edges crossing the boundary are split cleanly at the intersection
- **Bézier curve** → control points inside the shape are removed; segments crossing the boundary get a new point inserted at the intersection (Bézier handles recomputed via de Casteljau so the curve shape is preserved up to the cut)

Draw the closed cutting shape, then **Enter / RMB** to apply.

---

## Vertex / Point Editing Reference

| Input | Action |
|-------|--------|
| Hover + **LMB drag** | Move the point/vertex/handle (no modifier — primary method) |
| **Ctrl+Shift** + LMB drag | Move a vertex (legacy modifier alternative) |
| **Alt+Shift** + LMB | Delete the hovered vertex |
| **Ctrl+Alt+Shift** + LMB | Insert a vertex on the nearest edge, then drag to reshape |

A **green dot** marks the hovered point; a **cyan dot** marks the nearest edge-insertion point. These work on both in-progress and committed geometry.

---

## Preferences

**Edit → Preferences → Add-ons → BB Poly Draw** (expand the entry):

| Setting | Default | Description |
|---------|---------|-------------|
| **Default Offset** | 1 mm | Offset distance new/opened files start with (used by offset scrolling) |
| **Edit Roll-Over Tolerance** | 4 px | Screen-space radius for hovering a point/handle to grab it |
| **Alignment Guides** | All shapes | Which points alignment guides snap to: All shapes / Current only / Off (toggle live with **G**) |
| **X / Y / Z Axis Keys** | `X` / `Y` / `Z`, no modifiers | Key (+ optional Ctrl/Shift/Alt) that arms axis-lock numeric entry for each axis — rebind to avoid clashing with other shortcuts |

---

## Offset Behaviour

In the edit phase, **Scroll** offsets or scales the shape depending on the view:

| View | Behaviour |
|------|-----------|
| Front / Back ortho | Translates along Y |
| Top / Bottom ortho | Translates along Z |
| Left / Right ortho | Translates along X |
| Perspective / Camera | Scales ±2 % from the camera origin |

**Alt+Scroll** adjusts the offset *distance* itself (±1 mm, or ±10 mm with Shift).

---

## Snap Support

### Blender Snap (`Shift+Tab`)
A **yellow dot** appears at the cursor when a snap target is active.

| Snap Mode | Behaviour |
|-----------|-----------|
| **Vertex** | Snaps to the nearest mesh vertex within 20 px |
| **Edge** | Snaps to the closest point on the nearest edge |
| **Edge Midpoint** | Snaps to the midpoint of the nearest edge |
| **Face** | Snaps to the ray-cast surface hit |
| **Grid** | Snaps to the adaptive viewport grid at any zoom level |

### Quick snap toggles (`V` / `C` / `X`)
Toggle individual snap elements on/off without opening the snap menu — works while drawing **and** while editing:

| Key | Toggles |
|-----|---------|
| `V` | Snap to **Vertex** |
| `C` | Snap to **Edge** |
| `X` | Snap to **Grid** |

Each key is **exclusive** — turning one on turns the other two off, so you can't end up stacked in a confusing mixed vertex+edge+grid mode. Pressing the already-active key again turns snapping off (and remembers it, so the next press re-enables the same one). The master snap flag follows automatically, and the header shows the live state (e.g. `V/C/X snap: Vert`).

### Viewport shading (`4` / `5`)
| Key | Action |
|-----|--------|
| `4` | Switch the viewport to **Wireframe** shading |
| `5` | Switch the viewport to **Solid** shading |

### Angle Snap (`Ctrl`)
Hold `Ctrl` while drawing to constrain the segment to the nearest angle increment from world X in the view plane. Default **5°**, adjustable 1°–90° via `Ctrl+Scroll`.

---

## Axis-Lock Numeric Entry

While drawing (Poly Draw, N-Gon, Hole, Bézier), press an axis key — default `X` / `Y` / `Z` — then type a distance and `Enter` to add the next point offset that far along the world axis from the last placed point:

`X` → `2` `2` → `Enter` adds a point 22 units along +X from the previous point.

- Needs a previous point to measure from, so it engages from the second point onward
- `Backspace` edits the typed value, `-` toggles negative, `.` for decimals
- `Esc` cancels just the numeric entry, not the whole tool
- The header shows the live typed value (`Axis lock X: 22_`)

Each axis key — and an optional Ctrl / Shift / Alt modifier — is rebindable in **Preferences**, so it can't collide with the `V`/`C`/`X` snap toggles or any other key you've remapped.

---

## Keyboard Shortcuts

### While drawing
| Key | Action |
|-----|--------|
| `LMB` | Place a point (Bézier: click = corner, click-drag = smooth) |
| Hover + `LMB` drag | Move an already-placed point |
| `Ctrl` (hold) | Snap the segment to the angle increment (Bézier: also snaps the handle) |
| `Ctrl` + `Scroll` | ±1° increment (`Shift+Ctrl+Scroll` = ±5°) |
| `G` | Cycle alignment-guide mode (All → Current → Off) |
| `X` / `Y` / `Z` (configurable) | Arm axis-lock numeric entry, then type a distance + `Enter` |
| `V` / `C` / `X` | Toggle snap to Vertex / Edge / Grid (exclusive) |
| `4` / `5` | Viewport shading: Wireframe / Solid |
| `Alt` + `Scroll` | Adjust offset value ±1 mm (`Shift+Alt` = ±10 mm) |
| `Ctrl` + `Z` | Remove last placed point |
| `Enter` / `RMB` | Commit (open polyline / open curve) |
| `Alt` + `RMB` | Poly Draw: close + fill polygon · Bézier: close sharp |
| `Shift` + `Alt` + `RMB` | Bézier: close with smooth seam |
| `Esc` | Cancel |

### Edit phase (after committing)
| Key | Action |
|-----|--------|
| Hover + `LMB` drag | Move point / handle / vertex |
| `Scroll` | Offset / scale the shape |
| `Alt` + `Scroll` | Adjust offset value |
| `LMB` (empty) | Start a new shape |
| `Shift` + `LMB` | Append / union a new shape |
| `Ctrl` + `LMB` | Cut a hole into the shape |
| `Alt` + `RMB` | Close/open the curve (sharp) or fill/clear a polygon |
| `Shift` + `Alt` + `RMB` | Close/open a curve with a smooth seam |
| `R` | Reverse the curve's direction (so `Shift`+`LMB` continues from the other end) |
| `V` / `C` / `X` | Toggle snap to Vertex / Edge / Grid (exclusive) |
| `4` / `5` | Viewport shading: Wireframe / Solid |
| `Q` | Pick another shape to edit |
| `Ctrl` + `Z` | Undo last committed operation |
| `Esc` | Exit |

### Vertex editing (any time)
| Key | Action |
|-----|--------|
| Hover + `LMB` drag | Move the point/handle/vertex |
| `Ctrl+Shift` + `LMB` drag | Move a vertex (legacy) |
| `Alt+Shift` + `LMB` | Delete hovered vertex |
| `Ctrl+Alt+Shift` + `LMB` | Insert vertex on edge and drag |

### Global (Object Mode — always available)
| Key | Action |
|-----|--------|
| `Q` | Enter pick mode — hover and click any curve/mesh to edit it |

---

## Technical Notes

- Each committed mesh shape creates a new object named `PolyDraw`; curves are `BezierDraw`
- The drawing plane resets with each new shape, so every shape picks its own plane
- Append uses a pure 2D polygon union algorithm (no Boolean modifier) so coplanar faces merge cleanly
- Hole cutting for solid meshes builds a prism spanning the target's bounding volume so the Boolean cuts all the way through
- Cutting a polyline uses a 2D point-in-polygon test on the hole polygon's plane; cutting a curve works directly on control points (Bézier boundary segments split via de Casteljau)
- **Sharp close (Alt+RMB):** Bézier sets VECTOR handle types at the seam. **Smooth close (Shift+Alt+RMB)** keeps tangent continuity at the seam.
- VECTOR (sharp) handles are displayed/hit-tested using geometrically correct positions computed from neighbouring points, not the stored RNA value which Blender's incremental recalc can leave collapsed. Dragging a VECTOR handle converts only that side to FREE.
- Pick-mode hover traces the actual geometry — Bézier segments are evaluated with the cubic formula, poly splines are linearly interpolated
- All point/handle indicators are drawn as view-aligned triangle fans rather than `point_size_set`, which is silently ignored on Metal and some Vulkan backends
- Objects drawn in perspective/camera view have their origin placed at the camera so scroll-to-scale and edits pivot from the camera (mesh and Bézier alike)
- The viewport draw handler is registered once at add-on load (not per-operator) and reads a module-level state dict, making it immune to Blender's operator RNA lifecycle
- Tool icons are Blender geometry (`VCO`) `.dat` icons carrying a letter badge — **P** (Poly Draw), **B** (Bézier)
- Clicks on the toolbar, header, N-panel, or any UI region pass through to Blender normally

---

## License

[GPL-3.0-or-later](https://spdx.org/licenses/GPL-3.0-or-later.html)
