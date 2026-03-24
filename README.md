# BB Poly Draw

A Blender 4.2+ extension for fast, interactive polyline and n-gon drawing directly in the 3D viewport — with boolean hole cutting, polyline trimming, mesh offsetting, full snap support, and angle-constrained drawing. A cleaner alternative to Blender's built-in Poly Build tool.

**Authors:** Blender Bob & Claude.ai

---

## Features

- **Polyline** — click to place vertices and build open edge chains
- **N-Gon** — same workflow, closes into a filled face on commit
- **Holes / Cut** — draw a closed shape over a mesh to punch a boolean hole, or over a polyline to trim the segment inside the drawn area
- **Offset** — translate selected mesh objects along any combination of X / Y / Z axes by a precise distance
- **Alt+RMB** — close a polyline into a loop without filling it
- **Ctrl** — snap the next segment to the nearest 5° angle (when Blender snap is off)
- **Full snap support** — respects Blender's snap settings (Vertex, Edge, Edge Midpoint, Face, Increment)
- Live rubber-band preview while drawing
- Yellow snap indicator dot shows when a snap target is locked

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
3. **Hold Ctrl** before placing a point to snap the segment to the nearest 5° angle
4. **Enter** or **RMB** to commit the edge chain
5. **Alt+RMB** to close the chain into a loop before committing
6. **Esc** to cancel

### N-Gon
1. Click **N-Gon** in the panel
2. **LMB** to place each point
3. **Hold Ctrl** to constrain a segment to the nearest 5° angle
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
1. Select one or more mesh objects
2. Set the **Offset Value** with the slider
3. Toggle the **X / Y / Z** axes you want to move along
4. Click **Offset −** or **Offset +**

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
│  [  X  ] [  Y  ] [  Z  ]│
│  [ Offset − ][ Offset + ]│
└─────────────────────────┘
```

---

## Keyboard Shortcuts (while drawing)

| Key | Action |
|-----|--------|
| `LMB` | Place a point |
| `Ctrl + LMB` | Place a point snapped to the nearest 5° angle |
| `Enter` / `RMB` | Commit shape |
| `Alt + RMB` | Close polyline into a loop (Polyline mode only) |
| `Esc` | Cancel and exit |
| `Shift + Tab` | Toggle Blender snap on/off |

---

## Snap Support

BB Poly Draw offers two independent snapping systems:

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
Hold `Ctrl` while drawing to constrain the current segment to the nearest **5° increment**, measured from world X in the view plane. Only active when Blender snap is off — the two systems do not interfere with each other.

---

## Notes

- Each committed shape creates a new mesh object named `PolyDraw`
- Points snap to visible surface geometry; if no surface is hit, they land at the depth of the 3D Cursor
- The Holes cutter uses a Solidify thickness of 10 m — sufficient for any typical mesh thickness
- The Cut tool for polylines uses a 2D point-in-polygon test projected onto the hole polygon's plane, so it works correctly regardless of the polyline's orientation in 3D space
- Offset moves whole objects, not individual vertices — use Blender's native Shrink/Fatten (`Alt+S` in Edit Mode) for vertex-level offsetting

---

## License

[GPL-2.0-or-later](https://spdx.org/licenses/GPL-2.0-or-later.html)
