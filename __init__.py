"""
BB Poly Draw — Blender Extension
N-Panel › BB Poly Draw tab
Authors: Blender Bob & Claude.ai
"""

import bpy
import bmesh
import gpu
from gpu_extras.batch import batch_for_shader
from mathutils import Vector
from bpy_extras import view3d_utils


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
    use_x: bpy.props.BoolProperty(name="X", default=False)
    use_y: bpy.props.BoolProperty(name="Y", default=False)
    use_z: bpy.props.BoolProperty(name="Z", default=True)

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
#  3-D cursor utility
# ═══════════════════════════════════════════════════════════════

def mouse_to_3d(context, mx, my):
    region = context.region
    rv3d   = context.region_data
    coord  = (mx, my)
    ray_origin = view3d_utils.region_2d_to_origin_3d(region, rv3d, coord)
    ray_dir    = view3d_utils.region_2d_to_vector_3d(region, rv3d, coord)
    hit, loc, *_ = context.scene.ray_cast(
        context.view_layer.depsgraph, ray_origin, ray_dir)
    if hit:
        return Vector(loc)
    return view3d_utils.region_2d_to_location_3d(
        region, rv3d, coord, context.scene.cursor.location)


# ═══════════════════════════════════════════════════════════════
#  Modal drawing operator
# ═══════════════════════════════════════════════════════════════

