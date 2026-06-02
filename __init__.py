"""
BB Poly Draw — Blender Add-on (LEGACY build, Blender 3.4 only)
Viewport Toolbar (T) › Poly Draw / Bézier
Authors: Blender Bob & Claude.ai

This is the legacy add-on build for Blender 3.4 (uses bl_info, install via
Install from File). For Blender 4.2+ use the official Extension build on `main`.
"""

bl_info = {
    "name": "BB Poly Draw",
    "author": "Blender Bob & Claude.ai",
    "version": (1, 8, 2),
    "blender": (3, 4, 0),
    "location": "View3D > Toolbar (T) > Poly Draw / Bézier",
    "description": "Interactive polyline / polygon and Bézier drawing with "
                   "roll-over editing, hole cutting, append, and view-aware offset.",
    "category": "Mesh",
}

import pathlib
import math
import os
import bpy
import bmesh
import gpu
from collections import defaultdict
from gpu_extras.batch import batch_for_shader
from mathutils import Vector
import bpy.utils.previews
from bpy_extras import view3d_utils

# Blender 3.x built-in GPU shader name (this legacy build targets 3.4 only;
# 4.0+ dropped the '3D_' prefix — use the Extension build on `main` there).
_UNIFORM_COLOR_SHADER = '3D_UNIFORM_COLOR'

_preview_collections = {}

# ═══════════════════════════════════════════════════════════════
#  Module-level draw state — no operator references, safe from RNA freeing
# ═══════════════════════════════════════════════════════════════

_DRAW_STATE = {'pts': [], 'mouse': None, 'snap_on': False,
               'vn_hover': None, 'vn_grab': None, 'vn_edge_pt': None,
               'nurbs_curve': [],
               'bezier_curve': [], 'bezier_handles': [],
               'cusp_handle_pts': [],
               'pick_hover_curve': [],
               'pick_hover_lines': [],
               'mesh_nudge_verts': []}

# Reference to the currently running draw modal (None when idle)
_active_draw_op    = None
_pending_pick_mode = False   # set by PickCurve operator when modal not yet running

# Mouse region coords of the LMB click that started the tool, set by the
# Start* operators (which see the real event) and consumed by Draw.invoke
# to place the first point without requiring a second click.
_pending_first_click = None


# ═══════════════════════════════════════════════════════════════
#  Properties
# ═══════════════════════════════════════════════════════════════

class POLYDRAW_Props(bpy.types.PropertyGroup):

    offset_value: bpy.props.FloatProperty(
        name="Offset Value",
        description="Distance applied by Offset - / Offset +",
        default=0.001, soft_min=-10.0, soft_max=10.0,
        precision=4, subtype='DISTANCE',
    )
    draw_mode: bpy.props.EnumProperty(
        name="Draw Mode",
        items=[
            ('NONE',     'None',     ''),
            ('POLYLINE', 'Polyline', ''),
            ('NGON',     'N-Gon',    ''),
            ('HOLE',     'Hole',     ''),
            ('NURBS',    'NURBS',    ''),
            ('BEZIER',   'Bézier',   ''),
        ],
        default='NONE',
    )


# ═══════════════════════════════════════════════════════════════
#  Add-on preferences
# ═══════════════════════════════════════════════════════════════

def _get_prefs(context=None):
    """Return this add-on's preferences, or None if unavailable."""
    context = context or bpy.context
    try:
        return context.preferences.addons[__package__].preferences
    except (KeyError, AttributeError):
        return None


def _on_default_offset_update(self, context):
    """Apply the new default to the active scene immediately so the change
    is visible without reopening a file."""
    scene = getattr(context, 'scene', None)
    if scene is not None and hasattr(scene, 'polydraw_props'):
        scene.polydraw_props.offset_value = self.default_offset


class POLYDRAW_AddonPreferences(bpy.types.AddonPreferences):
    bl_idname = __package__

    default_offset: bpy.props.FloatProperty(
        name="Default Offset",
        description="Offset distance new files start with (used by Offset - / Offset +)",
        default=0.001, soft_min=-10.0, soft_max=10.0,
        precision=4, subtype='DISTANCE',
        update=_on_default_offset_update,
    )

    grab_tolerance: bpy.props.IntProperty(
        name="Edit Roll-Over Tolerance",
        description="Screen-space radius (pixels) for rolling over a point or handle "
                    "to grab it in edit mode. Smaller = more precise, needs a closer aim",
        default=4, min=1, max=50, subtype='PIXEL',
    )

    def draw(self, context):
        layout = self.layout
        layout.prop(self, "default_offset")
        layout.prop(self, "grab_tolerance")


# ═══════════════════════════════════════════════════════════════
#  Snap-aware 3D position from mouse
# ═══════════════════════════════════════════════════════════════

_SNAP_PX = 20  # screen-space pixel threshold for vertex / edge snapping
_GRAB_PX = 4   # tighter threshold for hover-to-grab — keep small so points can
               # be placed close together for fine detail without grabbing


def _project_to_screen(context, world_co):
    """Return (sx, sy) screen coords for a world-space point, or None if behind camera."""
    return view3d_utils.location_3d_to_region_2d(
        context.region, context.region_data, world_co)


def _screen_dist(ax, ay, bx, by):
    return ((ax - bx) ** 2 + (ay - by) ** 2) ** 0.5


# Region types that should swallow a click (UI bars). Everything else inside the
# WINDOW region — including the overlapping toolbar (TOOLS) and N-panel (UI) when
# region-overlap is on — counts as drawable canvas, so points can be placed under
# those side panels instead of being lost.
_BLOCK_REGION_TYPES = {'HEADER', 'TOOL_HEADER', 'FOOTER', 'HUD',
                       'ASSET_SHELF', 'ASSET_SHELF_HEADER'}


def _in_draw_canvas(context, sx, sy):
    """True if the window-space click (sx, sy) lands on the 3D viewport drawing
    canvas: inside the WINDOW region and not over a header / HUD bar. Clicks over
    the overlapping toolbar or N-panel ARE on the canvas (draw-through)."""
    area = context.area
    if area is None:
        return False
    win = next((r for r in area.regions if r.type == 'WINDOW'), None)
    if not win or not (win.x <= sx < win.x + win.width and
                       win.y <= sy < win.y + win.height):
        return False
    for r in area.regions:
        if (r.type in _BLOCK_REGION_TYPES and
                r.x <= sx < r.x + r.width and r.y <= sy < r.y + r.height):
            return False
    return True


def _closest_point_on_segment(p, a, b):
    """Return the closest point on segment a→b to point p (all Vector)."""
    ab = b - a
    ll = ab.length_squared
    if ll < 1e-10:
        return a.copy()
    return a + ab * max(0.0, min(1.0, (p - a).dot(ab) / ll))


def mouse_to_3d(context, mx, my):
    """
    Return a snapped 3D position for the mouse cursor, respecting Blender's
    current snap settings (Vertex, Edge, Edge Midpoint, Face, Grid).
    Falls back to face ray-cast then 3D-cursor depth when snapping is off.
    """
    region = context.region
    rv3d   = context.region_data
    coord  = (mx, my)
    ts     = context.scene.tool_settings

    ray_origin = view3d_utils.region_2d_to_origin_3d(region, rv3d, coord)
    ray_dir    = view3d_utils.region_2d_to_vector_3d(region, rv3d, coord)

    # Baseline: face ray-cast or cursor-depth fallback
    hit, face_loc, *_ = context.scene.ray_cast(
        context.view_layer.depsgraph, ray_origin, ray_dir)
    baseline = Vector(face_loc) if hit else \
        view3d_utils.region_2d_to_location_3d(
            region, rv3d, coord, context.scene.cursor.location)

    if not ts.use_snap:
        return baseline

    snap_elements = set(ts.snap_elements)

    # ── GRID snap ───────────────────────────────────────────────
    # 'GRID' in Blender 4.x+, 'INCREMENT' in older builds
    if 'GRID' in snap_elements or 'INCREMENT' in snap_elements:
        overlay    = context.space_data.overlay if hasattr(context.space_data, 'overlay') else None
        grid_scale = overlay.grid_scale if overlay else 1.0
        grid_subs  = max(1, getattr(overlay, 'grid_subdivisions', 1)) if overlay else 1
        # Match Blender's adaptive grid: smallest power-of-10 multiple of grid_scale
        # whose screen spacing is at least ~30 px (the threshold Blender uses).
        if rv3d and rv3d.view_perspective == 'ORTHO':
            units_per_px = rv3d.view_distance * 2.0 / max(region.height, 1)
            raw_inc      = units_per_px * 30.0
            exp          = math.ceil(math.log10(raw_inc / grid_scale)) if raw_inc > grid_scale * 1e-6 else 0
            inc          = grid_scale * (10.0 ** exp) / grid_subs
        else:
            inc = grid_scale / grid_subs
        if inc < 1e-6:
            inc = 1.0
        return Vector(round(c / inc) * inc for c in baseline)

    # ── VERTEX / EDGE / EDGE_MIDPOINT snap ──────────────────────
    want_vert = 'VERTEX'        in snap_elements
    want_edge = 'EDGE'          in snap_elements
    want_mid  = 'EDGE_MIDPOINT' in snap_elements
    want_face = 'FACE'          in snap_elements

    best_dist = _SNAP_PX
    best_pos  = None
    depsgraph = context.view_layer.depsgraph

    for obj in context.visible_objects:
        if obj.type != 'MESH':
            continue
        eval_obj  = obj.evaluated_get(depsgraph)
        mesh_data = eval_obj.to_mesh()
        mw        = obj.matrix_world

        try:
            if want_vert:
                for v in mesh_data.vertices:
                    wp = mw @ v.co
                    s  = _project_to_screen(context, wp)
                    if s is None:
                        continue
                    d = _screen_dist(mx, my, s.x, s.y)
                    if d < best_dist:
                        best_dist = d
                        best_pos  = wp.copy()

            if want_edge or want_mid:
                for edge in mesh_data.edges:
                    va = mw @ mesh_data.vertices[edge.vertices[0]].co
                    vb = mw @ mesh_data.vertices[edge.vertices[1]].co

                    if want_mid:
                        mid = (va + vb) * 0.5
                        s   = _project_to_screen(context, mid)
                        if s is not None:
                            d = _screen_dist(mx, my, s.x, s.y)
                            if d < best_dist:
                                best_dist = d
                                best_pos  = mid

                    if want_edge:
                        for i in range(9):
                            pt = va.lerp(vb, i / 8)
                            s  = _project_to_screen(context, pt)
                            if s is None:
                                continue
                            d = _screen_dist(mx, my, s.x, s.y)
                            if d < best_dist:
                                # Refine in screen space — correct for ortho and persp,
                                # including edges with a depth component in ortho view.
                                sa = _project_to_screen(context, va)
                                sb = _project_to_screen(context, vb)
                                if sa and sb:
                                    ex, ey = sb.x - sa.x, sb.y - sa.y
                                    denom  = ex * ex + ey * ey
                                    t_best = max(0.0, min(1.0,
                                        ((mx - sa.x) * ex + (my - sa.y) * ey) / denom
                                    )) if denom > 1e-10 else 0.0
                                    cp = va.lerp(vb, t_best)
                                else:
                                    cp = _closest_point_on_segment(baseline, va, vb)
                                s2 = _project_to_screen(context, cp)
                                d2 = _screen_dist(mx, my, s2.x, s2.y) if s2 else d
                                best_dist = d2
                                best_pos  = cp
                                break
        finally:
            eval_obj.to_mesh_clear()

    if best_pos is None and want_face and hit:
        return Vector(face_loc)

    return best_pos if best_pos is not None else baseline


# ═══════════════════════════════════════════════════════════════
#  Angle snap and ray-plane helpers
# ═══════════════════════════════════════════════════════════════

_ANGLE_STEP_DEFAULT = 5.0


# ═══════════════════════════════════════════════════════════════
#  NURBS curve tessellation (de Boor, clamped uniform)
# ═══════════════════════════════════════════════════════════════

def _nurbs_tessellate(pts, resolution=96):
    """
    Evaluate a clamped uniform NURBS curve through control points `pts`.
    Degree is cubic when there are ≥4 points, quadratic for 3, linear for 2.
    Returns a list of (x, y, z) tuples suitable for GPU LINE_STRIP.
    """
    n = len(pts)
    if n < 2:
        return []
    if n == 2:
        return [tuple(pts[0]), tuple(pts[1])]

    p = min(3, n - 1)           # degree (cubic or lower)

    # Clamped uniform knot vector: [0]*( p+1) + interior + [1]*(p+1)
    knots = [0.0] * (p + 1)
    for i in range(1, n - p):
        knots.append(i / (n - p))
    knots += [1.0] * (p + 1)   # length = n + p + 1

    def find_span(t):
        """Return the knot span index i such that knots[i] <= t < knots[i+1]."""
        if t >= 1.0:
            # Step back from the right to skip the trailing repeated knot
            for i in range(n - 1, p - 1, -1):
                if knots[i] < 1.0:
                    return i
            return n - 1
        lo, hi = p, n
        mid = (lo + hi) // 2
        while t < knots[mid] or t >= knots[mid + 1]:
            if t < knots[mid]:
                hi = mid
            else:
                lo = mid
            mid = (lo + hi) // 2
        return mid

    def de_boor(span, t):
        d = [Vector(pts[span - p + j]) for j in range(p + 1)]
        for r in range(1, p + 1):
            for j in range(p, r - 1, -1):
                ki = j + span - p
                denom = knots[ki + p - r + 1] - knots[ki]
                alpha = (t - knots[ki]) / denom if abs(denom) > 1e-10 else 0.0
                d[j] = (1.0 - alpha) * d[j - 1] + alpha * d[j]
        return d[p]

    out = []
    for i in range(resolution + 1):
        t    = i / resolution
        span = find_span(t)
        out.append(tuple(de_boor(span, t)))
    return out


# ═══════════════════════════════════════════════════════════════
#  Bézier curve tessellation (cubic, per-segment de Casteljau)
# ═══════════════════════════════════════════════════════════════

def _bezier_tessellate(bezier_pts, resolution=24):
    """
    Tessellate a Bézier spline from a list of {'co', 'hl', 'hr'} dicts.
    Each segment is a cubic Bézier: P0, P0.hr, P1.hl, P1
    Returns a list of (x, y, z) tuples for GPU LINE_STRIP.
    """
    n = len(bezier_pts)
    if n < 2:
        return []
    out = []
    for seg in range(n - 1):
        p0  = bezier_pts[seg    ]['co']
        h0r = bezier_pts[seg    ]['hr']
        h1l = bezier_pts[seg + 1]['hl']
        p1  = bezier_pts[seg + 1]['co']
        # Include the last sample only on the final segment to avoid
        # duplicating the shared knot between adjacent segments.
        end = resolution + 1 if seg == n - 2 else resolution
        for j in range(end):
            t  = j / resolution
            mt = 1.0 - t
            pt = (mt**3 * p0
                  + 3.0 * mt**2 * t  * h0r
                  + 3.0 * mt   * t**2 * h1l
                  + t**3 * p1)
            out.append(tuple(pt))
    return out


def _bez_split(p0, h0r, h1l, p1, t):
    """De Casteljau split of a cubic Bézier segment at parameter t.
    Returns (new_h0r, new_pt_hl, new_pt_co, new_pt_hr, new_h1l) —
    everything needed to update the left anchor's outgoing handle,
    build the new mid-point, and update the right anchor's incoming handle."""
    def _l(a, b): return a + (b - a) * t
    p01  = _l(p0,  h0r);  p12  = _l(h0r, h1l);  p23  = _l(h1l, p1)
    p012 = _l(p01, p12);  p123 = _l(p12, p23)
    return p01, p012, _l(p012, p123), p123, p23


def angle_snap(raw_pos, last_pos, view_normal, step=_ANGLE_STEP_DEFAULT):
    """Constrain raw_pos to the nearest angle increment from last_pos."""
    delta = raw_pos - last_pos
    dist  = delta.length
    if dist < 1e-6:
        return raw_pos.copy()

    n  = view_normal.normalized()
    lx = Vector((1, 0, 0)) - Vector((1, 0, 0)).dot(n) * n
    if lx.length < 1e-6:
        lx = Vector((0, 1, 0)) - Vector((0, 1, 0)).dot(n) * n
    lx = lx.normalized()
    ly = n.cross(lx).normalized()

    angle_deg   = math.degrees(math.atan2(delta.dot(ly), delta.dot(lx)))
    snapped_rad = math.radians(round(angle_deg / step) * step)
    direction   = lx * math.cos(snapped_rad) + ly * math.sin(snapped_rad)
    return last_pos + direction * dist


def ray_plane_intersect(ray_origin, ray_dir, plane_origin, plane_normal):
    """Return the intersection of a ray with a plane, or None if parallel."""
    denom = ray_dir.dot(plane_normal)
    if abs(denom) < 1e-6:
        return None
    t = (plane_origin - ray_origin).dot(plane_normal) / denom
    return None if t < 0 else ray_origin + ray_dir * t


# ═══════════════════════════════════════════════════════════════
#  Main modal draw operator
# ═══════════════════════════════════════════════════════════════

