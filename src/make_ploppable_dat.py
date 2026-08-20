#!/usr/bin/env python3
"""
make_ploppable_dat.py

Generate a Plugin-Manager-style Landmark ploppable .dat directly from:
    gid, width, height, depth

No Lot Editor / Plugin Manager / Reader / template DAT required.
Python 3 standard library only.

Assumptions
-----------
* Dimensions are in meters.
* The BAT model uses the usual S3D base key:
      Type     0x5AD0E817
      Group    = gid
      Instance 0x00030000
* The generated lot is a simple Civic/Plopped landmark-style item.
* By default Building / Lot / Icon all share one package IID.
* Building Exemplar PluginPackID is set to that same package IID.
* The generated DAT resources are deliberately UNCOMPRESSED.
  Your .SC4Model itself may still be QFS-compressed; that does not matter
  because the Building Exemplar references it by TGI.
* The model is centered on the lot.
* No props, base textures, foundations, transit paths, jobs, utilities, etc.
  are generated.

This is intended to be imported by an SC4Model generator:

    from make_ploppable_dat import generate_ploppable_dat

    result = generate_ploppable_dat(
        "MyBuilding_Ploppable.dat",
        gid=gid,
        width=model_width,
        height=model_height,
        depth=model_depth,
        name="My Building",
    )

It also has a CLI:

    python make_ploppable_dat.py out.dat \
        --gid 0x12345678 \
        --width 35.2 --height 120.0 --depth 27.4 \
        --name "My Building"\n        --icon my_icon.png\n"""

from __future__ import annotations

import argparse
import binascii
import io
import math
import struct
import time
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence


# ---------------------------------------------------------------------------
# SC4 / DBPF constants
# ---------------------------------------------------------------------------

TYPE_EXEMPLAR = 0x6534284A
TYPE_S3D      = 0x5AD0E817
TYPE_PNG      = 0x856DDBAC

# LotConfiguration Exemplars use this group.
GROUP_LOT = 0xA8FBD372

# Menu icon PNGs conventionally use this group.
GROUP_ICON = 0x6A386D26

# Exemplar properties.
P_EXEMPLAR_TYPE       = 0x00000010
P_EXEMPLAR_NAME       = 0x00000020

P_OCCUPANT_SIZE       = 0x27812810
P_RESOURCE_KEY_TYPE_1 = 0x27812821
P_PURPOSE             = 0x27812833

P_OCCUPANT_GROUPS     = 0xAA1DD396
P_LOT_RESOURCE_KEY    = 0xEA260589

P_PLOP_COST           = 0x49CAC341
P_BULLDOZE_COST       = 0x099AFACD

P_ITEM_NAME            = 0x899AFBAD
P_ITEM_DESCRIPTION     = 0x8A2602A9
P_ITEM_ICON            = 0x8A2602B8
P_ITEM_ORDER           = 0x8A2602B9
P_PLUGIN_PACK_ID        = 0x6A871B82

# Landmark / building properties mirrored from a normal Plugin Manager
# "Custom Ploppable" Landmark descriptor.
P_WEALTH                = 0x27812832
P_POLLUTION_CENTER      = 0x27812851
P_POWER_CONSUMED        = 0x27812854
P_FLAMMABILITY          = 0x29244DE5
P_QUERY_EXEMPLAR_GUID   = 0x2A499F85
P_EXEMPLAR_CATEGORY     = 0x2C8F8746
P_MAX_FIRE_STAGE        = 0x49BEDA31
P_POLLUTION_RADII       = 0x68EE9764
P_LANDMARK_EFFECT       = 0x87CD6399
P_BUILDING_FOUNDATION   = 0x88FCD877
P_SFX_QUERY_SOUND       = 0xAA1DD397
P_WATER_CONSUMED        = 0xC8ED2D84
P_SFX_DEFAULT_PLOP      = 0xC9B93A56
P_MAYOR_RATING_EFFECT   = 0xCA5B9305
P_BUDGET_DEPARTMENT     = 0xEA54D283
P_BUDGET_LINE           = 0xEA54D284
P_BUDGET_PURPOSE        = 0xEA54D285
P_BUDGET_COST           = 0xEA54D286

# LotConfiguration properties mirrored from the shown working Landmark lot.
P_GROWTH_STAGE          = 0x27812837
P_LOT_REQUIRED_ROADS    = 0x4A4A88F0
P_LOT_MIN_SLOPE         = 0x699B08A4
P_LOT_MAX_SLOPE_BEFORE  = 0x88EDC792
P_LOT_WEALTH_TYPES      = 0x88EDC795
P_LOT_PURPOSE_TYPES     = 0x88EDC796
P_LOT_RETAINING_WALL    = 0x88EDC798
P_LOT_MAX_SLOPE         = 0xE99B068C

# Values seen / resolved for the normal Landmark family.
LANDMARK_QUERY_GUID     = 0x2A56675C
LANDMARK_CATEGORY       = 0x2C8FBC6C
LANDMARK_QUERY_SOUND    = 0x0A8C9EF0
LANDMARK_PLOP_SOUND     = 0x6A5EC589

OG_LANDMARK             = 0x0000150A
OG_YIMBY                 = 0x00001920
OG_LANDMARK_OGLE        = 0x00001935

