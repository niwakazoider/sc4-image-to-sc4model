#!/usr/bin/env python3
"""
Standalone SC4 BAT-style ground-safe camera-plane OBJ generator for memo33/fshgen.

Goal
----
Generate one camera-facing quad per zoom/rotation for the fshgen pipeline without Blender.

It mirrors the important conventions used by memo33/BAT4Blender:
- 4 rotations x 5 zooms
- orthographic SC4 camera angles
- BAT4Blender instance-id / TGI naming
- one camera-facing quad per zoom/rotation
- one material / one Day PNG per view
- UV corners fixed to (0,0), (1,0), (1,1), (0,1)
- two triangles per view (required by fshgen)

Dimensions are used only to calculate the projected BAT footprint/canvas.
The exported S3D geometry itself is a single camera-facing rectangular quad.

Reference implementation:
  https://github.com/memo33/BAT4Blender
"""

from __future__ import annotations

import argparse
import io
import json
import math
import shlex
import struct
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from collections import deque
from typing import Callable, Iterable, List, Sequence, Tuple


Vec3 = Tuple[float, float, float]
Vec2 = Tuple[float, float]

TID_S3D = "5ad0e817"
TID_FSH = "7ab50e44"

# memo33/BAT4Blender Renderer.py (SD mode)
ZOOM_SIZES = [8, 16, 32, 73, 146]  # horizontal px extent of a 16m x 16m reference cell
SLOP_PX = 3
MAX_TILE_PX = 256

# Camera rotations used by this standalone generator.
# Requested order: NORTH=0, EAST=1, SOUTH=2, WEST=3
ROTATIONS = [
    ("N", 112.5),
    ("E",  22.5),
    ("S", -67.5),
    ("W", 202.5),
]
PITCHES = [60.0, 55.0, 50.0, 45.0, 45.0]


@dataclass
class PV:
    """Polygon vertex carrying both world and camera-plane coordinates."""
    world: Vec3
    cx: float
    cy: float


def vadd(a: Vec3, b: Vec3) -> Vec3:
    return (a[0]+b[0], a[1]+b[1], a[2]+b[2])


def vsub(a: Vec3, b: Vec3) -> Vec3:
    return (a[0]-b[0], a[1]-b[1], a[2]-b[2])


def vmul(a: Vec3, s: float) -> Vec3:
    return (a[0]*s, a[1]*s, a[2]*s)


def dot(a: Vec3, b: Vec3) -> float:
    return a[0]*b[0] + a[1]*b[1] + a[2]*b[2]


def cross(a: Vec3, b: Vec3) -> Vec3:
    return (
        a[1]*b[2] - a[2]*b[1],
        a[2]*b[0] - a[0]*b[2],
        a[0]*b[1] - a[1]*b[0],
    )


def length(a: Vec3) -> float:
    return math.sqrt(dot(a, a))


def norm(a: Vec3) -> Vec3:
    n = length(a)
    if n < 1e-12:
        raise ValueError("zero-length vector")
    return (a[0]/n, a[1]/n, a[2]/n)


def lerp3(a: Vec3, b: Vec3, t: float) -> Vec3:
    return (
        a[0] + (b[0]-a[0])*t,
        a[1] + (b[1]-a[1])*t,
        a[2] + (b[2]-a[2])*t,
    )


def round_up_fsh_chunk(value: float) -> int:
    """
    Mirrors the Canvas rounding idea used by BAT4Blender:
    chunks beyond full 256px tiles are 8/16/32/64/128/256.
    """
    n = math.ceil(value)
    full, rem = divmod(n, 256)
    if rem == 0:
        tail = 0
    elif rem <= 8:
        tail = 8
    elif rem <= 16:
        tail = 16
    elif rem <= 32:
        tail = 32
    elif rem <= 64:
        tail = 64
    elif rem <= 128:
        tail = 128
    else:
        tail = 256
    return full * 256 + tail


def instance_id(z: int, rotation: int, count: int, is_night: bool = False) -> int:
    """BAT4Blender-compatible IID mapping."""
    if not (0 <= count < (1 << 10)):
        raise ValueError("tile count out of supported range")
    offset = (
        ((count & 0x0380) << 5)
        | ((count & 0x0040) << 5)
        | ((count & 0x0030) << 2)
        | (count & 0x000F)
    )
    return 0x30000 + (0x8000 if is_night else 0) + z * 0x100 + rotation * 0x10 + offset


def tgi_stem(tid: str, gid: str, iid: int) -> str:
    gid = gid.lower().removeprefix("0x")
    return f"{tid}_{gid}_{iid:08x}"


def make_box(width: float, depth: float, height: float, min_z: float = 0.0):
    x = width / 2.0
    y = depth / 2.0
    z0 = min_z
    z1 = min_z + height

    verts: List[Vec3] = [
        (-x, -y, z0), ( x, -y, z0), ( x,  y, z0), (-x,  y, z0),
        (-x, -y, z1), ( x, -y, z1), ( x,  y, z1), (-x,  y, z1),
    ]
    # outward-facing quads in Blender-like Z-up world coordinates
    faces = [
        (0, 3, 2, 1),  # bottom
        (4, 5, 6, 7),  # roof
        (0, 1, 5, 4),  # -Y
        (1, 2, 6, 5),  # +X
        (2, 3, 7, 6),  # +Y
        (3, 0, 4, 7),  # -X
    ]
    return verts, faces


def camera_basis(pitch_deg: float, yaw_deg: float):
    """
    Use the same camera location angles as BAT4Blender, then derive an
    orthographic camera plane looking toward the origin.
    """
    pitch = math.radians(pitch_deg)
    yaw = math.radians(yaw_deg)

    cam_dir_from_origin = norm((
        math.sin(pitch) * math.cos(yaw),
        math.sin(pitch) * math.sin(yaw),
        math.cos(pitch),
    ))
    view = vmul(cam_dir_from_origin, -1.0)  # camera -> origin

    world_up = (0.0, 0.0, 1.0)
    right = norm(cross(view, world_up))
    up = norm(cross(right, view))
    return right, up, view


def project(v: Vec3, right: Vec3, up: Vec3) -> Vec2:
    return dot(v, right), dot(v, up)


def face_normal(poly: Sequence[Vec3]) -> Vec3:
    return norm(cross(vsub(poly[1], poly[0]), vsub(poly[2], poly[0])))


def visible_faces(verts, faces, view: Vec3):
    result = []
    for f in faces:
        poly = [verts[i] for i in f]
        # Same sign convention as BAT4Blender's visible-face test.
        if dot(face_normal(poly), view) < -1e-9:
            result.append(poly)
    return result


def ref_cell_camera_width(right: Vec3) -> float:
    corners = [
        (-8.0, -8.0, 0.0), (-8.0, 8.0, 0.0),
        ( 8.0, -8.0, 0.0), ( 8.0, 8.0, 0.0),
    ]
    xs = [dot(c, right) for c in corners]
    return max(xs) - min(xs)


def clip_polygon(poly: List[PV], inside: Callable[[PV], bool], intersect: Callable[[PV, PV], PV]) -> List[PV]:
    if not poly:
        return []
    out: List[PV] = []
    prev = poly[-1]
    prev_in = inside(prev)

    for cur in poly:
        cur_in = inside(cur)
        if cur_in:
            if not prev_in:
                out.append(intersect(prev, cur))
            out.append(cur)
        elif prev_in:
            out.append(intersect(prev, cur))
        prev = cur
        prev_in = cur_in
    return out


def intersection_x(a: PV, b: PV, x: float) -> PV:
    d = b.cx - a.cx
    t = 0.0 if abs(d) < 1e-12 else (x - a.cx) / d
    return PV(lerp3(a.world, b.world, t), x, a.cy + (b.cy-a.cy)*t)