class POLYDRAW_OT_Draw(bpy.types.Operator):
    """LMB place point | Alt+RMB close polyline | Enter/RMB commit | Esc cancel"""
    bl_idname  = "polydraw.draw"
    bl_label   = "BB Poly Draw (Modal)"
    bl_options = {'REGISTER', 'UNDO'}

    # ── viewport drawing callback ────────────────────────────────

    @staticmethod
    def _draw_dots_geo(shader, positions, px_radius, region, rv3d, color, n_sides=10):
        """Draw filled view-aligned dots as TRIS geometry.
        Replaces POINTS + point_size_set, which is silently ignored on many
        GPU backends (Metal, some Vulkan).  px_radius is in screen pixels;
        the world-space radius is computed per-point so perspective is correct."""
        # HiDPI / Retina: region coords are in device pixels, so scale the radius
        # by pixel_size (1.0 normal, ~2.0 Retina) to keep a constant visual size.
        px_radius *= bpy.context.preferences.system.pixel_size
        right  = rv3d.view_rotation @ Vector((1.0, 0.0, 0.0))
        up     = rv3d.view_rotation @ Vector((0.0, 1.0, 0.0))
        angles = [2.0 * math.pi * k / n_sides for k in range(n_sides)]
        verts  = []
        for center in positions:
            s = view3d_utils.location_3d_to_region_2d(region, rv3d, center)
            if s is None:
                continue
            edge = view3d_utils.region_2d_to_location_3d(
                region, rv3d, Vector((s.x + px_radius, s.y)), Vector(center))
            if edge is None:
                continue
            r   = (Vector(edge) - Vector(center)).length
            c   = Vector(center)
            rim = [c + r * (math.cos(a) * right + math.sin(a) * up) for a in angles]
            for k in range(n_sides):
                verts += [c, rim[k], rim[(k + 1) % n_sides]]
        if not verts:
            return
        shader.bind()
        shader.uniform_float("color", color)
        batch_for_shader(shader, 'TRIS', {"pos": verts}).draw(shader)

    @staticmethod
    def _draw_cb():
        pts         = _DRAW_STATE['pts']
        mouse       = _DRAW_STATE['mouse']
        snap_on     = _DRAW_STATE['snap_on']
        vn_hov      = _DRAW_STATE['vn_hover']
        vn_grab     = _DRAW_STATE['vn_grab']
        nurbs_curve  = _DRAW_STATE.get('nurbs_curve',   [])
        bezier_curve = _DRAW_STATE.get('bezier_curve',  [])
        bez_handles  = _DRAW_STATE.get('bezier_handles', [])

        shader = gpu.shader.from_builtin(_UNIFORM_COLOR_SHADER)
        gpu.state.blend_set('ALPHA')

        # Region + rv3d for geometry-based dot drawing (point_size_set is
        # unreliable on Metal / some Vulkan backends).
        _ctx   = bpy.context
        _area  = getattr(_ctx, 'area', None)
        region = (next((r for r in _area.regions if r.type == 'WINDOW'), None)
                  if _area else None)
        rv3d   = getattr(_ctx, 'region_data', None)
        dots   = POLYDRAW_OT_Draw._draw_dots_geo   # shorthand
        # HiDPI / Retina: line widths are in device pixels, so scale by pixel_size
        # (1.0 normal, ~2.0 Retina) to keep a constant visual thickness.
        _pxs   = _ctx.preferences.system.pixel_size
        def _lw(w):
            gpu.state.line_width_set(w * _pxs)

        if bezier_curve:
            # ── Bézier mode ───────────────────────────────────────
            # Handle lines (anchor → each handle) — translucent white
            if bez_handles:
                _lw(2.0)
                shader.bind()
                shader.uniform_float("color", (1.0, 1.0, 1.0, 0.45))
                for anchor, handle in bez_handles:
                    if anchor != handle:
                        batch_for_shader(shader, 'LINES',
                                         {"pos": [anchor, handle]}).draw(shader)
            # Handle dots — geometry circles, 3 px radius
            handle_dots = [h for a, h in bez_handles if a != h]
            if handle_dots and region and rv3d:
                dots(shader, handle_dots, 3, region, rv3d, (1.0, 1.0, 1.0, 0.9))
            # Cusp handle dots — dot only (no arm line), slightly smaller
            cusp_pts = _DRAW_STATE.get('cusp_handle_pts', [])
            if cusp_pts and region and rv3d:
                dots(shader, cusp_pts, 2, region, rv3d, (1.0, 1.0, 1.0, 0.7))
            # Rubber band from last anchor to mouse
            if mouse and pts:
                _lw(1.0)
                shader.bind()
                shader.uniform_float("color", (0.18, 0.76, 1.0, 0.3))
                batch_for_shader(shader, 'LINES',
                                 {"pos": [pts[-1], mouse]}).draw(shader)
            # Evaluated curve — solid cyan
            _lw(2.5)
            shader.bind()
            shader.uniform_float("color", (0.18, 0.76, 1.0, 0.85))
            batch_for_shader(shader, 'LINE_STRIP',
                             {"pos": bezier_curve}).draw(shader)
            # Anchor dots — orange, 2 px radius
            anchor_dots = [a for a, _ in bez_handles[::2]]
            if anchor_dots and region and rv3d:
                dots(shader, anchor_dots, 2, region, rv3d, (1.0, 0.55, 0.10, 1.0))

        elif nurbs_curve:
            # ── NURBS mode ────────────────────────────────────────
            ctrl_preview = pts + ([mouse] if mouse else [])
            if len(ctrl_preview) > 1:
                _lw(2.0)
                shader.bind()
                shader.uniform_float("color", (0.18, 0.76, 1.0, 0.25))
                batch_for_shader(shader, 'LINE_STRIP',
                                 {"pos": ctrl_preview}).draw(shader)
            _lw(2.5)
            shader.bind()
            shader.uniform_float("color", (0.18, 0.76, 1.0, 0.85))
            batch_for_shader(shader, 'LINE_STRIP',
                             {"pos": nurbs_curve}).draw(shader)
            if pts and region and rv3d:
                dots(shader, pts, 2, region, rv3d, (1.0, 0.55, 0.10, 1.0))

        else:
            # ── Polyline / N-Gon / Hole mode ─────────────────────
            preview = pts + ([mouse] if mouse else [])
            if len(preview) > 1:
                _lw(2.5)
                shader.bind()
                shader.uniform_float("color", (0.18, 0.76, 1.0, 0.85))
                batch_for_shader(shader, 'LINE_STRIP', {"pos": preview}).draw(shader)
            if pts and region and rv3d:
                dots(shader, pts, 2, region, rv3d, (1.0, 0.55, 0.10, 1.0))
            if mouse and snap_on and region and rv3d:
                dots(shader, [mouse], 4, region, rv3d, (1.0, 0.95, 0.0, 1.0))

        # Persistent vertex dots for polygon mesh in edit mode
        mesh_verts = _DRAW_STATE.get('mesh_nudge_verts', [])
        if mesh_verts and region and rv3d:
            dots(shader, mesh_verts, 4, region, rv3d, (1.0, 0.55, 0.10, 1.0))

        # Vertex-nudge highlights — appear during vertex drag
        if region and rv3d:
            if vn_hov and not vn_grab:
                dots(shader, [vn_hov], 5, region, rv3d, (0.2, 1.0, 0.3, 1.0))
            edge_pt = _DRAW_STATE.get('vn_edge_pt')
            if edge_pt:
                dots(shader, [edge_pt], 5, region, rv3d, (0.0, 0.85, 1.0, 1.0))
            if vn_grab:
                dots(shader, [vn_grab], 5, region, rv3d, (1.0, 1.0, 1.0, 1.0))
        # Pick-mode hover highlight — bright line along the hovered shape
        pick_curve = _DRAW_STATE.get('pick_hover_curve', [])
        pick_lines = _DRAW_STATE.get('pick_hover_lines', [])
        if pick_curve or pick_lines:
            _lw(3.0)
            shader.bind()
            shader.uniform_float('color', (0.0, 1.0, 0.5, 0.9))
            if pick_curve:
                batch_for_shader(shader, 'LINE_STRIP',
                                 {'pos': pick_curve}).draw(shader)
            if pick_lines:
                batch_for_shader(shader, 'LINES',
                                 {'pos': pick_lines}).draw(shader)
        gpu.state.blend_set('NONE')

    @classmethod
    def poll(cls, context):
        return context.area is not None and context.area.type == 'VIEW_3D'

    # ── point resolution ─────────────────────────────────────────

    def _resolve_point(self, context, mx, my):
        """
        Return a 3D point for the mouse position.
        First click locks the drawing plane; subsequent clicks project onto it
        so all points stay coplanar regardless of perspective distortion.
        """
        region = context.region
        rv3d   = context.region_data

        if self._draw_plane is None:
            pt          = mouse_to_3d(context, mx, my)
            view_normal = rv3d.view_rotation @ Vector((0, 0, -1))
            self._draw_plane = (pt.copy(), view_normal.normalized())
            return pt

        coord      = (mx, my)
        ray_origin = view3d_utils.region_2d_to_origin_3d(region, rv3d, coord)
        ray_dir    = view3d_utils.region_2d_to_vector_3d(region, rv3d, coord)
        pt = ray_plane_intersect(ray_origin, ray_dir, self._draw_plane[0], self._draw_plane[1])

        if pt is None:
            raw = mouse_to_3d(context, mx, my)
            n   = self._draw_plane[1]
            pt  = raw - (raw - self._draw_plane[0]).dot(n) * n

        if context.scene.tool_settings.use_snap:
            snapped = mouse_to_3d(context, mx, my)
            n  = self._draw_plane[1]
            pt = snapped - (snapped - self._draw_plane[0]).dot(n) * n

        return pt

    # ── nudge header helper ───────────────────────────────────────

    def _nudge_header(self, context):
        rv3d  = context.region_data
        props = context.scene.polydraw_props
        hint  = "Persp: scroll to scale  |  " \
                if (rv3d and rv3d.view_perspective in {'PERSP', 'CAMERA'}) \
                else "Scroll to offset depth  |  "
        # Curves have sharp/smooth close; a polygon mesh just toggles its face.
        if self._last_obj is not None and self._last_obj.type == 'CURVE':
            close = "Alt+RMB close (sharp)  Shift+Alt+RMB close (smooth)"
        else:
            close = "Alt+RMB close / open polygon"
        context.area.header_text_set(
            f"BB Poly Draw  |  {hint}"
            f"Alt+Scroll ±1 mm  Shift+Alt ±10 mm  (offset: {props.offset_value * 1000:.1f} mm)  |  "
            "LMB new  |  Shift+LMB append  |  Ctrl+LMB hole  |  "
            f"{close}  |  Ctrl+Z undo  |  Esc exit")

    def _pick_header(self, context):
        context.area.header_text_set(
            "BB Poly Draw  |  PICK MODE  |  Click a shape to edit it  |  Esc cancel")

    @staticmethod
    def _sample_spline(spline, mw, steps=16):
        """Return a list of world-space Vector samples along a spline.
        Bezier: cubic evaluation per segment.  NURBS/POLY: linear interpolation."""
        pts = []
        if spline.type == 'BEZIER':
            bps = spline.bezier_points
            n   = len(bps)
            pairs = (list(range(n)) + [0]) if spline.use_cyclic_u else list(range(n - 1))
            for i in pairs if spline.use_cyclic_u else range(n - 1):
                p0 = mw @ bps[i].co
                h0 = mw @ bps[i].handle_right
                h1 = mw @ bps[(i + 1) % n].handle_left
                p3 = mw @ bps[(i + 1) % n].co
                for k in range(steps + 1):
                    t  = k / steps
                    u  = 1.0 - t
                    pt = (u**3 * p0 + 3*u**2*t * h0 +
                          3*u*t**2 * h1 + t**3 * p3)
                    pts.append(pt)
        else:
            raw = [mw @ Vector(p.co.xyz) for p in spline.points]
            if spline.use_cyclic_u and raw:
                raw = raw + [raw[0]]
            for i in range(len(raw) - 1):
                for k in range(steps + 1):
                    t = k / steps
                    pts.append(raw[i].lerp(raw[i + 1], t))
        return pts

    def _pick_shape_at_mouse(self, context, mx, my, threshold_px=20):
        """Return the nearest CURVE or MESH object to (mx,my) within threshold_px, or None."""
        best_d   = threshold_px
        best_obj = None
        for obj in context.view_layer.objects:
            if not obj.visible_get():
                continue
            mw = obj.matrix_world
            if obj.type == 'CURVE':
                # NURBS curves are not editable in this tool — don't offer them.
                if not any(s.type == 'BEZIER' for s in obj.data.splines):
                    continue
                for spline in obj.data.splines:
                    for co in self._sample_spline(spline, mw, steps=12):
                        s = _project_to_screen(context, co)
                        if s is None:
                            continue
                        d = _screen_dist(mx, my, s.x, s.y)
                        if d < best_d:
                            best_d   = d
                            best_obj = obj
            elif obj.type == 'MESH':
                for edge in obj.data.edges:
                    v1 = mw @ obj.data.vertices[edge.vertices[0]].co
                    v2 = mw @ obj.data.vertices[edge.vertices[1]].co
                    mid = (v1 + v2) / 2
                    for co in (v1, mid, v2):
                        s = _project_to_screen(context, co)
                        if s is None:
                            continue
                        d = _screen_dist(mx, my, s.x, s.y)
                        if d < best_d:
                            best_d   = d
                            best_obj = obj
        return best_obj

    def _curve_sampled_pts(self, obj):
        """Return world-space sampled points for highlighting a curve (LINE_STRIP)."""
        pts = []
        mw  = obj.matrix_world
        for spline in obj.data.splines:
            pts.extend(tuple(co) for co in self._sample_spline(spline, mw, steps=16))
        return pts

    def _mesh_sampled_lines(self, obj):
        """Return flat list of world-space edge endpoint pairs for highlighting a mesh (LINES)."""
        pts = []
        mw  = obj.matrix_world
        for edge in obj.data.edges:
            v1 = mw @ obj.data.vertices[edge.vertices[0]].co
            v2 = mw @ obj.data.vertices[edge.vertices[1]].co
            pts.append(tuple(v1))
            pts.append(tuple(v2))
        return pts

    # ── invoke ───────────────────────────────────────────────────

    def invoke(self, context, event):
        self._points        = []
        self._mouse_3d      = None
        self._closed        = False
        self._target        = None
        self._ctrl          = False
        self._draw_plane    = None
        self._angle_step    = _ANGLE_STEP_DEFAULT
        self._last_obj      = None
        self._nudging       = False
        self._last_mode     = 'NONE'
        self._append_target = None
        self._pre_hole_mode = None
        self._undo_state    = None
        self._vn_hover      = None   # (source, idx, world_co) hovered vertex
        self._vn_grab       = None   # same — currently being dragged
        self._vn_plane      = None   # (origin, normal) drag constraint plane
        self._vn_mirror     = None   # per-drag: does the grabbed handle mirror its opposite?
        self._vn_edge_pt    = None   # nearest edge insert point for ctrl+alt+shift
        self._last_plane_n  = None   # plane normal stored at commit time
        self._bezier_pts    = []     # list of {'co','hl','hr'} for Bézier mode
        self._bezier_dragging = False  # True while LMB held pulling a handle
        self._extend_target = None   # Curve object to extend on commit (Shift+LMB on curve)
        self._sharp_close   = False  # True when Alt+RMB close: sharp VECTOR corner at seam
        self._make_face     = False  # True when Alt+RMB closes a polyline → fill an N-Gon face
        self._edit_existing = False  # True when entered via E on existing curve; LMB stays in nudge
        self._picking       = False  # True while in Q pick-mode
        self._pick_hover    = None   # Curve object currently hovered in pick mode

        props = context.scene.polydraw_props

        if props.draw_mode == 'HOLE':
            obj = context.active_object
            if obj and obj.type == 'MESH':
                self._target        = obj
                self._pre_hole_mode = 'NONE'
            else:
                self.report({'WARNING'}, "Holes: select the target mesh first")
                props.draw_mode = 'NONE'
                return {'CANCELLED'}
        elif props.draw_mode in {'NGON', 'POLYLINE', 'BEZIER'}:
            # Auto-enter nudge if compatible geometry is already selected
            obj = context.active_object
            if obj and obj.type == 'MESH':
                self._last_obj  = obj
                self._nudging   = True
                self._last_mode = props.draw_mode
            elif obj and obj.type == 'CURVE' and props.draw_mode == 'BEZIER':
                self._last_obj  = obj
                self._nudging   = True
                self._last_mode = props.draw_mode

        # Reset module-level draw state for this session
        _DRAW_STATE.update({'pts': [], 'mouse': None, 'snap_on': False,
                            'vn_hover': None, 'vn_grab': None, 'vn_edge_pt': None})
        context.window_manager.modal_handler_add(self)
        global _active_draw_op
        _active_draw_op = self

        # If PickCurve operator pre-armed pick mode before invoking us, activate it now
        global _pending_pick_mode
        if _pending_pick_mode:
            _pending_pick_mode  = False
            self._picking       = True
            self._pick_hover    = None
            _DRAW_STATE['pick_hover_curve'] = []
            _DRAW_STATE['pick_hover_lines']  = []
            self._pick_header(context)
        elif self._nudging:
            self._nudge_header(context)
        else:
            self._update_header(context)

        # ── First-click fast-path ────────────────────────────────
        return {'RUNNING_MODAL'}

    # ── header text ──────────────────────────────────────────────

    def _update_header(self, context):
        props      = context.scene.polydraw_props
        ctrl_hint  = f"Ctrl {self._angle_step:.0f}° snap (scroll to change, Shift×5)"
        alt_hint   = f"Alt+Scroll ±1 mm  Shift+Alt ±10 mm  (offset: {props.offset_value * 1000:.1f} mm)"
        if props.draw_mode == 'POLYLINE':
            context.area.header_text_set(
                f"BB Poly Draw  |  LMB place point  |  {ctrl_hint}  |  {alt_hint}  |  "
                "Enter/RMB commit polyline  |  Alt+RMB close + fill polygon  |  Esc cancel")
        elif props.draw_mode == 'HOLE':
            context.area.header_text_set(
                f"BB Poly Draw  |  HOLE MODE  |  LMB place point  |  {alt_hint}  |  "
                "Enter/RMB cut hole  |  Esc cancel")
        elif props.draw_mode == 'BEZIER':
            context.area.header_text_set(
                f"BB Poly Draw  |  BÉZIER  |  LMB click (corner) or click-drag (smooth)  |  "
                f"{ctrl_hint}  |  {alt_hint}  |  Alt+RMB close (sharp)  Shift+Alt+RMB close (smooth)  |  Enter/RMB commit  |  Esc cancel")
        else:
            context.area.header_text_set(
                f"BB Poly Draw  |  LMB place point  |  {ctrl_hint}  |  {alt_hint}  |  "
                "Enter/RMB commit  |  Esc cancel")

    # ── modal ────────────────────────────────────────────────────

    def modal(self, context, event):
        context.area.tag_redraw()
        props = context.scene.polydraw_props

        # ── Pending first click (from Start operator) ────────────
        # When a Start operator resets the modal in-place, the triggering LMB
        # is consumed by that operator and never reaches modal(). We stash the
        # click coords in _pending_first_click so the next modal() call can
        # place the first point, giving true single-click-to-draw behaviour.
        # If the modal auto-entered nudge mode (selected object in scene), we
        # also exit nudge here so the point is placed in a clean draw state.
        global _pending_first_click
        if _pending_first_click is not None:
            mx, my = _pending_first_click
            _pending_first_click = None
            if self._nudging:
                # Exit nudge cleanly before placing the first point
                self._nudging       = False
                self._last_obj      = None
                self._append_target = None
                self._target        = None
                self._draw_plane    = None
                self._points        = []
                self._update_header(context)
            raw = self._resolve_point(context, mx, my)
            if props.draw_mode == 'BEZIER':
                self._bezier_pts.append(
                    {'co': raw.copy(), 'hl': raw.copy(), 'hr': raw.copy()})
                # NOT a drag: the tool-activation click was consumed by the start
                # operator, so the modal never receives its release. Leaving
                # _bezier_dragging True here would make the move toward the second
                # point drag this first point's handle onto it. The fast-path first
                # point is therefore a settled corner (cusp).
                self._bezier_dragging = False
                self._sync_draw_state(context)
            elif props.draw_mode not in {'NONE'}:
                self._points.append(raw)
                _DRAW_STATE['pts'] = [tuple(p) for p in self._points]
            return {'RUNNING_MODAL'}

        # ── Active-tool sync ─────────────────────────────────────
        # The modal consumes LMB, so the tool keymap operators (start_poly /
        # start_nurbs …) never fire while the modal is running.  Instead we
        # detect an external tool switch here and update the mode in-place.
        if not self._nudging and props.draw_mode not in {'HOLE', 'NONE'} and event.type == 'MOUSEMOVE':
            try:
                active_tool = context.workspace.tools.from_space_view3d_mode(
                    context.mode, create=False)
                if active_tool:
                    tid = active_tool.idname
                    if tid == 'polydraw.polyline_tool' and props.draw_mode != 'POLYLINE':
                        props.draw_mode  = 'POLYLINE'
                        self._last_mode  = 'POLYLINE'
                        self._points     = []
                        self._draw_plane = None
                        _DRAW_STATE['nurbs_curve'] = []
                        self._update_header(context)
                    elif tid == 'polydraw.bezier_tool' and props.draw_mode != 'BEZIER':
                        props.draw_mode       = 'BEZIER'
                        self._last_mode       = 'BEZIER'
                        self._points          = []
                        self._bezier_pts      = []
                        self._bezier_dragging = False
                        self._draw_plane      = None
                        _DRAW_STATE['bezier_curve']   = []
                        _DRAW_STATE['bezier_handles'] = []
                        self._update_header(context)
            except Exception:
                pass

        mode  = props.draw_mode

        # ESC always exits immediately — also revert to select so the WorkSpaceTool
        # releases and the user isn't trapped re-entering draw on every LMB click.
        if event.type == 'ESC' and event.value == 'PRESS':
            self._cleanup(context)
            self._nudging       = False
            self._last_obj      = None
            self._edit_existing = False
            self._picking       = False
            self._pick_hover    = None
            _DRAW_STATE['pick_hover_curve'] = []
            _DRAW_STATE['pick_hover_lines']  = []
            props.draw_mode = 'NONE'
            try:
                bpy.ops.wm.tool_set_by_id(name='builtin.select_box')
            except Exception:
                pass
            return {'CANCELLED'}

        # Track Ctrl — RUNNING_MODAL so Blender's keymap doesn't see it
        # and accidentally activate competing modal operators.
        if event.type in {'LEFT_CTRL', 'RIGHT_CTRL'}:
            self._ctrl = (event.value == 'PRESS')
            return {'RUNNING_MODAL'}

        # ── vertex nudge (Ctrl+Shift) ───────────────────────────
        # Use both self._ctrl (tracked) and event.ctrl (live) so the check works
        # even when Ctrl was held before the tool started.
        _ctrl          = self._ctrl or event.ctrl
        ctrl_shift     = _ctrl and event.shift and not event.alt
        alt_shift      = event.shift and event.alt and not _ctrl
        ctrl_alt_shift = _ctrl and event.shift and event.alt

        if ctrl_shift or ctrl_alt_shift or alt_shift or self._vn_grab:
            # ── Auto-enter nudge on active object if not already nudging ─
            if (ctrl_shift or ctrl_alt_shift or alt_shift) and not self._nudging:
                if not self._points and not self._bezier_pts:
                    active = context.active_object
                    if active:
                        if active.type == 'MESH':
                            self._last_obj  = active
                            self._nudging   = True
                            self._nudge_header(context)
                        elif active.type == 'CURVE':
                            if any(s.type == 'BEZIER' for s in active.data.splines):
                                self._last_obj  = active
                                self._nudging   = True
                                self._last_mode = 'BEZIER'
                                self._nudge_header(context)
            # Kill rubber-band immediately — don't wait for _sync_draw_state
            _DRAW_STATE['mouse'] = None
            if event.type == 'MOUSEMOVE':
                mx = event.mouse_region_x; my = event.mouse_region_y
                self._mouse_3d = None
                if self._vn_grab:
                    region = context.region; rv3d = context.region_data
                    ro = view3d_utils.region_2d_to_origin_3d(region, rv3d, (mx, my))
                    rd = view3d_utils.region_2d_to_vector_3d(region, rv3d, (mx, my))
                    origin, normal = self._vn_plane
                    pt = ray_plane_intersect(ro, rd, origin, normal)
                    if pt:
                        self._vn_apply(context, pt)
                elif ctrl_alt_shift:
                    # Show nearest edge insertion point (cyan dot)
                    self._vn_hover = None
                    result = self._vn_find_nearest_edge_pt(context, mx, my)
                    self._vn_edge_pt = result[0] if result else None
                else:
                    self._vn_hover = self._vn_find_nearest(context, mx, my)
                    self._vn_edge_pt = None
                self._sync_draw_state(context)
                context.area.tag_redraw()
                return {'RUNNING_MODAL'}

            if event.type == 'LEFTMOUSE' and event.value == 'PRESS':
                mx = event.mouse_region_x; my = event.mouse_region_y

                # ── Ctrl+Alt+Shift+LMB: add vertex ──────────────
                if ctrl_alt_shift:
                    self._vn_add_vertex(context, mx, my)
                    self._sync_draw_state(context)
                    return {'RUNNING_MODAL'}

                # ── Alt+Shift+LMB: delete hovered vertex ────────
                if alt_shift:
                    if self._vn_hover:
                        self._vn_delete_vertex(context)
                        self._vn_hover = None
                        self._sync_draw_state(context)
                    return {'RUNNING_MODAL'}

                # ── Ctrl+Shift+LMB: grab vertex ──────────────────
                if ctrl_shift and self._vn_hover:
                    source, idx, wco = self._vn_hover
                    self._vn_grab  = self._vn_hover
                    self._vn_plane = self._vn_get_plane(context, source, wco)
                    self._sync_draw_state(context)
                return {'RUNNING_MODAL'}

            if event.type == 'LEFTMOUSE' and event.value == 'RELEASE':
                self._vn_grab = None
                self._sync_draw_state(context)
                return {'RUNNING_MODAL'}

        # Clear the edge-insert highlight when its modifiers are released.
        # NB: _vn_hover is intentionally NOT cleared here — it is owned by the
        # MOUSEMOVE handler (set/cleared every move) and must survive until the
        # LMB-press grab check reads it; clearing it here would break hover-grab.
        if not (ctrl_shift or ctrl_alt_shift or alt_shift or self._vn_grab):
            if self._vn_edge_pt:
                self._vn_edge_pt = None
                self._sync_draw_state(context)
                context.area.tag_redraw()

        # ── Alt+Scroll: adjust offset_value (works at any point) ───────
        if (event.alt and not _ctrl
                and event.type in {'WHEELUPMOUSE', 'WHEELDOWNMOUSE'}):
            delta = 0.01 if event.shift else 0.001   # Shift+Alt = 10 mm, Alt = 1 mm
            if event.type == 'WHEELDOWNMOUSE':
                delta = -delta
            props.offset_value = round(
                max(-10.0, min(10.0, props.offset_value + delta)), 4)
            if self._nudging:
                self._nudge_header(context)
            else:
                self._update_header(context)
            context.area.tag_redraw()
            return {'RUNNING_MODAL'}

        # ── nudge phase ──────────────────────────────────────────
        # Sync _last_mode with the active tool even while nudging, so the next
        # LMB click starts a new shape in the correct mode.
        # Skip entirely while picking — otherwise the nudge LMB handler eats the
        # click meant to confirm a pick (Q pressed while already editing a curve).
        if self._nudging and not self._picking and event.type == 'MOUSEMOVE':
            try:
                active_tool = context.workspace.tools.from_space_view3d_mode(
                    context.mode, create=False)
                if active_tool:
                    tid = active_tool.idname
                    if tid == 'polydraw.polyline_tool':
                        self._last_mode = 'POLYLINE'
                    elif tid == 'polydraw.bezier_tool':
                        self._last_mode = 'BEZIER'
            except Exception:
                pass

        if self._nudging and self._last_obj and not self._picking:

            if event.type in {'WHEELUPMOUSE', 'WHEELDOWNMOUSE'}:
                val  = context.scene.polydraw_props.offset_value
                val  = val if event.type == 'WHEELUPMOUSE' else -val
                rv3d = context.region_data
                if rv3d and rv3d.view_perspective in {'PERSP', 'CAMERA'}:
                    factor = 1.02 if val > 0 else (1.0 / 1.02)
                    self._last_obj.scale = self._last_obj.scale * factor
                else:
                    view_dir = rv3d.view_rotation @ Vector((0, 0, -1))
                    axes = [Vector((1,0,0)), Vector((0,1,0)), Vector((0,0,1))]
                    best = max(axes, key=lambda a: abs(view_dir.dot(a)))
                    if view_dir.dot(best) < 0:
                        best = -best
                    self._last_obj.location += best * val
                context.area.tag_redraw()
                return {'RUNNING_MODAL'}

            if event.type == 'LEFTMOUSE' and event.value in {'PRESS', 'CLICK'}:
                if not _in_draw_canvas(context, event.mouse_x, event.mouse_y):
                    return {'PASS_THROUGH'}

                # Bare LMB on a hovered vertex/handle — grab it, no modifier needed.
                if (self._vn_hover
                        and not event.shift and not event.alt
                        and not (event.ctrl or self._ctrl)):
                    source, idx, wco = self._vn_hover
                    self._vn_grab  = self._vn_hover
                    self._vn_plane = self._vn_get_plane(context, source, wco)
                    self._sync_draw_state(context)
                    return {'RUNNING_MODAL'}

                # In edit-existing mode (entered via E): only stay in nudge if
                # the click lands near a control point or handle.  A click on
                # empty space clears the flag and falls through to start a new
                # shape normally — same as plain nudge behaviour.
                if self._edit_existing and not event.shift and not event.ctrl and not self._ctrl:
                    if self._vn_hover is None:
                        if self._last_obj and self._last_obj.type == 'MESH':
                            # Click outside a polygon mesh — exit cleanly, don't draw.
                            self._edit_existing = False
                            self._nudging       = False
                            self._last_obj      = None
                            _DRAW_STATE['mesh_nudge_verts'] = []
                            props.draw_mode = 'NONE'
                            self._update_header(context)
                            return {'RUNNING_MODAL'}
                        # For curves: exit edit mode and fall through to start a new shape.
                        self._edit_existing = False

                saved_obj     = self._last_obj
                self._nudging = False
                self._last_obj = None
                self._edit_existing = False

                if event.shift and saved_obj and saved_obj.type == 'MESH':
                    self._append_target = saved_obj
                    props.draw_mode     = self._last_mode
                    context.area.header_text_set(
                        "BB Poly Draw  |  APPEND MODE  |  LMB place point  |  "
                        "Enter/RMB merge into shape  |  Esc cancel")
                elif event.shift and saved_obj and saved_obj.type == 'CURVE' and self._last_mode == 'BEZIER':
                    # Extend existing curve — seed the last point so drawing
                    # connects seamlessly, then commit appends only the new points.
                    self._extend_target = saved_obj
                    props.draw_mode     = self._last_mode
                    mw = saved_obj.matrix_world
                    for spline in saved_obj.data.splines:
                        if spline.type == 'BEZIER' and spline.bezier_points:
                            last = spline.bezier_points[-1]
                            self._bezier_pts = [{
                                'co': (mw @ last.co).copy(),
                                'hl': (mw @ last.handle_left).copy(),
                                'hr': (mw @ last.handle_right).copy(),
                            }]
                            break
                    context.area.header_text_set(
                        "BB Poly Draw  |  BÉZIER EXTEND  |  LMB place point  |  "
                        "Enter/RMB append to curve  |  Esc cancel")
                elif (event.ctrl or self._ctrl) and saved_obj and saved_obj.type in {'MESH', 'CURVE'}:
                    self._append_target = None
                    self._target        = saved_obj
                    self._pre_hole_mode = self._last_mode
                    props.draw_mode     = 'HOLE'
                    context.area.header_text_set(
                        "BB Poly Draw  |  HOLE MODE  |  LMB place point  |  "
                        "Enter/RMB cut hole  |  Esc cancel")
                else:
                    self._append_target = None
                    props.draw_mode     = self._last_mode
                    self._update_header(context)
                # fall through to place first point

        # ── Q: enter pick mode ──────────────────────────────────
        if (event.type == 'Q' and event.value == 'PRESS'
                and not event.ctrl and not event.shift and not event.alt):
            self._picking     = True
            self._pick_hover  = None
            _DRAW_STATE['pick_hover_curve'] = []
            _DRAW_STATE['pick_hover_lines']  = []
            self._pick_header(context)
            context.area.tag_redraw()
            return {'RUNNING_MODAL'}

        # ── pick mode: MOUSEMOVE hover highlight ─────────────────
        if self._picking and event.type == 'MOUSEMOVE':
            hov = self._pick_shape_at_mouse(
                context, event.mouse_region_x, event.mouse_region_y)
            if hov is not self._pick_hover:
                self._pick_hover = hov
                if hov is None:
                    _DRAW_STATE['pick_hover_curve'] = []
                    _DRAW_STATE['pick_hover_lines']  = []
                elif hov.type == 'MESH':
                    _DRAW_STATE['pick_hover_curve'] = []
                    _DRAW_STATE['pick_hover_lines']  = self._mesh_sampled_lines(hov)
                else:
                    _DRAW_STATE['pick_hover_curve'] = self._curve_sampled_pts(hov)
                    _DRAW_STATE['pick_hover_lines']  = []
                context.area.tag_redraw()
            return {'RUNNING_MODAL'}

        # ── pick mode: LMB confirm ───────────────────────────────
        if self._picking and event.type == 'LEFTMOUSE' and event.value == 'PRESS':
            picked = self._pick_hover or self._pick_shape_at_mouse(
                context, event.mouse_region_x, event.mouse_region_y)
            # NURBS curves are not editable in this tool — ignore them.
            if (picked and picked.type == 'CURVE'
                    and not any(s.type == 'BEZIER' for s in picked.data.splines)):
                picked = None
            self._picking    = False
            self._pick_hover = None
            _DRAW_STATE['pick_hover_curve'] = []
            _DRAW_STATE['pick_hover_lines']  = []
            if picked:
                if picked.type == 'MESH':
                    mesh_mode = 'NGON' if picked.data.polygons else 'POLYLINE'
                    props.draw_mode = mesh_mode
                    shape_mode      = mesh_mode
                else:
                    shape_mode = 'BEZIER'
                    props.draw_mode = shape_mode
                self._points        = []
                self._bezier_pts    = []
                self._bezier_dragging = False
                self._mouse_3d      = None
                self._closed        = False
                self._draw_plane    = None
                self._nudging       = True
                self._last_obj      = picked
                self._last_mode     = shape_mode
                self._edit_existing = True
                self._vn_hover      = None
                self._vn_grab       = None
                self._vn_plane      = None
                self._vn_edge_pt    = None
                self._extend_target = None
                # Select the picked object so it's obvious what's active
                for o in context.view_layer.objects:
                    o.select_set(False)
                context.view_layer.objects.active = picked
                picked.select_set(True)
                _DRAW_STATE.update({'pts': [], 'mouse': None, 'snap_on': False,
                                    'vn_hover': None, 'vn_grab': None, 'vn_edge_pt': None,
                                    'nurbs_curve': [], 'bezier_curve': [], 'bezier_handles': [], 'cusp_handle_pts': []})
                self._sync_draw_state(context)
                self._nudge_header(context)
            else:
                # Clicked empty space — restore previous header
                if self._nudging:
                    self._nudge_header(context)
                else:
                    self._update_header(context)
            context.area.tag_redraw()
            return {'RUNNING_MODAL'}

        # ── pick mode: Esc cancel ────────────────────────────────
        if self._picking and event.type == 'ESC' and event.value == 'PRESS':
            self._picking    = False
            self._pick_hover = None
            _DRAW_STATE['pick_hover_curve'] = []
            _DRAW_STATE['pick_hover_lines']  = []
            if self._nudging:
                self._nudge_header(context)
            else:
                self._update_header(context)
            context.area.tag_redraw()
            return {'RUNNING_MODAL'}

        # ── Ctrl+Z ──────────────────────────────────────────────
        if self._ctrl and event.type == 'Z' and event.value == 'PRESS':
            if self._nudging and self._undo_state:
                kind, target_obj, data_snapshot = self._undo_state
                if kind == 'obj':
                    bpy.data.objects.remove(target_obj, do_unlink=True)
                else:
                    # 'mesh' / 'data' — restore the datablock snapshot
                    # (works for both Mesh and Curve, e.g. undo a close/cut/merge)
                    old_data = target_obj.data
                    target_obj.data = data_snapshot
                    if isinstance(old_data, bpy.types.Mesh):
                        bpy.data.meshes.remove(old_data)
                    elif isinstance(old_data, bpy.types.Curve):
                        bpy.data.curves.remove(old_data)
                    for _o in context.view_layer.objects: _o.select_set(False)
                    context.view_layer.objects.active = target_obj
                    target_obj.select_set(True)
                self._undo_state = None
                if kind == 'obj':
                    self._last_obj  = None
                    self._nudging   = False
                    props.draw_mode = self._last_mode
                    self._update_header(context)
                else:
                    self._last_obj = target_obj
                    self._nudging  = True
                    self._nudge_header(context)
                context.area.tag_redraw()
            elif self._bezier_pts:
                self._bezier_pts.pop()
                if self._bezier_dragging:
                    self._bezier_dragging = False
                if not self._bezier_pts:
                    self._draw_plane = None
                self._sync_draw_state(context)
                context.area.tag_redraw()
            elif self._points:
                self._points.pop()
                if not self._points:
                    self._draw_plane = None
                context.area.tag_redraw()
            return {'RUNNING_MODAL'}

        # ── Ctrl+Scroll: adjust angle step ──────────────────────
        if self._ctrl and event.type in {'WHEELUPMOUSE', 'WHEELDOWNMOUSE'}:
            inc = 5.0 if event.shift else 1.0
            if event.type == 'WHEELUPMOUSE':
                self._angle_step = min(90.0, self._angle_step + inc)
            else:
                self._angle_step = max(1.0, self._angle_step - inc)
            self._update_header(context)
            context.area.tag_redraw()
            return {'RUNNING_MODAL'}

        # ── mouse move: update preview ───────────────────────────
        if event.type == 'MOUSEMOVE':
            mx, my = event.mouse_region_x, event.mouse_region_y

            # Bézier handle drag — update right/left handles of last point live
            if props.draw_mode == 'BEZIER' and self._bezier_dragging and self._bezier_pts:
                raw = self._resolve_point(context, mx, my)
                bp  = self._bezier_pts[-1]
                bp['hr'] = raw.copy()
                bp['hl'] = Vector(2.0 * bp['co'] - raw)
                self._mouse_3d = None   # suppress rubber band while dragging handle
                self._sync_draw_state(context)
                return {'RUNNING_MODAL'}

            # Hover-to-grab: if the cursor is over an existing point/handle,
            # highlight it (green dot) and suppress the new-point preview so a
            # plain LMB grabs it to move instead of placing a point. No modifier
            # needed — works while drawing and while editing committed geometry.
            if not (_ctrl or event.shift or event.alt):
                hov = self._vn_find_nearest(context, mx, my)
                if hov is not None:
                    self._vn_hover = hov
                    self._mouse_3d = None
                    self._sync_draw_state(context)
                    context.area.tag_redraw()
                    return {'PASS_THROUGH'}
                elif self._vn_hover is not None:
                    self._vn_hover = None

            raw = self._resolve_point(context, mx, my)
            if not self._points and not self._bezier_pts:
                self._draw_plane = None
            if self._ctrl and self._bezier_pts:
                view_n = context.region_data.view_rotation @ Vector((0, 0, -1))
                raw    = angle_snap(raw, self._bezier_pts[-1]['co'], view_n, self._angle_step)
            elif self._ctrl and self._points:
                view_n = context.region_data.view_rotation @ Vector((0, 0, -1))
                raw    = angle_snap(raw, self._points[-1], view_n, self._angle_step)
            self._mouse_3d = raw
            self._sync_draw_state(context)
            return {'PASS_THROUGH'}

        # ── LMB: place point ────────────────────────────────────
        if event.type == 'LEFTMOUSE' and event.value == 'PRESS':
            if not _in_draw_canvas(context, event.mouse_x, event.mouse_y):
                return {'PASS_THROUGH'}

            # Hover-to-grab: a plain click on an existing point/handle grabs it
            # to move (drag, then release) instead of placing a new point.
            if (self._vn_hover and not event.shift and not event.alt
                    and not (event.ctrl or self._ctrl)):
                source, idx, wco = self._vn_hover
                self._vn_grab  = self._vn_hover
                self._vn_plane = self._vn_get_plane(context, source, wco)
                self._sync_draw_state(context)
                return {'RUNNING_MODAL'}

            # Re-read draw_mode here — nudge fall-through may have just changed it
            cur_mode = props.draw_mode
            if cur_mode == 'BEZIER':
                anchor = self._resolve_point(context,
                                             event.mouse_region_x,
                                             event.mouse_region_y)
                if self._ctrl and self._bezier_pts:
                    view_n = context.region_data.view_rotation @ Vector((0, 0, -1))
                    anchor = angle_snap(anchor, self._bezier_pts[-1]['co'],
                                        view_n, self._angle_step)
                self._bezier_pts.append(
                    {'co': anchor.copy(), 'hl': anchor.copy(), 'hr': anchor.copy()})
                self._bezier_dragging = True
                self._mouse_3d = None
                self._sync_draw_state(context)
                return {'RUNNING_MODAL'}

            raw = self._resolve_point(context, event.mouse_region_x, event.mouse_region_y)
            if self._ctrl and self._points:
                view_n = context.region_data.view_rotation @ Vector((0, 0, -1))
                raw    = angle_snap(raw, self._points[-1], view_n, self._angle_step)
            self._points.append(raw)
            return {'RUNNING_MODAL'}

        # ── LMB RELEASE: finalise Bézier handle ─────────────────
        if (event.type == 'LEFTMOUSE' and event.value == 'RELEASE'
                and props.draw_mode == 'BEZIER' and self._bezier_dragging):
            self._bezier_dragging = False
            self._sync_draw_state(context)
            return {'RUNNING_MODAL'}

        # ── Alt+RMB: close loop (Polyline, NURBS, Bézier) ────────
        # Alt+RMB        = sharp corner at seam (NURBS / Bézier)
        # Shift+Alt+RMB  = smooth tangent continuity at seam
        # Also fires while nudging a committed curve (just drawn or picked) so
        # the loop can be closed/opened after the fact, not only mid-draw.
        editing_curve = (self._nudging and self._last_obj is not None
                         and self._last_obj.type == 'CURVE')
        editing_mesh  = (self._nudging and self._last_obj is not None
                         and self._last_obj.type == 'MESH'
                         and not self._points and not self._bezier_pts)
        if (event.type == 'RIGHTMOUSE' and event.value == 'PRESS' and event.alt
                and (props.draw_mode in {'POLYLINE', 'BEZIER'}
                     or editing_curve or editing_mesh)):

            # ── Edit mode: toggle open/closed on a committed polyline mesh ──
            if editing_mesh:
                # Snapshot for single-step undo so Ctrl+Z reverts just the close,
                # not the whole object.
                self._undo_state = ('data', self._last_obj, self._last_obj.data.copy())
                self._toggle_mesh_closed(context)
                self._sync_draw_state(context)
                context.area.tag_redraw()
                return {'RUNNING_MODAL'}

            # ── Edit mode: toggle cyclic on the committed curve ───
            if editing_curve:
                obj    = self._last_obj
                # Snapshot for single-step undo so Ctrl+Z reverts just the close,
                # not the whole curve object.
                self._undo_state = ('data', obj, obj.data.copy())
                # Alt alone = sharp; Shift+Alt = smooth (no sharp override).
                # Always a curve here, so sharp depends only on Shift.
                sharp  = not event.shift
                for spline in obj.data.splines:
                    n = (len(spline.bezier_points) if spline.type == 'BEZIER'
                         else len(spline.points))
                    if n < 3:
                        continue
                    if spline.type == 'BEZIER':
                        bpts   = spline.bezier_points
                        closing = not spline.use_cyclic_u

                        # Snapshot every handle position and type
                        snap = [(bp.handle_left_type,  bp.handle_left.copy(),
                                 bp.handle_right_type, bp.handle_right.copy())
                                for bp in bpts]

                        # Precompute VECTOR positions from neighbours (cyclic-aware)
                        nb = len(bpts)
                        def vector_hr(i):
                            nco = bpts[(i + 1) % nb].co
                            return bpts[i].co + (nco - bpts[i].co) / 3.0
                        def vector_hl(i):
                            pco = bpts[(i - 1) % nb].co
                            return bpts[i].co + (pco - bpts[i].co) / 3.0

                        # Freeze only ALIGNED, FREE, and VECTOR handles so their
                        # positions survive the cyclic change unchanged.
                        # AUTO handles are left as AUTO — Blender will recalculate
                        # them correctly once cyclic is set, producing proper
                        # tangent continuity at the seam.
                        for i, bp in enumerate(bpts):
                            lt, lv, rt, rv = snap[i]
                            if lt == 'VECTOR':
                                bp.handle_left = vector_hl(i)
                                bp.handle_left_type = 'FREE'
                            elif lt in {'ALIGNED', 'FREE'}:
                                bp.handle_left = lv
                                bp.handle_left_type = 'FREE'

                            if rt == 'VECTOR':
                                bp.handle_right = vector_hr(i)
                                bp.handle_right_type = 'FREE'
                            elif rt in {'ALIGNED', 'FREE'}:
                                bp.handle_right = rv
                                bp.handle_right_type = 'FREE'

                        # Set cyclic — AUTO handles recalculate with correct seam
                        spline.use_cyclic_u = closing

                        # Restore VECTOR types (positions already correct as FREE)
                        # AUTO stays AUTO (already recalculated for smooth seam)
                        for i, bp in enumerate(bpts):
                            lt, _, rt, _ = snap[i]
                            if lt == 'VECTOR':
                                bp.handle_left_type  = 'VECTOR'
                            if rt == 'VECTOR':
                                bp.handle_right_type = 'VECTOR'

                        # Sharp-seam override (Alt without Shift):
                        # Both endpoint points become fully sharp corners —
                        # all four handles (seam-facing AND outward) are set to
                        # VECTOR so neither point pulls tangentially into the join.
                        if sharp:
                            if closing:
                                bpts[0].handle_left_type   = 'VECTOR'
                                bpts[0].handle_right_type  = 'VECTOR'
                                bpts[-1].handle_left_type  = 'VECTOR'
                                bpts[-1].handle_right_type = 'VECTOR'
                            else:
                                bpts[0].handle_left_type   = 'AUTO'
                                bpts[0].handle_right_type  = 'AUTO'
                                bpts[-1].handle_left_type  = 'AUTO'
                                bpts[-1].handle_right_type = 'AUTO'
                    else:
                        # NURBS / POLY — no handle types, just toggle
                        spline.use_cyclic_u = not spline.use_cyclic_u
                self._sync_draw_state(context)
                context.area.tag_redraw()
                return {'RUNNING_MODAL'}

            # ── Drawing mode: close and commit ────────────────────
            pts_to_check = self._bezier_pts if props.draw_mode == 'BEZIER' else self._points
            if len(pts_to_check) >= 3:
                self._closed = True
                # Polyline + Alt+RMB → close the loop and fill a polygon face
                # (RMB alone leaves it an open polyline). NURBS/Bézier keep their
                # curve close behaviour below.
                if props.draw_mode == 'POLYLINE':
                    self._make_face = True
            # Alt alone   = sharp close (VECTOR corner at seam)
            # Shift+Alt   = smooth close (AUTO seam tangent, keeps tangents)
            if (not event.shift) and props.draw_mode == 'BEZIER':
                self._sharp_close = True
            self._commit(context)
            return {'RUNNING_MODAL'}

        # ── Enter / RMB: commit ──────────────────────────────────
        if (event.type in {'RET', 'NUMPAD_ENTER', 'RIGHTMOUSE'}
                and event.value == 'PRESS' and not event.alt):
            self._commit(context)
            return {'RUNNING_MODAL'}

        return {'RUNNING_MODAL'}

    # ── commit ───────────────────────────────────────────────────

    def _commit(self, context):
        props = context.scene.polydraw_props
        mode  = props.draw_mode
        pts   = self._points
        self._draw_plane = None
        _DRAW_STATE['nurbs_curve']    = []
        _DRAW_STATE['bezier_curve']   = []
        _DRAW_STATE['bezier_handles'] = []

        # ── Bézier curve object ───────────────────────────────────
        if mode == 'BEZIER':
            bz = self._bezier_pts
            if len(bz) < 2:
                self.report({'INFO'}, "BB Poly Draw: need at least 2 points")
                return

            def _write_bz_pts(spline, bz_list, offset=0):
                nb = len(bz_list)
                cusp_indices = set()
                # Pass 1: co only — neighbour positions must exist before
                # computing the click-point handle arms in pass 2.
                for i, bp in enumerate(bz_list):
                    spline.bezier_points[offset + i].co = bp['co']
                # Pass 2: handles + types.
                # Smooth drag points → ALIGNED (Q edit mode mirrors handles).
                # Click-without-drag (cusp) → FREE with handles at co so the
                # committed shape matches the drawing preview exactly.
                for i, bp in enumerate(bz_list):
                    sp = spline.bezier_points[offset + i]
                    if (bp['hr'] - bp['co']).length < 1e-4:
                        co = bp['co']
                        # Keep handles at co (zero-length) to match the
                        # drawing preview exactly. The anchor is still
                        # grabbable in nudge mode via its 'bzco' source.
                        sp.handle_left       = co
                        sp.handle_right      = co
                        sp.handle_left_type  = 'FREE'
                        sp.handle_right_type = 'FREE'
                        cusp_indices.add(offset + i)
                    else:
                        sp.handle_left       = bp['hl']
                        sp.handle_right      = bp['hr']
                        sp.handle_left_type  = 'ALIGNED'
                        sp.handle_right_type = 'ALIGNED'
                return cusp_indices

            if self._extend_target and self._extend_target.type == 'CURVE':
                # ── Append new points to existing Bézier spline ──
                # bz[0] is the seeded existing endpoint — skip it but update its
                # outgoing handle, then append bz[1:] as truly new points.
                new_bz = bz[1:]
                if len(new_bz) < 1:
                    self.report({'INFO'}, "BB Poly Draw: need at least 1 new point")
                    return
                obj    = self._extend_target
                mw_inv = obj.matrix_world.inverted()
                for spline in obj.data.splines:
                    if spline.type != 'BEZIER': continue
                    last_sp = spline.bezier_points[-1]
                    # Update the existing last point's outgoing handle from the seed
                    new_hr = mw_inv @ bz[0]['hr']
                    last_sp.handle_right = new_hr
                    last_sp.handle_right_type = 'FREE'
                    # Append new points — FREE type keeps drawn positions exact
                    old_count = len(spline.bezier_points)
                    spline.bezier_points.add(len(new_bz))
                    for i, bp in enumerate(new_bz):
                        sp = spline.bezier_points[old_count + i]
                        sp.handle_left_type  = 'FREE'
                        sp.handle_right_type = 'FREE'
                        sp.co           = mw_inv @ bp['co']
                        sp.handle_left  = mw_inv @ bp['hl']
                        sp.handle_right = mw_inv @ bp['hr']
                    break
                result_obj          = obj
                self._extend_target = None
            else:
                # In persp/camera view, place the object origin at the camera
                # position so scroll-to-scale works from the camera origin.
                # Pre-offset the source data BEFORE writing to the spline so
                # Blender's cyclic-handle recalculation never sees world coords.
                rv3d_commit = context.region_data
                cam_pos = None
                if rv3d_commit and rv3d_commit.view_perspective in {'PERSP', 'CAMERA'}:
                    cam_pos = rv3d_commit.view_matrix.inverted().to_translation()
                bz_write = ([{'co': bp['co'] - cam_pos,
                               'hl': bp['hl'] - cam_pos,
                               'hr': bp['hr'] - cam_pos} for bp in bz]
                            if cam_pos is not None else bz)
                curve_data             = bpy.data.curves.new("BezierDraw", type='CURVE')
                curve_data.dimensions  = '3D'
                curve_data.resolution_u = 12
                spline = curve_data.splines.new('BEZIER')
                spline.bezier_points.add(len(bz_write) - 1)
                _cusp_set = _write_bz_pts(spline, bz_write)
                if self._closed and len(bz_write) >= 3:
                    bpts = spline.bezier_points
                    # Freeze ALIGNED handles to FREE so Blender does not
                    # recalculate their positions when use_cyclic_u is set.
                    for bp in bpts:
                        if bp.handle_left_type  == 'ALIGNED': bp.handle_left_type  = 'FREE'
                        if bp.handle_right_type == 'ALIGNED': bp.handle_right_type = 'FREE'
                    spline.use_cyclic_u = True
                    if self._sharp_close:
                        # Alt+RMB: sharp FREE corner at the seam (handles at co).
                        bpts[0].handle_left_type   = 'FREE'
                        bpts[0].handle_left        = bpts[0].co.copy()
                        bpts[-1].handle_right_type = 'FREE'
                        bpts[-1].handle_right      = bpts[-1].co.copy()
                    else:
                        # Shift+Alt+RMB: smooth — mirror the outward handle
                        # so the seam has tangent continuity.
                        co = bpts[0].co.copy()
                        hr = bpts[0].handle_right.copy()
                        bpts[0].handle_left_type = 'FREE'
                        bpts[0].handle_left      = 2 * co - hr

                        co = bpts[-1].co.copy()
                        hl = bpts[-1].handle_left.copy()
                        bpts[-1].handle_right_type = 'FREE'
                        bpts[-1].handle_right      = 2 * co - hl
                else:
                    spline.use_cyclic_u = False
                obj = bpy.data.objects.new("BezierDraw", curve_data)
                context.collection.objects.link(obj)
                if cam_pos is not None:
                    obj.location = cam_pos
                result_obj             = obj
                obj["bb_cusp_indices"] = sorted(_cusp_set)
                self._undo_state       = ('obj', obj, None)

            for _o in context.view_layer.objects: _o.select_set(False)
            context.view_layer.objects.active = result_obj
            result_obj.select_set(True)

            props.draw_mode       = 'NONE'
            self._last_obj        = result_obj
            self._nudging         = True
            self._last_mode       = 'BEZIER'
            self._bezier_pts      = []
            self._bezier_dragging = False
            self._mouse_3d        = None
            self._closed          = False
            self._sharp_close     = False
            if self._draw_plane:
                self._last_plane_n = self._draw_plane[1].copy()
            self._draw_plane      = None
            self._nudge_header(context)
            return

        if len(pts) < 2:
            self.report({'INFO'}, "BB Poly Draw: need at least 2 points")
            return

        me  = bpy.data.meshes.new("PolyDraw")
        obj = bpy.data.objects.new("PolyDraw", me)
        context.collection.objects.link(obj)
        bm = bmesh.new()

        # Appending to an existing polygon (face mesh): the added shape is
        # implicitly a closed polygon too, so plain RMB closes it — no Alt+RMB.
        if (self._append_target and self._append_target.type == 'MESH'
                and self._append_target.data.polygons):
            self._make_face = True
            self._closed    = True

        # Fill a face for N-Gon / Hole modes, or when a polyline was closed
        # with Alt+RMB (self._make_face). Otherwise build an edge polyline.
        make_face = (mode in {'NGON', 'HOLE'} or self._make_face)
        if make_face and len(pts) >= 3:
            # Compute Newell normal and ensure face winds toward the viewer
            n_pts  = len(pts)
            newell = Vector((0, 0, 0))
            for i in range(n_pts):
                a = pts[i]; b = pts[(i + 1) % n_pts]
                newell.x += (a.y - b.y) * (a.z + b.z)
                newell.y += (a.z - b.z) * (a.x + b.x)
                newell.z += (a.x - b.x) * (a.y + b.y)
            rv3d_c   = context.region_data
            view_dir = (rv3d_c.view_rotation @ Vector((0, 0, -1))) if rv3d_c else Vector((0, 0, 1))
            if newell.length > 1e-6 and newell.dot(view_dir) > 0:
                pts = list(reversed(pts))
            verts = [bm.verts.new(p) for p in pts]
            bm.faces.new(verts)
        else:
            verts = [bm.verts.new(p) for p in pts]
            for i in range(len(verts) - 1):
                bm.edges.new((verts[i], verts[i + 1]))
            if self._closed and len(verts) >= 3:
                bm.edges.new((verts[-1], verts[0]))

        bm.to_mesh(me)
        bm.free()

        # In persp/camera view, place the object origin at the camera position
        # (skip for HOLE and append — those use world-space transforms directly)
        rv3d     = context.region_data
        is_persp = rv3d and rv3d.view_perspective in {'PERSP', 'CAMERA'}
        if is_persp and mode != 'HOLE' and not self._append_target:
            cam_pos = rv3d.view_matrix.inverted().to_translation()
            for v in me.vertices:
                v.co -= cam_pos
            me.update()
            obj.location = cam_pos

        if mode == 'HOLE' and self._target:
            # Curve targets get converted to mesh during the cut — can't snapshot
            # the Curve datablock for undo, so disable single-step undo for them.
            if self._target.type == 'MESH':
                self._undo_state = ('mesh', self._target, self._target.data.copy())
            else:
                self._undo_state = None
            self._cut_hole(context, obj, self._target)
            result_obj = self._target
        elif self._append_target:
            self._undo_state = ('mesh', self._append_target, self._append_target.data.copy())
            self._merge_into(context, obj, self._append_target)
            result_obj = self._append_target
            self._append_target = None
        else:
            for _o in context.view_layer.objects: _o.select_set(False)
            context.view_layer.objects.active = obj
            obj.select_set(True)
            result_obj       = obj
            self._undo_state = ('obj', obj, None)

        props.draw_mode = 'NONE'

        # Enter nudge phase
        self._last_obj = result_obj
        self._nudging  = True
        if mode == 'HOLE' and self._pre_hole_mode is not None:
            self._last_mode     = self._pre_hole_mode
            self._pre_hole_mode = None
        else:
            self._last_mode = mode
        self._points        = []
        self._mouse_3d      = None
        self._closed        = False
        self._make_face     = False
        # Keep plane normal so vertex nudge can constrain to it after commit
        if self._draw_plane:
            self._last_plane_n = self._draw_plane[1].copy()
        self._draw_plane    = None
        self._vn_hover      = None
        self._vn_grab       = None
        self._vn_plane      = None
        self._nudge_header(context)

    # ── toggle a committed polyline mesh open/closed ─────────────

    def _toggle_mesh_closed(self, context):
        """Alt+RMB on a committed simple polyline/polygon mesh: toggle between
        open (edge chain, no face) and closed (closing edge + filled N-Gon).
        Mirrors the curve close behaviour. No-op on meshes that aren't a single
        open chain (so hole / merged / complex meshes are left untouched)."""
        obj = self._last_obj
        if not obj or obj.type != 'MESH':
            return

        bm = bmesh.new()
        bm.from_mesh(obj.data)
        bm.verts.ensure_lookup_table()
        bm.edges.ensure_lookup_table()

        if bm.faces:
            # ── OPEN: drop the face, then remove the closing edge (the edge
            # joining the lowest- and highest-index verts added on close). ──
            bmesh.ops.delete(bm, geom=list(bm.faces), context='FACES_ONLY')
            bm.edges.ensure_lookup_table()
            if bm.verts:
                idxs   = [v.index for v in bm.verts]
                lo, hi = min(idxs), max(idxs)
                for e in bm.edges:
                    if {e.verts[0].index, e.verts[1].index} == {lo, hi}:
                        bm.edges.remove(e)
                        break
        else:
            # ── CLOSE: require a single open chain (2 endpoints, rest degree 2). ──
            deg = {v.index: 0 for v in bm.verts}
            for e in bm.edges:
                deg[e.verts[0].index] += 1
                deg[e.verts[1].index] += 1
            endpoints = [i for i, d in deg.items() if d == 1]
            if len(bm.verts) < 3 or any(d > 2 for d in deg.values()) or len(endpoints) != 2:
                bm.free()
                self.report({'INFO'}, "BB Poly Draw: not a simple open polyline to close")
                return

            adj = {v.index: [] for v in bm.verts}
            for e in bm.edges:
                adj[e.verts[0].index].append(e.verts[1].index)
                adj[e.verts[1].index].append(e.verts[0].index)
            order = [endpoints[0]]
            prev, cur = None, endpoints[0]
            while True:
                nxts = [w for w in adj[cur] if w != prev]
                if not nxts:
                    break
                prev, cur = cur, nxts[0]
                order.append(cur)
                if cur == endpoints[1]:
                    break

            vmap  = {v.index: v for v in bm.verts}
            verts = [vmap[i] for i in order]

            if not any({e.verts[0].index, e.verts[1].index} == {order[0], order[-1]}
                       for e in bm.edges):
                bm.edges.new((verts[0], verts[-1]))

            # Wind the face toward the viewer (Newell normal, world space).
            mw     = obj.matrix_world
            wpts   = [mw @ v.co for v in verts]
            n      = len(wpts)
            newell = Vector((0, 0, 0))
            for i in range(n):
                a = wpts[i]; b = wpts[(i + 1) % n]
                newell.x += (a.y - b.y) * (a.z + b.z)
                newell.y += (a.z - b.z) * (a.x + b.x)
                newell.z += (a.x - b.x) * (a.y + b.y)
            rv3d     = context.region_data
            view_dir = (rv3d.view_rotation @ Vector((0, 0, -1))) if rv3d else Vector((0, 0, 1))
            face_verts = (list(reversed(verts))
                          if (newell.length > 1e-6 and newell.dot(view_dir) > 0)
                          else verts)
            try:
                bm.faces.new(face_verts)
            except ValueError:
                pass   # face already exists

        bm.to_mesh(obj.data)
        obj.data.update()
        bm.free()

    # ── 2D polygon union ─────────────────────────────────────────

    def _merge_into(self, context, src_obj, dst_obj):
        """
        2D polygon union on the shared draw plane.
        Merges coplanar faces cleanly with no leftover intersection geometry.
        Falls back to raw bmesh join for edge-only geometry.
        """
        context.view_layer.update()

        def seg_isect(p1, p2, p3, p4, eps=1e-8):
            dx1, dy1 = p2[0]-p1[0], p2[1]-p1[1]
            dx2, dy2 = p4[0]-p3[0], p4[1]-p3[1]
            cross = dx1*dy2 - dy1*dx2
            if abs(cross) < 1e-12:
                return None
            dx3, dy3 = p3[0]-p1[0], p3[1]-p1[1]
            t = (dx3*dy2 - dy3*dx2) / cross
            u = (dx3*dy1 - dy3*dx1) / cross
            if eps < t < 1-eps and eps < u < 1-eps:
                return t, p1[0]+t*dx1, p1[1]+t*dy1
            return None

        def point_in_poly(p, poly):
            x, y = p; inside = False; j = len(poly)-1
            for i in range(len(poly)):
                xi, yi = poly[i]; xj, yj = poly[j]
                if ((yi > y) != (yj > y)) and x < (xj-xi)*(y-yi)/(yj-yi)+xi:
                    inside = not inside
                j = i
            return inside

        def augment(poly, other):
            out = []
            for i in range(len(poly)):
                p1 = poly[i]; p2 = poly[(i+1) % len(poly)]
                out.append(p1)
                hits = []
                for j in range(len(other)):
                    r = seg_isect(p1, p2, other[j], other[(j+1) % len(other)])
                    if r:
                        hits.append(r)
                for _, ix, iy in sorted(hits):
                    out.append((ix, iy))
            return out

        def poly_union(pa, pb):
            aug_a = augment(pa, pb)
            aug_b = augment(pb, pa)
            if len(aug_a) == len(pa):           # no intersections
                if point_in_poly(pa[0], pb): return [pb]
                if point_in_poly(pb[0], pa): return [pa]
                return [pa, pb]
            PR = 5
            def sn(p): return (round(p[0], PR), round(p[1], PR))
            edges = []
            for i in range(len(aug_a)):
                p1, p2 = aug_a[i], aug_a[(i+1) % len(aug_a)]
                if not point_in_poly(((p1[0]+p2[0])/2, (p1[1]+p2[1])/2), pb):
                    edges.append((sn(p1), sn(p2)))
            for i in range(len(aug_b)):
                p1, p2 = aug_b[i], aug_b[(i+1) % len(aug_b)]
                if not point_in_poly(((p1[0]+p2[0])/2, (p1[1]+p2[1])/2), pa):
                    edges.append((sn(p1), sn(p2)))
            if not edges:
                return [pb] if point_in_poly(pa[0], pb) else [pa]
            adj = defaultdict(list)
            for p1, p2 in edges:
                adj[p1].append(p2)
            def best_next(prv, cur, cands):
                if len(cands) == 1: return cands[0]
                rx, ry = cur[0]-prv[0], cur[1]-prv[1]
                best = cands[0]; best_a = -4.0
                for c in cands:
                    dx, dy = c[0]-cur[0], c[1]-cur[1]
                    a = math.atan2(rx*dy - ry*dx, rx*dx + ry*dy)
                    if a > best_a: best_a = a; best = c
                return best
            visited = set(); polys = []
            for start in list(adj.keys()):
                for fn in list(adj[start]):
                    if (start, fn) in visited: continue
                    chain = [start]; visited.add((start, fn))
                    prv = start; cur = fn; ok = True
                    for _ in range(len(edges) + 5):
                        if cur == start: break
                        chain.append(cur)
                        cands = [v for v in adj[cur] if (cur, v) not in visited]
                        if not cands: ok = False; break
                        nxt = best_next(prv, cur, cands)
                        visited.add((cur, nxt)); prv = cur; cur = nxt
                    else:
                        ok = False
                    if ok and cur == start and len(chain) >= 3:
                        polys.append(chain)
            return polys if polys else [pa, pb]

        def face_verts_world(obj):
            mw = obj.matrix_world
            bm2 = bmesh.new(); bm2.from_mesh(obj.data)
            result = [[mw @ v.co for v in f.verts] for f in bm2.faces]
            bm2.free(); return result

        src_faces = face_verts_world(src_obj)
        dst_faces = face_verts_world(dst_obj)

        if not src_faces or not dst_faces:
            # Edge-only: raw bmesh join
            dst_inv = dst_obj.matrix_world.inverted()
            src_mw  = src_obj.matrix_world
            bm_dst  = bmesh.new(); bm_dst.from_mesh(dst_obj.data)
            bm_src  = bmesh.new(); bm_src.from_mesh(src_obj.data)
            nv = [bm_dst.verts.new(dst_inv @ (src_mw @ v.co)) for v in bm_src.verts]
            bm_dst.verts.index_update()
            for e in bm_src.edges:
                try: bm_dst.edges.new((nv[e.verts[0].index], nv[e.verts[1].index]))
                except ValueError: pass
            for f in bm_src.faces:
                try: bm_dst.faces.new([nv[v.index] for v in f.verts])
                except ValueError: pass
            bm_src.free(); bm_dst.to_mesh(dst_obj.data); dst_obj.data.update(); bm_dst.free()
            bpy.data.objects.remove(src_obj, do_unlink=True)
            for _o in context.view_layer.objects: _o.select_set(False)
            context.view_layer.objects.active = dst_obj; dst_obj.select_set(True)
            return

        # Build 2D coordinate system from first dst face
        ref = dst_faces[0]; n = len(ref)
        normal = Vector((0, 0, 0))
        for i in range(n):
            a = ref[i]; b = ref[(i+1) % n]
            normal.x += (a.y-b.y)*(a.z+b.z)
            normal.y += (a.z-b.z)*(a.x+b.x)
            normal.z += (a.x-b.x)*(a.y+b.y)
        if normal.length < 1e-6: normal = Vector((0, 0, 1))
        else: normal.normalize()

        origin = ref[0]
        lx = ref[1] - ref[0]; lx -= lx.dot(normal) * normal
        if lx.length < 1e-6:
            lx = Vector((1, 0, 0)); lx -= lx.dot(normal) * normal
            if lx.length < 1e-6: lx = Vector((0, 0, 1))
        lx.normalize()
        ly = lx.cross(normal).normalized()

        def to2d(p): d = p - origin; return (d.dot(lx), d.dot(ly))
        def to3d(p): return origin + lx*p[0] + ly*p[1]

        current = [[to2d(v) for v in face] for face in dst_faces]
        for src_face in src_faces:
            sp = [to2d(v) for v in src_face]
            merged = False
            for i, dp in enumerate(current):
                result = poly_union(dp, sp)
                if len(result) == 1:
                    current[i] = result[0]; merged = True; break
            if not merged:
                current.append(sp)

        bpy.data.objects.remove(src_obj, do_unlink=True)
        me      = dst_obj.data
        dst_inv = dst_obj.matrix_world.inverted()
        bm2     = bmesh.new()
        for poly_2d in current:
            bv = [bm2.verts.new(dst_inv @ to3d(p)) for p in poly_2d]
            try: bm2.faces.new(bv)
            except Exception: pass
        bm2.to_mesh(me); bm2.free(); me.update()

        for _o in context.view_layer.objects: _o.select_set(False)
        context.view_layer.objects.active = dst_obj
        dst_obj.select_set(True)

    # ── hole cutting ─────────────────────────────────────────────

    def _cut_hole(self, context, cutter_obj, target_obj):
        """Route to boolean or polyline cutter based on target geometry."""
        if target_obj.type == 'CURVE':
            self._cut_hole_curve(context, cutter_obj, target_obj)
            return
        target_bm = bmesh.new()
        target_bm.from_mesh(target_obj.data)
        has_faces = len(target_bm.faces) > 0
        target_bm.free()
        if has_faces:
            self._cut_hole_boolean(context, cutter_obj, target_obj)
        else:
            self._cut_hole_polyline(context, cutter_obj, target_obj)

    def _cut_hole_curve(self, context, cutter_obj, target_obj):
        """
        Cut a NURBS/Bézier curve by the hole polygon:
        - control points inside the polygon are removed
        - segments that CROSS the polygon boundary get a new control point
          inserted at the intersection (linear approximation for NURBS,
          de Casteljau for Bézier) so the cut happens at the right position.
        """
        hole_pts = [cutter_obj.matrix_world @ v.co for v in cutter_obj.data.vertices]
        if len(hole_pts) < 3:
            bpy.data.objects.remove(cutter_obj, do_unlink=True)
            return

        # ── 2-D projection basis (Newell normal) ─────────────────
        normal = Vector((0, 0, 0))
        nh = len(hole_pts)
        for i in range(nh):
            a = hole_pts[i]; b = hole_pts[(i + 1) % nh]
            normal.x += (a.y - b.y) * (a.z + b.z)
            normal.y += (a.z - b.z) * (a.x + b.x)
            normal.z += (a.x - b.x) * (a.y + b.y)
        normal = Vector((0, 0, 1)) if normal.length < 1e-6 else normal.normalized()
        local_x = hole_pts[1] - hole_pts[0]
        local_x -= local_x.dot(normal) * normal
        if local_x.length < 1e-6:
            local_x = normal.orthogonal()
        local_x.normalize()
        local_y = normal.cross(local_x).normalized()
        origin  = hole_pts[0]

        def to_2d(p):
            d = p - origin
            return (d.dot(local_x), d.dot(local_y))

        hole_2d = [to_2d(p) for p in hole_pts]

        def point_in_polygon(px, py, poly):
            inside = False; j = len(poly) - 1
            for i in range(len(poly)):
                xi, yi = poly[i]; xj, yj = poly[j]
                if ((yi > py) != (yj > py) and
                        px < (xj - xi) * (py - yi) / (yj - yi + 1e-12) + xi):
                    inside = not inside
                j = i
            return inside

        def seg_intersect_2d(a1, a2, b1, b2):
            dx = a2[0]-a1[0]; dy = a2[1]-a1[1]
            ex = b2[0]-b1[0]; ey = b2[1]-b1[1]
            denom = dx*ey - dy*ex
            if abs(denom) < 1e-10: return None
            fx = b1[0]-a1[0]; fy = b1[1]-a1[1]
            t = (fx*ey - fy*ex) / denom
            u = (fx*dy - fy*dx) / denom
            return t if 0.0 < t < 1.0 and 0.0 <= u <= 1.0 else None

        def find_boundary_t(p0_2d, p1_2d):
            """Return best t in (0,1) where segment p0→p1 crosses the hole boundary."""
            best = None
            for k in range(nh):
                t = seg_intersect_2d(p0_2d, p1_2d, hole_2d[k], hole_2d[(k+1) % nh])
                if t is not None and (best is None or abs(t-0.5) < abs(best-0.5)):
                    best = t
            return best

        mw      = target_obj.matrix_world
        mw_inv  = mw.inverted()
        curve_data = target_obj.data

        for spline in list(curve_data.splines):

            # ── BÉZIER ───────────────────────────────────────────
            if spline.type == 'BEZIER':
                bps    = spline.bezier_points
                n      = len(bps)
                bp_w   = [mw @ bp.co for bp in bps]
                inside = [point_in_polygon(*to_2d(pw), hole_2d) for pw in bp_w]

                if not any(inside):
                    continue  # nothing to cut

                # Build new point list as dicts; also track pending hl for next pt
                entries = []   # list of dict(co, hl, hr, hl_t, hr_t)
                pending_hl = None  # modified handle_left for the next appended pt

                for i in range(n):
                    j = i + 1
                    has_next = (j < n)  # no wrap for open splines

                    if not inside[i]:
                        e = dict(co   = bps[i].co.copy(),
                                 hl   = bps[i].handle_left.copy(),
                                 hr   = bps[i].handle_right.copy(),
                                 hl_t = bps[i].handle_left_type,
                                 hr_t = bps[i].handle_right_type)
                        if pending_hl is not None:
                            e['hl'] = pending_hl; e['hl_t'] = 'FREE'
                            pending_hl = None
                        entries.append(e)

                    if has_next and inside[i] != inside[j]:
                        t = find_boundary_t(to_2d(bp_w[i]), to_2d(bp_w[j]))
                        if t is not None:
                            # De Casteljau split of cubic Bézier at t
                            P0 = bp_w[i]
                            P1 = mw @ bps[i].handle_right
                            P2 = mw @ bps[j].handle_left
                            P3 = bp_w[j]
                            P01   = P0.lerp(P1, t)
                            P12   = P1.lerp(P2, t)
                            P23   = P2.lerp(P3, t)
                            P012  = P01.lerp(P12, t)
                            P123  = P12.lerp(P23, t)
                            P0123 = P012.lerp(P123, t)

                            if not inside[i]:  # outside→inside: trim right end of kept seg
                                if entries:
                                    entries[-1]['hr']   = mw_inv @ P01
                                    entries[-1]['hr_t'] = 'FREE'
                            else:              # inside→outside: next pt gets trimmed hl
                                pending_hl = mw_inv @ P23

                            entries.append(dict(co   = mw_inv @ P0123,
                                                hl   = mw_inv @ P012,
                                                hr   = mw_inv @ P123,
                                                hl_t = 'FREE', hr_t = 'FREE'))

                if len(entries) < 2:
                    curve_data.splines.remove(spline)
                    continue
                if len(entries) == n and not any(inside):
                    continue  # nothing changed

                new_sp = curve_data.splines.new('BEZIER')
                new_sp.bezier_points.add(len(entries) - 1)
                for idx, e in enumerate(entries):
                    bp = new_sp.bezier_points[idx]
                    bp.co = e['co']; bp.handle_left = e['hl']; bp.handle_right = e['hr']
                    bp.handle_left_type = e['hl_t']; bp.handle_right_type = e['hr_t']
                curve_data.splines.remove(spline)

            # ── NURBS / POLY ──────────────────────────────────────
            else:
                pts    = spline.points
                n      = len(pts)
                pw     = [mw @ Vector(p.co.xyz) for p in pts]
                inside = [point_in_polygon(*to_2d(p), hole_2d) for p in pw]

                if not any(inside):
                    continue

                new_co4 = []  # list of 4-tuples (local x,y,z,w)

                for i in range(n):
                    j = i + 1
                    has_next = (j < n) or spline.use_cyclic_u
                    jj = j % n

                    if not inside[i]:
                        new_co4.append(tuple(pts[i].co))

                    if has_next and inside[i] != inside[jj]:
                        t = find_boundary_t(to_2d(pw[i]), to_2d(pw[jj]))
                        if t is not None:
                            new_w = pw[i].lerp(pw[jj], t)
                            nl    = mw_inv @ new_w
                            new_co4.append((*nl, 1.0))

                if len(new_co4) < 2:
                    curve_data.splines.remove(spline)
                    continue
                if len(new_co4) == n and not any(inside):
                    continue

                new_sp = curve_data.splines.new(spline.type)
                new_sp.points.add(len(new_co4) - 1)
                for idx, co in enumerate(new_co4):
                    new_sp.points[idx].co = co
                if spline.type == 'NURBS':
                    new_sp.order_u        = min(spline.order_u, len(new_co4))
                    new_sp.use_endpoint_u = spline.use_endpoint_u
                    new_sp.use_cyclic_u   = (spline.use_cyclic_u and
                                             len(new_co4) >= new_sp.order_u)
                curve_data.splines.remove(spline)

        bpy.data.objects.remove(cutter_obj, do_unlink=True)
        for _o in context.view_layer.objects: _o.select_set(False)
        context.view_layer.objects.active = target_obj
        target_obj.select_set(True)

    def _cut_hole_boolean(self, context, cutter_obj, target_obj):
        """Build a prism from the cutter polygon and boolean-difference it into the target."""
        me = cutter_obj.data

        # Capture the flat target's plane (local space) BEFORE the boolean, so we
        # can strip any prism residue afterwards. A DIFFERENCE on a flat, open
        # face leaves the cutter prism's side walls/caps when the hole is fully
        # enclosed (a "middle" hole) — those faces are not coplanar with the face.
        _tgt_plane = None
        tgt_me = target_obj.data
        if tgt_me.polygons:
            _big   = max(tgt_me.polygons, key=lambda p: p.area)
            _tnorm = _big.normal.copy()
            _tpt   = tgt_me.vertices[_big.vertices[0]].co.copy()
            _tol   = 1e-3 * max(1.0, max(target_obj.dimensions))
            _tgt_plane = (_tnorm, _tpt, _tol)

        context.view_layer.update()
        mw_cutter = cutter_obj.matrix_world
        vcos = [mw_cutter @ v.co for v in me.vertices]
        n    = len(vcos)
        if n < 3:
            bpy.data.objects.remove(cutter_obj, do_unlink=True)
            return

        # Polygon normal via Newell
        normal = Vector((0, 0, 0))
        for i in range(n):
            a = vcos[i]; b = vcos[(i + 1) % n]
            normal.x += (a.y - b.y) * (a.z + b.z)
            normal.y += (a.z - b.z) * (a.x + b.x)
            normal.z += (a.x - b.x) * (a.y + b.y)
        normal = Vector((0, 0, 1)) if normal.length < 1e-6 else normal.normalized()

        # Extrude prism to fully span the target bounding volume
        context.view_layer.update()
        world_verts = [target_obj.matrix_world @ v.co for v in target_obj.data.vertices]
        if not world_verts:
            bpy.data.objects.remove(cutter_obj, do_unlink=True)
            return

        poly_center = sum(vcos, Vector()) / n
        dots        = [(c - poly_center).dot(normal) for c in world_verts]
        extend_pos  = max(dots) + 1.0
        extend_neg  = min(dots) - 1.0

        top = [co + normal * extend_pos for co in vcos]
        bot = [co + normal * extend_neg for co in vcos]

        bm2 = bmesh.new()
        tv  = [bm2.verts.new(co) for co in top]
        bv  = [bm2.verts.new(co) for co in bot]
        bm2.faces.new(tv)
        bm2.faces.new(list(reversed(bv)))
        for i in range(n):
            j = (i + 1) % n
            bm2.faces.new([tv[i], bv[i], bv[j], tv[j]])
        # Recalculate normals — the side quads' winding depends on the input
        # polygon orientation; letting bmesh sort it out is more reliable than
        # trying to guarantee CCW winding manually.
        bm2.normal_update()
        bmesh.ops.recalc_face_normals(bm2, faces=bm2.faces[:])
        bm2.to_mesh(me); me.update(); bm2.free()

        context.view_layer.update()
        for _o in context.view_layer.objects: _o.select_set(False)
        context.view_layer.objects.active = target_obj
        target_obj.select_set(True)

        bmod           = target_obj.modifiers.new("_PD_Bool", 'BOOLEAN')
        bmod.operation = 'DIFFERENCE'
        bmod.object    = cutter_obj
        bmod.solver    = 'EXACT'
        with context.temp_override(active_object=target_obj, object=target_obj,
                                   selected_objects=[target_obj]):
            bpy.ops.object.modifier_apply(modifier="_PD_Bool")
        bpy.data.objects.remove(cutter_obj, do_unlink=True)

        # ── Strip prism residue ──────────────────────────────────────
        # Keep only faces coplanar with the original flat target; drop the
        # cutter prism's leftover side walls / caps (off-plane geometry that a
        # boolean on an open face leaves behind for an enclosed "middle" hole).
        if _tgt_plane is not None:
            tnorm, tpt, tol = _tgt_plane
            bm = bmesh.new()
            bm.from_mesh(target_obj.data)
            kill = [f for f in bm.faces
                    if not (abs(f.normal.dot(tnorm)) > 0.999
                            and abs((f.calc_center_median() - tpt).dot(tnorm)) < tol)]
            if kill and len(kill) < len(bm.faces):
                bmesh.ops.delete(bm, geom=kill, context='FACES')
                loose = [v for v in bm.verts if not v.link_faces]
                if loose:
                    bmesh.ops.delete(bm, geom=loose, context='VERTS')
                bm.to_mesh(target_obj.data); target_obj.data.update()
            bm.free()

    def _cut_hole_polyline(self, context, cutter_obj, target_obj):
        """
        For edge-only targets: delete vertices inside the drawn polygon and
        trim edges that cross the boundary by inserting intersection vertices.
        """
        hole_pts = [cutter_obj.matrix_world @ v.co for v in cutter_obj.data.vertices]
        if len(hole_pts) < 3:
            bpy.data.objects.remove(cutter_obj, do_unlink=True)
            return

        # Build local 2D basis on the hole plane via Newell normal
        normal = Vector((0, 0, 0))
        n = len(hole_pts)
        for i in range(n):
            a = hole_pts[i]; b = hole_pts[(i + 1) % n]
            normal.x += (a.y - b.y) * (a.z + b.z)
            normal.y += (a.z - b.z) * (a.x + b.x)
            normal.z += (a.x - b.x) * (a.y + b.y)
        normal = Vector((0, 0, 1)) if normal.length < 1e-6 else normal.normalized()

        local_x = hole_pts[1] - hole_pts[0]
        local_x -= local_x.dot(normal) * normal
        if local_x.length < 1e-6:
            local_x = normal.orthogonal()
        local_x.normalize()
        local_y = normal.cross(local_x).normalized()
        origin  = hole_pts[0]

        def to_2d(p):
            d = p - origin
            return (d.dot(local_x), d.dot(local_y))

        hole_2d = [to_2d(p) for p in hole_pts]

        def point_in_polygon(px, py, poly):
            inside = False; j = len(poly) - 1
            for i in range(len(poly)):
                xi, yi = poly[i]; xj, yj = poly[j]
                if ((yi > py) != (yj > py) and
                        px < (xj - xi) * (py - yi) / (yj - yi + 1e-12) + xi):
                    inside = not inside
                j = i
            return inside

        def seg_intersect_2d(a1, a2, b1, b2):
            dx = a2[0]-a1[0]; dy = a2[1]-a1[1]
            ex = b2[0]-b1[0]; ey = b2[1]-b1[1]
            denom = dx*ey - dy*ex
            if abs(denom) < 1e-10: return None
            fx = b1[0]-a1[0]; fy = b1[1]-a1[1]
            t = (fx*ey - fy*ex) / denom
            u = (fx*dy - fy*dx) / denom
            return t if 0.0 <= t <= 1.0 and 0.0 <= u <= 1.0 else None

        bm = bmesh.new()
        bm.from_mesh(target_obj.data)
        bm.verts.ensure_lookup_table()
        bm.edges.ensure_lookup_table()
        mw = target_obj.matrix_world

        vert_inside = {v.index: point_in_polygon(*to_2d(mw @ v.co), hole_2d) for v in bm.verts}

        for edge in list(bm.edges):
            v0, v1 = edge.verts[0], edge.verts[1]
            if vert_inside.get(v0.index) == vert_inside.get(v1.index):
                continue
            p0_2d = to_2d(mw @ v0.co); p1_2d = to_2d(mw @ v1.co)
            best_t = None
            for i in range(n):
                t = seg_intersect_2d(p0_2d, p1_2d, hole_2d[i], hole_2d[(i+1) % n])
                if t is not None and (best_t is None or abs(t-0.5) < abs(best_t-0.5)):
                    best_t = t
            if best_t is None:
                continue
            wp_new   = (mw @ v0.co).lerp(mw @ v1.co, best_t)
            co_local = target_obj.matrix_world.inverted() @ wp_new
            _, new_vert = bmesh.utils.edge_split(edge, v0, best_t)
            new_vert.co = co_local
            vert_inside[new_vert.index] = False

        bm.verts.ensure_lookup_table()
        bmesh.ops.delete(bm,
            geom=[v for v in bm.verts if vert_inside.get(v.index, False)],
            context='VERTS')
        bm.to_mesh(target_obj.data); target_obj.data.update(); bm.free()

        bpy.data.objects.remove(cutter_obj, do_unlink=True)
        for _o in context.view_layer.objects: _o.select_set(False)
        context.view_layer.objects.active = target_obj
        target_obj.select_set(True)

    def _vn_find_nearest(self, context, mx, my, threshold_px=None):
        """Scan all draggable points: regular pts, bezier anchors/handles, committed mesh/curve.
        Returns (source, idx, world_co) or None.
        threshold_px defaults to the 'Edit Roll-Over Tolerance' preference."""
        if threshold_px is None:
            prefs        = _get_prefs(context)
            threshold_px = prefs.grab_tolerance if prefs else _GRAB_PX
        threshold = threshold_px
        best_d    = threshold
        best      = None

        # ── in-progress regular points ───────────────────────────
        for i, p in enumerate(self._points):
            s = _project_to_screen(context, p)
            if s is None: continue
            d = _screen_dist(mx, my, s.x, s.y)
            if d < best_d:
                best_d = d; best = ('pts', i, p.copy())

        # ── in-progress Bézier anchors and handles ────────────────
        for i, bp in enumerate(self._bezier_pts):
            for src, co in [('bzco', bp['co']), ('bzhr', bp['hr']), ('bzhl', bp['hl'])]:
                if src != 'bzco' and (bp['co'] - co).length < 1e-4:
                    continue   # skip collapsed handles — invisible under anchor
                s = _project_to_screen(context, co)
                if s is None: continue
                d = _screen_dist(mx, my, s.x, s.y)
                if d < best_d:
                    best_d = d; best = (src, i, co.copy())

        if self._nudging and self._last_obj:

            # ── committed mesh vertices ───────────────────────────
            if self._last_obj.type == 'MESH':
                obj = self._last_obj
                mw  = obj.matrix_world
                for v in obj.data.vertices:
                    wp = mw @ v.co
                    s  = _project_to_screen(context, wp)
                    if s is None: continue
                    d = _screen_dist(mx, my, s.x, s.y)
                    if d < best_d:
                        best_d = d; best = ('obj', v.index, wp.copy())

            # ── committed Bézier curve anchors and handles ────────
            elif self._last_obj.type == 'CURVE':
                obj = self._last_obj
                mw  = obj.matrix_world
                for spline in obj.data.splines:
                    if spline.type == 'BEZIER':
                        bpts   = spline.bezier_points
                        n_bpts = len(bpts)
                        for i, bpt in enumerate(bpts):
                            co_w = mw @ bpt.co
                            hl_w = mw @ bpt.handle_left
                            hr_w = mw @ bpt.handle_right
                            # VECTOR handles: stored value may equal co; recompute
                            if bpt.handle_right_type == 'VECTOR':
                                if spline.use_cyclic_u or i < n_bpts - 1:
                                    nco = mw @ bpts[(i + 1) % n_bpts].co
                                    hr_w = co_w + (nco - co_w) / 3.0
                            if bpt.handle_left_type == 'VECTOR':
                                if spline.use_cyclic_u or i > 0:
                                    pco = mw @ bpts[(i - 1) % n_bpts].co
                                    hl_w = co_w + (pco - co_w) / 3.0
                            # Synthesize a short arm for zero-length (cusp) handles
                            # so they are hittable; matches the display offset above.
                            # Skip seam-crossing handles (bpts[0].hl, bpts[-1].hr on
                            # cyclic splines) — those must stay at co for sharp seams.
                            seam_hl = spline.use_cyclic_u and i == 0
                            seam_hr = spline.use_cyclic_u and i == n_bpts - 1
                            if (hr_w - co_w).length < 1e-4 and not seam_hr and (spline.use_cyclic_u or i < n_bpts - 1):
                                _nb = mw @ bpts[(i + 1) % n_bpts].handle_left
                                if (_nb - co_w).length < 1e-6:
                                    _nb = mw @ bpts[(i + 1) % n_bpts].co
                                hr_w = co_w + (_nb - co_w) * 0.05
                            if (hl_w - co_w).length < 1e-4 and not seam_hl and (spline.use_cyclic_u or i > 0):
                                _pb = mw @ bpts[(i - 1) % n_bpts].handle_right
                                if (_pb - co_w).length < 1e-6:
                                    _pb = mw @ bpts[(i - 1) % n_bpts].co
                                hl_w = co_w + (_pb - co_w) * 0.05
                            for src, co in [('bzco', co_w), ('bzhr', hr_w), ('bzhl', hl_w)]:
                                if src != 'bzco' and (co - co_w).length < 1e-4:
                                    continue
                                s = _project_to_screen(context, co)
                                if s is None: continue
                                d = _screen_dist(mx, my, s.x, s.y)
                                if d < best_d:
                                    best_d = d; best = (src, i, co.copy())
                    elif spline.type == 'NURBS':
                        for i, pt in enumerate(spline.points):
                            co_w = mw @ Vector(pt.co.xyz)
                            s = _project_to_screen(context, co_w)
                            if s is None: continue
                            d = _screen_dist(mx, my, s.x, s.y)
                            if d < best_d:
                                best_d = d; best = ('nurbspt', i, co_w.copy())

        return best

    def _vn_find_nearest_edge_pt(self, context, mx, my):
        """Find the closest point on any edge/curve segment.
        Returns (world_pt, source, seg_idx, t) or None."""
        best_d  = float('inf')
        best    = None
        SAMPLES = 20   # samples per Bézier segment

        # ── segments from in-progress drawn points ───────────────
        # NURBS: trace the evaluated curve, not the straight control hull, so the
        # insert point lands on the curve. Polyline/N-Gon: the points ARE the
        # curve, so the straight segments are correct.
        n = len(self._points)
        if context.scene.polydraw_props.draw_mode == 'NURBS' and n >= 2:
            tess = _nurbs_tessellate(list(self._points), resolution=SAMPLES * n)
            for j in range(len(tess) - 1):
                va = Vector(tess[j]); vb = Vector(tess[j + 1])
                sa = _project_to_screen(context, va)
                sb = _project_to_screen(context, vb)
                if not (sa and sb):
                    continue
                ex, ey = sb.x - sa.x, sb.y - sa.y
                denom  = ex*ex + ey*ey
                if denom < 1e-10:
                    continue
                t = max(0.0, min(1.0, ((mx - sa.x)*ex + (my - sa.y)*ey) / denom))
                d = _screen_dist(mx, my, sa.x + t*ex, sa.y + t*ey)
                if d < best_d:
                    tess_t  = (j + t) / max(len(tess) - 1, 1)
                    seg_idx = max(0, min(n - 2, int(tess_t * (n - 1))))
                    # Cyan preview dot rides the curve (where the cursor is). The
                    # actual control point is placed on the hull at insert time
                    # (see _vn_add_vertex) so the NURBS doesn't deform.
                    best_d  = d
                    best    = (va.lerp(vb, t), 'pts', seg_idx, tess_t)
        else:
            for i in range(n - 1):
                va = self._points[i]
                vb = self._points[i + 1]
                sa = _project_to_screen(context, va)
                sb = _project_to_screen(context, vb)
                if not (sa and sb):
                    continue
                ex, ey = sb.x - sa.x, sb.y - sa.y
                denom  = ex*ex + ey*ey
                if denom < 1e-10:
                    continue
                t = max(0.0, min(1.0, ((mx - sa.x)*ex + (my - sa.y)*ey) / denom))
                d = _screen_dist(mx, my, sa.x + t*ex, sa.y + t*ey)
                if d < best_d:
                    best_d = d
                    best   = (va.lerp(vb, t), 'pts', i, t)

        # ── in-progress Bézier curve segments ────────────────────
        n_bz = len(self._bezier_pts)
        for seg in range(n_bz - 1):
            bp0 = self._bezier_pts[seg]
            bp1 = self._bezier_pts[seg + 1]
            p0, h0r = bp0['co'], bp0['hr']
            h1l, p1 = bp1['hl'], bp1['co']
            best_d_seg = float('inf'); best_t = 0.0; best_wp = p0.copy()
            for j in range(SAMPLES + 1):
                t  = j / SAMPLES; mt = 1.0 - t
                wp = mt**3*p0 + 3*mt**2*t*h0r + 3*mt*t**2*h1l + t**3*p1
                s  = _project_to_screen(context, wp)
                if s:
                    d = _screen_dist(mx, my, s.x, s.y)
                    if d < best_d_seg:
                        best_d_seg = d; best_t = t; best_wp = wp.copy()
            if best_d_seg < best_d:
                best_d = best_d_seg
                best   = (best_wp, 'bzpts', seg, best_t)

        # ── edges of the committed mesh ───────────────────────────
        if self._last_obj and self._last_obj.type == 'MESH':
            obj = self._last_obj
            mw  = obj.matrix_world
            for edge in obj.data.edges:
                va = mw @ obj.data.vertices[edge.vertices[0]].co
                vb = mw @ obj.data.vertices[edge.vertices[1]].co
                sa = _project_to_screen(context, va)
                sb = _project_to_screen(context, vb)
                if not (sa and sb):
                    continue
                ex, ey = sb.x - sa.x, sb.y - sa.y
                denom  = ex*ex + ey*ey
                if denom < 1e-10:
                    continue
                t = max(0.0, min(1.0, ((mx - sa.x)*ex + (my - sa.y)*ey) / denom))
                d = _screen_dist(mx, my, sa.x + t*ex, sa.y + t*ey)
                if d < best_d:
                    best_d = d
                    best   = (va.lerp(vb, t), 'obj', edge.index, t)

        # ── committed Bézier curve segments ──────────────────────
        if self._last_obj and self._last_obj.type == 'CURVE':
            obj = self._last_obj
            mw  = obj.matrix_world
            for spline in obj.data.splines:
                if spline.type == 'BEZIER':
                    bpts     = spline.bezier_points
                    n_sp     = len(bpts)
                    loop_n   = n_sp if spline.use_cyclic_u else n_sp - 1
                    for seg in range(loop_n):
                        bp0 = bpts[seg]; bp1 = bpts[(seg + 1) % n_sp]
                        p0  = mw @ bp0.co;  h0r = mw @ bp0.handle_right
                        h1l = mw @ bp1.handle_left; p1 = mw @ bp1.co
                        best_d_seg = float('inf'); best_t = 0.0; best_wp = p0.copy()
                        for j in range(SAMPLES + 1):
                            t  = j / SAMPLES; mt = 1.0 - t
                            wp = mt**3*p0 + 3*mt**2*t*h0r + 3*mt*t**2*h1l + t**3*p1
                            s  = _project_to_screen(context, wp)
                            if s:
                                d = _screen_dist(mx, my, s.x, s.y)
                                if d < best_d_seg:
                                    best_d_seg = d; best_t = t; best_wp = wp.copy()
                        if best_d_seg < best_d:
                            best_d = best_d_seg
                            best   = (best_wp, 'bzobj', seg, best_t)

                elif spline.type == 'NURBS':
                    ctrl_w = [mw @ Vector(pt.co.xyz) for pt in spline.points]
                    n_sp   = len(ctrl_w)
                    if n_sp < 2: continue
                    tess   = _nurbs_tessellate(ctrl_w, resolution=SAMPLES * n_sp)
                    for j in range(len(tess) - 1):
                        va = Vector(tess[j]); vb = Vector(tess[j + 1])
                        sa = _project_to_screen(context, va)
                        sb = _project_to_screen(context, vb)
                        if not (sa and sb): continue
                        ex, ey = sb.x - sa.x, sb.y - sa.y
                        denom  = ex*ex + ey*ey
                        if denom < 1e-10: continue
                        t = max(0.0, min(1.0, ((mx-sa.x)*ex + (my-sa.y)*ey) / denom))
                        d = _screen_dist(mx, my, sa.x + t*ex, sa.y + t*ey)
                        if d < best_d:
                            tess_t  = (j + t) / max(len(tess) - 1, 1)
                            seg_idx = max(0, min(n_sp - 2, int(tess_t * (n_sp - 1))))
                            # Cyan dot rides the curve; control point lands on the
                            # hull at insert time (see _vn_add_vertex).
                            best_d  = d
                            best    = (va.lerp(vb, t), 'nurbsobj', seg_idx, tess_t)

        return best

    def _vn_delete_vertex(self, context):
        """Remove the currently hovered anchor from _points, bezier_pts, or committed geometry."""
        if not self._vn_hover:
            return
        source, idx, _ = self._vn_hover

        if source == 'pts':
            if 0 <= idx < len(self._points):
                self._points.pop(idx)
                if not self._points:
                    self._draw_plane = None

        elif source == 'bzco':
            # Delete Bézier anchor — handles and their mirrors go with it
            if self._bezier_pts:
                if 0 <= idx < len(self._bezier_pts):
                    self._bezier_pts.pop(idx)
                    if not self._bezier_pts:
                        self._draw_plane = None
            elif self._nudging and self._last_obj and self._last_obj.type == 'CURVE':
                obj = self._last_obj
                mw  = obj.matrix_world
                mw_inv = mw.inverted()
                for spline in obj.data.splines:
                    if spline.type != 'BEZIER': continue
                    bpts = spline.bezier_points
                    if len(bpts) <= 2:
                        break   # don't delete below 2
                    # Collect remaining points in world space, skip the deleted index
                    all_data = [(mw @ bpt.co, mw @ bpt.handle_left, mw @ bpt.handle_right,
                                 bpt.handle_left_type, bpt.handle_right_type)
                                for i, bpt in enumerate(bpts) if i != idx]
                    # Build a fresh curve data to replace the old one
                    old_data = obj.data
                    new_data = bpy.data.curves.new(old_data.name, type='CURVE')
                    new_data.dimensions   = old_data.dimensions
                    new_data.resolution_u = old_data.resolution_u
                    new_sp = new_data.splines.new('BEZIER')
                    new_sp.bezier_points.add(len(all_data) - 1)
                    new_sp.use_cyclic_u   = spline.use_cyclic_u
                    new_sp.order_u        = spline.order_u
                    for i, (co, hl, hr, hlt, hrt) in enumerate(all_data):
                        bpt = new_sp.bezier_points[i]
                        bpt.co = mw_inv @ co
                        bpt.handle_left  = mw_inv @ hl
                        bpt.handle_right = mw_inv @ hr
                        bpt.handle_left_type  = hlt
                        bpt.handle_right_type = hrt
                    obj.data = new_data
                    bpy.data.curves.remove(old_data)
                    break

        elif source == 'bzhr' or source == 'bzhl':
            # Collapse handle back to anchor (makes a corner point)
            if self._bezier_pts and 0 <= idx < len(self._bezier_pts):
                bp = self._bezier_pts[idx]
                if source == 'bzhr':
                    bp['hr'] = bp['co'].copy()
                else:
                    bp['hl'] = bp['co'].copy()
            elif self._nudging and self._last_obj and self._last_obj.type == 'CURVE':
                obj = self._last_obj
                for spline in obj.data.splines:
                    if spline.type != 'BEZIER': continue
                    if 0 <= idx < len(spline.bezier_points):
                        bpt = spline.bezier_points[idx]
                        if source == 'bzhr':
                            bpt.handle_right = bpt.co.copy()
                            bpt.handle_right_type = 'VECTOR'
                        else:
                            bpt.handle_left = bpt.co.copy()
                            bpt.handle_left_type = 'VECTOR'
                    break   # no data.update() needed for curves

        elif source == 'nurbspt':
            if self._nudging and self._last_obj and self._last_obj.type == 'CURVE':
                obj    = self._last_obj
                mw     = obj.matrix_world
                mw_inv = mw.inverted()
                for spline in obj.data.splines:
                    if spline.type != 'NURBS': continue
                    pts = spline.points
                    if len(pts) <= 2: break
                    all_data = [(mw @ Vector(pt.co.xyz), pt.co.w)
                                for i, pt in enumerate(pts) if i != idx]
                    old_data = obj.data
                    new_data = bpy.data.curves.new(old_data.name, type='CURVE')
                    new_data.dimensions   = old_data.dimensions
                    new_data.resolution_u = old_data.resolution_u
                    new_sp = new_data.splines.new('NURBS')
                    new_sp.points.add(len(all_data) - 1)
                    new_sp.order_u        = spline.order_u
                    new_sp.use_endpoint_u = spline.use_endpoint_u
                    new_sp.use_cyclic_u   = spline.use_cyclic_u
                    for i, (co, w) in enumerate(all_data):
                        new_sp.points[i].co = (*(mw_inv @ co), w)
                    obj.data = new_data
                    bpy.data.curves.remove(old_data)
                    break

        elif source == 'obj' and self._last_obj:
            obj = self._last_obj
            bm  = bmesh.new()
            bm.from_mesh(obj.data)
            bm.verts.ensure_lookup_table()
            if idx < len(bm.verts):
                bmesh.ops.delete(bm, geom=[bm.verts[idx]], context='VERTS')
            bm.to_mesh(obj.data); obj.data.update(); bm.free()

    def _vn_add_vertex(self, context, mx, my):
        """Insert a vertex/point on the nearest edge or curve segment."""
        result = self._vn_find_nearest_edge_pt(context, mx, my)
        if result is None:
            return
        world_pt, source, seg_idx, t = result
        self._vn_edge_pt = None
        new_idx  = None

        if source == 'pts':
            # NURBS control points sit off the curve — inserting at the on-curve
            # point (world_pt, the cyan dot) would pull the curve toward it. Place
            # the new control point on the control hull instead so the shape barely
            # changes. Polyline/N-Gon points ARE the curve, so use world_pt as-is.
            if (context.scene.polydraw_props.draw_mode == 'NURBS'
                    and 0 <= seg_idx < len(self._points) - 1):
                n      = len(self._points)
                localf = max(0.0, min(1.0, t * (n - 1) - seg_idx))
                world_pt = self._points[seg_idx].lerp(self._points[seg_idx + 1], localf)
            self._points.insert(seg_idx + 1, world_pt)
            new_idx = seg_idx + 1

        elif source == 'bzpts':
            # De Casteljau split of in-progress Bézier segment
            bp0 = self._bezier_pts[seg_idx]
            bp1 = self._bezier_pts[seg_idx + 1]
            nh0r, nhl, nco, nhr, nh1l = _bez_split(
                bp0['co'], bp0['hr'], bp1['hl'], bp1['co'], t)
            bp0['hr'] = nh0r
            self._bezier_pts.insert(seg_idx + 1,
                {'co': nco.copy(), 'hl': nhl.copy(), 'hr': nhr.copy()})
            self._bezier_pts[seg_idx + 2]['hl'] = nh1l   # was bp1 before insert
            world_pt = nco.copy()
            self._vn_hover = ('bzco', seg_idx + 1, world_pt)
            self._vn_grab  = self._vn_hover
            self._vn_plane = self._vn_get_plane(context, 'bzco', world_pt)
            return

        elif source in ('nurbsobj', 'bzobj') and self._last_obj and self._last_obj.type == 'CURVE':
            obj    = self._last_obj
            mw     = obj.matrix_world
            mw_inv = mw.inverted()
            for spline in obj.data.splines:
                if spline.type != 'NURBS': continue
                pts      = spline.points
                all_data = [(mw @ Vector(pt.co.xyz), pt.co.w) for pt in pts]
                # Place the new control point on the hull (between its neighbours)
                # so the curve barely deforms; the cyan preview dot stayed on the
                # curve for visual feedback.
                n_sp     = len(all_data)
                localf   = max(0.0, min(1.0, t * (n_sp - 1) - seg_idx))
                new_co   = (all_data[seg_idx][0].lerp(all_data[seg_idx + 1][0], localf)
                            if 0 <= seg_idx < n_sp - 1 else world_pt.copy())
                all_data.insert(seg_idx + 1, (new_co, 1.0))
                old_data = obj.data
                new_data = bpy.data.curves.new(old_data.name, type='CURVE')
                new_data.dimensions   = old_data.dimensions
                new_data.resolution_u = old_data.resolution_u
                new_sp = new_data.splines.new('NURBS')
                new_sp.points.add(len(all_data) - 1)
                new_sp.order_u        = spline.order_u
                new_sp.use_endpoint_u = spline.use_endpoint_u
                new_sp.use_cyclic_u   = spline.use_cyclic_u
                for i, (co, w) in enumerate(all_data):
                    new_sp.points[i].co = (*(mw_inv @ co), w)
                obj.data = new_data
                bpy.data.curves.remove(old_data)
                self._vn_hover = ('nurbspt', seg_idx + 1, new_co.copy())
                self._vn_grab  = self._vn_hover
                self._vn_plane = self._vn_get_plane(context, 'nurbspt', new_co)
                return
            obj    = self._last_obj
            mw     = obj.matrix_world
            mw_inv = mw.inverted()
            for spline in obj.data.splines:
                if spline.type != 'BEZIER': continue
                bpts = spline.bezier_points
                n_sp = len(bpts)
                bp0  = bpts[seg_idx]; bp1 = bpts[(seg_idx + 1) % n_sp]
                p0,  h0r = mw @ bp0.co, mw @ bp0.handle_right
                h1l, p1  = mw @ bp1.handle_left, mw @ bp1.co
                nh0r, nhl, nco, nhr, nh1l = _bez_split(p0, h0r, h1l, p1, t)
                # Collect all current bezier point data in world space
                all_data = [(mw @ bpt.co, mw @ bpt.handle_left, mw @ bpt.handle_right,
                             bpt.handle_left_type, bpt.handle_right_type)
                            for bpt in bpts]
                # Update the adjacent handles for the split
                co0, hl0, _, hlt0, hrt0 = all_data[seg_idx]
                all_data[seg_idx] = (co0, hl0, nh0r, hlt0, hrt0)
                co1, _, hr1, hlt1, hrt1 = all_data[(seg_idx + 1) % n_sp]
                all_data[(seg_idx + 1) % n_sp] = (co1, nh1l, hr1, hlt1, hrt1)
                # Insert new point
                new_entry = (nco, nhl, nhr, 'ALIGNED', 'ALIGNED')
                all_data.insert(seg_idx + 1, new_entry)
                # Rebuild spline
                spline.bezier_points.add(1)
                for i, (co, hl, hr, hlt, hrt) in enumerate(all_data):
                    bpt = spline.bezier_points[i]
                    bpt.co                = mw_inv @ co
                    bpt.handle_left       = mw_inv @ hl
                    bpt.handle_right      = mw_inv @ hr
                    bpt.handle_left_type  = hlt
                    bpt.handle_right_type = hrt
                world_pt = nco.copy()
                self._vn_hover = ('bzco', seg_idx + 1, world_pt)
                self._vn_grab  = self._vn_hover
                self._vn_plane = self._vn_get_plane(context, 'bzco', world_pt)
                return

        elif source == 'obj' and self._last_obj:
            obj = self._last_obj
            mw  = obj.matrix_world
            bm  = bmesh.new()
            bm.from_mesh(obj.data)
            bm.edges.ensure_lookup_table()
            if seg_idx >= len(bm.edges):
                bm.free(); return
            edge = bm.edges[seg_idx]
            va_world  = (mw @ edge.verts[0].co).copy()
            vb_world  = (mw @ edge.verts[1].co).copy()
            new_world = va_world.lerp(vb_world, t)
            local_pt  = mw.inverted() @ new_world
            _, new_vert = bmesh.utils.edge_split(edge, edge.verts[0], t)
            new_vert.co = local_pt
            bm.verts.ensure_lookup_table()
            new_idx = new_vert.index
            bm.to_mesh(obj.data); obj.data.update(); bm.free()

        if new_idx is not None:
            self._vn_hover = (source, new_idx, world_pt.copy())
            self._vn_grab  = self._vn_hover
            self._vn_plane = self._vn_get_plane(context, source, world_pt)

    def _vn_get_plane(self, context, source, world_co):
        """Return (origin, normal) constraint plane for a grabbed vertex.
        Priority: stored draw-plane normal → face normal → view normal → world Z."""
        # New grab: re-decide on first apply whether a dragged Bézier handle
        # should mirror its opposite (smooth) or stay independent (cusp).
        self._vn_mirror = None
        # 1. Plane locked at draw time — most accurate
        if self._last_plane_n:
            return (world_co.copy(), self._last_plane_n.copy())
        # 2. Face normal of the target mesh
        if source == 'obj' and self._last_obj:
            bm_tmp = bmesh.new()
            bm_tmp.from_mesh(self._last_obj.data)
            bm_tmp.faces.ensure_lookup_table()
            if bm_tmp.faces:
                n = (self._last_obj.matrix_world.to_3x3() @ bm_tmp.faces[0].normal).normalized()
                bm_tmp.free()
                return (world_co.copy(), n)
            bm_tmp.free()
        # 3. Current view normal — correct for ortho and persp when no other info
        rv3d = context.region_data
        if rv3d:
            n = (rv3d.view_rotation @ Vector((0, 0, -1))).normalized()
            return (world_co.copy(), n)
        # 4. Last resort
        return (world_co.copy(), Vector((0, 0, 1)))

    def _vn_apply(self, context, world_pt):
        """Move the grabbed vertex/handle to world_pt, projected onto the constraint plane."""
        if not self._vn_grab or not self._vn_plane:
            return
        source, idx, _ = self._vn_grab
        origin, normal = self._vn_plane
        wp = world_pt - (world_pt - origin).dot(normal) * normal
        self._vn_grab = (source, idx, wp.copy())

        if source == 'pts':
            self._points[idx] = wp

        elif source in {'bzco', 'bzhr', 'bzhl'}:
            # ── in-progress Bézier points ─────────────────────────
            if self._bezier_pts and 0 <= idx < len(self._bezier_pts):
                bp = self._bezier_pts[idx]
                if source == 'bzco':
                    delta    = wp - bp['co']
                    bp['co'] = wp
                    bp['hr'] = bp['hr'] + delta
                    bp['hl'] = bp['hl'] + delta
                elif source == 'bzhr':
                    bp['hr'] = wp
                    bp['hl'] = Vector(2.0 * bp['co'] - wp)
                else:
                    bp['hl'] = wp
                    bp['hr'] = Vector(2.0 * bp['co'] - wp)
            # ── committed Bézier curve ────────────────────────────
            elif self._nudging and self._last_obj and self._last_obj.type == 'CURVE':
                obj    = self._last_obj
                mw_inv = obj.matrix_world.inverted()
                local  = mw_inv @ wp
                for spline in obj.data.splines:
                    if spline.type != 'BEZIER': continue
                    if not (0 <= idx < len(spline.bezier_points)): continue
                    bpt = spline.bezier_points[idx]
                    _cusp_idx = set(obj.get("bb_cusp_indices", []))
                    if source == 'bzco':
                        delta            = local - bpt.co
                        bpt.co           = local
                        bpt.handle_left  = bpt.handle_left  + delta
                        bpt.handle_right = bpt.handle_right + delta
                    elif source == 'bzhr':
                        # Track whether handle was originally FREE before any
                        # VECTOR→FREE conversion (VECTOR handles must not mirror).
                        _was_free = (bpt.handle_right_type == 'FREE')
                        if bpt.handle_right_type == 'VECTOR':
                            bpt.handle_right_type = 'FREE'
                        # Decide ONCE per drag whether this handle mirrors its
                        # opposite. Cusp (independent) points never mirror; smooth
                        # points do: ALIGNED type, or (FREE pair frozen colinear at
                        # commit). Re-deciding every frame made a cusp snap back to
                        # smooth whenever its handles passed through a straight line.
                        if self._vn_mirror is None:
                            _is_cusp = idx in _cusp_idx
                            _hl = bpt.handle_left  - bpt.co
                            _hr = bpt.handle_right - bpt.co
                            self._vn_mirror = (not _is_cusp and (
                                bpt.handle_right_type == 'ALIGNED' or
                                (_was_free and bpt.handle_left_type == 'FREE' and
                                 _hl.length > 1e-4 and _hr.length > 1e-4 and
                                 _hl.normalized().dot(_hr.normalized()) < -0.99)))
                        bpt.handle_right = local
                        if self._vn_mirror:
                            bpt.handle_left = 2 * bpt.co - local
                    else:
                        _was_free = (bpt.handle_left_type == 'FREE')
                        if bpt.handle_left_type == 'VECTOR':
                            bpt.handle_left_type = 'FREE'
                        if self._vn_mirror is None:
                            _is_cusp = idx in _cusp_idx
                            _hl = bpt.handle_left  - bpt.co
                            _hr = bpt.handle_right - bpt.co
                            self._vn_mirror = (not _is_cusp and (
                                bpt.handle_left_type  == 'ALIGNED' or
                                (_was_free and bpt.handle_right_type == 'FREE' and
                                 _hl.length > 1e-4 and _hr.length > 1e-4 and
                                 _hl.normalized().dot(_hr.normalized()) < -0.99)))
                        bpt.handle_left = local
                        if self._vn_mirror:
                            bpt.handle_right = 2 * bpt.co - local
                    break   # no data.update() needed for curves

        elif source == 'nurbspt':
            if self._nudging and self._last_obj and self._last_obj.type == 'CURVE':
                obj    = self._last_obj
                mw_inv = obj.matrix_world.inverted()
                local  = mw_inv @ wp
                for spline in obj.data.splines:
                    if spline.type != 'NURBS': continue
                    if 0 <= idx < len(spline.points):
                        spline.points[idx].co = (*local, 1.0)
                    break

        else:
            obj      = self._last_obj
            local_co = obj.matrix_world.inverted() @ wp
            obj.data.vertices[idx].co = local_co
            obj.data.update()

    def _sync_draw_state(self, context):
        """Push display state into the module-level dict read by the draw callback."""
        _DRAW_STATE['pts']        = [tuple(p) for p in self._points]
        _DRAW_STATE['mouse']      = tuple(self._mouse_3d) if self._mouse_3d else None
        _DRAW_STATE['snap_on']    = context.scene.tool_settings.use_snap
        _DRAW_STATE['vn_hover']   = tuple(self._vn_hover[2]) if self._vn_hover else None
        _DRAW_STATE['vn_grab']    = tuple(self._vn_grab[2])  if self._vn_grab  else None
        _DRAW_STATE['vn_edge_pt'] = tuple(self._vn_edge_pt)  if self._vn_edge_pt else None

        if self._nudging and self._last_obj and self._last_obj.type == 'MESH':
            mw = self._last_obj.matrix_world
            _DRAW_STATE['mesh_nudge_verts'] = [tuple(mw @ v.co)
                                               for v in self._last_obj.data.vertices]
        else:
            _DRAW_STATE['mesh_nudge_verts'] = []

        mode = context.scene.polydraw_props.draw_mode

        # Bézier live preview
        if mode == 'BEZIER' and self._bezier_pts:
            preview = self._bezier_pts[:]
            # If not currently dragging a handle, append a ghost point at mouse
            if self._mouse_3d and not self._bezier_dragging:
                preview.append({'co': self._mouse_3d,
                                'hl': self._mouse_3d,
                                'hr': self._mouse_3d})
            _DRAW_STATE['bezier_curve'] = _bezier_tessellate(preview)
            # Build (anchor, handle) pairs for handle-line rendering
            # Only show handles for points that actually have spread handles
            handles = []
            for bp in self._bezier_pts:
                handles.append((tuple(bp['co']), tuple(bp['hr'])))
                handles.append((tuple(bp['co']), tuple(bp['hl'])))
            _DRAW_STATE['bezier_handles'] = handles
            # pts is used for the rubber-band line from last anchor to mouse
            _DRAW_STATE['pts']        = [tuple(self._bezier_pts[-1]['co'])]
            _DRAW_STATE['nurbs_curve'] = []

        else:
            _DRAW_STATE['nurbs_curve']    = []
            _DRAW_STATE['bezier_curve']   = []
            _DRAW_STATE['bezier_handles'] = []

        # During nudge phase with a committed Bézier/NURBS curve, always show curve + points
        if (self._nudging and self._last_obj
                and self._last_obj.type == 'CURVE'):
            obj = self._last_obj
            mw  = obj.matrix_world
            for spline in obj.data.splines:
                if spline.type == 'BEZIER':
                    bpts    = spline.bezier_points
                    n_bpts  = len(bpts)
                    bz_world      = []
                    handles       = []
                    cusp_hdl_pts  = []
                    for i, bpt in enumerate(bpts):
                        co = mw @ bpt.co
                        hl = mw @ bpt.handle_left
                        hr = mw @ bpt.handle_right
                        # VECTOR handles: stored RNA value may still be at co if
                        # Blender's incremental recalc didn't propagate fully.
                        # Compute the correct 1/3-segment position from neighbours.
                        if bpt.handle_right_type == 'VECTOR':
                            if spline.use_cyclic_u or i < n_bpts - 1:
                                next_co = mw @ bpts[(i + 1) % n_bpts].co
                                hr = co + (next_co - co) / 3.0
                        if bpt.handle_left_type == 'VECTOR':
                            if spline.use_cyclic_u or i > 0:
                                prev_co = mw @ bpts[(i - 1) % n_bpts].co
                                hl = co + (prev_co - co) / 3.0
                        bz_world.append({'co': co, 'hl': hl, 'hr': hr})
                        # Seam-crossing handles on cyclic splines stay at co (sharp seam).
                        seam_hl = spline.use_cyclic_u and i == 0
                        seam_hr = spline.use_cyclic_u and i == n_bpts - 1
                        # Right handle
                        if (hr - co).length < 1e-4 and not seam_hr and (spline.use_cyclic_u or i < n_bpts - 1):
                            # Cusp: synthesize 5% offset for hit-testing but draw
                            # as a dot only — no arm line, to avoid tangent-arm look.
                            # Aim at the next point's facing handle (the actual curve
                            # direction leaving this cusp), not its anchor — otherwise
                            # the dot points sideways toward the neighbour instead of
                            # along the curve.
                            nb = mw @ bpts[(i + 1) % n_bpts].handle_left
                            if (nb - co).length < 1e-6:
                                nb = mw @ bpts[(i + 1) % n_bpts].co
                            disp_hr = co + (nb - co) * 0.05
                            cusp_hdl_pts.append(tuple(disp_hr))
                            handles.append((tuple(co), tuple(co)))   # keeps anchor_dots stride
                        else:
                            handles.append((tuple(co), tuple(hr)))
                        # Left handle
                        if (hl - co).length < 1e-4 and not seam_hl and (spline.use_cyclic_u or i > 0):
                            pb = mw @ bpts[(i - 1) % n_bpts].handle_right
                            if (pb - co).length < 1e-6:
                                pb = mw @ bpts[(i - 1) % n_bpts].co
                            disp_hl = co + (pb - co) * 0.05
                            cusp_hdl_pts.append(tuple(disp_hl))
                            handles.append((tuple(co), tuple(co)))
                        else:
                            handles.append((tuple(co), tuple(hl)))
                    if len(bz_world) >= 2:
                        _DRAW_STATE['bezier_curve']    = _bezier_tessellate(bz_world)
                        _DRAW_STATE['bezier_handles']  = handles
                        _DRAW_STATE['cusp_handle_pts'] = cusp_hdl_pts
                        _DRAW_STATE['nurbs_curve']     = []

                elif spline.type == 'NURBS':
                    ctrl_w = [mw @ Vector(pt.co.xyz) for pt in spline.points]
                    if len(ctrl_w) >= 2:
                        _DRAW_STATE['nurbs_curve']    = _nurbs_tessellate(ctrl_w)
                        _DRAW_STATE['pts']            = [tuple(p) for p in ctrl_w]
                        _DRAW_STATE['bezier_curve']   = []
                        _DRAW_STATE['bezier_handles'] = []

    def _cleanup(self, context=None):
        global _active_draw_op
        _active_draw_op = None
        _DRAW_STATE.update({'pts': [], 'mouse': None, 'snap_on': False,
                            'vn_hover': None, 'vn_grab': None, 'vn_edge_pt': None,
                            'nurbs_curve': [],
                            'bezier_curve': [], 'bezier_handles': [], 'cusp_handle_pts': [],
                            'mesh_nudge_verts': []})
        self._undo_state      = None
        self._bezier_pts      = []
        self._bezier_dragging = False
        self._extend_target   = None
        if context and context.area:
            context.area.header_text_set(None)

    def cancel(self, context):
        """Called by Blender on forced operator termination (mode switch, undo, etc)."""
        self._cleanup(context)


