# BB Poly Draw

A Blender 4.2+ extension for fast, interactive polyline and n-gon drawing directly in the 3D viewport — with boolean hole cutting, polyline trimming, smart view-aware offsetting, full snap support, and angle-constrained drawing. A cleaner alternative to Blender's built-in Poly Build tool.

**Authors:** Blender Bob & Claude.ai

---

## Features

- **Polyline** — click to place vertices and build open edge chains
- **N-Gon** — same workflow, closes into a filled face on commit
- **Holes / Cut** — draw a closed shape over a mesh to punch a boolean hole, or over a polyline to trim the segment inside the drawn area
- **View-aware offset** — auto-detects the correct axis from your viewport; scales from camera in perspective mode
- **Alt+RMB** — close a polyline into a loop without filling it
- **Ctrl** — snap the next segment to a configurable angle increment (default 5°)
- **Ctrl + Scroll** — adjust the snap angle increment live (1° steps; Shift for 5° steps)
- **Full snap support** — respects Blender's snap settings (Vertex, Edge, Edge Midpoint, Face, Increment)
- **Flat drawing plane** — first click locks a coplanar surface so all points stay flat in perspective view
- **Perspective origin** — objects drawn in perspective have their origin placed at the camera
- Live rubber-band preview while drawing
- Yellow snap indicator dot when a Blender snap target is locked

---

## Requirements

- Blender **4.2.0** or newer (Extension system)

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
4. **Ctrl + Scroll** to change the snap increment live
5. **Enter** or **RMB** to commit the edge chain
6. **Alt+RMB** to close the chain into a loop before committing
7. **Esc** to cancel

### N-Gon
1. Click **N-Gon** in the panel
2. **LMB** to place each point
3. **Hold Ctrl** to constrain a segment to the current angle increment
4. **Enter** or **RMB** to commit — the shape closes into a filled face
5. **Esc** to cancel

### Holes (solid mesh)
1. Select and activate the **target mesh** you want to cut into
2. Click **Holes**
3. Draw a closed shape over the target area
4. **Enter** or **RMB** to commit — a Boolean Difference is applied and the cutter is removed automatically

### Cut (polyline)
When a **polyline** (edge-only mesh) is active, the button changes to **Cut**:
1. Activate the polyline you want to trim
2. Click **Cut**
3. Draw a closed shape over the section you want removed
4. **Enter** or **RMB** to commit — vertices inside the shape are deleted, edges crossing the boundary are split cleanly at the intersection point

### Offset
The panel shows a live label indicating what the offset buttons will do based on the current view:

| View | Label | Behaviour |
|------|-------|-----------|
| Front / Back ortho | `Offset axis: Y` | Translates along Y |
| Top / Bottom ortho | `Offset axis: Z` | Translates along Z |
| Left / Right ortho | `Offset axis: X` | Translates along X |
| Perspective / Camera | `Persp: scales from camera` | Scales ±2% from camera origin |

Simply click **Offset −** or **Offset +** — no axis selection needed.

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
│  [   Holes / Cut      ] │
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
| `Ctrl + LMB` | Place a point snapped to current angle increment |
| `Ctrl + Scroll Up` | Increase angle increment by 1° |
| `Ctrl + Scroll Down` | Decrease angle increment by 1° |
| `Ctrl + Shift + Scroll` | Change angle increment by 5° |
| `Enter` / `RMB` | Commit shape |
| `Alt + RMB` | Close polyline into a loop (Polyline mode only) |
| `Esc` | Cancel and exit |
| `Shift + Tab` | Toggle Blender snap on/off |

## Keyboard Shortcuts (after committing a shape)

After committing, the tool enters a **nudge phase** — the last shape can be repositioned before starting the next one:

| Key | Action |
|-----|--------|
| `Scroll Up` | Move forward / scale up (2% per tick in perspective) |
| `Scroll Down` | Move back / scale down (2% per tick in perspective) |
| `LMB` (viewport) | Exit nudge and start drawing next shape |
| `Esc` | Exit drawing entirely |

---

## Snap Support

BB Poly Draw offers two independent snapping systems that do not interfere with each other.

### Blender Snap (magnet icon / `Shift+Tab`)
Enabled via Blender's standard snap header. A **yellow dot** appears at the cursor when a snap target is active.

| Snap Mode | Behaviour |
|-----------|-----------|
| **Vertex** | Snaps to the nearest mesh vertex within 20 px |
| **Edge** | Snaps to the closest point on the nearest edge |
| **Edge Midpoint** | Snaps to the midpoint of the nearest edge |
| **Face** | Snaps to the ray-cast surface hit |
| **Increment** | Snaps to the viewport grid |

### Angle Snap (`Ctrl`)
Hold `Ctrl` while drawing to constrain the current segment to the nearest angle increment, measured from world X in the view plane. Default is **5°**, adjustable from **1° to 90°** via scroll wheel.

| Action | Result |
|--------|--------|
| `Ctrl + Scroll Up/Down` | ±1° per tick |
| `Ctrl + Shift + Scroll Up/Down` | ±5° per tick |

The current increment is shown live in the viewport header while drawing.

---

## Perspective Drawing

When drawing in a **perspective or camera** viewport:
- The **first click** locks a flat drawing plane perpendicular to the view — all subsequent points snap to that plane so the shape stays coplanar
- The created object's **origin is placed at the camera position**
- **Offset −/+** and **scroll nudge** both scale the object uniformly ±2% per step from the camera origin — keeping the apparent screen size identical while changing depth order
- Scaling up moves the shape visually closer; scaling down pushes it further away

---

## Notes

- Each committed shape creates a new mesh object named `PolyDraw`
- The drawing plane resets with each new shape so every shape picks its own plane
- The Holes cutter uses a Solidify thickness of 10 m — sufficient for any typical mesh thickness
- The Cut tool for polylines uses a 2D point-in-polygon test projected onto the hole polygon's plane, so it works correctly regardless of the polyline's orientation in 3D space

---

## License

[GPL-2.0-or-later](https://spdx.org/licenses/GPL-2.0-or-later.html)