BUDGET_DEPT_LANDMARKS   = 0x2A5A723F
BUDGET_PURPOSE_LANDMARK = 0xAA59670C

P_LOT_VERSION          = 0x88EDC789
P_LOT_SIZE             = 0x88EDC790
P_LOT_ZONE_TYPES       = 0x88EDC793
P_LOT_DO_CONSTRUCTION  = 0xE99B068D
P_LOT_OBJECT_BASE      = 0x88EDC900


# Exemplar types.
EXEMPLAR_TYPE_BUILDING = 2
EXEMPLAR_TYPE_LOT      = 16

# Lot zone 0x0F = Civic/Plopped.
ZONE_CIVIC_PLOPPED = 0x0F

# OccupantGroups: Landmark Menu Placement.
OG_LANDMARK_MENU = 0x0000150A

# Purpose "Other".
PURPOSE_OTHER = 9

# Exemplar binary value types.
VT_UINT8   = 0x0100
VT_UINT16  = 0x0200
VT_UINT32  = 0x0300
VT_SINT32  = 0x0700
VT_SINT64  = 0x0800
VT_FLOAT32 = 0x0900
VT_BOOL    = 0x0B00
VT_STRING  = 0x0C00

EXEMPLAR_BINARY_MAGIC = b"EQZB1###"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _u32(v: int) -> bytes:
    return struct.pack("<I", v & 0xFFFFFFFF)


def _s64(v: int) -> bytes:
    return struct.pack("<q", int(v))


def _f32(v: float) -> bytes:
    return struct.pack("<f", float(v))


def _parse_hex_int(text: str) -> int:
    return int(text, 0)


def _check_u32(name: str, value: int) -> int:
    value = int(value)
    if not 0 <= value <= 0xFFFFFFFF:
        raise ValueError(f"{name} must fit UInt32, got {value!r}")
    return value


def _fixed16_16(meters: float) -> int:
    """
    LotConfig positions use 16.16-style fixed point.
    1 meter == 0x00010000.
    """
    return int(round(float(meters) * 65536.0)) & 0xFFFFFFFF


def _derived_iid(gid: int, tag: bytes) -> int:
    """
    Stable deterministic IID derived from the model GID.

    We deliberately do NOT use Python's hash(), because it changes between
    interpreter runs.
    """
    raw = struct.pack("<I", gid) + b":" + tag
    iid = zlib.crc32(raw) & 0xFFFFFFFF

    # Avoid tiny / visually suspicious IDs and all-ones.
    iid |= 0x10000000
    if iid == 0xFFFFFFFF:
        iid ^= 0x01010101
    return iid


# ---------------------------------------------------------------------------
# Binary Exemplar writer
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ExemplarProperty:
    property_id: int
    value_type: int
    values: tuple
    force_array: bool = False


def _pack_scalar(value_type: int, value) -> bytes:
    if value_type == VT_UINT8:
        return struct.pack("<B", int(value) & 0xFF)
    if value_type == VT_UINT16:
        return struct.pack("<H", int(value) & 0xFFFF)
    if value_type == VT_UINT32:
        return struct.pack("<I", int(value) & 0xFFFFFFFF)
    if value_type == VT_SINT32:
        return struct.pack("<i", int(value))
    if value_type == VT_SINT64:
        return struct.pack("<q", int(value))
    if value_type == VT_FLOAT32:
        return struct.pack("<f", float(value))
    if value_type == VT_BOOL:
        return struct.pack("<B", 1 if value else 0)
    raise ValueError(f"Unsupported scalar value type 0x{value_type:04X}")


def _pack_property(prop: ExemplarProperty) -> bytes:
    pid = _check_u32("property_id", prop.property_id)
    vt = prop.value_type
    vals = prop.values

    # IMPORTANT:
    # SC4 binary exemplar properties contain one extra BYTE immediately after
    # KeyType.  For KeyType 0x00 it is the repetition flag/count byte (normally
    # zero for a scalar).  For KeyType 0x80 it is an unused byte, followed by a
    # DWORD repetition count.  Omitting this byte shifts the rest of the
    # exemplar and makes Reader/game parse the following properties incorrectly.
    if vt == VT_STRING:
        if len(vals) != 1:
            raise ValueError("String property needs exactly one string")
        # Strings are represented as repeated bytes (KeyType 0x80).
        raw = str(vals[0]).encode("cp1252", errors="replace")
        return (
            struct.pack("<IHHB", pid, vt, 0x0080, 0)
            + struct.pack("<I", len(raw))
            + raw
        )

    if len(vals) == 1 and not prop.force_array:
        # KeyType 0x00: one unused/zero repetition byte, then scalar value.
        return (
            struct.pack("<IHHB", pid, vt, 0x0000, 0)
            + _pack_scalar(vt, vals[0])
        )

    # KeyType 0x80: unused byte, DWORD repetition count, then values.
    payload = b"".join(_pack_scalar(vt, v) for v in vals)
    return (
        struct.pack("<IHHB", pid, vt, 0x0080, 0)
        + struct.pack("<I", len(vals))
        + payload
    )