# ═══════════════════════════════════════════════════════════════
#  Offset operator
# ═══════════════════════════════════════════════════════════════

class POLYDRAW_OT_Offset(bpy.types.Operator):
    """Translate selected mesh objects along the view direction."""
    bl_idname  = "polydraw.offset"
    bl_label   = "Offset"
    bl_options = {'REGISTER', 'UNDO'}

    direction: bpy.props.EnumProperty(
        items=[('POS', '+', 'Positive'), ('NEG', '-', 'Negative')])

    def execute(self, context):
        props = context.scene.polydraw_props
        val   = props.offset_value * (1.0 if self.direction == 'POS' else -1.0)
        rv3d  = context.region_data
        if rv3d is None:
            self.report({'WARNING'}, "No 3D viewport found")
            return {'CANCELLED'}

        moved = 0
        if rv3d.view_perspective in {'PERSP', 'CAMERA'}:
            factor = 1.02 if self.direction == 'POS' else (1.0 / 1.02)
            for obj in context.selected_objects:
                if obj.type == 'MESH':
                    obj.scale = obj.scale * factor; moved += 1
        else:
            view_dir  = rv3d.view_rotation @ Vector((0, 0, -1))
            axes      = [Vector((1,0,0)), Vector((0,1,0)), Vector((0,0,1))]
            best_axis = max(axes, key=lambda a: abs(view_dir.dot(a)))
            if view_dir.dot(best_axis) < 0:
                best_axis = -best_axis
            delta = best_axis * val
            for obj in context.selected_objects:
                if obj.type == 'MESH':
                    obj.location += delta; moved += 1

        if moved == 0:
            self.report({'WARNING'}, "No mesh objects selected")
            return {'CANCELLED'}
        return {'FINISHED'}