class POLYDRAW_OT_Draw(bpy.types.Operator):
    """LMB place point | Alt+RMB close polyline | Enter/RMB commit | Esc cancel"""
    bl_idname  = "polydraw.draw"
    bl_label   = "BB Poly Draw (Modal)"
    bl_options = {'REGISTER', 'UNDO'}

    @staticmethod
    def _draw_cb(op, context):
        if not op._points:
            return
        pts     = [tuple(p) for p in op._points]
        preview = pts + ([tuple(op._mouse_3d)] if op._mouse_3d else [])
        shader  = gpu.shader.from_builtin('UNIFORM_COLOR')
        gpu.state.blend_set('ALPHA')
        gpu.state.line_width_set(2.5)
        gpu.state.point_size_set(8.0)
        if len(preview) > 1:
            shader.bind()
            shader.uniform_float("color", (0.18, 0.76, 1.0, 0.85))
            batch_for_shader(shader, 'LINE_STRIP', {"pos": preview}).draw(shader)
        shader.bind()
        shader.uniform_float("color", (1.0, 0.55, 0.10, 1.0))
        batch_for_shader(shader, 'POINTS', {"pos": pts}).draw(shader)
        gpu.state.blend_set('NONE')

    @classmethod
    def poll(cls, context):
        return context.area is not None and context.area.type == 'VIEW_3D'

    def invoke(self, context, event):
        self._points   = []
        self._mouse_3d = None
        self._closed   = False
        self._target   = None

        props = context.scene.polydraw_props
        if props.draw_mode == 'HOLE':
            obj = context.active_object
            if obj and obj.type == 'MESH':
                self._target = obj
            else:
                self.report({'WARNING'},
                    "Holes: select the target mesh before clicking Holes")
                props.draw_mode = 'NONE'
                return {'CANCELLED'}

        self._handle = bpy.types.SpaceView3D.draw_handler_add(
            POLYDRAW_OT_Draw._draw_cb, (self, context), 'WINDOW', 'POST_VIEW')
        context.window_manager.modal_handler_add(self)
        context.area.header_text_set(
            "BB Poly Draw  |  LMB place point  |  "
            "Alt+RMB close loop  |  Enter/RMB commit  |  Esc cancel")
        return {'RUNNING_MODAL'}

    def modal(self, context, event):
        context.area.tag_redraw()
        props = context.scene.polydraw_props
        mode  = props.draw_mode

        if event.type == 'ESC' and event.value == 'PRESS':
            self._cleanup(context)
            props.draw_mode = 'NONE'
            return {'CANCELLED'}

        if event.type == 'MOUSEMOVE':
            self._mouse_3d = mouse_to_3d(
                context, event.mouse_region_x, event.mouse_region_y)
            return {'PASS_THROUGH'}

        if event.type == 'LEFTMOUSE' and event.value == 'PRESS':
            pt = mouse_to_3d(context, event.mouse_region_x, event.mouse_region_y)
            self._points.append(Vector(pt))
            return {'RUNNING_MODAL'}

        if (event.type == 'RIGHTMOUSE' and event.value == 'PRESS'
                and event.alt and mode == 'POLYLINE'):
            if len(self._points) >= 3:
                self._closed = True
            self._commit(context)
            return {'FINISHED'}

        if (event.type in {'RET', 'NUMPAD_ENTER', 'RIGHTMOUSE'}
                and event.value == 'PRESS' and not event.alt):
            self._commit(context)
            return {'FINISHED'}

        return {'RUNNING_MODAL'}

    def _commit(self, context):
        self._cleanup(context)
        props = context.scene.polydraw_props
        mode  = props.draw_mode
        pts   = self._points

        if len(pts) < 2:
            self.report({'INFO'}, "BB Poly Draw: need at least 2 points")
            return

        me  = bpy.data.meshes.new("PolyDraw")
        obj = bpy.data.objects.new("PolyDraw", me)
        context.collection.objects.link(obj)
        bm    = bmesh.new()
        verts = [bm.verts.new(p) for p in pts]

        if mode in {'NGON', 'HOLE'} and len(pts) >= 3:
            bm.faces.new(verts)
        else:
            for i in range(len(verts) - 1):
                bm.edges.new((verts[i], verts[i + 1]))
            if self._closed and len(verts) >= 3:
                bm.edges.new((verts[-1], verts[0]))

        bm.to_mesh(me)
        bm.free()

        if mode == 'HOLE' and self._target:
            self._cut_hole(context, obj, self._target)
        else:
            bpy.ops.object.select_all(action='DESELECT')
            context.view_layer.objects.active = obj
            obj.select_set(True)

        props.draw_mode = 'NONE'

    def _cut_hole(self, context, cutter_obj, target_obj):
        # Decide strategy based on whether the target has faces
        target_bm = bmesh.new()
        target_bm.from_mesh(target_obj.data)
        has_faces = len(target_bm.faces) > 0
        target_bm.free()

        if has_faces:
            self._cut_hole_boolean(context, cutter_obj, target_obj)
        else:
            self._cut_hole_polyline(context, cutter_obj, target_obj)

    def _cut_hole_boolean(self, context, cutter_obj, target_obj):
        """Standard boolean difference for solid/face meshes."""
        sol           = cutter_obj.modifiers.new("_PD_Sol", 'SOLIDIFY')
        sol.thickness = 10.0
        sol.offset    = 0.0
        bpy.ops.object.select_all(action='DESELECT')
        context.view_layer.objects.active = cutter_obj
        cutter_obj.select_set(True)
        bpy.ops.object.modifier_apply(modifier="_PD_Sol")
        bpy.ops.object.select_all(action='DESELECT')
        context.view_layer.objects.active = target_obj
        target_obj.select_set(True)
        bmod           = target_obj.modifiers.new("_PD_Bool", 'BOOLEAN')
        bmod.operation = 'DIFFERENCE'
        bmod.object    = cutter_obj
        bpy.ops.object.modifier_apply(modifier="_PD_Bool")
        bpy.data.objects.remove(cutter_obj, do_unlink=True)

    def _cut_hole_polyline(self, context, cutter_obj, target_obj):
        """
        For edge-only targets (polylines): delete vertices that fall inside
        the drawn hole polygon, and trim edges that cross its boundary.

        Strategy:
          1. Build a 2D local coordinate system from the hole polygon's normal.
          2. Project all target vertices onto that plane.
          3. Point-in-polygon test (ray casting) to find inside vertices.
          4. For edges that cross the boundary, insert a new vertex at the
             intersection point before deleting the inside portion.
        """
        import mathutils

        # ── hole polygon in world space ─────────────────────────
        hole_pts = [cutter_obj.matrix_world @ v.co
                    for v in cutter_obj.data.vertices]

        if len(hole_pts) < 3:
            bpy.data.objects.remove(cutter_obj, do_unlink=True)
            return

        # ── build local 2D basis on the hole plane ──────────────
        # Normal via Newell's method (robust for any planar polygon)
        normal = Vector((0, 0, 0))
        n = len(hole_pts)
        for i in range(n):
            a = hole_pts[i]
            b = hole_pts[(i + 1) % n]
            normal.x += (a.y - b.y) * (a.z + b.z)
            normal.y += (a.z - b.z) * (a.x + b.x)
            normal.z += (a.x - b.x) * (a.y + b.y)

        if normal.length < 1e-6:
            normal = Vector((0, 0, 1))
        else:
            normal.normalize()

        # Local X: from first to second hole vertex, projected perpendicular to normal
        local_x = (hole_pts[1] - hole_pts[0])
        local_x -= local_x.dot(normal) * normal
        if local_x.length < 1e-6:
            local_x = normal.orthogonal()
        local_x.normalize()
        local_y = normal.cross(local_x).normalized()

        origin = hole_pts[0]

        def to_2d(p):
            d = p - origin
            return (d.dot(local_x), d.dot(local_y))

        # ── project hole polygon to 2D ──────────────────────────
        hole_2d = [to_2d(p) for p in hole_pts]

        def point_in_polygon(px, py, poly):
            """Ray casting point-in-polygon test."""
            inside = False
            n = len(poly)
            j = n - 1
            for i in range(n):
                xi, yi = poly[i]
                xj, yj = poly[j]
                if ((yi > py) != (yj > py) and
                        px < (xj - xi) * (py - yi) / (yj - yi + 1e-12) + xi):
                    inside = not inside
                j = i
            return inside

        def seg_intersect_2d(a1, a2, b1, b2):
            """
            Return the parameter t along a1→a2 where it crosses b1→b2,
            or None if no crossing in [0,1]×[0,1].
            """
            dx = a2[0] - a1[0]; dy = a2[1] - a1[1]
            ex = b2[0] - b1[0]; ey = b2[1] - b1[1]
            denom = dx * ey - dy * ex
            if abs(denom) < 1e-10:
                return None
            fx = b1[0] - a1[0]; fy = b1[1] - a1[1]
            t = (fx * ey - fy * ex) / denom
            u = (fx * dy - fy * dx) / denom
            if 0.0 <= t <= 1.0 and 0.0 <= u <= 1.0:
                return t
            return None

        # ── edit target polyline in bmesh ───────────────────────
        bm = bmesh.new()
        bm.from_mesh(target_obj.data)
        bm.verts.ensure_lookup_table()
        bm.edges.ensure_lookup_table()

        mw = target_obj.matrix_world

        # Map each vertex → inside/outside
        vert_inside = {}
        for v in bm.verts:
            wp = mw @ v.co
            p2 = to_2d(wp)
            vert_inside[v.index] = point_in_polygon(p2[0], p2[1], hole_2d)

        # For edges that cross the boundary, insert intersection vertices
        new_verts_inside = set()  # indices of newly created verts that are inside
        edges_to_check = list(bm.edges)

        for edge in edges_to_check:
            v0, v1 = edge.verts[0], edge.verts[1]
            in0 = vert_inside.get(v0.index, False)
            in1 = vert_inside.get(v1.index, False)
            if in0 == in1:
                continue  # both inside or both outside, no crossing

            # Find intersection along this edge
            wp0 = mw @ v0.co
            wp1 = mw @ v1.co
            p0_2d = to_2d(wp0)
            p1_2d = to_2d(wp1)

            best_t = None
            for i in range(n):
                h0 = hole_2d[i]
                h1 = hole_2d[(i + 1) % n]
                t = seg_intersect_2d(p0_2d, p1_2d, h0, h1)
                if t is not None:
                    if best_t is None or abs(t - 0.5) < abs(best_t - 0.5):
                        best_t = t

            if best_t is None:
                continue

            # Insert new vertex at intersection in local object space
            wp_new   = wp0.lerp(wp1, best_t)
            co_local = target_obj.matrix_world.inverted() @ wp_new
            new_v    = bmesh.ops.bisect_edges(
                bm, edges=[edge], cuts=1,
            )
            # bisect_edges returns new geom; find the new vert closest to co_local
            for el in new_v['geom_split']:
                if isinstance(el, bmesh.types.BMVert):
                    el.co = co_local
                    # Mark new vert as inside only if it was on the inside portion
                    # (it sits on the boundary — treat as outside so it stays)
                    vert_inside[el.index] = False
                    break

        # Refresh after bisect
        bm.verts.ensure_lookup_table()

        # Delete all vertices marked as inside
        verts_to_delete = [v for v in bm.verts if vert_inside.get(v.index, False)]
        bmesh.ops.delete(bm, geom=verts_to_delete, context='VERTS')

        bm.to_mesh(target_obj.data)
        target_obj.data.update()
        bm.free()

        # Remove the temporary cutter object
        bpy.data.objects.remove(cutter_obj, do_unlink=True)

        bpy.ops.object.select_all(action='DESELECT')
        context.view_layer.objects.active = target_obj
        target_obj.select_set(True)

    def _cleanup(self, context):
        if self._handle:
            bpy.types.SpaceView3D.draw_handler_remove(self._handle, 'WINDOW')
            self._handle = None
        context.area.header_text_set(None)