def build_exemplar(
    properties: Sequence[ExemplarProperty],
    *,
    parent_tgi: tuple[int, int, int] = (0, 0, 0),
) -> bytes:
    """
    Build an EQZB binary Exemplar.
    """
    t, g, i = parent_tgi
    body = b"".join(_pack_property(p) for p in properties)
    return (
        EXEMPLAR_BINARY_MAGIC
        + struct.pack("<III", t & 0xFFFFFFFF, g & 0xFFFFFFFF, i & 0xFFFFFFFF)
        + struct.pack("<I", len(properties))
        + body
    )


def p_u8(pid: int, *values: int) -> ExemplarProperty:
    return ExemplarProperty(pid, VT_UINT8, tuple(values))


def p_u32(pid: int, *values: int) -> ExemplarProperty:
    return ExemplarProperty(pid, VT_UINT32, tuple(values))


def p_u32_rep(pid: int, *values: int) -> ExemplarProperty:
    """Uint32 property forced to KeyType 0x80, even for a single value."""
    return ExemplarProperty(pid, VT_UINT32, tuple(values), True)


def p_s32(pid: int, *values: int) -> ExemplarProperty:
    return ExemplarProperty(pid, VT_SINT32, tuple(values))


def p_s64(pid: int, value: int) -> ExemplarProperty:
    return ExemplarProperty(pid, VT_SINT64, (value,))


def p_f32(pid: int, *values: float) -> ExemplarProperty:
    return ExemplarProperty(pid, VT_FLOAT32, tuple(values))


def p_str(pid: int, value: str) -> ExemplarProperty:
    return ExemplarProperty(pid, VT_STRING, (value,))


def inspect_exemplar(data: bytes) -> list[tuple[int, int, int, tuple]]:
    """
    Parse the subset of EQZB properties emitted by this generator.
    Returns (property_id, value_type, key_type, values).

    This is intentionally used as a self-check so malformed property framing is
    detected before a DAT is written.
    """
    if len(data) < 24 or data[:8] != EXEMPLAR_BINARY_MAGIC:
        raise ValueError("Not an EQZB1### binary exemplar")

    count = struct.unpack_from("<I", data, 20)[0]
    off = 24
    out = []

    sizes = {
        VT_UINT8: 1, VT_UINT16: 2, VT_UINT32: 4, VT_SINT32: 4,
        VT_SINT64: 8, VT_FLOAT32: 4, VT_BOOL: 1,
    }

    for _ in range(count):
        if off + 9 > len(data):
            raise ValueError("Truncated exemplar property header")
        pid, vt, key = struct.unpack_from("<IHH", data, off)
        off += 8
        flag = data[off]
        off += 1

        if key == 0x0000:
            reps = 0
            nvals = 1
        elif key == 0x0080:
            if off + 4 > len(data):
                raise ValueError("Truncated exemplar rep count")
            reps = struct.unpack_from("<I", data, off)[0]
            off += 4
            nvals = reps
        else:
            raise ValueError(f"Unsupported exemplar KeyType 0x{key:04X}")

        if vt == VT_STRING:
            if key != 0x0080:
                raise ValueError("Generated strings must use KeyType 0x80")
            if off + nvals > len(data):
                raise ValueError("Truncated exemplar string")
            raw = data[off:off+nvals]
            off += nvals
            values = (raw.decode("cp1252", errors="replace"),)
        else:
            size = sizes.get(vt)
            if size is None:
                raise ValueError(f"Unsupported exemplar value type 0x{vt:04X}")
            if off + size * nvals > len(data):
                raise ValueError("Truncated exemplar values")
            values = []
            for __ in range(nvals):
                if vt == VT_UINT8:
                    v = data[off]
                elif vt == VT_UINT16:
                    v = struct.unpack_from("<H", data, off)[0]
                elif vt == VT_UINT32:
                    v = struct.unpack_from("<I", data, off)[0]
                elif vt == VT_SINT32:
                    v = struct.unpack_from("<i", data, off)[0]
                elif vt == VT_SINT64:
                    v = struct.unpack_from("<q", data, off)[0]
                elif vt == VT_FLOAT32:
                    v = struct.unpack_from("<f", data, off)[0]
                elif vt == VT_BOOL:
                    v = bool(data[off])
                off += size
                values.append(v)
            values = tuple(values)

        out.append((pid, vt, key, values))

    if off != len(data):
        raise ValueError(
            f"Exemplar parser ended at {off}, but resource size is {len(data)}"
        )
    return out


# ---------------------------------------------------------------------------
# Tiny placeholder menu icon PNG
# ---------------------------------------------------------------------------

def _png_chunk(kind: bytes, payload: bytes) -> bytes:
    return (
        struct.pack(">I", len(payload))
        + kind
        + payload
        + struct.pack(">I", binascii.crc32(kind + payload) & 0xFFFFFFFF)
    )