# ═══════════════════════════════════════════════════════════════
#  Mode-toggle operators
# ═══════════════════════════════════════════════════════════════

def _start_draw(context, mode):
    """Switch to the requested draw mode.
    If a modal session is already running, reset it in-place instead of
    stacking a second modal operator on top of the existing one."""
    props = context.scene.polydraw_props
    props.draw_mode = mode
    op = _active_draw_op
    if op is not None:
        # Reuse the running modal — wipe its per-session state and restart clean
        op._points        = []
        op._mouse_3d      = None
        op._closed        = False
        op._target        = None
        op._ctrl          = False
        op._draw_plane    = None
        op._nudging       = False
        op._last_obj      = None
        op._last_mode     = mode
        op._append_target = None
        op._pre_hole_mode = None
        op._vn_hover      = None
        op._vn_grab       = None
        op._vn_plane      = None
        op._vn_edge_pt    = None
        op._bezier_pts    = []
        op._bezier_dragging = False
        op._extend_target = None
        op._make_face     = False
        op._edit_existing = False
        op._picking         = False
        op._pick_hover      = None
        _DRAW_STATE.update({'pts': [], 'mouse': None, 'snap_on': False,
                            'vn_hover': None, 'vn_grab': None, 'vn_edge_pt': None,
                            'nurbs_curve': [],
                            'bezier_curve': [], 'bezier_handles': [], 'cusp_handle_pts': [],
                            'pick_hover_curve': [],
                            'pick_hover_lines': []})
        op._update_header(context)
    else:
        bpy.ops.polydraw.draw('INVOKE_DEFAULT')


