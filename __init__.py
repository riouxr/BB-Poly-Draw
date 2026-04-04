"""
BB Poly Draw — Blender Extension
N-Panel › BB Poly Draw tab
Authors: Blender Bob & Claude.ai
"""

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

_preview_collections = {}

# ═══════════════════════════════════════════════════════════════
#  Module-level draw state — no operator references, safe from RNA freeing
# ═══════════════════════════════════════════════════════════════

_DRAW_STATE = {'pts': [], 'mouse': None, 'snap_on': False,
               'vn_hover': None, 'vn_grab': None, 'vn_edge_pt': None}

# Reference to the currently running draw modal (None when idle)
_active_draw_op = None


# ═══════════════════════════════════════════════════════════════
#  Properties
# ═══════════════════════════════════════════════════════════════

class POLYDRAW_Props(bpy.types.PropertyGroup):

    offset_value: bpy.props.FloatProperty(
        name="Offset Value",
        description="Distance applied by Offset - / Offset +",
        default=0.1, soft_min=-10.0, soft_max=10.0,
        precision=3, subtype='DISTANCE',
    )
    draw_mode: bpy.props.EnumProperty(
        name="Draw Mode",
        items=[
            ('NONE',     'None',     ''),
            ('POLYLINE', 'Polyline', ''),
            ('NGON',     'N-Gon',    ''),
            ('HOLE',     'Hole',     ''),
        ],
        default='NONE',
    )


# ═══════════════════════════════════════════════════════════════
#  Snap-aware 3D position from mouse
# ═══════════════════════════════════════════════════════════════

_SNAP_PX = 20  # screen-space pixel threshold for vertex / edge snapping


def _project_to_screen(context, world_co):
    """Return (sx, sy) screen coords for a world-space point, or None if behind camera."""
    return view3d_utils.location_3d_to_region_2d(
        context.region, context.region_data, world_co)