def intersection_y(a: PV, b: PV, y: float) -> PV:
    d = b.cy - a.cy
    t = 0.0 if abs(d) < 1e-12 else (y - a.cy) / d
    return PV(lerp3(a.world, b.world, t), a.cx + (b.cx-a.cx)*t, y)


def clip_to_rect(poly: List[PV], left: float, right: float, bottom: float, top: float) -> List[PV]:
    p = clip_polygon(poly, lambda q: q.cx >= left - 1e-9,
                     lambda a,b: intersection_x(a,b,left))
    p = clip_polygon(p, lambda q: q.cx <= right + 1e-9,
                     lambda a,b: intersection_x(a,b,right))
    p = clip_polygon(p, lambda q: q.cy >= bottom - 1e-9,
                     lambda a,b: intersection_y(a,b,bottom))
    p = clip_polygon(p, lambda q: q.cy <= top + 1e-9,
                     lambda a,b: intersection_y(a,b,top))
    return p


def triangulate(poly: List[PV]):
    """Fan triangulation; clipped box faces remain convex."""
    if len(poly) < 3:
        return []
    return [(poly[0], poly[i], poly[i+1]) for i in range(1, len(poly)-1)]


def export_axis(v: Vec3, rot_index: int) -> Vec3:
    """
    Approximate the axis conversion used by Blender's OBJ export with
    up_axis=Y and rotation-dependent forward axis.

    SOUTH corresponds to the familiar Blender OBJ conversion (X,Z,-Y);
    other rotations are quarter-turns around the exported Y axis.

    This standalone script uses the requested logical rotation order
    NORTH=0, EAST=1, SOUTH=2, WEST=3.
    """
    x, y, z = v
    south = (x, z, -y)
    X, Y, Z = south
    if rot_index == 0:   # NORTH, forward +Z
        return (-X, Y, -Z)
    if rot_index == 1:   # EAST, forward +X
        return (-Z, Y, X)
    if rot_index == 2:   # SOUTH, forward -Z
        return (X, Y, Z)
    if rot_index == 3:   # WEST, forward -X
        return (Z, Y, -X)
    raise ValueError(rot_index)


def uv_for(v: PV, left: float, right: float, bottom: float, top: float) -> Vec2:
    u = (v.cx - left) / max(right-left, 1e-12)
    vv = (v.cy - bottom) / max(top-bottom, 1e-12)
    # Clamp tiny floating errors at cut lines.
    u = min(1.0, max(0.0, u))
    vv = min(1.0, max(0.0, vv))
    return u, vv


def write_obj(path: Path, tile_meshes, rot_index: int):
    """
    tile_meshes:
      [(mesh_name, material_name, triangles, rect), ...]
    Each triangle vertex is emitted independently. This is verbose but makes
    OBJ vertex/UV indexing unambiguous for fshgen.
    """
    lines = [
        "# Standalone BAT4Blender-style LOD export",
        "# Triangles only; material names carry the FSH IID.",
    ]
    next_index = 1

    for mesh_name, material_name, triangles, rect in tile_meshes:
        left, right, bottom, top = rect
        lines += [
            f"o {mesh_name}",
            f"g {mesh_name}",
            f"usemtl {material_name}",
        ]

        for tri in triangles:
            normal_world = face_normal([q.world for q in tri])
            # Transform normal with same rotation-only mapping.
            normal = norm(export_axis(normal_world, rot_index))

            local_indices = []
            for q in tri:
                px, py, pz = export_axis(q.world, rot_index)
                u, v = uv_for(q, left, right, bottom, top)
                lines.append(f"v {px:.9f} {py:.9f} {pz:.9f}")
                lines.append(f"vt {u:.9f} {v:.9f}")
                lines.append(f"vn {normal[0]:.9f} {normal[1]:.9f} {normal[2]:.9f}")
                local_indices.append(next_index)
                next_index += 1

            a,b,c = local_indices
            lines.append(f"f {a}/{a}/{a} {b}/{b}/{b} {c}/{c}/{c}")

    lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")



def world_to_canvas_px(
    cx: float, cy: float,
    left_canvas: float, top_canvas: float,
    px_per_unit: float,
) -> Tuple[float, float]:
    """Convert camera-plane coordinates to full-canvas pixel coordinates."""
    x = (cx - left_canvas) * px_per_unit
    y = (top_canvas - cy) * px_per_unit
    return x, y


def draw_template_png(
    out_path: Path,
    canvas_w: int,
    canvas_h: int,
    verts: List[Vec3],
    faces,
    right_vec: Vec3,
    up_vec: Vec3,
    left_canvas: float,
    top_canvas: float,
    px_per_unit: float,
    tile_records,
    transparent: bool = True,
):
    """
    Create a PNG guide showing exactly where the projected LOD lands
    in the full BAT canvas.

    Guide:
      - transparent/white background
      - projected visible edges
      - vertex dots
      - building bounding box
      - 256px tile borders
      - image center crosshair
      - labels for canvas size and tile indices

    This template is for alignment/debugging only. Do not pass it to
    fshgen as the final Day texture unless you intentionally want the
    guide rendered into the model.
    """
    try:
        from PIL import Image, ImageDraw
    except ImportError as e:
        raise RuntimeError(
            "Template generation requires Pillow. Install with: pip install pillow"
        ) from e

    bg = (255, 255, 255, 0) if transparent else (255, 255, 255, 255)
    img = Image.new("RGBA", (int(canvas_w), int(canvas_h)), bg)
    draw = ImageDraw.Draw(img)

    # Project all vertices.
    proj = [project(v, right_vec, up_vec) for v in verts]
    pix = [
        world_to_canvas_px(cx, cy, left_canvas, top_canvas, px_per_unit)
        for cx, cy in proj
    ]

    # Bounding box of projected LOD.
    xs = [p[0] for p in pix]
    ys = [p[1] for p in pix]
    bbox = (
        int(round(min(xs))),
        int(round(min(ys))),
        int(round(max(xs))),
        int(round(max(ys))),
    )

    # 256 px tile grid.
    for x in range(0, int(canvas_w) + 1, 256):
        draw.line([(x, 0), (x, canvas_h)], fill=(0, 120, 255, 160), width=1)
    for y in range(0, int(canvas_h) + 1, 256):
        draw.line([(0, y), (canvas_w, y)], fill=(0, 120, 255, 160), width=1)

    # Canvas center crosshair.
    cx0, cy0 = canvas_w / 2.0, canvas_h / 2.0
    draw.line([(cx0 - 8, cy0), (cx0 + 8, cy0)], fill=(255, 220, 0, 220), width=1)
    draw.line([(cx0, cy0 - 8), (cx0, cy0 + 8)], fill=(255, 220, 0, 220), width=1)

    # Projected world origin / building footprint center.
    # make_box() is centered on X=Y=0 and starts at Z=0, so (0,0,0) is
    # the exact center of the building footprint on the ground plane.
    origin_cx, origin_cy = project((0.0, 0.0, 0.0), right_vec, up_vec)
    origin_px = world_to_canvas_px(
        origin_cx, origin_cy, left_canvas, top_canvas, px_per_unit
    )
    ox, oy = origin_px
    r = 7
    draw.ellipse((ox-r, oy-r, ox+r, oy+r), outline=(0, 80, 255, 255), width=3)
    draw.line([(ox-12, oy), (ox+12, oy)], fill=(0, 80, 255, 255), width=2)
    draw.line([(ox, oy-12), (ox, oy+12)], fill=(0, 80, 255, 255), width=2)
    draw.text((ox+10, oy+8), "ORIGIN / FOOTPRINT CENTER (0,0,0)", fill=(0, 80, 255, 255))

    # Draw visible projected faces/edges.
    _, _, view_vec = camera_basis(45.0, -67.5)  # dummy init, replaced below
    # Determine visibility using the camera basis already passed to us.
    # Reconstruct view direction from right/up orientation is awkward,
    # so simply draw all box edges; for a box this is still a useful guide.
    edge_set = set()
    for f in faces:
        for i in range(len(f)):
            a = f[i]
            b = f[(i+1) % len(f)]
            edge_set.add(tuple(sorted((a, b))))

    for a, b in sorted(edge_set):
        draw.line([pix[a], pix[b]], fill=(255, 60, 60, 230), width=2)

    # Vertex dots and labels.
    for i, (x, y) in enumerate(pix):
        r = 2
        draw.ellipse((x-r, y-r, x+r, y+r), fill=(0, 255, 100, 255))
        draw.text((x+4, y-6), str(i), fill=(0, 0, 0, 255))

    # Building projected bounding box.
    draw.rectangle(bbox, outline=(255, 0, 255, 230), width=1)

    # Tile labels from manifest records.
    for rec in tile_records:
        row, col = rec["row"], rec["col"]
        x0 = col * 256
        y0 = row * 256
        draw.text((x0 + 4, y0 + 4), f"tile {rec['count']}", fill=(0, 0, 0, 255))

    draw.text((4, max(0, canvas_h - 16)), f"{canvas_w}x{canvas_h}", fill=(0,0,0,255))
    img.save(out_path)

    return {
        "template": out_path.name,
        "building_bbox_px": {
            "left": bbox[0],
            "top": bbox[1],
            "right": bbox[2],
            "bottom": bbox[3],
        },
        "projected_vertices_px": [
            [round(x, 3), round(y, 3)] for x, y in pix
        ],
        "ground_origin_px": [round(origin_px[0], 3), round(origin_px[1], 3)],
    }


