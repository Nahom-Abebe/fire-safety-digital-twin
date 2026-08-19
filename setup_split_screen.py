import bpy

def build_split_screen_compositor(
    cam_overview_name="Camera_Overview", 
    cam_interior_name="Interior_CloseUp_Cam"
):
    scene = bpy.context.scene
    scene.use_nodes = True
    tree = scene.node_tree
    tree.nodes.clear()

    # 1. Verify required cameras exist
    cam_overview = bpy.data.objects.get(cam_overview_name)
    cam_interior = bpy.data.objects.get(cam_interior_name)

    if not cam_overview or not cam_interior:
        raise ValueError(
            f"Missing cameras! Ensure both '{cam_overview_name}' and "
            f"'{cam_interior_name}' exist in the Blender scene."
        )

    # 2. Configure View Layer 1 (Macro Overview)
    vl_overview = scene.view_layers.get("ViewLayer_Overview")
    if not vl_overview:
        vl_overview = scene.view_layers.new(name="ViewLayer_Overview")
    vl_overview.camera = cam_overview

    # 3. Configure View Layer 2 (Interior Close-Up)
    vl_interior = scene.view_layers.get("ViewLayer_Interior")
    if not vl_interior:
        vl_interior = scene.view_layers.new(name="ViewLayer_Interior")
    vl_interior.camera = cam_interior

    # 4. Create Compositor Nodes
    # Left Render Layer (Overview)
    rl_left = tree.nodes.new(type='CompositorNodeRenderLayers')
    rl_left.layer = "ViewLayer_Overview"
    rl_left.location = (-400, 200)

    # Right Render Layer (Interior)
    rl_right = tree.nodes.new(type='CompositorNodeRenderLayers')
    rl_right.layer = "ViewLayer_Interior"
    rl_right.location = (-400, -200)

    # Box Mask for 50/50 vertical split
    mask = tree.nodes.new(type='CompositorNodeBoxMask')
    mask.location = (-150, 0)
    mask.inputs['X'].default_value = 0.25     # Covers left 50% of frame
    mask.inputs['Y'].default_value = 0.5
    mask.inputs['Width'].default_value = 0.5
    mask.inputs['Height'].default_value = 1.0

    # Mix Node to join both layers
    mix = tree.nodes.new(type='CompositorNodeMixRGB')
    mix.location = (100, 0)

    # Composite Output Node
    comp = tree.nodes.new(type='CompositorNodeComposite')
    comp.location = (300, 0)

    # 5. Connect Node Pipeline
    tree.links.new(rl_right.outputs['Image'], mix.inputs[1])   # Background: Right View
    tree.links.new(rl_left.outputs['Image'], mix.inputs[2])    # Foreground: Left View
    tree.links.new(mask.outputs['Mask'], mix.inputs['Factor'])  # Split Mask
    tree.links.new(mix.outputs['Image'], comp.inputs['Image'])

    print("[SUCCESS] Split-screen compositor nodes successfully configured.")

if __name__ == "__main__":
    build_split_screen_compositor()