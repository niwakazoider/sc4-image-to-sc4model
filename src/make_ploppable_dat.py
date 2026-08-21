#!/usr/bin/env python3
"""
make_ploppable_dat.py v6.1.1

Generate a SimCity 4 ploppable DBPF .dat from an SC4Model GID and dimensions.
Preset-specific Building/Lot values are loaded from JSON files under presets/.

Python 3.10+.
Pillow is optional and is only required when --icon is used.
"""
from __future__ import annotations

import argparse
import binascii
import io
import json
import math
import struct
import time
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

VERSION = "6.1.1"

# ---------------------------------------------------------------------------
# SC4 / DBPF constants
# ---------------------------------------------------------------------------

TYPE_EXEMPLAR = 0x6534284A
TYPE_S3D = 0x5AD0E817
TYPE_PNG = 0x856DDBAC
GROUP_LOT = 0xA8FBD372
GROUP_ICON = 0x6A386D26

P_EXEMPLAR_TYPE = 0x00000010
P_EXEMPLAR_NAME = 0x00000020
P_OCCUPANT_SIZE = 0x27812810
P_RESOURCE_KEY_TYPE_1 = 0x27812821
P_WEALTH = 0x27812832
P_OCCUPANT_GROUPS = 0xAA1DD396
P_LOT_RESOURCE_KEY = 0xEA260589
P_PLOP_COST = 0x49CAC341
P_BULLDOZE_COST = 0x099AFACD
P_ITEM_NAME = 0x899AFBAD
P_ITEM_DESCRIPTION = 0x8A2602A9
P_ITEM_ICON = 0x8A2602B8
P_ITEM_ORDER = 0x8A2602B9
P_PLUGIN_PACK_ID = 0x6A871B82
P_POLLUTION_CENTER = 0x27812851
P_POWER_CONSUMED = 0x27812854
P_FLAMMABILITY = 0x29244DE5
P_QUERY_EXEMPLAR_GUID = 0x2A499F85
P_EXEMPLAR_CATEGORY = 0x2C8F8746
P_MAX_FIRE_STAGE = 0x49BEDA31
P_POLLUTION_RADII = 0x68EE9764
# Building exemplar desirability effects.
P_LANDMARK_EFFECT = 0x2781284F
P_PARK_EFFECT = 0x27812850
P_BUILDING_FOUNDATION = 0x88FCD877
P_SFX_QUERY_SOUND = 0xAA1DD397
P_WATER_CONSUMED = 0xC8ED2D84
P_SFX_DEFAULT_PLOP = 0xC9B93A56
P_MAYOR_RATING_EFFECT = 0xCA5B9305
P_BUDGET_DEPARTMENT = 0xEA54D283
P_BUDGET_LINE = 0xEA54D284
P_BUDGET_PURPOSE = 0xEA54D285
P_BUDGET_COST = 0xEA54D286

P_GROWTH_STAGE = 0x27812837
P_LOT_REQUIRED_ROADS = 0x4A4A88F0
P_LOT_MIN_SLOPE = 0x699B08A4
P_LOT_VERSION = 0x88EDC789
P_LOT_SIZE = 0x88EDC790
P_LOT_MAX_SLOPE_BEFORE = 0x88EDC792
P_LOT_ZONE_TYPES = 0x88EDC793
P_LOT_WEALTH_TYPES = 0x88EDC795
P_LOT_PURPOSE_TYPES = 0x88EDC796
P_LOT_RETAINING_WALL = 0x88EDC798
P_LOT_OBJECT_BASE = 0x88EDC900
P_LOT_MAX_SLOPE = 0xE99B068C
P_LOT_DO_CONSTRUCTION = 0xE99B068D
P_LOT_UNKNOWN_CBE243F7 = 0xCBE243F7

EXEMPLAR_TYPE_BUILDING = 2
EXEMPLAR_TYPE_LOT = 16

VT_UINT8 = 0x0100
VT_UINT16 = 0x0200
VT_UINT32 = 0x0300
VT_SINT32 = 0x0700
VT_SINT64 = 0x0800
VT_FLOAT32 = 0x0900
VT_BOOL = 0x0B00
VT_STRING = 0x0C00
EXEMPLAR_BINARY_MAGIC = b"EQZB1###"

