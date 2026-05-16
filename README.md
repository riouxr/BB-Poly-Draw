# BB Poly Draw

A Blender 5.0+ extension for fast, interactive polyline and n-gon drawing directly in the 3D viewport — with boolean hole cutting, polyline trimming, curve trimming, 2D polygon union, full vertex editing, smart view-aware offsetting, full snap support, angle-constrained drawing, single-step undo, and a full custom curve editing mode.

**Authors:** Blender Bob & Claude.ai

---

## Features

- **Polyline** — click to place vertices and build open edge chains
- **N-Gon** — same workflow, closes into a filled face on commit
- **NURBS** — click to place control points and commit a smooth NURBS spline
- **Bézier** — click for corner points, click-drag for smooth points with live handle pulling
- **Append** — Shift+LMB after committing to union a new shape into the previous one using clean 2D polygon math (no Boolean modifier, no leftover geometry)
- **Holes / Cut** — Ctrl+LMB after committing to draw a hole into the previous shape; uses Boolean Difference for solid meshes, 2D point-in-polygon trimming for polylines, and direct control-point removal with boundary splitting for NURBS and Bézier curves
- **Vertex nudge** — Ctrl+Shift: hover and drag any vertex, constrained to the polygon's plane (works in perspective and ortho)
- **Vertex delete** — Alt+Shift+LMB: delete the hovered vertex
- **Vertex insert** — Ctrl+Alt+Shift+LMB: click on any edge to insert a vertex there, then drag immediately to reshape
- **Re-invoke on selection** — if a mesh or curve is already selected when the tool starts, it enters nudge mode immediately so you can append or cut right away
- **View-aware offset** — auto-detects the correct axis from your viewport; scales from camera in perspective mode
- **Alt+RMB** — close a polyline into a loop; on NURBS and Bézier, closes with a smooth tangent at the seam. In edit mode, **toggles** the curve open or closed
- **Shift+Alt+RMB** — close a NURBS or Bézier spline with a sharp corner at the seam. In edit mode, toggles the sharp-seam treatment
- **Ctrl** — snap the next segment to a configurable angle increment (default 5°)
- **Ctrl+Scroll** — adjust the snap angle increment live
- **Ctrl+Z** — undo: removes the last placed point while drawing, or undoes the last committed shape / append / hole during nudge
- **E** — enter BB PolyDraw edit mode on the selected curve object (without using Blender's native Edit Mode)
- **Q** — enter pick mode: hover any curve in the viewport to highlight it, then click anywhere on it to edit it
- **Full snap support** — respects Blender's snap settings (Vertex, Edge, Edge Midpoint, Face, Grid) with adaptive grid spacing that matches the visible grid at any zoom level
- **Flat drawing plane** — first click locks a coplanar surface so all points stay flat in perspective view
- **Perspective origin** — objects drawn in perspective have their origin placed at the camera so scroll-to-scale pivots correctly
- Live rubber-band preview while drawing
- Yellow snap indicator dot when a Blender snap target is active
- Cyan dot indicator showing edge insertion point when Ctrl+Alt+Shift is held

---

## Requirements

- Blender **5.0.0** or newer

---

## Installation

1. Download `bb_poly_draw.zip`
2. In Blender: **Edit → Preferences → Get Extensions**
3. Click the **▾** dropdown (top right) → **Install from Disk**
4. Select `bb_poly_draw.zip`
5. Enable the extension
6. Open the **N-Panel** in the 3D Viewport (`N` key) → **BB Poly Draw** tab

---

## Usage

### Polyline
1. Click **Polyline** in the panel
2. **LMB** to place each point in the viewport
3. **Hold Ctrl** to constrain the segment to the current angle increment
4. **Ctrl+Scroll** to change the snap increment live
5. **Enter** or **RMB** to commit the edge chain
6. **Alt+RMB** to close the chain into a loop before committing
7. **Ctrl+Z** to remove the last placed point
8. **Esc** to cancel

### N-Gon
1. Click **N-Gon** in the panel
2. **LMB** to place each point
3. **Hold Ctrl** to constrain a segment to the current angle increment
4. **Enter** or **RMB** to commit — the shape closes into a filled face
5. **Ctrl+Z** to remove the last placed point
6. **Esc** to cancel

### NURBS
1. Click **NURBS** in the panel
2. **LMB** to place each control point in the viewport
3. **Enter** or **RMB** to commit the spline open
4. **Alt+RMB** to close the spline with a smooth tangent at the seam
5. **Shift+Alt+RMB** to close the spline with a sharp corner at the seam
6. **Ctrl+Z** to remove the last placed control point
7. **Esc** to cancel

### Bézier
1. Click **Bézier** in the panel
2. **LMB click** to place a corner point; **LMB click-drag** to place a smooth point and pull its handles
3. **Enter** or **RMB** to commit the curve open
4. **Alt+RMB** to close the curve with a smooth tangent at the seam
5. **Shift+Alt+RMB** to close the curve with a sharp corner at the seam (VECTOR handles at the join)
6. **Ctrl+Z** to remove the last placed point
7. **Esc** to cancel

---

After committing a shape the tool enters **nudge mode**. From there:
1. **Shift+LMB** in the viewport to draw a new shape and union it into the previous one
2. Draw your new shape, then **Enter** or **RMB** to commit

You can also start the tool with a mesh already selected — it enters nudge mode immediately, ready for an append or hole.

### Holes (solid mesh)
After committing, in nudge mode:
1. **Ctrl+LMB** to enter Hole mode targeting the previous shape
2. Draw a closed shape over the area to cut
3. **Enter** or **RMB** to commit — a Boolean Difference prism is applied and the cutter removed

### Cut (polyline)
Same workflow as Holes, but when the target is an **edge-only mesh** — vertices inside the shape are deleted, edges crossing the boundary are split cleanly at the intersection point.

### Cut (NURBS / Bézier curve)
Same workflow as Holes, but when the target is a **NURBS or Bézier curve object** — control points inside the drawn shape are removed, and segments that cross the boundary get a new control point inserted at the intersection. For Bézier curves, handles at the cut boundary are recomputed using de Casteljau subdivision so the curve shape is preserved up to the cut.

---

### Curve Edit Mode (E)

Press **E** in Object Mode with a Bézier or NURBS curve selected to enter BB PolyDraw's own edit mode — no Blender Edit Mode required.

In curve edit mode:
- **Ctrl+Shift+drag** — move any anchor point or handle
- **Ctrl+Alt+Shift+LMB** — insert a new control point on a segment
- **Alt+RMB** — toggle the curve open/closed (smooth seam)
- **Shift+Alt+RMB** — toggle closed with a sharp corner at the seam
- **LMB near a control point/handle** — stays in edit mode
- **LMB on empty space** — exits edit mode and starts drawing a new curve there
- **Esc** — exits edit mode entirely

Sharp corner handles (VECTOR type) are fully selectable and draggable. Dragging one converts only that handle to FREE, leaving the opposite handle auto-computed — so one side of the corner stays sharp while the other curves freely.

---

### Pick Mode (Q)

Press **Q** at any time (including before drawing anything) to enter pick mode.

- **Move the mouse** over any curve — it highlights in green as you hover
- **Click anywhere on the curve** to select it and enter edit mode immediately
- **Click on empty space** — cancels pick without changing anything
- **Esc** — cancels pick mode

Q works as a global Object Mode shortcut, so it fires even if the BB PolyDraw tool has not yet been used in the current session.

---

### Vertex Editing
Available at any time while the tool is active. A **green dot** indicates the hovered vertex; a **cyan dot** indicates the nearest edge insertion point.

#### Nudge (move) a vertex — Ctrl+Shift
1. Hold **Ctrl+Shift** — a green dot appears on the nearest vertex
2. **LMB** to grab it (turns white)
3. **Move the mouse** — the vertex follows, constrained to the polygon's plane
4. **Release LMB** to drop

#### Delete a vertex — Alt+Shift
1. Hold **Alt+Shift** — a green dot appears on the nearest vertex
2. **LMB** to delete it

#### Insert a vertex on an edge — Ctrl+Alt+Shift
1. Hold **Ctrl+Alt+Shift** — a cyan dot tracks the nearest point on any edge
2. **LMB** to insert a vertex there — it is immediately grabbed
3. **Drag** to reshape while still holding the button
4. **Release LMB** to drop

All three operations work on both in-progress drawn points and committed mesh vertices/edges.

### Undo
- **Ctrl+Z while drawing** — removes the last placed point
- **Ctrl+Z during nudge** — undoes the last committed operation (new shape, append, or hole)

### Offset
The panel shows a live label indicating what the offset buttons will do based on the current view:

| View | Label | Behaviour |
|------|-------|-----------|
| Front / Back ortho | `Offset axis: Y` | Translates along Y |
| Top / Bottom ortho | `Offset axis: Z` | Translates along Z |
| Left / Right ortho | `Offset axis: X` | Translates along X |
| Perspective / Camera | `Persp: scales from camera` | Scales ±2% from camera origin |

---

## Panel Layout

```
┌─────────────────────────┐
│  BB Poly Draw           │
├─────────────────────────┤
│  [Offset Value ──────]  │
│                         │
│  [ Polyline ] [ N-Gon ] │
│                         │
│  Offset axis: Y  (auto) │
│  [ Offset − ][ Offset + ]│
└─────────────────────────┘
```

---

## Keyboard Shortcuts (while drawing)

| Key | Action |
|-----|--------|
| `LMB` | Place a point |
| `Hold Ctrl` + `LMB` | Place a point snapped to current angle increment |
| `Ctrl` + `Scroll` | ±1° angle increment per tick |
| `Ctrl` + `Shift` + `Scroll` | ±5° angle increment per tick |
| `Ctrl` + `Z` | Remove last placed point |
| `Enter` / `RMB` | Commit shape |
| `Alt` + `RMB` | Close loop (smooth seam — Polyline, NURBS, Bézier) |
| `Shift` + `Alt` + `RMB` | Close loop with sharp corner at seam (NURBS, Bézier only) |
| `Esc` | Cancel and exit |

## Keyboard Shortcuts (nudge phase — after committing)

| Key | Action |
|-----|--------|
| `Scroll` | Move / scale the last shape |
| `LMB` (viewport) | Start drawing next shape |
| `Shift` + `LMB` | Draw a shape to union into the previous one |
| `Ctrl` + `LMB` | Draw a hole / cut into the previous shape |
| `E` | Edit the active curve object in BB PolyDraw mode |
| `Q` | Enter pick mode — click any curve to edit it |
| `Ctrl` + `Z` | Undo last committed operation |
| `Esc` | Exit drawing entirely |

## Keyboard Shortcuts (curve edit mode — after E or Q pick)

| Key | Action |
|-----|--------|
| `Ctrl+Shift` + drag | Move anchor point or handle |
| `Ctrl+Alt+Shift` + `LMB` | Insert control point on segment |
| `Alt` + `RMB` | Toggle curve open / closed (smooth seam) |
| `Shift` + `Alt` + `RMB` | Toggle closed with sharp corner at seam |
| `LMB` near control point | Stay in edit mode |
| `LMB` on empty space | Exit edit mode, start new curve |
| `Q` | Enter pick mode to switch to a different curve |
| `Esc` | Exit edit mode |

## Keyboard Shortcuts (vertex editing — any time)

| Key | Action |
|-----|--------|
| `Hold Ctrl+Shift` | Hover nearest vertex (green dot) |
| `Ctrl+Shift` + `LMB` | Grab and drag vertex along polygon plane |
| `Hold Alt+Shift` | Hover nearest vertex (green dot) |
| `Alt+Shift` + `LMB` | Delete hovered vertex |
| `Hold Ctrl+Alt+Shift` | Show nearest edge insertion point (cyan dot) |
| `Ctrl+Alt+Shift` + `LMB` | Insert vertex on edge and drag immediately |

## Global Shortcuts (Object Mode — always available)

| Key | Action |
|-----|--------|
| `E` | Edit selected curve in BB PolyDraw mode |
| `Q` | Enter pick mode — hover and click any curve to edit it |

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
| **Grid** | Snaps to the adaptive viewport grid — matches visible grid lines at any zoom level |

### Angle Snap (`Ctrl`)
Hold `Ctrl` while drawing to constrain the current segment to the nearest angle increment from world X in the view plane. Default **5°**, adjustable 1°–90° via scroll wheel.

---

## Technical Notes

- Each committed shape creates a new mesh object named `PolyDraw`
- The drawing plane resets with each new shape so every shape picks its own plane
- Append uses a pure 2D polygon union algorithm — no Boolean modifier — so coplanar faces merge cleanly
- Hole cutting for solid meshes builds a prism that fully spans the target's bounding volume, ensuring the Boolean Difference cuts all the way through
- The Cut tool for edge-only polylines uses a 2D point-in-polygon test projected onto the hole polygon's plane
- The Cut tool for NURBS and Bézier curves works directly on the spline control points — no mesh conversion. Bézier segments crossing the boundary are split via de Casteljau subdivision so handles at the cut point are geometrically correct
- **Sharp close (Shift+Alt+RMB):** for Bézier, sets VECTOR handle types on the first and last points at the seam. For NURBS, appends `degree − 1` duplicate copies of the first control point at the end to raise knot multiplicity at the seam — the standard technique for a NURBS corner
- **Curve edit mode:** VECTOR handles (sharp corners) are displayed and hit-tested using geometrically correct positions computed from neighbouring control points, rather than the stored RNA value which Blender's incremental recalculation can leave collapsed. Dragging a VECTOR handle converts only that side to FREE, preserving the opposite side's auto-computed sharp corner
- **Pick mode curve sampling:** hover detection traces the actual curve geometry — Bézier segments are evaluated with the cubic formula (16 steps per segment), NURBS/poly splines are linearly interpolated. Click threshold is 20 px from the curve line
- **Dot rendering:** all control point and handle indicators are drawn as view-aligned triangle fans rather than using `point_size_set`, which is silently ignored on Metal and some Vulkan backends
- Objects drawn in perspective/camera view have their origin placed at the camera position, so scroll-to-scale and nudge operations pivot from the camera — this applies equally to mesh, Bézier, and NURBS objects
- The viewport draw handler is registered once at addon load time (not per-operator) and reads from a module-level state dict, making it immune to Blender's operator RNA lifecycle
- Clicks on the N-panel, toolbar, header, or any UI region are passed through to Blender normally

---

## License

[GPL-3.0-or-later](https://spdx.org/licenses/GPL-3.0-or-later.html)