# ═══════════════════════════════════════════════════════════════
#  Offset operator
# ═══════════════════════════════════════════════════════════════

class POLYDRAW_OT_Offset(bpy.types.Operator):
    """Translate selected mesh objects along the active axes"""
    bl_idname  = "polydraw.offset"
    bl_label   = "Offset"
    bl_options = {'REGISTER', 'UNDO'}

    direction: bpy.props.EnumProperty(
        items=[('POS', '+', 'Positive'), ('NEG', '-', 'Negative')])

    def execute(self, context):
        props = context.scene.polydraw_props
        val   = props.offset_value * (1.0 if self.direction == 'POS' else -1.0)
        if not any((props.use_x, props.use_y, props.use_z)):
            self.report({'WARNING'}, "Enable at least one axis (X / Y / Z)")
            return {'CANCELLED'}
        delta = Vector((
            val if props.use_x else 0.0,
            val if props.use_y else 0.0,
            val if props.use_z else 0.0,
        ))
        moved = sum(1 for obj in context.selected_objects
                    if obj.type == 'MESH' and not setattr(obj, 'location',
                    obj.location + delta))
        if moved == 0:
            self.report({'WARNING'}, "No mesh objects selected")
            return {'CANCELLED'}
        return {'FINISHED'}


# ═══════════════════════════════════════════════════════════════
#  Mode-toggle operators
# ═══════════════════════════════════════════════════════════════