PRESET_DIR = Path(__file__).resolve().parent.parent / "presets"

# ---------------------------------------------------------------------------
# Preset loader
# ---------------------------------------------------------------------------

def _parse_hex_int(text: str) -> int:
    return int(text, 0)


def _preset_int(value, *, name: str = "value") -> int:
    if isinstance(value, bool):
        raise TypeError(f"{name} must be int, not bool")
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value, 0)
        except ValueError as exc:
            raise ValueError(f"{name}: invalid integer {value!r}") from exc
    raise TypeError(f"{name} must be int or hex string")


def _preset_int_list(values, *, name: str) -> list[int]:
    if not isinstance(values, list):
        raise TypeError(f"{name} must be a list")
    return [_preset_int(v, name=f"{name}[{i}]") for i, v in enumerate(values)]


def load_preset(name: str, *, preset_dir: str | Path | None = None) -> dict:
    if not name:
        raise ValueError("preset name is required")
    if not all(c.isalnum() or c in ("_", "-") for c in name):
        raise ValueError(f"Invalid preset name: {name!r}")

    root = PRESET_DIR if preset_dir is None else Path(preset_dir)
    path = root / f"{name}.json"
    if not path.is_file():
        available = sorted(p.stem for p in root.glob("*.json") if p.is_file()) if root.is_dir() else []
        raise ValueError(
            f"Preset not found: {name!r} ({path}). "
            f"Available: {', '.join(available) or '(none)'}"
        )

    try:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid preset JSON {path}: {exc}") from exc

    if data.get("schema_version") != 1:
        raise ValueError(f"{path}: unsupported schema_version {data.get('schema_version')!r}")
    if data.get("id") != name:
        raise ValueError(f"{path}: id must be {name!r}")

    building = data.get("building")
    lot = data.get("lot")
    if not isinstance(building, dict):
        raise ValueError(f"{path}: building must be an object")
    if not isinstance(lot, dict):
        raise ValueError(f"{path}: lot must be an object")

    for key in (
        "wealth", "pollution_center", "pollution_radii", "power_consumed",
        "water_consumed", "flammability", "max_fire_stage",
        "occupant_groups", "plop_cost", "bulldoze_cost",
    ):
        if key not in building:
            raise ValueError(f"{path}: missing building.{key}")

    for key in (
        "growth_stage", "required_roads", "min_slope", "max_slope_before",
        "max_slope", "version", "zone_types", "wealth_types",
        "purpose_types", "do_construction",
    ):
        if key not in lot:
            raise ValueError(f"{path}: missing lot.{key}")

    if len(building["pollution_center"]) != 4 or len(building["pollution_radii"]) != 4:
        raise ValueError(f"{path}: pollution_center and pollution_radii must have 4 values")

    building["occupant_groups"] = _preset_int_list(
        building["occupant_groups"], name="building.occupant_groups"
    )
    return data

# ---------------------------------------------------------------------------
# Helpers / exemplar writer
# ---------------------------------------------------------------------------

def _check_u32(name: str, value: int) -> int:
    value = int(value)
    if not 0 <= value <= 0xFFFFFFFF:
        raise ValueError(f"{name} must fit UInt32, got {value!r}")
    return value


def _fixed16_16(meters: float) -> int:
    return int(round(float(meters) * 65536.0)) & 0xFFFFFFFF


def _derived_iid(gid: int, tag: bytes) -> int:
    raw = struct.pack("<I", gid) + b":" + tag
    iid = zlib.crc32(raw) & 0xFFFFFFFF
    iid |= 0x10000000
    if iid == 0xFFFFFFFF:
        iid ^= 0x01010101
    return iid


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
    if vt == VT_STRING:
        if len(vals) != 1:
            raise ValueError("String property needs exactly one string")
        raw = str(vals[0]).encode("cp1252", errors="replace")
        return struct.pack("<IHHB", pid, vt, 0x0080, 0) + struct.pack("<I", len(raw)) + raw
    if len(vals) == 1 and not prop.force_array:
        return struct.pack("<IHHB", pid, vt, 0x0000, 0) + _pack_scalar(vt, vals[0])
    payload = b"".join(_pack_scalar(vt, v) for v in vals)
    return struct.pack("<IHHB", pid, vt, 0x0080, 0) + struct.pack("<I", len(vals)) + payload