def make_placeholder_icon_png() -> bytes:
    """
    Create a 176x44 RGBA PNG: the SC4 menu-icon strip size (4 x 44px states).

    The four states are intentionally simple; this only ensures the generated
    ploppable has a usable icon without requiring external image libraries.
    """
    w, h = 176, 44
    rows = bytearray()

    for y in range(h):
        rows.append(0)  # PNG filter 0
        for x in range(w):
            state_x = x % 44

            # Transparent exterior with a simple grayscale "building" shape.
            a = 0
            r = g = b = 0

            # faint panel
            if 2 <= state_x <= 41 and 2 <= y <= 41:
                r = g = b = 70
                a = 190

            # building body
            if 13 <= state_x <= 30 and 10 <= y <= 37:
                r = g = b = 210
                a = 255

            # roof
            if 16 <= state_x <= 27 and 6 <= y < 10:
                r = g = b = 235
                a = 255

            # windows
            if (
                16 <= state_x <= 19 or 24 <= state_x <= 27
            ) and (
                14 <= y <= 18 or 23 <= y <= 27
            ):
                r = g = b = 65
                a = 255

            rows.extend((r, g, b, a))

    ihdr = struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", ihdr)
        + _png_chunk(b"IDAT", zlib.compress(bytes(rows), 9))
        + _png_chunk(b"IEND", b"")
    )


def _alpha_bbox_for_icon(img, threshold: int = 32):
    """Return a simple alpha-based bounding box for icon extraction."""
    alpha = img.getchannel("A")
    mask = alpha.point(lambda a: 255 if a >= threshold else 0)
    bbox = mask.getbbox()
    if bbox is None:
        return (0, 0, img.width, img.height)
    return bbox


def make_sc4_menu_icon_png(icon_image_path: str | Path) -> bytes:
    """
    Build a valid 176x44 SC4 menu icon PNG from an input image.

    Rules:
    - If the source image is already 176x44, it is normalized and reused.
    - If the source image is 44x44, it becomes the base state and is expanded
      to a 176x44 4-state strip.
    - Otherwise the non-transparent content is cropped, scaled to fit inside a
      44x44 icon, bottom-aligned, and then expanded to a 176x44 strip.

    Requires Pillow only when this function is used.
    """
    try:
        from PIL import Image, ImageEnhance
    except ImportError as e:
        raise RuntimeError(
            '--icon requires Pillow. Install with: pip install pillow'
        ) from e

    icon_image_path = Path(icon_image_path)
    if not icon_image_path.is_file():
        raise FileNotFoundError(f'icon file not found: {icon_image_path}')

    with Image.open(icon_image_path) as im0:
        im = im0.convert('RGBA')

    if im.size == (176, 44):
        out = io.BytesIO()
        im.save(out, format='PNG')
        return out.getvalue()

    if im.size == (44, 44):
        normal = im
    else:
        bbox = _alpha_bbox_for_icon(im)
        crop = im.crop(bbox).convert('RGBA')

        normal = Image.new('RGBA', (44, 44), (0, 0, 0, 0))
        inner_w = 38
        inner_h = 38
        scale = min(
            inner_w / max(crop.width, 1),
            inner_h / max(crop.height, 1),
        )
        nw = max(1, int(round(crop.width * scale)))
        nh = max(1, int(round(crop.height * scale)))
        resized = crop.resize((nw, nh), Image.Resampling.LANCZOS)
        x = (44 - nw) // 2
        y = 42 - nh
        normal.alpha_composite(resized, (x, y))

    states = [
        normal,
        ImageEnhance.Brightness(normal).enhance(1.12),
        ImageEnhance.Brightness(normal).enhance(0.82),
        ImageEnhance.Brightness(normal).enhance(0.60),
    ]

    strip = Image.new('RGBA', (176, 44), (0, 0, 0, 0))
    for i, state in enumerate(states):
        strip.alpha_composite(state, (i * 44, 0))

    out = io.BytesIO()
    strip.save(out, format='PNG')
    return out.getvalue()


# ---------------------------------------------------------------------------
# DBPF writer
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Resource:
    type_id: int
    group_id: int
    instance_id: int
    data: bytes


def build_dbpf(resources: Sequence[Resource], *, timestamp: int | None = None) -> bytes:
    """
    Build an uncompressed SC4 DBPF 1.0 file with index version 7.0.

    Layout:
        96-byte DBPF header
        resource data...
        20-byte index entries...
    """
    if timestamp is None:
        timestamp = int(time.time()) & 0xFFFFFFFF

    header_size = 96
    data_blob = bytearray()
    index_rows = []

    pos = header_size
    seen_tgis = set()

    for res in resources:
        tgi = (
            _check_u32("resource type", res.type_id),
            _check_u32("resource group", res.group_id),
            _check_u32("resource instance", res.instance_id),
        )
        if tgi in seen_tgis:
            raise ValueError(
                "Duplicate resource TGI: "
                + " ".join(f"0x{x:08X}" for x in tgi)
            )
        seen_tgis.add(tgi)

        data = bytes(res.data)
        index_rows.append((*tgi, pos, len(data)))
        data_blob.extend(data)
        pos += len(data)

    index_offset = header_size + len(data_blob)
    index_blob = b"".join(
        struct.pack("<IIIII", t, g, i, off, size)
        for t, g, i, off, size in index_rows
    )
    index_size = len(index_blob)

    header = bytearray(96)
    header[0:4] = b"DBPF"

    # DBPF version 1.0
    struct.pack_into("<I", header, 0x04, 1)
    struct.pack_into("<I", header, 0x08, 0)

    # User version fields are left zero.
    # Dates.
    struct.pack_into("<I", header, 0x18, timestamp)
    struct.pack_into("<I", header, 0x1C, timestamp)

    # Index version 7.0, 20 bytes per entry.
    struct.pack_into("<I", header, 0x20, 7)
    struct.pack_into("<I", header, 0x24, len(resources))
    struct.pack_into("<I", header, 0x28, index_offset)
    struct.pack_into("<I", header, 0x2C, index_size)

    # Hole table: none.
    struct.pack_into("<I", header, 0x30, 0)
    struct.pack_into("<I", header, 0x34, 0)
    struct.pack_into("<I", header, 0x38, 0)

    # Index minor version 0.
    struct.pack_into("<I", header, 0x3C, 0)

    return bytes(header) + bytes(data_blob) + index_blob