def _screen_dist(ax, ay, bx, by):
    return ((ax - bx) ** 2 + (ay - by) ** 2) ** 0.5


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
    def _draw_cb():
        # Reads from module-level _DRAW_STATE — no operator or context reference,
        # so Blender freeing the operator RNA cannot affect this callback.
        pts     = _DRAW_STATE['pts']
        mouse   = _DRAW_STATE['mouse']
        snap_on = _DRAW_STATE['snap_on']
        vn_hov  = _DRAW_STATE['vn_hover']
        vn_grab = _DRAW_STATE['vn_grab']

        preview = pts + ([mouse] if mouse else [])
        shader  = gpu.shader.from_builtin('UNIFORM_COLOR')
        gpu.state.blend_set('ALPHA')
        gpu.state.line_width_set(2.5)
        gpu.state.point_size_set(8.0)
        if len(preview) > 1:
            shader.bind()
            shader.uniform_float("color", (0.18, 0.76, 1.0, 0.85))
            batch_for_shader(shader, 'LINE_STRIP', {"pos": preview}).draw(shader)
        if pts:
            shader.bind()
            shader.uniform_float("color", (1.0, 0.55, 0.10, 1.0))
            batch_for_shader(shader, 'POINTS', {"pos": pts}).draw(shader)
        if mouse and snap_on:
            gpu.state.point_size_set(14.0)
            shader.bind()
            shader.uniform_float("color", (1.0, 0.95, 0.0, 1.0))
            batch_for_shader(shader, 'POINTS', {"pos": [mouse]}).draw(shader)
        if vn_hov and not vn_grab:
            gpu.state.point_size_set(20.0)
            shader.bind()
            shader.uniform_float("color", (0.2, 1.0, 0.3, 1.0))
            batch_for_shader(shader, 'POINTS', {"pos": [vn_hov]}).draw(shader)
        edge_pt = _DRAW_STATE.get('vn_edge_pt')
        if edge_pt:
            gpu.state.point_size_set(20.0)
            shader.bind()
            shader.uniform_float("color", (0.0, 0.85, 1.0, 1.0))
            batch_for_shader(shader, 'POINTS', {"pos": [edge_pt]}).draw(shader)
        if vn_grab:
            gpu.state.point_size_set(20.0)
            shader.bind()
            shader.uniform_float("color", (1.0, 1.0, 1.0, 1.0))
            batch_for_shader(shader, 'POINTS', {"pos": [vn_grab]}).draw(shader)
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
        context.area.header_text_set(
            f"BB Poly Draw  |  {hint}"
            f"Alt+Scroll ±1 mm  Shift+Alt ±10 mm  (offset: {props.offset_value * 1000:.1f} mm)  |  "
            "LMB new  |  Shift+LMB append  |  Ctrl+LMB hole  |  Ctrl+Z undo  |  Esc exit")

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
        self._vn_edge_pt    = None   # nearest edge insert point for ctrl+alt+shift
        self._last_plane_n  = None   # plane normal stored at commit time

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
        elif props.draw_mode in {'NGON', 'POLYLINE'}:
            # If a mesh is already selected, enter nudge immediately so the
            # user can Shift+LMB append or Ctrl+LMB hole into it right away.
            obj = context.active_object
            if obj and obj.type == 'MESH':
                self._last_obj  = obj
                self._nudging   = True
                self._last_mode = props.draw_mode

        # Reset module-level draw state for this session
        _DRAW_STATE.update({'pts': [], 'mouse': None, 'snap_on': False,
                            'vn_hover': None, 'vn_grab': None, 'vn_edge_pt': None})
        context.window_manager.modal_handler_add(self)
        global _active_draw_op
        _active_draw_op = self

        if self._nudging:
            self._nudge_header(context)
        else:
            self._update_header(context)

        # ── First-click fast-path ────────────────────────────────
        # When the WorkSpaceTool keymap fires start_ngon/start_polyline on LMB
        # PRESS, that click is consumed by those operators before the modal ever
        # sees it.  Detect that here and place the first point immediately, so
        # the user only needs ONE click to start drawing instead of two.
        # Only applies when:
        #   • the triggering event really was an LMB press (not a panel button),
        #   • we are not in nudge mode (nudge has its own shift/ctrl fall-through),
        #   • the cursor is inside the viewport window and not over a UI region.
        if (event.type == 'LEFTMOUSE' and event.value == 'PRESS'
                and not self._nudging):
            sx, sy = event.mouse_x, event.mouse_y
            win = next((r for r in context.area.regions if r.type == 'WINDOW'), None)
            in_win = win and (win.x <= sx < win.x + win.width and
                              win.y <= sy < win.y + win.height)
            over_ui = any(r.type != 'WINDOW' and
                          r.x <= sx < r.x + r.width and
                          r.y <= sy < r.y + r.height
                          for r in context.area.regions)
            if in_win and not over_ui:
                raw = self._resolve_point(context,
                                          event.mouse_region_x,
                                          event.mouse_region_y)
                self._points.append(raw)
                _DRAW_STATE['pts'] = [tuple(p) for p in self._points]

        return {'RUNNING_MODAL'}

    # ── header text ──────────────────────────────────────────────

    def _update_header(self, context):
        props      = context.scene.polydraw_props
        ctrl_hint  = f"Ctrl {self._angle_step:.0f}° snap (scroll to change, Shift×5)"
        alt_hint   = f"Alt+Scroll ±1 mm  Shift+Alt ±10 mm  (offset: {props.offset_value * 1000:.1f} mm)"
        if props.draw_mode == 'POLYLINE':
            context.area.header_text_set(
                f"BB Poly Draw  |  LMB place point  |  {ctrl_hint}  |  {alt_hint}  |  "
                "Alt+RMB close loop  |  Enter/RMB commit  |  Esc cancel")
        elif props.draw_mode == 'HOLE':
            context.area.header_text_set(
                f"BB Poly Draw  |  HOLE MODE  |  LMB place point  |  {alt_hint}  |  "
                "Enter/RMB cut hole  |  Esc cancel")
        else:
            context.area.header_text_set(
                f"BB Poly Draw  |  LMB place point  |  {ctrl_hint}  |  {alt_hint}  |  "
                "Enter/RMB commit  |  Esc cancel")

    # ── modal ────────────────────────────────────────────────────

    def modal(self, context, event):
        context.area.tag_redraw()
        props = context.scene.polydraw_props

        # ── Active-tool sync ─────────────────────────────────────
        # The modal consumes LMB, so the tool keymap operators (start_ngon /
        # start_polyline) never fire while the modal is running.  Instead we
        # detect an external tool switch here and update the mode in-place.
        if not self._nudging and event.type == 'MOUSEMOVE':
            try:
                active_tool = context.workspace.tools.from_space_view3d_mode(
                    context.mode, create=False)
                if active_tool:
                    tid = active_tool.idname
                    if tid == 'polydraw.polyline_tool' and props.draw_mode == 'NGON':
                        props.draw_mode = 'POLYLINE'
                        self._last_mode = 'POLYLINE'
                        self._points    = []
                        self._draw_plane = None
                        self._update_header(context)
                    elif tid == 'polydraw.ngon_tool' and props.draw_mode == 'POLYLINE':
                        props.draw_mode = 'NGON'
                        self._last_mode = 'NGON'
                        self._points    = []
                        self._draw_plane = None
                        self._update_header(context)
            except Exception:
                pass

        mode  = props.draw_mode

        # ESC always exits immediately — also revert to select so the WorkSpaceTool
        # releases and the user isn't trapped re-entering draw on every LMB click.
        if event.type == 'ESC' and event.value == 'PRESS':
            self._cleanup(context)
            self._nudging   = False
            self._last_obj  = None
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

        # Clear hover/edge-pt when all vertex-nudge modifiers are released
        if not (ctrl_shift or ctrl_alt_shift or alt_shift or self._vn_grab):
            changed = False
            if self._vn_hover:    self._vn_hover = None;    changed = True
            if self._vn_edge_pt:  self._vn_edge_pt = None;  changed = True
            if changed:
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
        if self._nudging and event.type == 'MOUSEMOVE':
            try:
                active_tool = context.workspace.tools.from_space_view3d_mode(
                    context.mode, create=False)
                if active_tool:
                    tid = active_tool.idname
                    if tid == 'polydraw.polyline_tool':
                        self._last_mode = 'POLYLINE'
                    elif tid == 'polydraw.ngon_tool':
                        self._last_mode = 'NGON'
            except Exception:
                pass

        if self._nudging and self._last_obj:

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
                sx, sy = event.mouse_x, event.mouse_y
                win = next((r for r in context.area.regions if r.type == 'WINDOW'), None)
                if not win or not (win.x <= sx < win.x + win.width and
                                   win.y <= sy < win.y + win.height):
                    return {'PASS_THROUGH'}
                if any(r.type != 'WINDOW' and r.x <= sx < r.x + r.width and
                       r.y <= sy < r.y + r.height for r in context.area.regions):
                    return {'PASS_THROUGH'}

                saved_obj     = self._last_obj
                self._nudging = False
                self._last_obj = None

                if event.shift and saved_obj:
                    self._append_target = saved_obj
                    props.draw_mode     = self._last_mode
                    context.area.header_text_set(
                        "BB Poly Draw  |  APPEND MODE  |  LMB place point  |  "
                        "Enter/RMB merge into shape  |  Esc cancel")
                elif (event.ctrl or self._ctrl) and saved_obj:
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

        # ── Ctrl+Z ──────────────────────────────────────────────
        if self._ctrl and event.type == 'Z' and event.value == 'PRESS':
            if self._nudging and self._undo_state:
                kind, target_obj, mesh_snapshot = self._undo_state
                if kind == 'obj':
                    bpy.data.objects.remove(target_obj, do_unlink=True)
                else:
                    old_mesh = target_obj.data
                    target_obj.data = mesh_snapshot
                    bpy.data.meshes.remove(old_mesh)
                    for _o in context.view_layer.objects: _o.select_set(False)
                    context.view_layer.objects.active = target_obj
                    target_obj.select_set(True)
                self._undo_state = None
                if kind == 'mesh':
                    self._last_obj = target_obj
                    self._nudging  = True
                    self._nudge_header(context)
                else:
                    self._last_obj  = None
                    self._nudging   = False
                    props.draw_mode = self._last_mode
                    self._update_header(context)
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
            raw = self._resolve_point(context, event.mouse_region_x, event.mouse_region_y)
            if not self._points:
                self._draw_plane = None
            if self._ctrl and self._points:
                view_n = context.region_data.view_rotation @ Vector((0, 0, -1))
                raw    = angle_snap(raw, self._points[-1], view_n, self._angle_step)
            self._mouse_3d = raw
            self._sync_draw_state(context)
            return {'PASS_THROUGH'}

        # ── LMB: place point ────────────────────────────────────
        if event.type == 'LEFTMOUSE' and event.value == 'PRESS':
            sx, sy = event.mouse_x, event.mouse_y
            win = next((r for r in context.area.regions if r.type == 'WINDOW'), None)
            if not win or not (win.x <= sx < win.x + win.width and
                               win.y <= sy < win.y + win.height):
                return {'PASS_THROUGH'}
            if any(r.type != 'WINDOW' and r.x <= sx < r.x + r.width and
                   r.y <= sy < r.y + r.height for r in context.area.regions):
                return {'PASS_THROUGH'}
            raw = self._resolve_point(context, event.mouse_region_x, event.mouse_region_y)
            if self._ctrl and self._points:
                view_n = context.region_data.view_rotation @ Vector((0, 0, -1))
                raw    = angle_snap(raw, self._points[-1], view_n, self._angle_step)
            self._points.append(raw)
            return {'RUNNING_MODAL'}

        # ── Alt+RMB: close polyline loop ─────────────────────────
        if (event.type == 'RIGHTMOUSE' and event.value == 'PRESS'
                and event.alt and mode == 'POLYLINE'):
            if len(self._points) >= 3:
                self._closed = True
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

        if len(pts) < 2:
            self.report({'INFO'}, "BB Poly Draw: need at least 2 points")
            return

        me  = bpy.data.meshes.new("PolyDraw")
        obj = bpy.data.objects.new("PolyDraw", me)
        context.collection.objects.link(obj)
        bm = bmesh.new()

        if mode in {'NGON', 'HOLE'} and len(pts) >= 3:
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
            self._undo_state = ('mesh', self._target, self._target.data.copy())
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
        # Keep plane normal so vertex nudge can constrain to it after commit
        if self._draw_plane:
            self._last_plane_n = self._draw_plane[1].copy()
        self._draw_plane    = None
        self._vn_hover      = None
        self._vn_grab       = None
        self._vn_plane      = None
        self._nudge_header(context)

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
        target_bm = bmesh.new()
        target_bm.from_mesh(target_obj.data)
        has_faces = len(target_bm.faces) > 0
        target_bm.free()
        if has_faces:
            self._cut_hole_boolean(context, cutter_obj, target_obj)
        else:
            self._cut_hole_polyline(context, cutter_obj, target_obj)

    def _cut_hole_boolean(self, context, cutter_obj, target_obj):
        """Build a prism from the cutter polygon and boolean-difference it into the target."""
        me = cutter_obj.data

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
        bpy.ops.object.modifier_apply(modifier="_PD_Bool")
        bpy.data.objects.remove(cutter_obj, do_unlink=True)

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
            new_v    = bmesh.ops.bisect_edges(bm, edges=[edge], cuts=1)
            for el in new_v['geom_split']:
                if isinstance(el, bmesh.types.BMVert):
                    el.co = co_local
                    vert_inside[el.index] = False
                    break

        bm.verts.ensure_lookup_table()
        bmesh.ops.delete(bm,
            geom=[v for v in bm.verts if vert_inside.get(v.index, False)],
            context='VERTS')
        bm.to_mesh(target_obj.data); target_obj.data.update(); bm.free()

        bpy.data.objects.remove(cutter_obj, do_unlink=True)
        for _o in context.view_layer.objects: _o.select_set(False)
        context.view_layer.objects.active = target_obj
        target_obj.select_set(True)

    def _vn_find_nearest(self, context, mx, my):
        """Scan in-progress points and committed mesh for the nearest vertex.
        Returns (source, idx, world_co) or None.  source is 'pts' or 'obj'."""
        threshold = _SNAP_PX * 1.5
        best_d    = threshold
        best      = None

        for i, p in enumerate(self._points):
            s = _project_to_screen(context, p)
            if s is None: continue
            d = _screen_dist(mx, my, s.x, s.y)
            if d < best_d:
                best_d = d; best = ('pts', i, p.copy())

        if self._nudging and self._last_obj:
            obj = self._last_obj
            mw  = obj.matrix_world
            for v in obj.data.vertices:
                wp = mw @ v.co
                s  = _project_to_screen(context, wp)
                if s is None: continue
                d = _screen_dist(mx, my, s.x, s.y)
                if d < best_d:
                    best_d = d; best = ('obj', v.index, wp.copy())

        return best

    def _vn_find_nearest_edge_pt(self, context, mx, my):
        """Find the closest point on any edge — either segments of _points (drawing)
        or edges of _last_obj (committed mesh).
        Returns (world_pt, source, seg_idx, t) or None."""
        best_d  = float('inf')
        best    = None

        # ── segments from in-progress drawn points ───────────────
        n = len(self._points)
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

        # ── edges of the committed mesh ───────────────────────────
        if self._last_obj:
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

        return best

    def _vn_delete_vertex(self, context):
        """Remove the currently hovered vertex from _points or the committed mesh."""
        if not self._vn_hover:
            return
        source, idx, _ = self._vn_hover

        if source == 'pts':
            if 0 <= idx < len(self._points):
                self._points.pop(idx)
                if not self._points:
                    self._draw_plane = None

        elif source == 'obj' and self._last_obj:
            obj = self._last_obj
            bm  = bmesh.new()
            bm.from_mesh(obj.data)
            bm.verts.ensure_lookup_table()
            if idx < len(bm.verts):
                bmesh.ops.delete(bm, geom=[bm.verts[idx]], context='VERTS')
            bm.to_mesh(obj.data); obj.data.update(); bm.free()

    def _vn_add_vertex(self, context, mx, my):
        """Insert a vertex on the nearest edge — works on both in-progress _points
        segments (drawing phase) and committed mesh edges (nudge phase)."""
        result = self._vn_find_nearest_edge_pt(context, mx, my)
        if result is None:
            return
        world_pt, source, seg_idx, t = result

        new_idx = None

        if source == 'pts':
            # Insert into _points list between seg_idx and seg_idx+1
            self._points.insert(seg_idx + 1, world_pt)
            new_idx = seg_idx + 1

        elif source == 'obj' and self._last_obj:
            obj = self._last_obj
            mw  = obj.matrix_world
            bm  = bmesh.new()
            bm.from_mesh(obj.data)
            bm.edges.ensure_lookup_table()
            if seg_idx >= len(bm.edges):
                bm.free()
                return
            edge = bm.edges[seg_idx]
            # Read positions before bisect invalidates refs
            va_world  = (mw @ edge.verts[0].co).copy()
            vb_world  = (mw @ edge.verts[1].co).copy()
            new_world = va_world.lerp(vb_world, t)
            local_pt  = mw.inverted() @ new_world
            bisect = bmesh.ops.bisect_edges(bm, edges=[edge], cuts=1)
            for el in bisect['geom_split']:
                if isinstance(el, bmesh.types.BMVert):
                    el.co = local_pt
                    bm.verts.ensure_lookup_table()
                    new_idx = el.index
                    break
            bm.to_mesh(obj.data); obj.data.update(); bm.free()

        self._vn_edge_pt = None

        # Immediately enter grab mode on the new vertex so the user can
        # drag it to reshape the edge without releasing the mouse button.
        if new_idx is not None:
            self._vn_hover = (source, new_idx, world_pt.copy())
            self._vn_grab  = self._vn_hover
            self._vn_plane = self._vn_get_plane(context, source, world_pt)

    def _vn_get_plane(self, context, source, world_co):
        """Return (origin, normal) constraint plane for a grabbed vertex.
        Priority: stored draw-plane normal → face normal → view normal → world Z."""
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
        """Move the grabbed vertex to world_pt, projected onto the constraint plane."""
        if not self._vn_grab or not self._vn_plane:
            return
        source, idx, _ = self._vn_grab
        origin, normal = self._vn_plane
        # Project onto plane
        wp = world_pt - (world_pt - origin).dot(normal) * normal
        self._vn_grab = (source, idx, wp.copy())   # update displayed position
        if source == 'pts':
            self._points[idx] = wp
        else:
            obj      = self._last_obj
            local_co = obj.matrix_world.inverted() @ wp
            obj.data.vertices[idx].co = local_co
            obj.data.update()

    def _sync_draw_state(self, context):
        """Push display state into the module-level dict read by the draw callback."""
        _DRAW_STATE['pts']      = [tuple(p) for p in self._points]
        _DRAW_STATE['mouse']    = tuple(self._mouse_3d) if self._mouse_3d else None
        _DRAW_STATE['snap_on']  = context.scene.tool_settings.use_snap
        _DRAW_STATE['vn_hover'] = tuple(self._vn_hover[2]) if self._vn_hover else None
        _DRAW_STATE['vn_grab']    = tuple(self._vn_grab[2])  if self._vn_grab  else None
        _DRAW_STATE['vn_edge_pt'] = tuple(self._vn_edge_pt)  if self._vn_edge_pt else None

    def _cleanup(self, context=None):
        global _active_draw_op
        _active_draw_op = None
        # Clear draw state so the persistent handler draws nothing
        _DRAW_STATE.update({'pts': [], 'mouse': None, 'snap_on': False,
                            'vn_hover': None, 'vn_grab': None, 'vn_edge_pt': None})
        self._undo_state = None
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
        _DRAW_STATE.update({'pts': [], 'mouse': None, 'snap_on': False,
                            'vn_hover': None, 'vn_grab': None, 'vn_edge_pt': None})
        op._update_header(context)
    else:
        bpy.ops.polydraw.draw('INVOKE_DEFAULT')