class POLYDRAW_OT_StartPolyline(bpy.types.Operator):
    """Draw an open polyline"""
    bl_idname = "polydraw.start_polyline"
    bl_label  = "Polyline"
    def invoke(self, context, event):
        global _pending_first_click
        # Stash viewport click coords for both first invocation and modal-reset cases.
        # Draw.invoke consumes it when spawning fresh; modal() consumes it when
        # resetting in-place (click already eaten by this operator, modal won't see it).
        if not _in_draw_canvas(context, event.mouse_x, event.mouse_y):
            # Click landed on a header / HUD bar (or outside the viewport). Let
            # the UI handle it and DON'T reset an in-progress drawing — otherwise
            # the whole curve would vanish.
            return {'PASS_THROUGH'}
        _pending_first_click = (event.mouse_region_x, event.mouse_region_y)
        _start_draw(context, 'POLYLINE')
        return {'FINISHED'}


class POLYDRAW_OT_StartBezier(bpy.types.Operator):
    """Draw a Bézier curve (produces a Curve object)"""
    bl_idname = "polydraw.start_bezier"
    bl_label  = "Bézier"
    def invoke(self, context, event):
        global _pending_first_click
        # Stash viewport click coords for both first invocation and modal-reset cases.
        # Draw.invoke consumes it when spawning fresh; modal() consumes it when
        # resetting in-place (click already eaten by this operator, modal won't see it).
        if not _in_draw_canvas(context, event.mouse_x, event.mouse_y):
            # Click landed on a header / HUD bar (or outside the viewport). Let
            # the UI handle it and DON'T reset an in-progress drawing — otherwise
            # the whole curve would vanish.
            return {'PASS_THROUGH'}
        _pending_first_click = (event.mouse_region_x, event.mouse_region_y)
        _start_draw(context, 'BEZIER')
        return {'FINISHED'}