def make_blank_day_pngs(
    out_dir: Path,
    tile_records,
    rgba=(255,255,255,0),
):
    """
    Optional helper: emit blank correctly-sized Day PNGs for each tile.
    Useful for alignment tests and for replacing with actual artwork later.
    """
    try:
        from PIL import Image
    except ImportError as e:
        raise RuntimeError(
            "Blank PNG generation requires Pillow. Install with: pip install pillow"
        ) from e

    created = []
    for rec in tile_records:
        w, h = rec["tile_px"]
        p = out_dir / rec["expected_png"]
        Image.new("RGBA", (int(w), int(h)), rgba).save(p)
        created.append(p.name)
    return created


def generate_view(
    out_dir: Path,
    model_name: str,
    gid: str,
    width: float,
    depth: float,
    height: float,
    z_index: int,
    rot_index: int,
    make_templates: bool = False,
    make_blank_pngs: bool = False,
    ground_clearance: float = 0.01,
):
    """Generate one continuous camera-facing tiled plane per zoom/rotation.

    Geometry is ONE OBJ object/group. 256 px FSH boundaries subdivide that plane
    into cells, but neighbouring cells share the same geometry vertices. Each
    cell has its own material/FSH and its own UV rectangle 0..1, so the texture
    seam exists only in UV/material space, not in geometry space.

    This is intentionally different from the previous version, which emitted
    one independent OBJ object/group per tile. Keeping a single continuous
    mesh is friendlier to SC4/Lot Editor while retaining <=256 px FSH tiles.
    """
    rot_letter, yaw = ROTATIONS[rot_index]
    pitch = PITCHES[z_index]
    right_vec, up_vec, view_vec = camera_basis(pitch, yaw)

    verts, faces = make_box(width, depth, height)
    projected_all = [project(v, right_vec, up_vec) for v in verts]
    xmin = min(p[0] for p in projected_all)
    xmax = max(p[0] for p in projected_all)
    ymin = min(p[1] for p in projected_all)
    ymax = max(p[1] for p in projected_all)

    ref_width = ref_cell_camera_width(right_vec)
    px_per_unit = ZOOM_SIZES[z_index] / ref_width

    object_w_px = (xmax - xmin) * px_per_unit
    object_h_px = (ymax - ymin) * px_per_unit
    canvas_w = round_up_fsh_chunk(object_w_px + 2 * SLOP_PX)
    canvas_h = round_up_fsh_chunk(object_h_px + 2 * SLOP_PX)

    left_canvas = xmin - SLOP_PX / px_per_unit
    top_canvas = ymax + SLOP_PX / px_per_unit

    ncols = math.ceil(canvas_w / MAX_TILE_PX)
    nrows = math.ceil(canvas_h / MAX_TILE_PX)

    # Pixel grid boundaries. The final cell may be narrower/shorter than 256 px.
    x_edges_px = [min(i * MAX_TILE_PX, canvas_w) for i in range(ncols + 1)]
    y_edges_px = [min(i * MAX_TILE_PX, canvas_h) for i in range(nrows + 1)]

    def unproject(cx: float, cy: float) -> Vec3:
        # Plane goes through world origin; right/up span the camera plane.
        return vadd(vmul(right_vec, cx), vmul(up_vec, cy))

    # Build ONE shared geometry vertex grid.
    # Row 0 is top of the image; increasing row moves downward in camera space.
    grid_world = []
    for row in range(nrows + 1):
        cy = top_canvas - y_edges_px[row] / px_per_unit
        row_verts = []
        for col in range(ncols + 1):
            cx = left_canvas + x_edges_px[col] / px_per_unit
            row_verts.append(unproject(cx, cy))
        grid_world.append(row_verts)

    # IMPORTANT FOR LOT EDITOR / GAME:
    # A camera-facing plane through world origin has negative-Z geometry below
    # the projected ground-origin scanline. Reader can preview that geometry,
    # but Lot Editor/game may clip the below-ground portion.
    #
    # Translate the ENTIRE plane along the camera normal until its lowest world-Z
    # is just above Z=0. Because this translation is parallel to the view vector,
    # dot(shift, right_vec) == dot(shift, up_vec) == 0: the orthographic screen
    # projection (and therefore texture alignment) does not change at all.
    min_world_z_before = min(v[2] for row in grid_world for v in row)
    target_min_z = max(0.0, ground_clearance)
    camera_side = vmul(view_vec, -1.0)  # origin -> camera; positive Z component
    if camera_side[2] <= 1e-12:
        raise RuntimeError("camera-side vector has no positive Z component")

    lift_along_camera = 0.0
    if min_world_z_before < target_min_z:
        lift_along_camera = (target_min_z - min_world_z_before) / camera_side[2]
        shift = vmul(camera_side, lift_along_camera)
        grid_world = [
            [vadd(v, shift) for v in row]
            for row in grid_world
        ]

    min_world_z_after = min(v[2] for row in grid_world for v in row)
    max_world_z_after = max(v[2] for row in grid_world for v in row)

    model_iid = instance_id(z_index, rot_index, 0, is_night=False)
    obj_name = f"{tgi_stem(TID_S3D, gid, model_iid)}.obj"
    obj_path = out_dir / obj_name

    mesh_name = f"{model_name}_UserModel_Z{z_index+1}{rot_letter}"
    lines = [
        "# Continuous camera-facing tiled-plane SC4 BAT export",
        "# ONE object/group; shared geometry vertices; one material per <=256 px tile.",
        "# Each tile has independent UV corners exactly 0..1.",
        f"o {mesh_name}",
        f"g {mesh_name}",
    ]

    # OBJ geometry vertices are emitted once and shared by neighbouring tiles.
    vertex_index = {}
    next_v = 1
    for row in range(nrows + 1):
        for col in range(ncols + 1):
            world = grid_world[row][col]
            x, y, z = export_axis(world, rot_index)
            lines.append(f"v {x:.9f} {y:.9f} {z:.9f}")
            vertex_index[(row, col)] = next_v
            next_v += 1

    # One common normal. Determine it from the actual exported geometry and
    # orient all triangles consistently. We want the normal on the camera side.
    desired_world = norm(vmul(view_vec, -1.0))
    desired_export = norm(export_axis(desired_world, rot_index))
    lines.append(f"vn {desired_export[0]:.9f} {desired_export[1]:.9f} {desired_export[2]:.9f}")
    normal_index = 1

    tile_records = []
    expected_pngs = []
    count = 0
    next_vt = 1

    for row in range(nrows):
        for col in range(ncols):
            l_px = x_edges_px[col]
            r_px = x_edges_px[col + 1]
            t_px = y_edges_px[row]
            b_px = y_edges_px[row + 1]

            iid = instance_id(z_index, rot_index, count, is_night=False)
            material_name = f"{iid:08X}_{model_name}_UserModel_Z{z_index+1}{rot_letter}"
            png_name = f"{iid:08X}_Day.png"
            lines.append(f"usemtl {material_name}")

            # Tile-local UVs. They are duplicated at material seams while the
            # geometry indices remain shared, which is valid OBJ indexing.
            # Order: BL, BR, TR, TL.
            tile_vts = [
                (0.0, 0.0),
                (1.0, 0.0),
                (1.0, 1.0),
                (0.0, 1.0),
            ]
            vt_ids = []
            for u, v in tile_vts:
                lines.append(f"vt {u:.9f} {v:.9f}")
                vt_ids.append(next_vt)
                next_vt += 1

            # Geometry indices. Image row increases downward, so:
            # TL=(row,col), TR=(row,col+1), BL=(row+1,col), BR=(row+1,col+1)
            tl = vertex_index[(row, col)]
            tr = vertex_index[(row, col + 1)]
            bl = vertex_index[(row + 1, col)]
            br = vertex_index[(row + 1, col + 1)]
            vt_bl, vt_br, vt_tr, vt_tl = vt_ids

            # Pick winding by checking the first triangle in exported space.
            e_bl = export_axis(grid_world[row + 1][col], rot_index)
            e_br = export_axis(grid_world[row + 1][col + 1], rot_index)
            e_tr = export_axis(grid_world[row][col + 1], rot_index)
            tri_n = norm(cross(vsub(e_br, e_bl), vsub(e_tr, e_bl)))
            front = dot(tri_n, desired_export) >= 0.0

            if front:
                # BL -> BR -> TR, BL -> TR -> TL
                lines.append(f"f {bl}/{vt_bl}/{normal_index} {br}/{vt_br}/{normal_index} {tr}/{vt_tr}/{normal_index}")
                lines.append(f"f {bl}/{vt_bl}/{normal_index} {tr}/{vt_tr}/{normal_index} {tl}/{vt_tl}/{normal_index}")
            else:
                # Reverse winding but keep corresponding UVs.
                lines.append(f"f {bl}/{vt_bl}/{normal_index} {tr}/{vt_tr}/{normal_index} {br}/{vt_br}/{normal_index}")
                lines.append(f"f {bl}/{vt_bl}/{normal_index} {tl}/{vt_tl}/{normal_index} {tr}/{vt_tr}/{normal_index}")

            tile_records.append({
                "count": count,
                "row": row,
                "col": col,
                "iid": f"{iid:08x}",
                "material": material_name,
                "expected_png": png_name,
                "tile_px": [r_px - l_px, b_px - t_px],
                "triangles": 2,
                "uv_full_01": True,
                "shared_geometry_vertices": True,
            })
            expected_pngs.append(png_name)
            count += 1

    lines.append("")
    obj_path.write_text("\n".join(lines), encoding="utf-8")

    template_info = None
    if make_templates:
        template_name = f"{model_iid:08X}_Template.png"
        template_info = draw_template_png(
            out_dir / template_name,
            canvas_w,
            canvas_h,
            verts,
            faces,
            right_vec,
            up_vec,
            left_canvas,
            top_canvas,
            px_per_unit,
            tile_records,
            transparent=True,
        )

    blank_pngs = []
    if make_blank_pngs:
        blank_pngs = make_blank_day_pngs(out_dir, tile_records)

    return {
        "zoom": z_index + 1,
        "rotation": rot_letter,
        "yaw_deg": yaw,
        "pitch_deg": pitch,
        "obj": obj_name,
        "canvas_px": [canvas_w, canvas_h],
        "px_per_world_unit": px_per_unit,
        "continuous_camera_facing_plane": True,
        "ground_safe_camera_normal_shift": True,
        "ground_clearance_m": ground_clearance,
        "min_world_z_before_shift": min_world_z_before,
        "min_world_z_after_shift": min_world_z_after,
        "max_world_z_after_shift": max_world_z_after,
        "camera_normal_shift_distance_m": lift_along_camera,
        "single_obj_group": True,
        "shared_geometry_grid": [ncols + 1, nrows + 1],
        "quad_count": len(tile_records),
        "uv_corners_each_tile": {
            "bottom_left": [0.0, 0.0],
            "bottom_right": [1.0, 0.0],
            "top_right": [1.0, 1.0],
            "top_left": [0.0, 1.0],
        },
        "tiles": tile_records,
        "expected_pngs": expected_pngs,
        "template_info": template_info,
        "blank_pngs": blank_pngs,
    }