def build_exemplar(
    properties: Sequence[ExemplarProperty],
    *,
    parent_tgi: tuple[int, int, int] = (0, 0, 0),
) -> bytes:
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
        pid, vt, key = struct.unpack_from("<IHH", data, off)
        off += 8
        off += 1  # framing flag byte
        if key == 0x0000:
            nvals = 1
        elif key == 0x0080:
            nvals = struct.unpack_from("<I", data, off)[0]
            off += 4
        else:
            raise ValueError(f"Unsupported exemplar KeyType 0x{key:04X}")
        if vt == VT_STRING:
            raw = data[off:off+nvals]
            off += nvals
            values = (raw.decode("cp1252", errors="replace"),)
        else:
            size = sizes[vt]
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
                else:
                    v = bool(data[off])
                off += size
                values.append(v)
            values = tuple(values)
        out.append((pid, vt, key, values))
    if off != len(data):
        raise ValueError(f"Exemplar parser ended at {off}, resource size is {len(data)}")
    return out

# ---------------------------------------------------------------------------
# Menu icon
# ---------------------------------------------------------------------------

def _png_chunk(kind: bytes, payload: bytes) -> bytes:
    return (
        struct.pack(">I", len(payload)) + kind + payload
        + struct.pack(">I", binascii.crc32(kind + payload) & 0xFFFFFFFF)
    )


def make_placeholder_icon_png() -> bytes:
    w, h = 176, 44
    rows = bytearray()
    for y in range(h):
        rows.append(0)
        for x in range(w):
            sx = x % 44
            a = 0
            r = g = b = 0
            if 2 <= sx <= 41 and 2 <= y <= 41:
                r = g = b = 70; a = 190
            if 13 <= sx <= 30 and 10 <= y <= 37:
                r = g = b = 210; a = 255
            if 16 <= sx <= 27 and 6 <= y < 10:
                r = g = b = 235; a = 255
            if (16 <= sx <= 19 or 24 <= sx <= 27) and (14 <= y <= 18 or 23 <= y <= 27):
                r = g = b = 65; a = 255
            rows.extend((r, g, b, a))
    ihdr = struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + _png_chunk(b"IHDR", ihdr) + _png_chunk(b"IDAT", zlib.compress(bytes(rows), 9)) + _png_chunk(b"IEND", b"")