# ═══════════════════════════════════════════════════════════════
#  Icon preview loading
# ═══════════════════════════════════════════════════════════════

def _load_icons():
    pcoll = bpy.utils.previews.new()
    icons_dir = os.path.join(os.path.dirname(os.path.realpath(__file__)), "icons")
    pcoll.load("ngon",     os.path.join(icons_dir, "ngon.png"),     'IMAGE')
    pcoll.load("polyline", os.path.join(icons_dir, "polyline.png"), 'IMAGE')
    _preview_collections["polydraw"] = pcoll


def _unload_icons():
    for pcoll in _preview_collections.values():
        bpy.utils.previews.remove(pcoll)
    _preview_collections.clear()


_POLYDRAW_TOOL_IDS = {
    'polydraw.polyline_tool', 'polydraw.bezier_tool',
}

def _polydraw_is_active(context):
    """True if the BB PolyDraw modal is running OR one of its tools is active."""
    if _active_draw_op is not None:
        return True
    try:
        return context.workspace.tools.from_space_view3d_mode(
            context.mode, create=False).idname in _POLYDRAW_TOOL_IDS
    except Exception:
        return False


# ═══════════════════════════════════════════════════════════════
#  Pick-curve operator  (Q key — works even before first draw)
# ═══════════════════════════════════════════════════════════════

