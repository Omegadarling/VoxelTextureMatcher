bl_info = {
    "name": "Voxel Texture Optimizer",
    "author": "VoxelTextureMatcher",
    "version": (1, 1, 0),
    "blender": (4, 0, 0),
    "location": "View3D > Sidebar > Voxel Tex",
    "description": "Optimizes voxel mesh textures to one pixel per face",
    "category": "Mesh",
}

import bpy
import math


def find_base_color_image(material):
    """Find the Image Texture node connected to a Principled BSDF's Base Color input."""
    if not material or not material.use_nodes:
        return None

    tree = material.node_tree

    # Find Principled BSDF node
    principled = None
    for node in tree.nodes:
        if node.type == 'BSDF_PRINCIPLED':
            principled = node
            break

    if principled is None:
        return None

    # Check what's connected to Base Color input
    base_color_input = principled.inputs.get('Base Color')
    if base_color_input is None or not base_color_input.is_linked:
        return None

    linked_node = base_color_input.links[0].from_node
    if linked_node.type == 'TEX_IMAGE' and linked_node.image:
        return linked_node.image

    return None


class MESH_OT_voxel_texture_optimize(bpy.types.Operator):
    bl_idname = "mesh.voxel_texture_optimize"
    bl_label = "Optimize Voxel Texture"
    bl_description = "Creates a 1-pixel-per-face optimized texture for voxel or retopologized meshes"
    bl_options = {'REGISTER', 'UNDO'}

    texture_size_mode: bpy.props.EnumProperty(
        name="Texture Size",
        items=[
            ('POWER_OF_2', "Power of 2", "Smallest power-of-2 square (best GPU compatibility)"),
            ('MINIMAL', "Minimal", "Smallest square that fits all faces"),
        ],
        default='POWER_OF_2',
    )

    backup_material: bpy.props.BoolProperty(
        name="Backup Original Material",
        description="Keep the original material renamed as a backup",
        default=True,
    )

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        return (
            obj is not None
            and obj.type == 'MESH'
            and obj.data.uv_layers.active is not None
            and len(obj.data.materials) > 0
        )

    def execute(self, context):
        obj = context.active_object
        mesh = obj.data

        # Ensure we're in object mode
        if context.mode != 'OBJECT':
            bpy.ops.object.mode_set(mode='OBJECT')

        # Find base color image from the first material that has one
        src_image = None
        src_mat_index = 0
        for i, mat_slot in enumerate(obj.material_slots):
            img = find_base_color_image(mat_slot.material)
            if img is not None:
                src_image = img
                src_mat_index = i
                break

        if src_image is None:
            self.report({'ERROR'}, "No Base Color image texture found in any material")
            return {'CANCELLED'}

        if not src_image.has_data:
            self.report({'ERROR'}, f"Image '{src_image.name}' has no pixel data loaded")
            return {'CANCELLED'}

        # Step 1: Read source pixels
        src_w, src_h = src_image.size[0], src_image.size[1]
        src_pixels = src_image.pixels[:]

        # Step 2: Compute texture dimensions
        face_count = len(mesh.polygons)
        if face_count == 0:
            self.report({'ERROR'}, "Mesh has no faces")
            return {'CANCELLED'}

        if self.texture_size_mode == 'POWER_OF_2':
            # Find the smallest power-of-2 rectangle that fits all faces
            tex_w = 1
            tex_h = 1
            while tex_w * tex_h < face_count:
                # Double the smaller dimension to keep the shape roughly square
                if tex_w <= tex_h:
                    tex_w *= 2
                else:
                    tex_h *= 2
        else:
            # Minimal: tightest rectangle (width >= height)
            tex_w = math.ceil(math.sqrt(face_count))
            tex_h = math.ceil(face_count / tex_w)

        # Step 3: Sample face colors from source texture
        face_colors = []
        uv_layer = mesh.uv_layers.active

        for poly in mesh.polygons:
            # Average all loop UVs to get face center
            u_sum = 0.0
            v_sum = 0.0
            for loop_idx in poly.loop_indices:
                uv = uv_layer.data[loop_idx].uv
                u_sum += uv.x
                v_sum += uv.y
            n = poly.loop_total
            center_u = u_sum / n
            center_v = v_sum / n

            # Handle UV wrapping
            center_u = center_u % 1.0
            center_v = center_v % 1.0

            # Convert UV to pixel coordinates (clamp to valid range)
            px = min(int(center_u * src_w), src_w - 1)
            py = min(int(center_v * src_h), src_h - 1)

            # Read RGBA from flat pixel array
            idx = (py * src_w + px) * 4
            r = src_pixels[idx]
            g = src_pixels[idx + 1]
            b = src_pixels[idx + 2]
            a = src_pixels[idx + 3]

            face_colors.append((r, g, b, a))

        # Step 4: Create new image
        new_image_name = f"{obj.name}_VoxelOptimized"
        # Remove existing image with same name to avoid conflicts
        if new_image_name in bpy.data.images:
            bpy.data.images.remove(bpy.data.images[new_image_name])

        new_image = bpy.data.images.new(
            name=new_image_name,
            width=tex_w,
            height=tex_h,
            alpha=True,
            float_buffer=False,
        )
        new_image.colorspace_settings.name = 'sRGB'

        # Step 5: Write pixel data
        new_pixels = [0.0] * (tex_w * tex_h * 4)

        for face_idx, (r, g, b, a) in enumerate(face_colors):
            col = face_idx % tex_w
            row = face_idx // tex_w
            pixel_idx = (row * tex_w + col) * 4
            new_pixels[pixel_idx] = r
            new_pixels[pixel_idx + 1] = g
            new_pixels[pixel_idx + 2] = b
            new_pixels[pixel_idx + 3] = a

        new_image.pixels[:] = new_pixels

        # Step 6: Pack into .blend
        new_image.pack()

        # Step 7: Create new UV layer and remap
        # Remove ALL existing UV layers so the optimized one is at index 0.
        # glTF export defaults to UV index 0 when no UV Map node is present,
        # and many programs (Blockbench, etc.) expect the primary UVs at index 0.
        while mesh.uv_layers:
            mesh.uv_layers.remove(mesh.uv_layers[0])

        new_uv = mesh.uv_layers.new(name="OptimizedUV")
        mesh.uv_layers.active = new_uv

        for face_idx, poly in enumerate(mesh.polygons):
            col = face_idx % tex_w
            row = face_idx // tex_w

            # UV bounds for this pixel with small inset to prevent bleeding
            pixel_u = 1.0 / tex_w
            pixel_v = 1.0 / tex_h
            inset = 0.05  # 5% inset from pixel edges
            u_min = col * pixel_u + inset * pixel_u
            u_max = (col + 1) * pixel_u - inset * pixel_u
            v_min = row * pixel_v + inset * pixel_v
            v_max = (row + 1) * pixel_v - inset * pixel_v

            # Get vertex positions for this face
            verts = [mesh.vertices[mesh.loops[li].vertex_index].co for li in poly.loop_indices]

            # Use face normal to pick the best projection plane
            normal = poly.normal
            abs_n = (abs(normal.x), abs(normal.y), abs(normal.z))
            dominant = max(range(3), key=lambda i: abs_n[i])

            if dominant == 0:
                ax_u, ax_v = 1, 2  # Project onto YZ
            elif dominant == 1:
                ax_u, ax_v = 0, 2  # Project onto XZ
            else:
                ax_u, ax_v = 0, 1  # Project onto XY

            # Get 2D coordinates on the projection plane
            u_vals = [v[ax_u] for v in verts]
            v_vals = [v[ax_v] for v in verts]

            face_u_min, face_u_max = min(u_vals), max(u_vals)
            face_v_min, face_v_max = min(v_vals), max(v_vals)
            face_u_range = face_u_max - face_u_min
            face_v_range = face_v_max - face_v_min

            # Handle degenerate ranges (edge-on faces)
            if face_u_range < 1e-7:
                face_u_range = 1.0
                face_u_min = u_vals[0] - 0.5
            if face_v_range < 1e-7:
                face_v_range = 1.0
                face_v_min = v_vals[0] - 0.5

            for i, loop_idx in enumerate(poly.loop_indices):
                co = verts[i]
                # Proportional mapping: vertex position in face → position in pixel cell
                t_u = (co[ax_u] - face_u_min) / face_u_range
                t_v = (co[ax_v] - face_v_min) / face_v_range
                t_u = max(0.0, min(1.0, t_u))
                t_v = max(0.0, min(1.0, t_v))

                new_uv.data[loop_idx].uv.x = u_min + t_u * (u_max - u_min)
                new_uv.data[loop_idx].uv.y = v_min + t_v * (v_max - v_min)

        # Set as active render UV
        new_uv.active_render = True

        # Step 8: Create new material
        orig_mat = obj.data.materials[src_mat_index]

        if self.backup_material and orig_mat:
            if not orig_mat.name.endswith("_backup"):
                orig_mat.name = orig_mat.name + "_backup"

        new_mat_name = f"{obj.name}_VoxelOptimized"
        # Remove existing material with same name
        if new_mat_name in bpy.data.materials:
            bpy.data.materials.remove(bpy.data.materials[new_mat_name])

        new_mat = bpy.data.materials.new(name=new_mat_name)
        new_mat.use_nodes = True
        tree = new_mat.node_tree

        # Clear default nodes
        for node in tree.nodes:
            tree.nodes.remove(node)

        # Create nodes
        bsdf = tree.nodes.new('ShaderNodeBsdfPrincipled')
        bsdf.location = (0, 0)

        tex_node = tree.nodes.new('ShaderNodeTexImage')
        tex_node.location = (-300, 0)
        tex_node.image = new_image
        tex_node.interpolation = 'Closest'
        tex_node.extension = 'CLIP'

        # Explicitly reference the OptimizedUV layer so glTF/other exporters
        # use the correct UVs instead of defaulting to index 0
        uv_map_node = tree.nodes.new('ShaderNodeUVMap')
        uv_map_node.location = (-550, 0)
        uv_map_node.uv_map = "OptimizedUV"
        tree.links.new(uv_map_node.outputs['UV'], tex_node.inputs['Vector'])

        output = tree.nodes.new('ShaderNodeOutputMaterial')
        output.location = (300, 0)

        # Link nodes
        tree.links.new(tex_node.outputs['Color'], bsdf.inputs['Base Color'])
        tree.links.new(bsdf.outputs['BSDF'], output.inputs['Surface'])

        # Assign new material to slot
        obj.data.materials[src_mat_index] = new_mat

        # Step 9: Report
        self.report(
            {'INFO'},
            f"Created {tex_w}x{tex_h} optimized texture "
            f"({face_count} faces, {face_count}/{tex_w * tex_h} pixels used)"
        )
        return {'FINISHED'}