# ---------------------------------------------------------------------------
# Ploppable generator
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class PloppableResult:
    output_path: Path
    gid: int
    model_tgi: tuple[int, int, int]
    building_tgi: tuple[int, int, int]
    lot_tgi: tuple[int, int, int]
    icon_tgi: tuple[int, int, int]
    lot_tiles_x: int
    lot_tiles_z: int


def generate_ploppable_dat(
    output_path: str | Path,
    *,
    gid: int,
    width: float,
    height: float,
    depth: float,
    name: str = "Generated BAT",
    description: str | None = None,
    model_iid: int = 0x00030000,
    package_iid: int | None = None,
    building_iid: int | None = None,
    lot_iid: int | None = None,
    icon_iid: int | None = None,
    building_group: int | None = None,
    plop_cost: int = 110,
    bulldoze_cost: int = 100,
    item_order: int = 0,
    orientation: int = 0,
    include_item_name: bool = True,
    include_item_description: bool = True,
    include_item_order: bool = True,
    icon_png_bytes: bytes | None = None,
) -> PloppableResult:
    """
    Generate a fuller Plugin-Manager-style Landmark ploppable .dat.

    Parameters
    ----------
    output_path:
        Destination .dat.
    gid:
        Group ID used by your generated SC4Model.
    width, height, depth:
        Model dimensions in meters. width -> lot X, depth -> lot Z.
    name:
        Menu/display name. Direct exemplar strings are 8-bit; non-CP1252
        characters become '?' in this minimal implementation.
    description:
        Menu description. Defaults to name.
    model_iid:
        Base S3D instance, normally 0x00030000.
    package_iid:
        Shared IID used by Building Exemplar, Lot Exemplar, menu icon, and
        PluginPackID by default. If omitted, a deterministic IID is derived
        from gid.
    building_iid / lot_iid / icon_iid:
        Optional per-resource overrides. If omitted, each uses package_iid.
        Normally leave these unset so all three resources share the same IID.
    building_group:
        Building Exemplar group ID. Defaults to gid.
    plop_cost / bulldoze_cost:
        Signed 64-bit exemplar values.
    item_order:
        Menu order.
    orientation:
        0=South, 1=West, 2=North, 3=East.
    """
    output_path = Path(output_path)

    gid = _check_u32("gid", gid)
    model_iid = _check_u32("model_iid", model_iid)

    for dim_name, dim in (("width", width), ("height", height), ("depth", depth)):
        if not math.isfinite(float(dim)) or float(dim) <= 0:
            raise ValueError(f"{dim_name} must be a finite positive number")

    width = float(width)
    height = float(height)
    depth = float(depth)

    if orientation not in (0, 1, 2, 3):
        raise ValueError("orientation must be 0, 1, 2, or 3")

    # One SC4 lot tile is 16m x 16m.
    lot_tiles_x = max(1, math.ceil(width / 16.0))
    lot_tiles_z = max(1, math.ceil(depth / 16.0))

    # LotConfigPropertySize is UInt8, so a single dimension cannot exceed 255.
    if lot_tiles_x > 255 or lot_tiles_z > 255:
        raise ValueError(
            f"Model needs a {lot_tiles_x}x{lot_tiles_z} lot, "
            "but LotConfigPropertySize is UInt8 (max 255 per axis)"
        )

    package_iid = _check_u32(
        "package_iid",
        _derived_iid(gid, b"package") if package_iid is None else package_iid,
    )

    # v3 convention:
    # Building / Lot / Icon all share the same Instance ID by default.
    building_iid = _check_u32(
        "building_iid",
        package_iid if building_iid is None else building_iid,
    )
    lot_iid = _check_u32(
        "lot_iid",
        package_iid if lot_iid is None else lot_iid,
    )
    icon_iid = _check_u32(
        "icon_iid",
        package_iid if icon_iid is None else icon_iid,
    )
    building_group = _check_u32(
        "building_group",
        gid if building_group is None else building_group,
    )

    if description is None:
        description = name

    model_tgi = (TYPE_S3D, gid, model_iid)
    building_tgi = (TYPE_EXEMPLAR, building_group, building_iid)
    lot_tgi = (TYPE_EXEMPLAR, GROUP_LOT, lot_iid)
    icon_tgi = (TYPE_PNG, GROUP_ICON, icon_iid)

    # ------------------------------------------------------------------
    # Building Exemplar
    # ------------------------------------------------------------------
    building_props = [
        p_u32(P_EXEMPLAR_TYPE, EXEMPLAR_TYPE_BUILDING),
        p_str(P_EXEMPLAR_NAME, f"{name} Building"),

        p_s64(P_BULLDOZE_COST, bulldoze_cost),
        p_f32(P_OCCUPANT_SIZE, width, height, depth),

        # BAT model key.
        p_u32(P_RESOURCE_KEY_TYPE_1, *model_tgi),

        # Normal Landmark descriptor values from the working example.
        p_u8(P_WEALTH, 0),                                  # None
        p_s32(P_POLLUTION_CENTER, 1, 1, 15, 0),
        p_u32(P_POWER_CONSUMED, 60),
        p_u8(P_FLAMMABILITY, 10),
        p_u32(P_QUERY_EXEMPLAR_GUID, LANDMARK_QUERY_GUID),
        p_u32(P_EXEMPLAR_CATEGORY, LANDMARK_CATEGORY),
        p_u8(P_MAX_FIRE_STAGE, 1),
        p_s64(P_PLOP_COST, plop_cost),
        p_f32(P_POLLUTION_RADII, 1.0, 2.0, 0.0, 0.0),

        # v3/v4 shared package identifier.
        p_u32(P_PLUGIN_PACK_ID, package_iid),

        p_f32(P_LANDMARK_EFFECT, 40.0, 20.0),
        p_u32(P_BUILDING_FOUNDATION, 0),

        # Item Name / Description / Order are appended conditionally below
        # for property-deletion tests.
        p_u32(P_ITEM_ICON, icon_iid),

        # Working Plugin Manager landmark uses all three groups.
        p_u32(P_OCCUPANT_GROUPS, OG_LANDMARK, OG_YIMBY, OG_LANDMARK_OGLE),

        p_u32(P_SFX_QUERY_SOUND, LANDMARK_QUERY_SOUND),
        p_u32(P_WATER_CONSUMED, 0),
        p_u32(P_SFX_DEFAULT_PLOP, LANDMARK_PLOP_SOUND),
        p_s32(P_MAYOR_RATING_EFFECT, 10, 256),

        # IMPORTANT: Plugin Manager/Lot Editor output stores this as Rep=1
        # (KeyType 0x80), not as a scalar Rep=0.
        p_u32_rep(P_LOT_RESOURCE_KEY, lot_iid),

        # The normal Landmark example also has a single budget row.
        p_u32_rep(P_BUDGET_DEPARTMENT, BUDGET_DEPT_LANDMARKS),
        p_u32_rep(P_BUDGET_LINE, 0),
        p_u32_rep(P_BUDGET_PURPOSE, BUDGET_PURPOSE_LANDMARK),
        ExemplarProperty(P_BUDGET_COST, VT_SINT64, (150,), True),
    ]

    # Keep the insertion order close to the usual descriptor layout.
    # These tests deliberately omit exactly one of the following.
    item_props = []
    if include_item_name:
        item_props.append(p_str(P_ITEM_NAME, name))
    if include_item_description:
        item_props.append(p_str(P_ITEM_DESCRIPTION, description))
    if include_item_order:
        item_props.append(p_u32(P_ITEM_ORDER, item_order))

    # Insert the optional item properties immediately after Item Icon.
    icon_index = next(
        n for n, prop in enumerate(building_props)
        if prop.property_id == P_ITEM_ICON
    ) + 1
    building_props[icon_index:icon_index] = item_props

    building_exemplar = build_exemplar(building_props)

    # ------------------------------------------------------------------
    # LotConfiguration Exemplar
    # ------------------------------------------------------------------
    lot_width_m = lot_tiles_x * 16.0
    lot_depth_m = lot_tiles_z * 16.0

    # Center model footprint on lot.
    cx = lot_width_m / 2.0
    cz = lot_depth_m / 2.0

    x1 = max(0.0, cx - width / 2.0)
    z1 = max(0.0, cz - depth / 2.0)
    x2 = min(lot_width_m, cx + width / 2.0)
    z2 = min(lot_depth_m, cz + depth / 2.0)

    # LotConfigPropertyLotObject -- Building:
    #
    #  1 type             = 0 Building
    #  2 LOD modifier     = 0
    #  3 orientation
    #  4 X position
    #  5 Z position
    #  6 Y position
    #  7 X1 bbox
    #  8 Z1 bbox
    #  9 X2 bbox
    # 10 Z2 bbox
    # 11 usage flag       = 0
    # 12 ObjectID         = 0 for this minimal generated object
    # 13 Building IID
    #
    # Positions are represented in 16.16 fixed point.
    building_lot_object = (
        0x00000000,
        0x00000000,
        orientation,
        _fixed16_16(cx),    # X
        _fixed16_16(0.0),   # Y / height
        _fixed16_16(cz),    # Z
        _fixed16_16(x1),
        _fixed16_16(z1),
        _fixed16_16(x2),
        _fixed16_16(z2),
        0x00000000,
        0x00000000,
        building_iid,
    )

    lot_props = [
        p_u32(P_EXEMPLAR_TYPE, EXEMPLAR_TYPE_LOT),
        p_str(P_EXEMPLAR_NAME, f"{name} Lot"),

        # Values mirrored from the working Plugin Manager Landmark lot.
        p_u8(P_GROWTH_STAGE, 0x64),
        p_u8(P_LOT_REQUIRED_ROADS, 0x08),
        p_f32(P_LOT_MIN_SLOPE, 0.0),

        p_u8(P_LOT_VERSION, 2),
        p_u8(P_LOT_SIZE, lot_tiles_x, lot_tiles_z),
        p_f32(P_LOT_MAX_SLOPE_BEFORE, 0.0),
        p_u8(P_LOT_ZONE_TYPES, ZONE_CIVIC_PLOPPED),
        p_u8(P_LOT_WEALTH_TYPES, 0),
        p_u8(P_LOT_PURPOSE_TYPES, 0),

        # Retaining-wall family used by the shown generated Landmark.
        p_u32(P_LOT_RETAINING_WALL, 0xC96D2135),

        # Building object. Texture objects (88EDC901...) are optional and are
        # deliberately not generated, producing a bare-ground lot.
        p_u32(P_LOT_OBJECT_BASE, *building_lot_object),

        # Working Landmark screenshot uses this foundation reference.
        p_u32(P_BUILDING_FOUNDATION, 0x890B7315),

        # Same unknown flag visible in the working lot.
        p_u32(0xCBE243F7, 1),

        p_f32(P_LOT_MAX_SLOPE, 50.0),

        # Fully built immediately when plopped.
        p_u8(P_LOT_DO_CONSTRUCTION, 0),
    ]
    lot_exemplar = build_exemplar(lot_props)

    # Validate both binary Exemplars before packaging them into DBPF.
    # In particular this catches framing/byte-alignment bugs which Reader would
    # otherwise show as only the first property being readable.
    parsed_building = inspect_exemplar(building_exemplar)
    parsed_lot = inspect_exemplar(lot_exemplar)

    if not any(
        pid == P_RESOURCE_KEY_TYPE_1 and values == model_tgi
        for pid, _vt, _key, values in parsed_building
    ):
        raise ValueError("Internal validation failed: RKT1 model TGI missing")

    if not any(
        pid == P_LOT_OBJECT_BASE and values[-1] == building_iid
        for pid, _vt, _key, values in parsed_lot
    ):
        raise ValueError("Internal validation failed: lot does not reference building IID")

    # ------------------------------------------------------------------
    # DBPF resources
    # ------------------------------------------------------------------
    if icon_png_bytes is None:
        icon_png_bytes = make_placeholder_icon_png()
    else:
        icon_png_bytes = bytes(icon_png_bytes)
        if not icon_png_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
            raise ValueError("icon_png_bytes must contain PNG data")

    resources = [
        Resource(*building_tgi, building_exemplar),
        Resource(*lot_tgi, lot_exemplar),
        Resource(*icon_tgi, icon_png_bytes),
    ]

    dbpf = build_dbpf(resources)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(dbpf)

    return PloppableResult(
        output_path=output_path,
        gid=gid,
        model_tgi=model_tgi,
        building_tgi=building_tgi,
        lot_tgi=lot_tgi,
        icon_tgi=icon_tgi,
        lot_tiles_x=lot_tiles_x,
        lot_tiles_z=lot_tiles_z,
    )