class POLYDRAW_OT_StartPolyline(bpy.types.Operator):
    """Draw an open polyline"""
    bl_idname = "polydraw.start_polyline"
    bl_label  = "Polyline"
    def execute(self, context):
        props = context.scene.polydraw_props
        if props.draw_mode == 'POLYLINE':
            props.draw_mode = 'NONE'
            return {'FINISHED'}
        props.draw_mode = 'POLYLINE'
        bpy.ops.polydraw.draw('INVOKE_DEFAULT')
        return {'FINISHED'}


class POLYDRAW_OT_StartNgon(bpy.types.Operator):
    """Draw a closed n-gon face"""
    bl_idname = "polydraw.start_ngon"
    bl_label  = "N-Gon"
    def execute(self, context):
        props = context.scene.polydraw_props
        if props.draw_mode == 'NGON':
            props.draw_mode = 'NONE'
            return {'FINISHED'}
        props.draw_mode = 'NGON'
        bpy.ops.polydraw.draw('INVOKE_DEFAULT')
        return {'FINISHED'}


class POLYDRAW_OT_StartHole(bpy.types.Operator):
    """Select target mesh first, then draw a hole shape"""
    bl_idname = "polydraw.start_hole"
    bl_label  = "Holes"
    def execute(self, context):
        props = context.scene.polydraw_props
        if props.draw_mode == 'HOLE':
            props.draw_mode = 'NONE'
            return {'FINISHED'}
        props.draw_mode = 'HOLE'
        bpy.ops.polydraw.draw('INVOKE_DEFAULT')
        return {'FINISHED'}