class POLYDRAW_OT_StartPolyline(bpy.types.Operator):
    """Draw an open polyline"""
    bl_idname = "polydraw.start_polyline"
    bl_label  = "Polyline"
    def execute(self, context):
        _start_draw(context, 'POLYLINE')
        return {'FINISHED'}


class POLYDRAW_OT_StartNgon(bpy.types.Operator):
    """Draw a closed n-gon face"""
    bl_idname = "polydraw.start_ngon"
    bl_label  = "N-Gon"
    def execute(self, context):
        _start_draw(context, 'NGON')
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


# ═══════════════════════════════════════════════════════════════
#  Toolbox tools  (N-Gon is default; Polyline is in the same flyout)
# ═══════════════════════════════════════════════════════════════

class POLYDRAW_WorkTool_Ngon(bpy.types.WorkSpaceTool):
    """Draw a closed N-Gon face in Object mode"""
    bl_space_type = 'VIEW_3D'
    bl_context_mode = 'OBJECT'
    bl_idname = "polydraw.ngon_tool"
    bl_label = "N-Gon Draw"
    bl_description = (
        "Draw a closed N-Gon face\n"
        "LMB: place point  |  Enter/RMB: commit  |  Esc: cancel\n"
        "Alt+Scroll: offset ±1 mm  |  Shift+Alt+Scroll: ±10 mm"
    )
    bl_icon = (pathlib.Path(__file__).parent / "icons" / "ngon").as_posix()

    bl_keymap = (
        ("polydraw.start_ngon", {"type": "LEFTMOUSE", "value": "PRESS"}, None),
    )

    @staticmethod
    def draw_settings(context, layout, tool):
        props = context.scene.polydraw_props
        layout.prop(props, "offset_value")