def alpha_mask(img, threshold: int = 32):
    """Return a binary mask from alpha using a configurable cutoff."""
    alpha = img.getchannel("A")
    return alpha.point(lambda a: 255 if a >= threshold else 0)


def largest_component_bbox(mask):
    """Find the bbox of the largest 8-connected non-zero component in a binary mask."""
    w, h = mask.size
    pix = mask.load()
    visited = bytearray(w * h)
    best_bbox = None
    best_count = 0

    def neighbors(x, y):
        for ny in range(max(0, y - 1), min(h, y + 2)):
            base = ny * w
            for nx in range(max(0, x - 1), min(w, x + 2)):
                if nx == x and ny == y:
                    continue
                yield nx, ny, base + nx

    for y in range(h):
        row = y * w
        for x in range(w):
            idx = row + x
            if visited[idx]:
                continue
            visited[idx] = 1
            if pix[x, y] == 0:
                continue

            q = deque([(x, y)])
            count = 0
            minx = maxx = x
            miny = maxy = y

            while q:
                cx, cy = q.popleft()
                count += 1
                if cx < minx:
                    minx = cx
                if cx > maxx:
                    maxx = cx
                if cy < miny:
                    miny = cy
                if cy > maxy:
                    maxy = cy

                for nx, ny, nidx in neighbors(cx, cy):
                    if visited[nidx]:
                        continue
                    visited[nidx] = 1
                    if pix[nx, ny] != 0:
                        q.append((nx, ny))

            if count > best_count:
                best_count = count
                best_bbox = (minx, miny, maxx + 1, maxy + 1)

    return best_bbox


def alpha_bbox(img, threshold: int = 32, largest_component_only: bool = True, pad: int = 1):
    """
    Robust bbox detection for transparent-background AI renders.

    - Ignores very faint edge noise by thresholding alpha.
    - Optionally keeps only the largest connected component, which helps when
      there are stray semi-transparent pixels or tiny detached fragments.
    - Adds a small padding so anti-aliased edges are not clipped too tightly.
    """
    mask = alpha_mask(img, threshold=threshold)
    bbox = None
    if largest_component_only:
        bbox = largest_component_bbox(mask)
    if bbox is None:
        bbox = mask.getbbox()
    if bbox is None:
        return (0, 0, img.width, img.height)

    if pad > 0:
        l, t, r, b = bbox
        bbox = (
            max(0, l - pad),
            max(0, t - pad),
            min(img.width, r + pad),
            min(img.height, b + pad),
        )
    return bbox