# ═══════════════════════════════════════════════════════════════
#  N-Panel UI
# ═══════════════════════════════════════════════════════════════

class POLYDRAW_PT_Panel(bpy.types.Panel):
    bl_label       = "BB Poly Draw"
    bl_idname      = "POLYDRAW_PT_panel"
    bl_space_type  = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category    = "BB Poly Draw"

    def draw(self, context):
        layout = self.layout
        props  = context.scene.polydraw_props
        mode   = props.draw_mode

        layout.prop(props, "offset_value", slider=True)
        layout.separator(factor=0.8)

        row = layout.row(align=True)
        row.scale_y = 1.35
        row.operator("polydraw.start_polyline", icon='CURVE_PATH',
                     depress=(mode == 'POLYLINE'))
        row.operator("polydraw.start_ngon", icon='MESH_CIRCLE',
                     depress=(mode == 'NGON'))

        layout.separator(factor=0.4)
        obj = context.active_object
        is_polyline = (obj and obj.type == 'MESH' and
                       len(obj.data.polygons) == 0 and len(obj.data.edges) > 0)
        hole_label = "Cut" if is_polyline else "Holes"
        layout.operator("polydraw.start_hole", text=hole_label, icon='MOD_BOOLEAN',
                        depress=(mode == 'HOLE'))
        layout.separator(factor=0.8)

        row = layout.row(align=True)
        row.prop(props, "use_x", toggle=True)
        row.prop(props, "use_y", toggle=True)
        row.prop(props, "use_z", toggle=True)

        row = layout.row(align=True)
        row.scale_y = 1.2
        op = row.operator("polydraw.offset", text="Offset -", icon='REMOVE')
        op.direction = 'NEG'
        op = row.operator("polydraw.offset", text="Offset +", icon='ADD')
        op.direction = 'POS'

        if mode != 'NONE':
            layout.separator(factor=0.5)
            box = layout.box()
            hint = {"POLYLINE": "Drawing POLYLINE  |  Esc to exit",
                    "NGON":     "Drawing N-GON  |  Esc to exit",
                    "HOLE":     "Drawing HOLE  |  Esc to exit"}
            box.label(text=hint.get(mode, ""), icon='INFO')


# ═══════════════════════════════════════════════════════════════
#  Register / Unregister
# ═══════════════════════════════════════════════════════════════

_classes = (
    POLYDRAW_Props,
    POLYDRAW_OT_Draw,
    POLYDRAW_OT_Offset,
    POLYDRAW_OT_StartPolyline,
    POLYDRAW_OT_StartNgon,
    POLYDRAW_OT_StartHole,
    POLYDRAW_PT_Panel,
)

def register():
    for cls in _classes:
        bpy.utils.register_class(cls)
    bpy.types.Scene.polydraw_props = bpy.props.PointerProperty(type=POLYDRAW_Props)

def unregister():
    for cls in reversed(_classes):
        bpy.utils.unregister_class(cls)
    del bpy.types.Scene.polydraw_props