class POLYDRAW_OT_PickCurve(bpy.types.Operator):
    """Enter BB PolyDraw pick mode: hover a shape to highlight it, click to edit (Q)"""
    bl_idname = "polydraw.pick_curve"
    bl_label  = "BB PolyDraw: Pick Shape"

    @classmethod
    def poll(cls, context):
        return (_polydraw_is_active(context) and
                context.area is not None and
                context.area.type == 'VIEW_3D' and
                context.mode == 'OBJECT')

    def invoke(self, context, event):
        op = _active_draw_op
        if op is not None:
            # Modal already running — tell it to enter pick mode
            op._picking    = True
            op._pick_hover = None
            _DRAW_STATE['pick_hover_curve'] = []
            _DRAW_STATE['pick_hover_lines']  = []
            op._pick_header(context)
            context.area.tag_redraw()
        else:
            # Start the modal fresh with pick mode pre-armed via a module flag
            global _pending_pick_mode
            _pending_pick_mode = True
            bpy.ops.polydraw.draw('INVOKE_DEFAULT')
        return {'FINISHED'}


# ═══════════════════════════════════════════════════════════════
#  Toolbox tools  (N-Gon is default; Polyline is in the same flyout)
# ═══════════════════════════════════════════════════════════════

class POLYDRAW_WorkTool_Polyline(bpy.types.WorkSpaceTool):
    """Draw a polyline or polygon in Object mode.
    RMB/Enter commits an open polyline; Alt+RMB closes the loop and fills a face."""
    bl_space_type = 'VIEW_3D'
    bl_context_mode = 'OBJECT'
    bl_idname = "polydraw.polyline_tool"
    bl_label = "Poly Draw"
    bl_description = (
        "Draw a polyline or polygon\n"
        "LMB: place point  |  Enter/RMB: commit open polyline\n"
        "Alt+RMB: close loop + fill polygon face  |  Esc: cancel\n"
        "Alt+Scroll: offset ±1 mm  |  Shift+Alt+Scroll: ±10 mm"
    )
    bl_icon = (pathlib.Path(__file__).parent / "icons" / "poly_p").as_posix()

    bl_keymap = (
        ("polydraw.start_polyline", {"type": "LEFTMOUSE", "value": "PRESS", "ctrl": False, "shift": False, "alt": False}, None),
    )

    @staticmethod
    def draw_settings(context, layout, tool):
        props = context.scene.polydraw_props
        layout.prop(props, "offset_value")