def fit_source_to_bbox(
    source_path: Path,
    canvas_size,
    target_bbox,
    preserve_aspect=True,
    fit_width=False,
    alpha_threshold: int = 32,
    largest_component_only: bool = True,
    bbox_pad: int = 1,
):
    try:
        from PIL import Image
    except ImportError as e:
        raise RuntimeError("Image fitting requires Pillow. Install with: pip install pillow") from e

    # Accept either a file path or an already-cropped PIL image (used by --quad-image).
    if hasattr(source_path, "convert") and hasattr(source_path, "crop"):
        src = source_path.convert("RGBA").copy()
    else:
        src = Image.open(source_path).convert("RGBA")
    src = src.crop(alpha_bbox(
        src,
        threshold=alpha_threshold,
        largest_component_only=largest_component_only,
        pad=bbox_pad,
    ))

    left, top, right, bottom = target_bbox
    tw = max(1, int(round(right - left)))
    th = max(1, int(round(bottom - top)))

    if fit_width:
        scale = tw / max(src.width, 1)
        nw = tw
        nh = max(1, int(round(src.height * scale)))
    elif preserve_aspect:
        scale = min(tw / src.width, th / src.height)
        nw = max(1, int(round(src.width * scale)))
        nh = max(1, int(round(src.height * scale)))
    else:
        nw, nh = tw, th

    resized = src.resize((nw, nh), Image.Resampling.LANCZOS)
    canvas = Image.new("RGBA", (int(canvas_size[0]), int(canvas_size[1])), (255, 255, 255, 0))

    # Buildings should sit on the same ground line: bottom-center align.
    x = int(round(left + (tw - nw) / 2))
    y = int(round(bottom - nh))
    canvas.alpha_composite(resized, (x, y))
    return canvas


def split_full_canvas_to_tiles(full_img, tile_records, out_dir: Path, *, z_index=None, rot_index=None, is_night=False):
    """Split a fitted full canvas into SC4 FSH-sized PNG tiles.

    Day mode preserves the filenames stored in tile_records. Night mode derives
    matching BAT night IIDs (+0x8000) from zoom/rotation/count and writes
    XXXXXXXX_Night.png.
    """
    created = []
    for rec in tile_records:
        row, col = rec["row"], rec["col"]
        w, h = rec["tile_px"]
        x0 = col * 256
        y0 = row * 256
        tile = full_img.crop((x0, y0, x0 + int(w), y0 + int(h)))

        if is_night:
            if z_index is None or rot_index is None:
                raise ValueError("z_index and rot_index are required for Night tile generation")
            iid = instance_id(z_index, rot_index, rec["count"], is_night=True)
            png_name = f"{iid:08X}_Night.png"
        else:
            png_name = rec["expected_png"]

        out_path = out_dir / png_name
        tile.save(out_path)
        created.append(out_path.name)
    return created


def split_quad_image(source_path: Path):
    """
    Split a 2x2 composite image into the four BAT rotations.

    Layout:
        top-left     = N
        top-right    = E
        bottom-left  = S
        bottom-right = W

    Returns a dict of cropped PIL RGBA images keyed by N/E/S/W.
    """
    try:
        from PIL import Image
    except ImportError as e:
        raise RuntimeError(
            "Quad image input requires Pillow. Install with: pip install pillow"
        ) from e

    img = Image.open(source_path).convert("RGBA")
    w, h = img.size
    if w < 2 or h < 2:
        raise ValueError(f"quad image is too small: {w}x{h}")

    mx = w // 2
    my = h // 2

    return {
        "N": img.crop((0, 0, mx, my)),
        "E": img.crop((mx, 0, w, my)),
        "S": img.crop((0, my, mx, h)),
        "W": img.crop((mx, my, w, h)),
    }


def generate_pngs_from_four_sources(
    manifest,
    out_dir: Path,
    source_paths,
    *,
    is_night=False,
    preserve_aspect=True,
    fit_width=False,
    alpha_threshold: int = 32,
    largest_component_only: bool = True,
    bbox_pad: int = 1,
):
    """Generate Day or Night texture tiles from four rotation sources.

    Day and Night use the exact same manifest canvas/bounding-box geometry, so
    their final SC4 placement is identical. Each source image is fitted into the
    same target bbox for its rotation/zoom before tile splitting.
    """
    created = []
    rot_to_index = {"N": 0, "E": 1, "S": 2, "W": 3}

    for view in manifest["views"]:
        rot = view["rotation"]
        info = view.get("template_info")
        if not info:
            raise RuntimeError("template_info missing; generate views with make_templates=True")

        bboxd = info["building_bbox_px"]
        bbox = (bboxd["left"], bboxd["top"], bboxd["right"], bboxd["bottom"])

        full_img = fit_source_to_bbox(
            source_paths[rot],
            view["canvas_px"],
            bbox,
            preserve_aspect=preserve_aspect,
            fit_width=fit_width,
            alpha_threshold=alpha_threshold,
            largest_component_only=largest_component_only,
            bbox_pad=bbox_pad,
        )
        created.extend(split_full_canvas_to_tiles(
            full_img,
            view["tiles"],
            out_dir,
            z_index=view["zoom"] - 1,
            rot_index=rot_to_index[rot],
            is_night=is_night,
        ))
    return created


def generate_day_pngs_from_four_sources(
    manifest, out_dir: Path, source_paths, preserve_aspect=True, fit_width=False,
    alpha_threshold: int = 32, largest_component_only: bool = True, bbox_pad: int = 1,
):
    """Backward-compatible Day wrapper."""
    return generate_pngs_from_four_sources(
        manifest, out_dir, source_paths, is_night=False,
        preserve_aspect=preserve_aspect, fit_width=fit_width,
        alpha_threshold=alpha_threshold,
        largest_component_only=largest_component_only, bbox_pad=bbox_pad,
    )




def format_dim(value: float) -> str:
    return f"{value:g}"


def build_sc4plugindesc_xml(model_name: str, gid: str, width: float, depth: float, height: float) -> str:
    gid = gid.lower().removeprefix("0x")
    return f"""<?xml version="1.0" encoding="UTF-8"?>

<SC4PLUGINDESC Name="{model_name}" ResKey="0x{TID_S3D}-0x{gid}-0x00030000" Version="2" BATVersion="0x00001073" Quality="3">
<DIMENSIONS Width="{format_dim(width)}" Height="{format_dim(height)}" Depth="{format_dim(depth)}">
</DIMENSIONS>
</SC4PLUGINDESC>
"""


def write_sc4plugindesc_xml(out_dir: Path, model_name: str, gid: str, width: float, depth: float, height: float) -> Path:
    xml_path = out_dir / f"88777601_{gid.lower().removeprefix('0x')}_00030000.xml"
    xml_path.write_text(build_sc4plugindesc_xml(model_name, gid, width, depth, height), encoding="utf-8")
    return xml_path


def choose_preview_view(manifest, preferred_rotation: str = "N", preferred_zoom: int = 5):
    for view in manifest.get("views", []):
        if view["zoom"] == preferred_zoom and view["rotation"] == preferred_rotation:
            return view
    for view in manifest.get("views", []):
        if view["rotation"] == preferred_rotation:
            return view
    return manifest.get("views", [None])[0]