class VIEW3D_PT_voxel_texture_optimizer(bpy.types.Panel):
    bl_label = "Voxel Texture Optimizer"
    bl_idname = "VIEW3D_PT_voxel_texture_optimizer"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'Voxel Tex'

    def draw(self, context):
        layout = self.layout
        obj = context.active_object

        if obj and obj.type == 'MESH':
            mesh = obj.data
            layout.label(text=f"Object: {obj.name}")
            layout.label(text=f"Faces: {len(mesh.polygons)}")

            if mesh.uv_layers.active:
                layout.label(text=f"UV Layer: {mesh.uv_layers.active.name}")
            else:
                layout.label(text="No UV layer!", icon='ERROR')

            # Show material info
            has_image = False
            for mat_slot in obj.material_slots:
                img = find_base_color_image(mat_slot.material)
                if img:
                    layout.label(text=f"Texture: {img.name}")
                    layout.label(text=f"Size: {img.size[0]}x{img.size[1]}")
                    has_image = True
                    break

            if not has_image:
                layout.label(text="No Base Color texture found", icon='ERROR')

            layout.separator()
            layout.operator("mesh.voxel_texture_optimize", icon='TEXTURE')
        else:
            layout.label(text="Select a mesh object", icon='INFO')


classes = (
    MESH_OT_voxel_texture_optimize,
    VIEW3D_PT_voxel_texture_optimizer,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)


if __name__ == "__main__":
    register()