# ---------------------------------------------------------------------------
# Lightweight structural validator for generated DATs
# ---------------------------------------------------------------------------

def inspect_generated_dat(path: str | Path) -> list[tuple[int, int, int, int, int]]:
    """
    Return DBPF index rows:
        (type, group, instance, offset, size)

    Useful for unit tests in your SC4Model generator.
    """
    data = Path(path).read_bytes()
    if len(data) < 96 or data[:4] != b"DBPF":
        raise ValueError("Not a DBPF file")

    major = struct.unpack_from("<I", data, 0x04)[0]
    index_major = struct.unpack_from("<I", data, 0x20)[0]
    count = struct.unpack_from("<I", data, 0x24)[0]
    index_offset = struct.unpack_from("<I", data, 0x28)[0]
    index_size = struct.unpack_from("<I", data, 0x2C)[0]
    index_minor = struct.unpack_from("<I", data, 0x3C)[0]

    if major != 1 or index_major != 7 or index_minor != 0:
        raise ValueError(
            f"Unexpected DBPF/index version: DBPF={major}, "
            f"index={index_major}.{index_minor}"
        )
    if index_size < count * 20:
        raise ValueError("Index size is too small")

    result = []
    for n in range(count):
        off = index_offset + n * 20
        if off + 20 > len(data):
            raise ValueError("Index extends past EOF")
        row = struct.unpack_from("<IIIII", data, off)
        t, g, i, data_off, size = row
        if data_off + size > len(data):
            raise ValueError("Resource extends past EOF")
        result.append(row)

    return result