def render_preview_from_sources(
    manifest,
    source_paths,
    *,
    preserve_aspect=True,
    fit_width=False,
    alpha_threshold: int = 32,
    largest_component_only: bool = True,
    bbox_pad: int = 1,
    preferred_rotation: str = "N",
    preferred_zoom: int = 5,
):
    if source_paths is None:
        return None
    view = choose_preview_view(manifest, preferred_rotation=preferred_rotation, preferred_zoom=preferred_zoom)
    if not view:
        return None
    info = view.get("template_info")
    if not info:
        return None
    bboxd = info["building_bbox_px"]
    bbox = (bboxd["left"], bboxd["top"], bboxd["right"], bboxd["bottom"])
    return fit_source_to_bbox(
        source_paths[view["rotation"]],
        view["canvas_px"],
        bbox,
        preserve_aspect=preserve_aspect,
        fit_width=fit_width,
        alpha_threshold=alpha_threshold,
        largest_component_only=largest_component_only,
        bbox_pad=bbox_pad,
    )


def make_preview_icon_from_full_image(full_img, size: int = 64, alpha_threshold: int = 32):
    try:
        from PIL import Image, ImageDraw
    except ImportError as e:
        raise RuntimeError("Preview icon generation requires Pillow. Install with: pip install pillow") from e

    bg = Image.new("RGBA", (size, size), (188, 188, 188, 255))
    if full_img is None:
        draw = ImageDraw.Draw(bg)
        draw.rectangle((8, 8, size - 9, size - 9), outline=(120, 120, 120, 255), width=1)
        draw.line((10, size - 12, size - 10, size - 12), fill=(140, 140, 140, 255), width=2)
        return bg

    crop = full_img.crop(alpha_bbox(full_img, threshold=alpha_threshold, largest_component_only=True, pad=2))
    if crop.width <= 0 or crop.height <= 0:
        return bg

    inner = size - 8
    scale = min(inner / max(crop.width, 1), inner / max(crop.height, 1))
    nw = max(1, int(round(crop.width * scale)))
    nh = max(1, int(round(crop.height * scale)))
    resized = crop.resize((nw, nh), Image.Resampling.LANCZOS)
    x = (size - nw) // 2
    y = size - 4 - nh
    bg.alpha_composite(resized, (x, y))
    return bg


def save_preview_debug_images(out_dir: Path, day_icon, night_icon):
    saved = []
    if day_icon is not None:
        p = out_dir / "preview_day_64.png"
        day_icon.save(p)
        saved.append(p.name)
    if night_icon is not None:
        p = out_dir / "preview_night_64.png"
        night_icon.save(p)
        saved.append(p.name)
    return saved


def encode_bitmap_bytes(img) -> bytes:
    bio = io.BytesIO()
    img.convert("RGBA").save(bio, format="BMP")
    return bio.getvalue()


def encode_jfif_bytes(img) -> bytes:
    bio = io.BytesIO()
    img.convert("RGB").save(bio, format="JPEG", quality=90, optimize=False)
    return bio.getvalue()


def read_dbpf_records(path: Path):
    raw = path.read_bytes()
    if raw[:4] != b"DBPF":
        raise RuntimeError(f"not a DBPF/SC4Model file: {path}")
    if len(raw) < 96:
        raise RuntimeError(f"file too small to be a valid DBPF: {path}")

    header = bytearray(raw[:96])
    entry_count = struct.unpack_from("<I", raw, 36)[0]
    index_offset = struct.unpack_from("<I", raw, 40)[0]
    index_size = struct.unpack_from("<I", raw, 44)[0]
    if index_offset + index_size > len(raw):
        raise RuntimeError("invalid DBPF index bounds")

    records = []
    for n in range(entry_count):
        off = index_offset + n * 20
        tid, gid, iid, loc, size = struct.unpack_from("<IIIII", raw, off)
        records.append({
            "tid": tid,
            "gid": gid,
            "iid": iid,
            "data": raw[loc:loc + size],
        })
    return header, records


def write_dbpf_records(path: Path, header: bytearray, records):
    if len(header) < 96:
        header = bytearray(header) + bytearray(96 - len(header))
    header = bytearray(header[:96])
    header[0:4] = b"DBPF"
    if struct.unpack_from("<I", header, 4)[0] == 0:
        struct.pack_into("<I", header, 4, 1)
    if struct.unpack_from("<I", header, 8)[0] == 0:
        struct.pack_into("<I", header, 8, 0)
    if struct.unpack_from("<I", header, 32)[0] == 0:
        struct.pack_into("<I", header, 32, 7)

    payload = bytearray()
    index = bytearray()
    cursor = 96
    for rec in records:
        data = bytes(rec["data"])
        index += struct.pack("<IIIII", rec["tid"], rec["gid"], rec["iid"], cursor, len(data))
        payload += data
        cursor += len(data)

    struct.pack_into("<I", header, 36, len(records))
    struct.pack_into("<I", header, 40, 96 + len(payload))
    struct.pack_into("<I", header, 44, len(index))

    path.write_bytes(bytes(header) + bytes(payload) + bytes(index))


def patch_sc4model_with_preview_and_xml(
    sc4model_path: Path,
    *,
    gid: str,
    model_name: str,
    width: float,
    depth: float,
    height: float,
    day_icon,
    night_icon,
):
    gid_int = int(gid.lower().removeprefix("0x"), 16)
    iid = 0x00030000
    header, records = read_dbpf_records(sc4model_path)

    replace_types = {0x66778001, 0x66778002, 0x74807101, 0x74807102, 0x88777601}
    kept = [
        rec for rec in records
        if not (rec["gid"] == gid_int and rec["iid"] == iid and rec["tid"] in replace_types)
    ]

    if day_icon is None and night_icon is None:
        day_icon = night_icon = make_preview_icon_from_full_image(None)
    elif day_icon is None:
        day_icon = night_icon
    elif night_icon is None:
        night_icon = day_icon

    xml_bytes = build_sc4plugindesc_xml(model_name, gid, width, depth, height).encode("utf-8")
    kept.extend([
        {"tid": 0x74807102, "gid": gid_int, "iid": iid, "data": encode_jfif_bytes(night_icon)},
        {"tid": 0x74807101, "gid": gid_int, "iid": iid, "data": encode_jfif_bytes(day_icon)},
        {"tid": 0x66778002, "gid": gid_int, "iid": iid, "data": encode_bitmap_bytes(night_icon)},
        {"tid": 0x66778001, "gid": gid_int, "iid": iid, "data": encode_bitmap_bytes(day_icon)},
        {"tid": 0x88777601, "gid": gid_int, "iid": iid, "data": xml_bytes},
    ])
    write_dbpf_records(sc4model_path, header, kept)


def run_fshgen_import(out_dir: Path, model_name: str, gid: str, mini_fshgen: Path) -> Path:
    """Run the bundled Python mini_fshgen.py BAT import workflow.

    Equivalent to:
      python mini_fshgen.py import --output building.SC4Model --force
        --with-BAT-models --format Dxt1 --gid 0x........
        output/*.obj output/*_Day.png [output/*_Night.png]

    The wildcard patterns are intentionally passed as literal strings;
    mini_fshgen.py expands them internally.
    """
    out_dir = out_dir.resolve()
    mini_fshgen = mini_fshgen.resolve()
    if not mini_fshgen.is_file():
        raise RuntimeError(
            f"mini_fshgen.py not found: {mini_fshgen}\n"
            "Place mini_fshgen.py beside this generator or specify "
            "--mini-fshgen /path/to/mini_fshgen.py"
        )

    output_path = out_dir / f"{model_name}.SC4Model"
    obj_files = sorted(out_dir.glob("*.obj"))
    day_files = sorted(out_dir.glob("*_Day.png"))
    night_files = sorted(out_dir.glob("*_Night.png"))

    if not obj_files:
        raise RuntimeError(f"no OBJ files found in: {out_dir}")
    if not day_files:
        raise RuntimeError(f"no *_Day.png files found in: {out_dir}")

    cmd = [
        sys.executable, str(mini_fshgen), "import",
        "--output", str(output_path),
        "--force",
        "--with-BAT-models",
        "--format", "Dxt1",
        "--gid", f"0x{gid}",
        f"{out_dir}/*.obj",
        f"{out_dir}/*_Day.png",
    ]
    if night_files:
        cmd.append(f"{out_dir}/*_Night.png")

    print("Running mini_fshgen.py:")
    print("  " + " ".join(shlex.quote(x) for x in cmd))
    proc = subprocess.run(cmd)
    if proc.returncode != 0:
        raise RuntimeError(f"mini_fshgen.py import failed with exit code {proc.returncode}")
    if not output_path.is_file():
        raise RuntimeError(f"mini_fshgen.py completed but SC4Model was not created: {output_path}")
    return output_path