class POLYDRAW_WorkTool_Polyline(bpy.types.WorkSpaceTool):
    """Draw an open polyline in Object mode"""
    bl_space_type = 'VIEW_3D'
    bl_context_mode = 'OBJECT'
    bl_idname = "polydraw.polyline_tool"
    bl_label = "Polyline Draw"
    bl_description = (
        "Draw an open polyline\n"
        "LMB: place point  |  Alt+RMB: close loop  |  Enter/RMB: commit  |  Esc: cancel\n"
        "Alt+Scroll: offset ±1 mm  |  Shift+Alt+Scroll: ±10 mm"
    )
    bl_icon = (pathlib.Path(__file__).parent / "icons" / "polyline").as_posix()

    bl_keymap = (
        ("polydraw.start_polyline", {"type": "LEFTMOUSE", "value": "PRESS"}, None),
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
    POLYDRAW_OT_Draw,
    POLYDRAW_OT_Offset,
    POLYDRAW_OT_StartPolyline,
    POLYDRAW_OT_StartNgon,
)

_draw_handler = None


def register():
    for cls in _classes:
        bpy.utils.register_class(cls)
    bpy.types.Scene.polydraw_props = bpy.props.PointerProperty(type=POLYDRAW_Props)

    _draw_handler = bpy.types.SpaceView3D.draw_handler_add(
        POLYDRAW_OT_Draw._draw_cb, (), 'WINDOW', 'POST_VIEW')

    bpy.utils.register_tool(POLYDRAW_WorkTool_Ngon, separator=True, group=True)
    bpy.utils.register_tool(POLYDRAW_WorkTool_Polyline, after={"polydraw.ngon_tool"})


def unregister():
    bpy.utils.unregister_tool(POLYDRAW_WorkTool_Polyline)
    bpy.utils.unregister_tool(POLYDRAW_WorkTool_Ngon)

    if _draw_handler:
        bpy.types.SpaceView3D.draw_handler_remove(_draw_handler, 'WINDOW')
        _draw_handler = None

    for cls in reversed(_classes):
        bpy.utils.unregister_class(cls)
    del bpy.types.Scene.polydraw_props