def _fmt_tgi(tgi: tuple[int, int, int]) -> str:
    return "/".join(f"0x{x:08X}" for x in tgi)



def generate_item_property_probes(
    output_dir: str | Path,
    *,
    gid: int,
    width: float,
    height: float,
    depth: float,
    name: str = "My Building",
    description: str = "Item property probe",
) -> list[Path]:
    """
    Generate four coexisting DATs:
      00 full reference
      01 Item Name removed
      02 Item Description removed
      03 Item Order removed

    Each uses a unique package IID so all four may be installed together.
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    tests = [
        ("00_full", True, True, True),
        ("01_no_item_name", False, True, True),
        ("02_no_item_description", True, False, True),
        ("03_no_item_order", True, True, False),
    ]

    paths = []
    for idx, (slug, inc_name, inc_desc, inc_order) in enumerate(tests):
        seed = struct.pack("<I", gid & 0xFFFFFFFF) + b":v5:" + slug.encode("ascii")
        package_iid = zlib.crc32(seed) & 0xFFFFFFFF
        package_iid |= 0x10000000
        if package_iid == 0xFFFFFFFF:
            package_iid ^= 0x01010101

        label = {
            "00_full": "V5 TEST 00 FULL",
            "01_no_item_name": "V5 TEST 01 NO NAME",
            "02_no_item_description": "V5 TEST 02 NO DESC",
            "03_no_item_order": "V5 TEST 03 NO ORDER",
        }[slug]

        out = output_dir / f"{slug}.dat"
        generate_ploppable_dat(
            out,
            gid=gid,
            width=width,
            height=height,
            depth=depth,
            name=f"{label} - {name}",
            description=description,
            package_iid=package_iid,
            item_order=(idx + 1) * 10,
            include_item_name=inc_name,
            include_item_description=inc_desc,
            include_item_order=inc_order,
        )
        paths.append(out)

    manifest = output_dir / "README.txt"
    manifest.write_text(
        "\n".join([
            "SimCity 4 v5 Item-property probe",
            "",
            "00_full.dat                : reference",
            "01_no_item_name.dat        : Item Name removed",
            "02_no_item_description.dat : Item Description removed",
            "03_no_item_order.dat       : Item Order removed",
            "",
            "All four use unique package IIDs and can coexist in Plugins.",
            "The LotObject coordinate order is also corrected in v5: X, Y, Z.",
        ]),
        encoding="utf-8",
    )
    return paths


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Generate a minimal ploppable SimCity 4 DAT from model GID and dimensions."
    )
    ap.add_argument("output", type=Path, help="output .dat")
    ap.add_argument("--gid", type=_parse_hex_int, required=True,
                    help="SC4Model Group ID, e.g. 0x12345678")
    ap.add_argument("--width", type=float, required=True, help="model width in meters")
    ap.add_argument("--height", type=float, required=True, help="model height in meters")
    ap.add_argument("--depth", type=float, required=True, help="model depth in meters")
    ap.add_argument("--name", default="Generated BAT")
    ap.add_argument("--description")
    ap.add_argument("--model-iid", type=_parse_hex_int, default=0x00030000)
    ap.add_argument(
        "--package-iid",
        type=_parse_hex_int,
        help="shared Building/Lot/Icon IID and PluginPackID; default derives from gid",
    )
    ap.add_argument("--building-iid", type=_parse_hex_int)
    ap.add_argument("--lot-iid", type=_parse_hex_int)
    ap.add_argument("--icon-iid", type=_parse_hex_int)
    ap.add_argument("--building-group", type=_parse_hex_int)
    ap.add_argument("--plop-cost", type=int, default=110)
    ap.add_argument("--bulldoze-cost", type=int, default=100)
    ap.add_argument("--item-order", type=_parse_hex_int, default=0)
    ap.add_argument("--orientation", type=int, choices=(0, 1, 2, 3), default=0)
    ap.add_argument(
        "--icon",
        type=Path,
        help=(
            "path to a custom menu icon image. If 176x44 it is used directly; "
            "if 44x44 or any other size, a 176x44 SC4 menu icon strip is generated from it"
        ),
    )
    ap.add_argument(
        "--probe-items",
        action="store_true",
        help="generate four DATs testing Item Name/Description/Order instead of one DAT",
    )

    args = ap.parse_args()

    if args.probe_items:
        paths = generate_item_property_probes(
            args.output,
            gid=args.gid,
            width=args.width,
            height=args.height,
            depth=args.depth,
            name=args.name,
            description=args.description or "Item property probe",
        )
        print(f"Generated {len(paths)} item-property probes in: {args.output}")
        for p in paths:
            print(f"  {p.name}")
        return 0

    icon_png_bytes = None
    if args.icon is not None:
        icon_png_bytes = make_sc4_menu_icon_png(args.icon)

    result = generate_ploppable_dat(
        args.output,
        gid=args.gid,
        width=args.width,
        height=args.height,
        depth=args.depth,
        name=args.name,
        description=args.description,
        model_iid=args.model_iid,
        package_iid=args.package_iid,
        building_iid=args.building_iid,
        lot_iid=args.lot_iid,
        icon_iid=args.icon_iid,
        building_group=args.building_group,
        plop_cost=args.plop_cost,
        bulldoze_cost=args.bulldoze_cost,
        item_order=args.item_order,
        orientation=args.orientation,
        icon_png_bytes=icon_png_bytes,
    )

    rows = inspect_generated_dat(result.output_path)

    print(f"Wrote:        {result.output_path}")
    print(f"Lot size:     {result.lot_tiles_x} x {result.lot_tiles_z} tiles")
    print(f"Model TGI:    {_fmt_tgi(result.model_tgi)}")
    print(f"Building TGI: {_fmt_tgi(result.building_tgi)}")
    print(f"Lot TGI:      {_fmt_tgi(result.lot_tgi)}")
    print(f"Icon TGI:     {_fmt_tgi(result.icon_tgi)}")
    print(f"Resources:    {len(rows)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