def main():
    ap = argparse.ArgumentParser(
        description="Generate a ground-safe continuous camera-facing tiled plane per SC4 zoom/rotation, with per-tile UV fixed to 0..1."
    )
    ap.add_argument("--width", type=float, required=True, help="LOD width, meters")
    ap.add_argument("--depth", type=float, required=True, help="LOD depth, meters")
    ap.add_argument("--height", type=float, required=True, help="LOD height, meters")
    ap.add_argument("--gid", default="ffffffff", help="SC4 group ID, e.g. ffffffff or 0xffffffff")
    ap.add_argument("--model-name", default="building", help="Name embedded in object/material names")
    ap.add_argument("--out", type=Path, default=Path("b4b_obj_out"))
    ap.add_argument("--ground-clearance", type=float, default=0.01, help=("Minimum world Z for the camera-facing plane, in meters. The plane is moved along the camera normal so its screen projection does not change. Default: 0.01"))
    ap.add_argument("--make-templates", action="store_true", help="Generate full-canvas PNG guides showing the exact projected LOD position.")
    ap.add_argument("--make-blank-pngs", action="store_true", help="Generate transparent Day PNGs with the exact tile filenames/sizes expected by fshgen.")
    ap.add_argument("--north-image", type=Path, help="Day source image for North rotation")
    ap.add_argument("--east-image", type=Path, help="Day source image for East rotation")
    ap.add_argument("--south-image", type=Path, help="Day source image for South rotation")
    ap.add_argument("--west-image", type=Path, help="Day source image for West rotation")
    ap.add_argument("--north-night-image", type=Path, help="Night source image for North rotation")
    ap.add_argument("--east-night-image", type=Path, help="Night source image for East rotation")
    ap.add_argument("--south-night-image", type=Path, help="Night source image for South rotation")
    ap.add_argument("--west-night-image", type=Path, help="Night source image for West rotation")
    ap.add_argument("--quad-image", type=Path, help="Single 2x2 DAY source image: top-left=N, top-right=E, bottom-left=S, bottom-right=W")
    ap.add_argument("--quad-night-image", type=Path, help="Single 2x2 NIGHT source image: top-left=N, top-right=E, bottom-left=S, bottom-right=W")
    ap.add_argument("--stretch-images", action="store_true", help="Stretch each source exactly to building_bbox_px instead of preserving aspect ratio.")
    ap.add_argument("--fit-width", action="store_true", help=("Scale each source so its detected building bbox width exactly matches the BAT building_bbox_px width, while preserving aspect ratio and bottom-center alignment."))
    ap.add_argument("--alpha-threshold", type=int, default=32, help=("Alpha cutoff for source bbox detection (0-255). Higher values ignore faint semi-transparent edge noise. Default: 32"))
    ap.add_argument("--bbox-pad", type=int, default=1, help="Extra pixels of padding to keep around the detected source building bbox. Default: 1")
    ap.add_argument("--no-largest-component", action="store_true", help=("Use the full thresholded alpha bbox instead of the largest connected component. By default the largest connected component is used for more stable AI-image bbox detection."))
    ap.add_argument(
        "--patch-sc4model",
        action="store_true",
        help="Patch --out/<model-name>.SC4Model with preview BMP/JFIF resources and SC4PLUGINDESC XML.",
    )
    ap.add_argument(
        "--run-fshgen",
        action="store_true",
        help="Run mini_fshgen.py import automatically, then patch the generated SC4Model.",
    )
    ap.add_argument(
        "--mini-fshgen",
        type=Path,
        default=Path(__file__).with_name("mini_fshgen_qfs.py"),
        help="Path to mini_fshgen.py used by --run-fshgen. Default: beside this generator script.",
    )
    args = ap.parse_args()

    if min(args.width, args.depth, args.height) <= 0:
        ap.error("width/depth/height must all be > 0")

    gid = args.gid.lower().removeprefix("0x")
    if len(gid) != 8 or any(c not in "0123456789abcdef" for c in gid):
        ap.error("--gid must be exactly 8 hexadecimal digits")
    if not (0 <= args.alpha_threshold <= 255):
        ap.error("--alpha-threshold must be in the range 0..255")
    if args.bbox_pad < 0:
        ap.error("--bbox-pad must be >= 0")
    if args.ground_clearance < 0:
        ap.error("--ground-clearance must be >= 0")
    if args.stretch_images and args.fit_width:
        ap.error("--stretch-images and --fit-width cannot be used together")
    if args.patch_sc4model and args.run_fshgen:
        ap.error("--patch-sc4model and --run-fshgen cannot be used together")

    args.out.mkdir(parents=True, exist_ok=True)

    manifest = {
        "generator": "standalone_b4b_obj_generator",
        "reference": "memo33/BAT4Blender",
        "model_name": args.model_name,
        "gid": gid,
        "rotation_order": ["N", "E", "S", "W"],
        "dimensions_m": {"width": args.width, "depth": args.depth, "height": args.height},
        "views": [],
    }
    manifest["source_bbox_detection"] = {"alpha_threshold": args.alpha_threshold, "bbox_pad": args.bbox_pad, "largest_component_only": not args.no_largest_component}
    manifest["source_fit_mode"] = ("stretch" if args.stretch_images else "fit_width" if args.fit_width else "preserve_aspect")

    for z in range(5):
        for r in range(4):
            rec = generate_view(
                args.out, args.model_name, gid,
                args.width, args.depth, args.height, z, r,
                make_templates=(
                    args.make_templates
                    or args.quad_image is not None
                    or args.quad_night_image is not None
                    or any([
                        args.north_image, args.east_image, args.south_image, args.west_image,
                        args.north_night_image, args.east_night_image,
                        args.south_night_image, args.west_night_image,
                    ])
                ),
                make_blank_pngs=args.make_blank_pngs,
                ground_clearance=args.ground_clearance,
            )
            manifest["views"].append(rec)
            print(f"Z{rec['zoom']}{rec['rotation']}: {rec['obj']} canvas={rec['canvas_px'][0]}x{rec['canvas_px'][1]} tiles={len(rec['tiles'])}")

    manifest_path = args.out / "manifest.json"

    day_supplied = [args.north_image, args.east_image, args.south_image, args.west_image]
    night_supplied = [args.north_night_image, args.east_night_image, args.south_night_image, args.west_night_image]
    has_day_individual = any(p is not None for p in day_supplied)
    has_night_individual = any(p is not None for p in night_supplied)

    if args.quad_image is not None and has_day_individual:
        ap.error("--quad-image cannot be combined with --north-image/--east-image/--south-image/--west-image")
    if args.quad_night_image is not None and has_night_individual:
        ap.error("--quad-night-image cannot be combined with --north-night-image/--east-night-image/--south-night-image/--west-night-image")

    day_sources = None
    day_source_description = None
    night_sources = None
    night_source_description = None

    if args.quad_image is not None:
        if not args.quad_image.is_file():
            ap.error(f"--quad-image not found: {args.quad_image}")
        day_sources = split_quad_image(args.quad_image)
        day_source_description = f"2x2 Day quad image {args.quad_image.name}"
        manifest["quad_image"] = {"source": str(args.quad_image), "layout": {"top_left": "N", "top_right": "E", "bottom_left": "S", "bottom_right": "W"}}
    elif has_day_individual:
        if not all(p is not None for p in day_supplied):
            ap.error("--north-image, --east-image, --south-image and --west-image must be supplied together")
        for pth in day_supplied:
            if not pth.is_file():
                ap.error(f"Day source image not found: {pth}")
        day_sources = {"N": args.north_image, "E": args.east_image, "S": args.south_image, "W": args.west_image}
        day_source_description = "4 Day source images"

    if args.quad_night_image is not None:
        if not args.quad_night_image.is_file():
            ap.error(f"--quad-night-image not found: {args.quad_night_image}")
        night_sources = split_quad_image(args.quad_night_image)
        night_source_description = f"2x2 Night quad image {args.quad_night_image.name}"
        manifest["quad_night_image"] = {"source": str(args.quad_night_image), "layout": {"top_left": "N", "top_right": "E", "bottom_left": "S", "bottom_right": "W"}}
    elif has_night_individual:
        if not all(p is not None for p in night_supplied):
            ap.error("--north-night-image, --east-night-image, --south-night-image and --west-night-image must be supplied together")
        for pth in night_supplied:
            if not pth.is_file():
                ap.error(f"Night source image not found: {pth}")
        night_sources = {"N": args.north_night_image, "E": args.east_night_image, "S": args.south_night_image, "W": args.west_night_image}
        night_source_description = "4 Night source images"

    common_generation_kwargs = dict(
        preserve_aspect=(not args.stretch_images and not args.fit_width),
        fit_width=args.fit_width,
        alpha_threshold=args.alpha_threshold,
        largest_component_only=not args.no_largest_component,
        bbox_pad=args.bbox_pad,
    )

    if day_sources is not None:
        created_day = generate_pngs_from_four_sources(manifest, args.out, day_sources, is_night=False, **common_generation_kwargs)
        manifest["generated_day_pngs"] = created_day
        print(f"Generated {len(created_day)} Day PNG tile(s) from {day_source_description}.")

    if night_sources is not None:
        created_night = generate_pngs_from_four_sources(manifest, args.out, night_sources, is_night=True, **common_generation_kwargs)
        manifest["generated_night_pngs"] = created_night
        print(f"Generated {len(created_night)} Night PNG tile(s) from {night_source_description}.")

    xml_path = write_sc4plugindesc_xml(args.out, args.model_name, gid, args.width, args.depth, args.height)
    manifest["generated_xml"] = xml_path.name

    preview_day_full = render_preview_from_sources(manifest, day_sources, **common_generation_kwargs)
    preview_night_full = render_preview_from_sources(manifest, night_sources, **common_generation_kwargs)
    preview_day_icon = make_preview_icon_from_full_image(preview_day_full, alpha_threshold=args.alpha_threshold)
    preview_night_icon = make_preview_icon_from_full_image(preview_night_full, alpha_threshold=args.alpha_threshold)
    manifest["preview_debug_images"] = save_preview_debug_images(args.out, preview_day_icon, preview_night_icon)

    target_sc4model = None
    if args.run_fshgen:
        target_sc4model = run_fshgen_import(args.out, args.model_name, gid, args.mini_fshgen)
        print(f"Built SC4Model via mini_fshgen.py: {target_sc4model}")
    elif args.patch_sc4model:
        target_sc4model = args.out / f"{args.model_name}.SC4Model"

    if target_sc4model is not None:
        if not target_sc4model.is_file():
            ap.error(f"SC4Model file not found for patching: {target_sc4model}")
        patch_sc4model_with_preview_and_xml(
            target_sc4model,
            gid=gid,
            model_name=args.model_name,
            width=args.width,
            depth=args.depth,
            height=args.height,
            day_icon=preview_day_icon,
            night_icon=preview_night_icon,
        )
        manifest["patched_sc4model"] = str(target_sc4model)
        print(f"Patched SC4Model with preview BMP/JFIF and XML: {target_sc4model}")

    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    readme = f"""Standalone BAT4Blender-style OBJ output

Model: {args.model_name}
GID:   {gid}
Size:  {args.width} x {args.depth} x {args.height} m

The directory contains 20 OBJ files (5 zooms x 4 rotations).
Each OBJ contains one continuous camera-facing mesh subdivided at 256 px tile boundaries; each tile uses its own Day PNG. Optional Night PNGs use the matching BAT night IID (+0x8000).

Default rotation order used by this script:
    N=0, E=1, S=2, W=3

If you want to build the SC4Model manually after generation, run from this directory:

python mini_fshgen.py import --output {args.model_name}.SC4Model --force --with-BAT-models --format Dxt1 --gid 0x{gid} *.obj *_Day.png *_Night.png

After that, place {args.model_name}.SC4Model in --out and re-run this script with --patch-sc4model. The target filename is inferred automatically from --model-name.

Important:
- PNG dimensions must match the tile_px dimensions in manifest.json.
- Each S3D view uses one material and one FSH/Day PNG.
- UV is always the full rectangle: (0,0), (1,0), (1,1), (0,1).
- Material names inside each OBJ begin with the FSH IID, as BAT4Blender does.
- No .mtl file is required by this workflow.
- The script always writes a sidecar XML file: {xml_path.name}
- --patch-sc4model takes no filename; it patches --out/<model-name>.SC4Model.
- If --patch-sc4model is used, the SC4Model is patched with these additional resources:
    66778001 / GID / 00030000  (BMP preview)
    66778002 / GID / 00030000  (BMP preview)
    74807101 / GID / 00030000  (JFIF preview)
    74807102 / GID / 00030000  (JFIF preview)
    88777601 / GID / 00030000  (SC4PLUGINDESC XML)
- The inserted previews are simple 64x64 images intended only to populate the unused BAT preview slots.
- If --run-fshgen is used, the script calls mini_fshgen.py, builds the SC4Model in --out, and patches it automatically.
- Use --mini-fshgen if mini_fshgen.py is not beside this generator script.
- If --make-templates is used, 00030000_Template.png-style files show the exact projected LOD position in the full canvas.
- manifest.json also records building_bbox_px and projected_vertices_px.
- If --make-blank-pngs is used, correctly named/sized transparent Day PNGs are generated for every tile.
- --quad-image accepts one 2x2 DAY image with this fixed layout: top-left=N, top-right=E, bottom-left=S, bottom-right=W.
- --quad-night-image accepts one 2x2 NIGHT image with the same layout.
- Night inputs can be supplied without Day inputs. Night PNG filenames use BAT night IIDs (the corresponding Day IID + 0x8000), for example 00030000_Day.png -> 00038000_Night.png.
- Day and Night use the same generated canvas/building_bbox_px per view so game placement aligns.
- Source image bbox detection uses alpha-thresholding and, by default, the largest connected component for more stable transparent-background AI renders.
- Fit modes:
    default            = preserve source aspect ratio and fit within building_bbox_px
    --stretch-images   = force exact building_bbox_px width and height
    --fit-width        = force exact building_bbox_px width only, preserve aspect ratio
"""
    (args.out / "README.txt").write_text(readme, encoding="utf-8")
    print(f"Manifest: {manifest_path}")
    print(f"SC4PLUGINDESC XML: {xml_path}")


if __name__ == "__main__":
    main()