def make_sc4_menu_icon_png(icon_image_path: str | Path) -> bytes:
    try:
        from PIL import Image, ImageEnhance
    except ImportError as exc:
        raise RuntimeError("--icon requires Pillow: pip install pillow") from exc
    path = Path(icon_image_path)
    with Image.open(path) as src:
        im = src.convert("RGBA")
    if im.size == (176, 44):
        out = io.BytesIO(); im.save(out, format="PNG"); return out.getvalue()
    if im.size != (44, 44):
        alpha = im.getchannel("A")
        bbox = alpha.point(lambda a: 255 if a >= 32 else 0).getbbox() or (0, 0, im.width, im.height)
        crop = im.crop(bbox).convert("RGBA")
        base = Image.new("RGBA", (44, 44), (0, 0, 0, 0))
        scale = min(38 / max(crop.width, 1), 38 / max(crop.height, 1))
        nw, nh = max(1, round(crop.width * scale)), max(1, round(crop.height * scale))
        resized = crop.resize((nw, nh), Image.Resampling.LANCZOS)
        base.alpha_composite(resized, ((44 - nw) // 2, 42 - nh))
        normal = base
    else:
        normal = im
    states = [normal, ImageEnhance.Brightness(normal).enhance(1.12), ImageEnhance.Brightness(normal).enhance(0.82), ImageEnhance.Brightness(normal).enhance(0.60)]
    strip = Image.new("RGBA", (176, 44), (0, 0, 0, 0))
    for i, state in enumerate(states):
        strip.alpha_composite(state, (i * 44, 0))
    out = io.BytesIO(); strip.save(out, format="PNG"); return out.getvalue()

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
    if timestamp is None:
        timestamp = int(time.time()) & 0xFFFFFFFF
    header_size = 96
    data_blob = bytearray()
    index_rows = []
    pos = header_size
    seen = set()
    for res in resources:
        tgi = (_check_u32("type", res.type_id), _check_u32("group", res.group_id), _check_u32("instance", res.instance_id))
        if tgi in seen:
            raise ValueError(f"Duplicate resource TGI: {tgi}")
        seen.add(tgi)
        data = bytes(res.data)
        index_rows.append((*tgi, pos, len(data)))
        data_blob.extend(data)
        pos += len(data)
    index_offset = header_size + len(data_blob)
    index_blob = b"".join(struct.pack("<IIIII", *row) for row in index_rows)
    header = bytearray(96)
    header[0:4] = b"DBPF"
    struct.pack_into("<I", header, 0x04, 1)
    struct.pack_into("<I", header, 0x08, 0)
    struct.pack_into("<I", header, 0x18, timestamp)
    struct.pack_into("<I", header, 0x1C, timestamp)
    struct.pack_into("<I", header, 0x20, 7)
    struct.pack_into("<I", header, 0x24, len(resources))
    struct.pack_into("<I", header, 0x28, index_offset)
    struct.pack_into("<I", header, 0x2C, len(index_blob))
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
    preset: str




def make_base_texture_lot_object(
    *,
    tile_x: int,
    tile_z: int,
    texture_iid: int,
) -> tuple[int, ...]:
    """Build a 13-rep LotConfigPropertyLotObject for one base texture tile.

    Rep 1  = 0x00000002 (texture)
    Reps 4-10 use SC4 16.16 fixed-point lot coordinates.
    Rep 12 = 0x8AB0C2EC (base texture marker)
    Rep 13 = texture IID
    """
    if tile_x < 0 or tile_z < 0:
        raise ValueError("tile_x and tile_z must be >= 0")
    texture_iid = _check_u32("texture_iid", texture_iid)

    x0 = tile_x * 16.0
    z0 = tile_z * 16.0
    x1 = x0 + 16.0
    z1 = z0 + 16.0
    cx = x0 + 8.0
    cz = z0 + 8.0

    return (
        0x00000002,  # Texture
        0x00000000,  # LOD
        0x00000000,  # Orientation
        _fixed16_16(cx),
        _fixed16_16(0.0),
        _fixed16_16(cz),
        _fixed16_16(x0),
        _fixed16_16(z0),
        _fixed16_16(x1),
        _fixed16_16(z1),
        0x00000000,
        0x8AB0C2EC,  # Base texture marker
        texture_iid,
    )

def _append_optional_u32(props: list[ExemplarProperty], pid: int, value, name: str) -> None:
    if value is not None:
        props.append(p_u32(pid, _preset_int(value, name=name)))


def generate_ploppable_dat(
    output_path: str | Path,
    *,
    gid: int,
    width: float,
    height: float,
    depth: float,
    name: str = "Generated BAT",
    description: str | None = None,
    preset: str = "landmark",
    preset_dir: str | Path | None = None,
    model_iid: int = 0x00030000,
    package_iid: int | None = None,
    building_iid: int | None = None,
    lot_iid: int | None = None,
    icon_iid: int | None = None,
    building_group: int | None = None,
    plop_cost: int | None = None,
    bulldoze_cost: int | None = None,
    item_order: int = 0,
    orientation: int = 0,
    include_item_name: bool = True,
    include_item_description: bool = True,
    include_item_order: bool = True,
    icon_png_bytes: bytes | None = None,
) -> PloppableResult:
    output_path = Path(output_path)
    cfg = load_preset(preset, preset_dir=preset_dir)
    bcfg = cfg["building"]
    lcfg = cfg["lot"]

    gid = _check_u32("gid", gid)
    model_iid = _check_u32("model_iid", model_iid)
    for dim_name, dim in (("width", width), ("height", height), ("depth", depth)):
        if not math.isfinite(float(dim)) or float(dim) <= 0:
            raise ValueError(f"{dim_name} must be a finite positive number")
    width, height, depth = float(width), float(height), float(depth)
    if orientation not in (0, 1, 2, 3):
        raise ValueError("orientation must be 0, 1, 2, or 3")

    lot_tiles_x = max(1, math.ceil(width / 16.0))
    lot_tiles_z = max(1, math.ceil(depth / 16.0))
    if lot_tiles_x > 255 or lot_tiles_z > 255:
        raise ValueError("Lot dimension exceeds UInt8 limit (255 tiles)")

    package_iid = _check_u32("package_iid", _derived_iid(gid, b"package") if package_iid is None else package_iid)
    building_iid = _check_u32("building_iid", package_iid if building_iid is None else building_iid)
    lot_iid = _check_u32("lot_iid", package_iid if lot_iid is None else lot_iid)
    icon_iid = _check_u32("icon_iid", package_iid if icon_iid is None else icon_iid)
    building_group = _check_u32("building_group", gid if building_group is None else building_group)
    description = name if description is None else description
    plop_cost = int(bcfg["plop_cost"] if plop_cost is None else plop_cost)
    bulldoze_cost = int(bcfg["bulldoze_cost"] if bulldoze_cost is None else bulldoze_cost)

    model_tgi = (TYPE_S3D, gid, model_iid)
    building_tgi = (TYPE_EXEMPLAR, building_group, building_iid)
    lot_tgi = (TYPE_EXEMPLAR, GROUP_LOT, lot_iid)
    icon_tgi = (TYPE_PNG, GROUP_ICON, icon_iid)

    building_props: list[ExemplarProperty] = [
        p_u32(P_EXEMPLAR_TYPE, EXEMPLAR_TYPE_BUILDING),
        p_str(P_EXEMPLAR_NAME, f"{name} Building"),
        p_s64(P_BULLDOZE_COST, bulldoze_cost),
        p_f32(P_OCCUPANT_SIZE, width, height, depth),
        p_u32(P_RESOURCE_KEY_TYPE_1, *model_tgi),
        p_u8(P_WEALTH, int(bcfg["wealth"])),
        p_s32(P_POLLUTION_CENTER, *[int(v) for v in bcfg["pollution_center"]]),
        p_u32(P_POWER_CONSUMED, int(bcfg["power_consumed"])),
        p_u8(P_FLAMMABILITY, int(bcfg["flammability"])),
        p_u8(P_MAX_FIRE_STAGE, int(bcfg["max_fire_stage"])),
        p_s64(P_PLOP_COST, plop_cost),
        p_f32(P_POLLUTION_RADII, *[float(v) for v in bcfg["pollution_radii"]]),
        p_u32(P_PLUGIN_PACK_ID, package_iid),
        p_u32(P_BUILDING_FOUNDATION, _preset_int(bcfg.get("building_foundation", 0), name="building.building_foundation")),
        p_u32(P_ITEM_ICON, icon_iid),
        p_u32(P_OCCUPANT_GROUPS, *bcfg["occupant_groups"]),
        p_u32(P_WATER_CONSUMED, int(bcfg["water_consumed"])),
        p_u32_rep(P_LOT_RESOURCE_KEY, lot_iid),
    ]

    _append_optional_u32(building_props, P_QUERY_EXEMPLAR_GUID, bcfg.get("query_exemplar_guid"), "building.query_exemplar_guid")
    _append_optional_u32(building_props, P_EXEMPLAR_CATEGORY, bcfg.get("exemplar_category"), "building.exemplar_category")
    _append_optional_u32(building_props, P_SFX_QUERY_SOUND, bcfg.get("query_sound"), "building.query_sound")
    _append_optional_u32(building_props, P_SFX_DEFAULT_PLOP, bcfg.get("plop_sound"), "building.plop_sound")

    effect = bcfg.get("landmark_effect")
    if effect is not None:
        building_props.append(p_s32(P_LANDMARK_EFFECT, int(effect["magnitude"]), int(effect["radius"])))
    effect = bcfg.get("park_effect")
    if effect is not None:
        building_props.append(p_s32(P_PARK_EFFECT, int(effect["magnitude"]), int(effect["radius"])))
    effect = bcfg.get("mayor_rating_effect")
    if effect is not None:
        building_props.append(p_s32(P_MAYOR_RATING_EFFECT, int(effect["magnitude"]), int(effect["radius"])))

    budget = bcfg.get("budget")
    if isinstance(budget, dict) and budget.get("enabled", False):
        building_props.extend([
            p_u32_rep(P_BUDGET_DEPARTMENT, _preset_int(budget["department"], name="building.budget.department")),
            p_u32_rep(P_BUDGET_LINE, _preset_int(budget.get("line", 0), name="building.budget.line")),
            p_u32_rep(P_BUDGET_PURPOSE, _preset_int(budget["purpose"], name="building.budget.purpose")),
            ExemplarProperty(P_BUDGET_COST, VT_SINT64, (int(budget["monthly_cost"]),), True),
        ])

    item_props = []
    if include_item_name:
        item_props.append(p_str(P_ITEM_NAME, name))
    if include_item_description:
        item_props.append(p_str(P_ITEM_DESCRIPTION, description))
    if include_item_order:
        item_props.append(p_u32(P_ITEM_ORDER, item_order))
    icon_index = next(i for i, p in enumerate(building_props) if p.property_id == P_ITEM_ICON) + 1
    building_props[icon_index:icon_index] = item_props
    building_exemplar = build_exemplar(building_props)

    # Center model on the automatically sized lot.
    lot_width_m, lot_depth_m = lot_tiles_x * 16.0, lot_tiles_z * 16.0
    cx, cz = lot_width_m / 2.0, lot_depth_m / 2.0
    x1, z1 = max(0.0, cx - width / 2.0), max(0.0, cz - depth / 2.0)
    x2, z2 = min(lot_width_m, cx + width / 2.0), min(lot_depth_m, cz + depth / 2.0)
    building_lot_object = (
        0, 0, orientation,
        _fixed16_16(cx), _fixed16_16(0.0), _fixed16_16(cz),
        _fixed16_16(x1), _fixed16_16(z1), _fixed16_16(x2), _fixed16_16(z2),
        0, 0, building_iid,
    )

    lot_props: list[ExemplarProperty] = [
        p_u32(P_EXEMPLAR_TYPE, EXEMPLAR_TYPE_LOT),
        p_str(P_EXEMPLAR_NAME, f"{name} Lot"),
        p_u8(P_GROWTH_STAGE, int(lcfg["growth_stage"])),
        p_u8(P_LOT_REQUIRED_ROADS, int(lcfg["required_roads"])),
        p_f32(P_LOT_MIN_SLOPE, float(lcfg["min_slope"])),
        p_u8(P_LOT_VERSION, int(lcfg["version"])),
        p_u8(P_LOT_SIZE, lot_tiles_x, lot_tiles_z),
        p_f32(P_LOT_MAX_SLOPE_BEFORE, float(lcfg["max_slope_before"])),
        p_u8(P_LOT_ZONE_TYPES, *[int(v) for v in lcfg["zone_types"]]),
        p_u8(P_LOT_WEALTH_TYPES, *[int(v) for v in lcfg["wealth_types"]]),
        p_u8(P_LOT_PURPOSE_TYPES, *[int(v) for v in lcfg["purpose_types"]]),
        p_u32(P_LOT_OBJECT_BASE, *building_lot_object),
        p_f32(P_LOT_MAX_SLOPE, float(lcfg["max_slope"])),
        p_u8(P_LOT_DO_CONSTRUCTION, int(lcfg["do_construction"])),
    ]

    value = lcfg.get("retaining_wall")
    if value is not None:
        lot_props.append(p_u32(P_LOT_RETAINING_WALL, _preset_int(value, name="lot.retaining_wall")))
    value = lcfg.get("building_foundation")
    if value is not None:
        lot_props.append(p_u32(P_BUILDING_FOUNDATION, _preset_int(value, name="lot.building_foundation")))
    value = lcfg.get("unknown_cbe243f7")
    if value is not None:
        lot_props.append(p_u32(P_LOT_UNKNOWN_CBE243F7, _preset_int(value, name="lot.unknown_cbe243f7")))

    # LotConfigPropertyLotObject entries must use distinct property IDs.
    # 0x88EDC900 is already used by the building, so textures begin at 0x88EDC901.
    base_texture = lcfg.get("base_texture")
    if base_texture is not None:
        texture_iid = _preset_int(base_texture, name="lot.base_texture")
        object_pid = P_LOT_OBJECT_BASE + 1
        for z in range(lot_tiles_z):
            for x in range(lot_tiles_x):
                if object_pid > 0x88EDCDFF:
                    raise ValueError("Too many LotConfigPropertyLotObject entries")
                lot_props.append(
                    p_u32(
                        object_pid,
                        *make_base_texture_lot_object(
                            tile_x=x,
                            tile_z=z,
                            texture_iid=texture_iid,
                        ),
                    )
                )
                object_pid += 1

    lot_exemplar = build_exemplar(lot_props)

    parsed_building = inspect_exemplar(building_exemplar)
    parsed_lot = inspect_exemplar(lot_exemplar)
    if not any(pid == P_RESOURCE_KEY_TYPE_1 and values == model_tgi for pid, _vt, _key, values in parsed_building):
        raise ValueError("Internal validation failed: RKT1 model TGI missing")
    if not any(pid == P_LOT_OBJECT_BASE and values[-1] == building_iid for pid, _vt, _key, values in parsed_lot):
        raise ValueError("Internal validation failed: lot does not reference building IID")

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
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(build_dbpf(resources))
    return PloppableResult(
        output_path, gid, model_tgi, building_tgi, lot_tgi, icon_tgi,
        lot_tiles_x, lot_tiles_z, preset,
    )


def inspect_generated_dat(path: str | Path) -> list[tuple[int, int, int, int, int]]:
    data = Path(path).read_bytes()
    if len(data) < 96 or data[:4] != b"DBPF":
        raise ValueError("Not a DBPF file")
    count = struct.unpack_from("<I", data, 0x24)[0]
    index_offset = struct.unpack_from("<I", data, 0x28)[0]
    result = []
    for n in range(count):
        off = index_offset + n * 20
        row = struct.unpack_from("<IIIII", data, off)
        t, g, i, data_off, size = row
        if data_off + size > len(data):
            raise ValueError("Resource extends past EOF")
        result.append(row)
    return result


def _fmt_tgi(tgi: tuple[int, int, int]) -> str:
    return "/".join(f"0x{x:08X}" for x in tgi)


def main() -> int:
    ap = argparse.ArgumentParser(description=f"SC4 ploppable DAT generator v{VERSION} (JSON presets)")
    ap.add_argument("output", type=Path)
    ap.add_argument("--gid", type=_parse_hex_int, required=True)
    ap.add_argument("--width", type=float, required=True)
    ap.add_argument("--height", type=float, required=True)
    ap.add_argument("--depth", type=float, required=True)
    ap.add_argument("--name", default="Generated BAT")
    ap.add_argument("--description")
    ap.add_argument("--preset", default="landmark")
    ap.add_argument("--preset-dir", type=Path)
    ap.add_argument("--model-iid", type=_parse_hex_int, default=0x00030000)
    ap.add_argument("--package-iid", type=_parse_hex_int)
    ap.add_argument("--building-iid", type=_parse_hex_int)
    ap.add_argument("--lot-iid", type=_parse_hex_int)
    ap.add_argument("--icon-iid", type=_parse_hex_int)
    ap.add_argument("--building-group", type=_parse_hex_int)
    ap.add_argument("--plop-cost", type=int, default=None, help="override preset value")
    ap.add_argument("--bulldoze-cost", type=int, default=None, help="override preset value")
    ap.add_argument("--item-order", type=_parse_hex_int, default=0)
    ap.add_argument("--orientation", type=int, choices=(0, 1, 2, 3), default=0)
    ap.add_argument("--icon", type=Path)
    ap.add_argument("--version", action="version", version=VERSION)
    args = ap.parse_args()

    icon_png_bytes = make_sc4_menu_icon_png(args.icon) if args.icon is not None else None
    result = generate_ploppable_dat(
        args.output,
        gid=args.gid,
        width=args.width,
        height=args.height,
        depth=args.depth,
        name=args.name,
        description=args.description,
        preset=args.preset,
        preset_dir=args.preset_dir,
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
    print(f"make_ploppable_dat v{VERSION}")
    print(f"Preset:       {result.preset}")
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