class POLYDRAW_WorkTool_Bezier(bpy.types.WorkSpaceTool):
    """Draw a Bézier curve in Object mode"""
    bl_space_type    = 'VIEW_3D'
    bl_context_mode  = 'OBJECT'
    bl_idname        = "polydraw.bezier_tool"
    bl_label         = "Bézier Draw"
    bl_description   = (
        "Draw a Bézier curve (outputs a Curve object)\n"
        "LMB click: corner point  |  LMB click-drag: smooth point with handles\n"
        "Alt+RMB: close loop  |  Enter/RMB: commit  |  Ctrl+Z: undo last point  |  Esc: cancel"
    )
    bl_icon = (pathlib.Path(__file__).parent / "icons" / "bezier_b").as_posix()

    bl_keymap = (
        ("polydraw.start_bezier", {"type": "LEFTMOUSE", "value": "PRESS", "ctrl": False, "shift": False, "alt": False}, None),
    )

    @staticmethod
    def draw_settings(context, layout, tool):
        props = context.scene.polydraw_props
        layout.prop(props, "offset_value")

# ═══════════════════════════════════════════════════════════════
#  Register / Unregister
# ═══════════════════════════════════════════════════════════════

_classes = (
    POLYDRAW_Props,
    POLYDRAW_AddonPreferences,
    POLYDRAW_OT_Draw,
    POLYDRAW_OT_Offset,
    POLYDRAW_OT_StartPolyline,
    POLYDRAW_OT_StartBezier,
    POLYDRAW_OT_PickCurve,
)

_draw_handler   = None
_addon_keymaps  = []


@bpy.app.handlers.persistent
def _apply_default_offset_on_load(_dummy):
    """On file load, seed each scene's offset from the add-on preference so the
    'Default Offset' setting governs what new/opened files start with."""
    prefs = _get_prefs()
    if prefs is None:
        return
    try:
        scenes = bpy.data.scenes
    except AttributeError:
        return   # restricted context (e.g. during register) — bpy.data not ready
    for scene in scenes:
        if hasattr(scene, 'polydraw_props'):
            scene.polydraw_props.offset_value = prefs.default_offset


def _deferred_seed_default_offset():
    """Timer callback: seed open scenes once registration finishes and bpy.data
    is accessible. Returns None so the timer fires only once."""
    _apply_default_offset_on_load(None)
    return None


def register():
    for cls in _classes:
        bpy.utils.register_class(cls)
    bpy.types.Scene.polydraw_props = bpy.props.PointerProperty(type=POLYDRAW_Props)

    global _draw_handler
    _draw_handler = bpy.types.SpaceView3D.draw_handler_add(
        POLYDRAW_OT_Draw._draw_cb, (), 'WINDOW', 'POST_VIEW')

    # Defensive: drop any leftover tools from a previous half-registration so a
    # reload never aborts with "Tool ... already exists".
    for _tcls in (POLYDRAW_WorkTool_Polyline, POLYDRAW_WorkTool_Bezier):
        try:
            bpy.utils.unregister_tool(_tcls)
        except Exception:
            pass

    bpy.utils.register_tool(POLYDRAW_WorkTool_Polyline, separator=True, group=True)
    bpy.utils.register_tool(POLYDRAW_WorkTool_Bezier,   after={"polydraw.polyline_tool"})

    # Global Q-key shortcut: enter pick mode (works even before any shape is drawn)
    wm  = bpy.context.window_manager
    kc  = wm.keyconfigs.addon
    if kc:
        km   = kc.keymaps.new(name='Object Mode', space_type='EMPTY')
        kmi2 = km.keymap_items.new(
            'polydraw.pick_curve', type='Q', value='PRESS',
            ctrl=False, shift=False, alt=False)
        _addon_keymaps.append((km, kmi2))

    if _apply_default_offset_on_load not in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.append(_apply_default_offset_on_load)

    # Seed currently-open scenes once data is accessible (register() runs in a
    # restricted context where bpy.data is unavailable).
    bpy.app.timers.register(_deferred_seed_default_offset, first_interval=0.0)


def unregister():
    if _apply_default_offset_on_load in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.remove(_apply_default_offset_on_load)

    for km, kmi in _addon_keymaps:
        km.keymap_items.remove(kmi)
    _addon_keymaps.clear()

    bpy.utils.unregister_tool(POLYDRAW_WorkTool_Bezier)
    bpy.utils.unregister_tool(POLYDRAW_WorkTool_Polyline)

    global _draw_handler
    if _draw_handler:
        bpy.types.SpaceView3D.draw_handler_remove(_draw_handler, 'WINDOW')
        _draw_handler = None

    for cls in reversed(_classes):
        bpy.utils.unregister_class(cls)
    del bpy.types.Scene.polydraw_props
