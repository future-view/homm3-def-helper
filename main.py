#!/usr/bin/env python3
"""
HoMM3 / VCMI DEF 文件解包与打包工具

功能:
  - 解包 (unpack)    : 将 .def 文件解包为 PNG/BMP 帧 + JSON/HDL 元数据
  - 打包 (repack)    : 从 PNG/BMP 帧 + 元数据重新封装为 .def 文件
  - 信息 (info)      : 查看 .def 文件结构信息
  - 验证 (valid)     : round-trip 自测 (unpack → repack → unpack → 像素对比)
  - 测试 (test)      : 批量 round-trip 验证 (递归扫描/随机抽样/并行执行)
  - 清理 (clean-unpack/clean-def): 清理解包目录或 .def 文件
  - 支持递归扫描 mod 目录, 批量处理所有 .def 文件

解包模式:
  --mode=png   (默认) VCMI 原生 PNG 格式: meta.json + {group}_{frame}.png
  --mode=bmp           H3DefTool 兼容:  .hdl + 8-bit BMP + Shadow/

打包根据输入目录自动检测格式:
  - meta.json (含 basepath + sequences) → PNG 模式
  - *.hdl 文件                          → BMP 模式
  可用 --mode 参数覆盖自动检测。
  --keep-pixels 可选参数可在 PNG 模式下额外保存 .pixels.dat 文件用于调试。

DEF 格式参考 (VCMI CDefFile.cpp):
  Offset  Size   Content
  0       4      类型标识 uint32 LE (0x40~0x49)
  4       8      宽/高 (已废弃, 跳过)
  12      4      totalBlocks (分组数)
  16      768    256 色调色板 (RGB × 256)
  784     ?      分组表:
                  每个分组:
                    4 bytes  blockID (组号)
                    4 bytes  totalEntries (帧数)
                    12 bytes 未知
                    总帧数 × 13 bytes 帧名
                    总帧数 × 4  bytes 帧偏移量
  帧数据 (偏移量指向):
    32 bytes SSpriteDef:
      4 bytes size
      4 bytes format (0~3)
      4 bytes fullWidth
      4 bytes fullHeight
      4 bytes width
      4 bytes height
      4 bytes leftMargin (int32)
      4 bytes topMargin  (int32)
    像素数据 (格式 0~3)
"""

from __future__ import annotations

import argparse
import io
import json
import locale as _locale
import os
import random as _random
import re
import shutil
import struct
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import redirect_stdout
from pathlib import Path
from typing import Any, TypedDict

# ══════════════════════════════════════════════════════════════════
#  类型定义
# ══════════════════════════════════════════════════════════════════


class HdlData(TypedDict, total=False):
    """parse_hdl / write_hdl 的数据结构"""

    def_type: int
    shadow_type: int
    groups: dict[int, list[str]]
    shadows: dict[int, list[str]]
    groups_number: int
    generate_selection: int
    colors_box: list[str]
    shadow_colors_box: list[str]
    color_checks: list[str]
    shadow_color_checks: list[str]


# ══════════════════════════════════════════════════════════════════
#  常量
# ══════════════════════════════════════════════════════════════════

SUFFIX_UNPACKED = ".def_unpacked"
SUFFIX_PIXELS = ".pixels.dat"

DEF_TYPES = {
    0x40: "SPELL",
    0x41: "SPRITE",
    0x42: "CREATURE",
    0x43: "MAP",
    0x44: "MAP_HERO",
    0x45: "TERRAIN",
    0x46: "CURSOR",
    0x47: "INTERFACE",
    0x48: "SPRITE_FRAME",
    0x49: "BATTLE_HERO",
}

# HoMM3 / VCMI 动画分组命名
GROUP_NAMES: dict[int, dict[int, str]] = {
    0x42: {  # CREATURE / 生物
        0: "Moving",
        1: "Mouse Over",
        2: "Standing",
        3: "Getting Hit",
        4: "Defend",
        5: "Death",
        6: "(reserved)",
        7: "Turn Left (near)",
        8: "Turn Left (far)",
        9: "Turn Right (near)",
        10: "Turn Right (far)",
        11: "Attack (up)",
        12: "Attack (straight)",
        13: "Attack (down)",
        14: "Shoot (up)",
        15: "Shoot (straight)",
        16: "Shoot (down)",
        17: "2-Hex Attack (up)",
        18: "2-Hex Attack (straight)",
        19: "2-Hex Attack (down)",
        20: "Start Moving",
        21: "Stop Moving",
    },
    0x49: {  # BATTLE_HERO
        0: "Standing",
        1: "Shuffle / Move",
        2: "Failure",
        3: "Victory / Spell Cast",
        4: "Cast Spell",
    },
    0x43: {  # MAP / 地图物件
        0: "Normal",
    },
    0x47: {  # INTERFACE / 界面
        0: "Normal",
        1: "Pressed",
        2: "Highlighted",
        3: "Disabled",
    },
}


def get_group_name(def_type: int, group_id: int) -> str:
    """根据 DEF 类型和分组 ID 返回语义名称"""
    type_map = GROUP_NAMES.get(def_type, {})
    return type_map.get(group_id, f"group_{group_id}")


# HoMM3 标准调色板 (256 色, RGB × 256, 共 768 字节)
STANDARD_PALETTE = bytes(
    [
        0x00,0xFF,0xFF, 0xFF,0x96,0xFF, 0xFF,0x64,0xFF, 0xFF,0x32,0xFF,
        0xFF,0x00,0xFF, 0xFF,0xFF,0x00, 0xB4,0x00,0xFF, 0x00,0xFF,0x00,
        0x10,0x08,0x19, 0x10,0x08,0x1C, 0x14,0x0C,0x21, 0x18,0x0C,0x25,
        0x18,0x10,0x29, 0x1C,0x14,0x2D, 0x20,0x14,0x31, 0x20,0x18,0x35,
        0x24,0x18,0x3D, 0x28,0x1C,0x3D, 0x28,0x1C,0x41, 0x2C,0x20,0x45,
        0x2C,0x24,0x49, 0x30,0x24,0x4D, 0x34,0x28,0x51, 0x34,0x28,0x59,
        0x38,0x2C,0x5D, 0x3C,0x2C,0x5D, 0x3C,0x30,0x61, 0x40,0x34,0x65,
        0x40,0x34,0x69, 0x44,0x38,0x6D, 0x48,0x38,0x71, 0x48,0x3C,0x75,
        0x4C,0x3C,0x7D, 0x4C,0x40,0x7D, 0x50,0x44,0x81, 0x54,0x44,0x85,
        0x54,0x48,0x89, 0x58,0x48,0x8D, 0x5C,0x4C,0x91, 0x5C,0x50,0x95,
        0x60,0x50,0x9D, 0x60,0x54,0x9D, 0x64,0x54,0xA1, 0x68,0x58,0xA5,
        0x68,0x5C,0xA9, 0x6C,0x5C,0xAD, 0x6C,0x60,0xB1, 0x70,0x60,0xB5,
        0x74,0x64,0xBD, 0x74,0x68,0xBD, 0x78,0x68,0xC1, 0x7C,0x6C,0xC5,
        0x7C,0x6C,0xC9, 0x80,0x70,0xCD, 0x80,0x74,0xD1, 0x84,0x74,0xD5,
        0x88,0x78,0xDD, 0x88,0x78,0xDD, 0x8C,0x7C,0xE1, 0x90,0x7C,0xE5,
        0x90,0x80,0xE9, 0x94,0x84,0xED, 0x94,0x84,0xF1, 0x98,0x88,0xF5,
        0x9C,0x88,0x00, 0x9C,0x8C,0x04, 0xA0,0x8C,0x08, 0xA0,0x90,0x0C,
        0xA4,0x94,0x10, 0xA8,0x94,0x14, 0xA8,0x98,0x18, 0xAC,0x98,0x1C,
        0xB0,0x9C,0x20, 0xB0,0x9C,0x24, 0xB4,0xA0,0x28, 0xB4,0xA4,0x2C,
        0xB8,0xA4,0x30, 0xBC,0xA8,0x34, 0xBC,0xA8,0x38, 0xC0,0xAC,0x3C,
        0xC4,0xAC,0x40, 0xC4,0xB0,0x44, 0xC8,0xB4,0x48, 0xC8,0xB4,0x4C,
        0xCC,0xB8,0x50, 0xD0,0xB8,0x54, 0xD0,0xBC,0x58, 0xD4,0xBC,0x5C,
        0xD8,0xC0,0x60, 0xD8,0xC4,0x64, 0xDC,0xC4,0x68, 0xDC,0xC8,0x6C,
        0xE0,0xC8,0x70, 0xE4,0xCC,0x74, 0xE4,0xD0,0x78, 0xE8,0xD0,0x7C,
        0xEC,0xD4,0x80, 0xEC,0xD4,0x84, 0xF0,0xD8,0x88, 0xF0,0xD8,0x8C,
        0xF4,0xDC,0x90, 0xF8,0xE0,0x94, 0xF8,0xE0,0x98, 0xFC,0xE4,0x9C,
        0x00,0x00,0x9C, 0x04,0x00,0x9C, 0x04,0x04,0x9C, 0x08,0x04,0x9C,
        0x0C,0x08,0x9C, 0x0C,0x08,0x9C, 0x10,0x0C,0x9C, 0x10,0x0C,0x9C,
        0x14,0x10,0x98, 0x18,0x10,0x98, 0x18,0x14,0x98, 0x1C,0x14,0x98,
        0x1C,0x18,0x98, 0x20,0x18,0x94, 0x24,0x1C,0x94, 0x24,0x20,0x94,
        0x28,0x20,0x94, 0x28,0x24,0x90, 0x2C,0x24,0x90, 0x30,0x28,0x90,
        0x30,0x28,0x90, 0x34,0x2C,0x8C, 0x34,0x2C,0x8C, 0x38,0x30,0x8C,
        0x3C,0x30,0x8C, 0x3C,0x34,0x88, 0x40,0x34,0x88, 0x40,0x38,0x88,
        0x44,0x38,0x84, 0x48,0x3C,0x84, 0x48,0x3C,0x84, 0x4C,0x40,0x84,
        0x4C,0x40,0x80, 0x50,0x44,0x80, 0x54,0x44,0x80, 0x54,0x48,0x7C,
        0x58,0x48,0x7C, 0x58,0x4C,0x7C, 0x5C,0x4C,0x78, 0x60,0x50,0x78,
        0x60,0x50,0x78, 0x64,0x54,0x74, 0x64,0x54,0x74, 0x68,0x58,0x74,
        0x6C,0x58,0x70, 0x6C,0x5C,0x70, 0x70,0x5C,0x70, 0x70,0x60,0x6C,
        0x74,0x60,0x6C, 0x78,0x64,0x6C, 0x78,0x64,0x68, 0x7C,0x68,0x68,
        0x7C,0x68,0x68, 0x80,0x6C,0x64, 0x84,0x6C,0x64, 0x84,0x70,0x64,
        0x88,0x70,0x60, 0x88,0x74,0x60, 0x8C,0x74,0x5C, 0x90,0x78,0x5C,
        0x90,0x78,0x5C, 0x94,0x7C,0x5C, 0x94,0x7C,0x58, 0x98,0x80,0x58,
        0x9C,0x80,0x58, 0x9C,0x84,0x54, 0xA0,0x84,0x54, 0xA0,0x88,0x54,
        0xA4,0x88,0x50, 0xA8,0x8C,0x50, 0xA8,0x8C,0x50, 0xAC,0x90,0x4C,
        0xAC,0x90,0x4C, 0xB0,0x94,0x4C, 0xB4,0x94,0x48, 0xB4,0x98,0x48,
        0xB8,0x98,0x48, 0xB8,0x9C,0x44, 0xBC,0x9C,0x44, 0xC0,0xA0,0x44,
        0xC0,0xA0,0x40, 0xC4,0xA4,0x40, 0xC4,0xA4,0x40, 0xC8,0xA8,0x3C,
        0xCC,0xA8,0x3C, 0xCC,0xAC,0x3C, 0xD0,0xAC,0x38, 0xD0,0xB0,0x38,
        0xD4,0xB0,0x38, 0xD8,0xB4,0x34, 0xD8,0xB4,0x34, 0xDC,0xB8,0x34,
        0xDC,0xB8,0x30, 0xE0,0xBC,0x30, 0xE4,0xBC,0x30, 0xE4,0xC0,0x2C,
        0xE8,0xC0,0x2C, 0xE8,0xC4,0x2C, 0xEC,0xC4,0x28, 0xF0,0xC8,0x28,
        0xF0,0xC8,0x28, 0xF4,0xCC,0x24, 0xF4,0xCC,0x24, 0xF8,0xD0,0x24,
        0xFC,0xD0,0x20, 0xFC,0xD4,0x20, 0xFC,0xD4,0x20, 0xFC,0xD8,0x1C,
        0xFC,0xD8,0x1C, 0xFC,0xDC,0x1C, 0xFC,0xDC,0x18, 0xFC,0xE0,0x18,
        0xFC,0xE0,0x18, 0xFC,0xE4,0x14, 0xFC,0xE4,0x14, 0xFC,0xE8,0x14,
        0xFC,0xE8,0x10, 0xFC,0xEC,0x10, 0xFC,0xEC,0x10, 0xFC,0xF0,0x0C,
        0xFC,0xF0,0x0C, 0xFC,0xF4,0x0C, 0xFC,0xF4,0x08, 0xFC,0xF8,0x08,
        0xFC,0xF8,0x08, 0xFC,0xFC,0x04, 0xFC,0xFC,0x04, 0xFC,0xFC,0x04,
        0x00,0x00,0x00, 0x04,0x04,0x04, 0x08,0x08,0x08, 0x0C,0x0C,0x0C,
        0x10,0x10,0x10, 0x14,0x14,0x14, 0x18,0x18,0x18, 0x1C,0x1C,0x1C,
        0x20,0x20,0x20, 0x24,0x24,0x24, 0x28,0x28,0x28, 0x2C,0x2C,0x2C,
        0x30,0x30,0x30, 0x34,0x34,0x34, 0x38,0x38,0x38, 0x3C,0x3C,0x3C,
        0x40,0x40,0x40, 0x44,0x44,0x44, 0x48,0x48,0x48, 0x4C,0x4C,0x4C,
        0x50,0x50,0x50, 0x54,0x54,0x54, 0x58,0x58,0x58, 0x5C,0x5C,0x5C
    ]
)

TARGET_ALPHA = [0, 64, 64, 128, 128, 0, 128, 64]

# 特殊颜色索引 (用于透明/阴影/选择)
PAL_INDEX_TRANSPARENT = 0
PAL_INDEX_SHADOW_EDGE = 1
PAL_INDEX_SHADOW_DEEP = 2
PAL_INDEX_SHADOW_BODY1 = 3
PAL_INDEX_SHADOW_BODY2 = 4
PAL_INDEX_SELECTION = 5
PAL_INDEX_SELECTION_DEEP = 6
PAL_INDEX_SELECTION_EDGE = 7

SSPRITE_DEF_SIZE = 32  # 8 × uint32 LE


# ══════════════════════════════════════════════════════════════════
#  I18N — 国际化 (外部 JSON 翻译文件)
# ══════════════════════════════════════════════════════════════════

_ENV_LANG_KEY = "HOMM3_LANG"

_LOCALE_MAP: dict[str, str] = {
    "zh": "zh_CN",
    "en": "en_US",
    "ja": "ja_JP",
    "ko": "ko_KR",
}


def _detect_locale() -> str:
    """检测系统语言, 返回 locale 代码 (如 zh_CN, en_US)"""
    # 1) 环境变量覆盖
    env_lang = os.environ.get(_ENV_LANG_KEY, "").strip().lower().replace("-", "_")
    if env_lang:
        return env_lang
    # 2) locale 模块
    try:
        loc = _locale.getlocale()
        if loc and loc[0]:
            code = loc[0].split("_")[0]
            return _LOCALE_MAP.get(code, "en_US")
    except (ValueError, AttributeError):
        pass
    # 3) Windows API
    try:
        import ctypes as _ctypes

        windll = _ctypes.windll.kernel32
        w_lang = windll.GetUserDefaultUILanguage()
        lang_id_to_code = {
            0x0804: "zh_CN",
            0x0404: "zh_TW",
            0x0409: "en_US",
            0x0809: "en_GB",
            0x0411: "ja_JP",
            0x0412: "ko_KR",
        }
        code = lang_id_to_code.get(w_lang)
        if code:
            return code
    except Exception:
        pass
    # 4) 兜底
    return "en_US"


class _Translator:
    """翻译器, 从外部 JSON 文件加载消息字典"""

    def __init__(self, lang_dir: str | None = None):
        self._lang_dir = Path(lang_dir or (Path(__file__).parent / "langs"))
        self._locale = _detect_locale()
        self._msgs: dict[str, str] = {}
        self._load()

    def _load(self) -> None:
        """加载当前语言文件"""
        lang_file = self._lang_dir / f"{self._locale}.json"
        fallback = self._lang_dir / "en_US.json"

        for path in [lang_file, fallback]:
            if path.exists():
                try:
                    data: dict[str, dict[str, str]] = json.loads(
                        path.read_text("utf-8")
                    )
                    self._msgs = data.get("messages", {})
                except Exception as e:
                    print(
                        f"[WARN] Failed to load translation file: {path}: {e}",
                        file=sys.stderr,
                    )
                break
        else:
            self._msgs = {}

    def set_locale(self, locale_code: str) -> None:
        """覆盖当前语言设置"""
        self._locale = locale_code
        self._load()

    @property
    def lang(self) -> str:
        return self._locale

    def __call__(self, msg_id: str, **kwargs) -> str:
        template = self._msgs.get(msg_id)
        if template is None:
            # 消息缺失时返回 msg_id 本身
            return msg_id
        return template.format(**kwargs) if kwargs else template


_L = _Translator()


# ══════════════════════════════════════════════════════════════════
#  工具函数
# ══════════════════════════════════════════════════════════════════


def _clean_json(obj: Any) -> Any:
    """清理对象中的空字符, 确保 JSON 序列化"""
    if isinstance(obj, str):
        return obj.replace("\x00", "").strip()
    if isinstance(obj, dict):
        return {k: _clean_json(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_clean_json(v) for v in obj]
    return obj


def palette_to_rgb_list(palette: list[int]) -> list[tuple[int, int, int]]:
    """Convert 768-byte palette to [(R,G,B), ...]"""
    result: list[tuple[int, int, int]] = []
    for i in range(256):
        r = palette[i * 3]
        g = palette[i * 3 + 1]
        b = palette[i * 3 + 2]
        result.append((r, g, b))
    return result


def palette_to_rgba_list(palette: list[int]) -> list[tuple[int, int, int, int]]:
    """将 768 字节调色板转为 [(R,G,B,A), ...]"""
    result: list[tuple[int, int, int, int]] = []
    for i in range(256):
        r = palette[i * 3]
        g = palette[i * 3 + 1]
        b = palette[i * 3 + 2]
        if i == 0:
            result.append((r, g, b, 0))
            continue
        a = 255
        for si in range(1, 8):
            if (r, g, b) == (
                STANDARD_PALETTE[si * 3],
                STANDARD_PALETTE[si * 3 + 1],
                STANDARD_PALETTE[si * 3 + 2],
            ):
                a = TARGET_ALPHA[si]
                break
        result.append((r, g, b, a))
    return result


# ══════════════════════════════════════════════════════════════════
#  像素编码/解码
# ══════════════════════════════════════════════════════════════════


def _decode_format0(data: bytes, width: int, height: int) -> list[list[int]]:
    """格式 0: 未压缩, 逐行复制像素"""
    rows: list[list[int]] = []
    pos = 0
    for _ in range(height):
        row = list(data[pos : pos + width])
        if len(row) < width:
            row.extend([0] * (width - len(row)))
        rows.append(row)
        pos += width
    return rows


def _decode_format1(data: bytes, width: int, height: int) -> list[list[int]]:
    """格式 1: RLE 压缩, 每行有偏移表"""
    import struct as _s

    if len(data) < height * 4:
        return _decode_format0(data, width, height)

    rows: list[list[int]] = []
    base_offset = height * 4
    offsets = list(_s.unpack(f"<{height}I", data[:base_offset]))
    data_len = len(data)

    for row_idx in range(height):
        row: list[int] = []
        pos = offsets[row_idx]
        while len(row) < width and pos < data_len:
            if pos + 2 > data_len:
                break
            seg_type = data[pos]
            length = min(data[pos + 1] + 1, width - len(row))
            pos += 2
            if seg_type == 0xFF:
                available = data_len - pos
                actual_len = min(length, available)
                row.extend(data[pos : pos + actual_len])
                row.extend([0] * (length - actual_len))
                pos += actual_len
            else:
                row.extend([seg_type] * length)
        rows.append(row[:width])
    return rows


def _decode_format2(data: bytes, width: int, height: int) -> list[list[int]]:
    """格式 2: 紧凑段编码"""
    import struct as _s

    rows: list[list[int]] = []
    if len(data) < 2:
        return rows
    base_offset = _s.unpack("<H", data[:2])[0]
    data_len = len(data)

    pos = base_offset
    for _ in range(height):
        row: list[int] = []
        while len(row) < width:
            if pos >= data_len:
                break
            segment = data[pos]
            pos += 1
            code = segment // 32
            length = min((segment & 31) + 1, width - len(row))
            if code == 7:
                available = data_len - pos
                actual = min(length, available)
                row.extend(data[pos : pos + actual])
                row.extend([0] * (length - actual))
                pos += actual
            else:
                row.extend([code] * length)
        rows.append(row[:width])
    return rows


def _decode_format3(data: bytes, width: int, height: int) -> list[list[int]]:
    """格式 3: 类似格式2, 但行偏移计算不同"""
    import struct as _s

    data_len = len(data)
    row_offsets_count = height * 2 * (width // 32)
    if row_offsets_count > data_len:
        return _decode_format0(data, width, height)

    rows: list[list[int]] = []
    for row_idx in range(height):
        row: list[int] = []
        table_pos = row_idx * 2 * (width // 32)
        if table_pos + 2 > data_len:
            break
        row_offset = _s.unpack("<H", data[table_pos : table_pos + 2])[0]
        if row_offset >= data_len:
            rows.append(row[:width])
            continue
        pos = row_offset
        while len(row) < width:
            if pos >= data_len:
                break
            segment = data[pos]
            pos += 1
            code = segment // 32
            length = min((segment & 31) + 1, width - len(row))
            if code == 7:
                available = data_len - pos
                actual = min(length, available)
                row.extend(data[pos : pos + actual])
                row.extend([0] * (length - actual))
                pos += actual
            else:
                row.extend([code] * length)
        rows.append(row[:width])
    return rows


def decode_pixels(data: bytes, fmt: int, width: int, height: int) -> list[list[int]]:
    """解码像素数据, 返回二维 uint8 索引数组 [height][width]"""
    decoders = {
        0: _decode_format0,
        1: _decode_format1,
        2: _decode_format2,
        3: _decode_format3,
    }
    decoder = decoders.get(fmt)
    if decoder is None:
        print(f"    [WARN] {_L('unsupported_format_warn', fmt=fmt)}")
        return _decode_format0(data, width, height)
    try:
        return decoder(data, width, height)
    except Exception as e:
        print(f"    [WARN] {_L('pixel_decode_fail_warn', fmt=fmt, err=e)}")
        return _decode_format0(data, width, height)


def _encode_format0(rows: list[list[int]]) -> bytes:
    """格式 0 编码: 未压缩"""
    result = bytearray()
    for row in rows:
        result.extend(row)
    return bytes(result)


def _encode_vside(rows: list[list[int]]) -> bytes:
    """VSIDE RLE 编码 (DEF 格式 1)"""
    import struct as _s

    def _encode_row(row: list[int]) -> bytes:
        enc = bytearray()
        i = 0
        w = len(row)
        while i < w:
            color = row[i]
            run = 1
            while i + run < w and row[i + run] == color:
                run += 1

            if run >= 3 and color != 0xFF:
                remaining = run
                while remaining > 0:
                    chunk = min(remaining, 256)
                    enc.append(color)
                    enc.append(chunk - 1)
                    remaining -= chunk
                i += run
            else:
                raw = bytearray()
                raw.append(color)
                i += 1
                while i < w:
                    cc = row[i]
                    rr = 1
                    while i + rr < w and row[i + rr] == cc:
                        rr += 1
                    if rr >= 3 and cc != 0xFF:
                        break
                    if len(raw) >= 256:
                        break
                    raw.extend([cc] * rr)
                    i += rr
                if raw:
                    enc.append(0xFF)
                    enc.append(len(raw) - 1)
                    enc.extend(raw)
        return bytes(enc)

    height = len(rows)
    encoded_rows = [_encode_row(row) for row in rows]

    base_offset = height * 4
    result = bytearray()
    offset = base_offset
    for er in encoded_rows:
        result += _s.pack("<I", offset)
        offset += len(er)
    for er in encoded_rows:
        result += er
    return bytes(result)


def encode_frame_pixels(rows: list[list[int]]) -> tuple[int, bytes]:
    """编码像素数据, 返回 (format, bytes)"""
    return 1, _encode_vside(rows)


# ══════════════════════════════════════════════════════════════════
#  颜色量化 (最近色匹配 + Floyd-Steinberg 抖动)
# ══════════════════════════════════════════════════════════════════


def _nearest_color(
    r: int,
    g: int,
    b: int,
    palette_rgb: list[tuple[int, int, int]],
    exclude_indices: set[int] | None = None,
) -> int:
    """在调色板中查找最近的颜色索引"""
    best_idx = 0
    best_dist = float("inf")
    for pi, (pr, pg, pb) in enumerate(palette_rgb):
        if exclude_indices and pi in exclude_indices:
            continue
        dr = r - pr
        dg = g - pg
        db = b - pb
        dist = dr * dr + dg * dg + db * db
        if dist < best_dist:
            best_dist = dist
            best_idx = pi
    return best_idx


def pixels_from_palette_indexed(
    image_data: bytes,
    width: int,
    height: int,
    palette: list[int] | None = None,
    dither: bool = False,
) -> list[list[int]]:
    """从 RGBA 字节数据生成 8-bit 调色板索引

    Args:
        image_data: PNG/图像原始字节
        width: 图像宽度
        height: 图像高度
        palette: 768 字节调色板 (RGB×256); 默认使用 STANDARD_PALETTE
        dither: 是否使用 Floyd-Steinberg 抖动 (大幅改善色带)
    """
    import io

    from PIL import Image

    img = Image.open(io.BytesIO(image_data))
    if img.mode != "RGBA":
        img = img.convert("RGBA")

    if hasattr(img, "get_flattened_data"):
        pixels_list = list(img.get_flattened_data())
    else:
        pixels_list = list(img.getdata())

    pal_bytes = palette if palette is not None else list(STANDARD_PALETTE)
    pal_rgb = palette_to_rgb_list(pal_bytes)

    if dither:
        return _dither_pixels(pixels_list, width, height, pal_rgb)

    rows: list[list[int]] = []
    for y in range(height):
        row: list[int] = []
        for x in range(width):
            idx = y * width + x
            if idx < len(pixels_list):
                r, g, b, a = pixels_list[idx]
                if a < 128:
                    row.append(PAL_INDEX_TRANSPARENT)
                else:
                    row.append(_nearest_color(r, g, b, pal_rgb))
            else:
                row.append(PAL_INDEX_TRANSPARENT)
        rows.append(row)
    return rows


def _dither_pixels(
    pixels_list: list, width: int, height: int, pal_rgb: list[tuple[int, int, int]]
) -> list[list[int]]:
    """Floyd-Steinberg 误差扩散抖动"""
    # 工作缓冲区: float[R, G, B, A]
    working = []
    for y in range(height):
        row = []
        for x in range(width):
            idx = y * width + x
            if idx < len(pixels_list):
                r, g, b, a = pixels_list[idx]
                row.append([float(r), float(g), float(b), float(a)])
            else:
                row.append([0.0, 0.0, 0.0, 0.0])
        working.append(row)

    result_rows: list[list[int]] = []
    for y in range(height):
        row: list[int] = []
        for x in range(width):
            r, g, b, a = working[y][x]

            if a < 128:
                row.append(PAL_INDEX_TRANSPARENT)
                continue

            # 最近色匹配
            best_idx = _nearest_color(int(r), int(g), int(b), pal_rgb)
            qr, qg, qb = pal_rgb[best_idx]

            # 计算误差
            er = r - qr
            eg = g - qg
            eb = b - qb

            # Floyd-Steinberg 误差扩散
            if x + 1 < width:
                working[y][x + 1][0] += er * 7.0 / 16.0
                working[y][x + 1][1] += eg * 7.0 / 16.0
                working[y][x + 1][2] += eb * 7.0 / 16.0
            if y + 1 < height:
                if x > 0:
                    working[y + 1][x - 1][0] += er * 3.0 / 16.0
                    working[y + 1][x - 1][1] += eg * 3.0 / 16.0
                    working[y + 1][x - 1][2] += eb * 3.0 / 16.0
                working[y + 1][x][0] += er * 5.0 / 16.0
                working[y + 1][x][1] += eg * 5.0 / 16.0
                working[y + 1][x][2] += eb * 5.0 / 16.0
                if x + 1 < width:
                    working[y + 1][x + 1][0] += er * 1.0 / 16.0
                    working[y + 1][x + 1][1] += eg * 1.0 / 16.0
                    working[y + 1][x + 1][2] += eb * 1.0 / 16.0

            row.append(best_idx)
        result_rows.append(row)
    return result_rows


# ══════════════════════════════════════════════════════════════════
#  BMP 8-bit 索引格式工具 (Phase 2: H3DefTool 兼容)
# ══════════════════════════════════════════════════════════════════


def png_to_indexed_bmp(
    png_data: bytes, output_path: Path, palette: list[int], dither: bool = False
) -> None:
    """将 PNG 保存为 8-bit 索引 BMP (H3DefTool 兼容)"""
    import io

    from PIL import Image

    img = Image.open(io.BytesIO(png_data))
    if img.mode != "RGBA":
        img = img.convert("RGBA")

    w, h = img.size
    if hasattr(img, "get_flattened_data"):
        pixels_list = list(img.get_flattened_data())
    else:
        pixels_list = list(img.getdata())
    pal_rgb = palette_to_rgb_list(palette)

    if dither:
        indexed = _dither_pixels(pixels_list, w, h, pal_rgb)
    else:
        indexed = pixels_from_palette_indexed(png_data, w, h, palette)

    pal_img = Image.new("P", (w, h))
    pal_img.putpalette(bytes(palette))
    flat = []
    for row in indexed:
        flat.extend(row)
    pal_img.putdata(flat)
    pal_img.save(str(output_path), format="BMP")


def load_indexed_bmp(bmp_path: Path) -> tuple[list[list[int]], list[int], int, int]:
    """加载 8-bit 索引 BMP, 返回 (像素行, 调色板, 宽, 高)"""
    from PIL import Image

    img = Image.open(str(bmp_path))
    if img.mode != "P":
        img = img.convert("P")

    palette_raw = img.getpalette()
    if palette_raw is None:
        palette = list(STANDARD_PALETTE)
    else:
        palette = palette_raw[:768]
        while len(palette) < 768:
            palette.append(0)

    w, h = img.size
    if hasattr(img, "get_flattened_data"):
        pixels = list(img.get_flattened_data())
    else:
        pixels = list(img.getdata())
    rows: list[list[int]] = []
    for y in range(h):
        rows.append(list(pixels[y * w : (y + 1) * w]))
    return rows, palette, w, h


# ══════════════════════════════════════════════════════════════════
#  HDL 文件格式解析/写入 (Phase 2: H3DefTool 兼容)
# ══════════════════════════════════════════════════════════════════


def parse_hdl(hdl_path: Path) -> HdlData:
    """解析 .hdl 文件

    返回:
        {
            'def_type': int,        # Type 字段
            'shadow_type': int,     # Shadow Type 字段
            'groups': {             # 分组帧列表
                0: ['frame1.bmp', 'frame2.bmp', ...],
                ...
            },
            'shadows': {            # 阴影帧列表
                0: ['Shadow\\frame1.bmp', ...],
                ...
            },
            'groups_number': int,   # Groups Number
            'generate_selection': int,  # Generate Selection
            'colors_box': [...],    # ColorsBox.Colors
            'shadow_colors_box': [...],  # ShadowColorsBox.Colors
            'color_checks': [...],  # ColorsBox.ColorChecks
            'shadow_color_checks': [...],  # ShadowColorsBox.ColorChecks
        }
    """
    text = hdl_path.read_text("utf-8", errors="replace")
    result: HdlData = {
        "def_type": 0x42,
        "shadow_type": 2,
        "groups": {},
        "shadows": {},
        "groups_number": 0,
        "generate_selection": 0,
        "colors_box": [],
        "shadow_colors_box": [],
        "color_checks": [],
        "shadow_color_checks": [],
    }

    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue

        # Group0=... | Shadow0=...
        m = re.match(r"^(Shadow)?Group(\d+)=(.+)$", line)
        if m:
            is_shadow = m.group(1) == "Shadow"
            gid = int(m.group(2))
            value = m.group(3)
            frames = [f.strip() for f in value.split("|") if f.strip()]
            if is_shadow:
                result["shadows"][gid] = frames
            else:
                result["groups"][gid] = frames
            continue

        # Type=...
        m = re.match(r"^Type=(\d+)$", line, re.IGNORECASE)
        if m:
            result["def_type"] = int(m.group(1))
            continue

        # Shadow Type=...
        m = re.match(r"^Shadow\s+Type=(\d+)$", line, re.IGNORECASE)
        if m:
            result["shadow_type"] = int(m.group(1))
            continue

        # Groups Number=...
        m = re.match(r"^Groups\s+Number=(\d+)$", line, re.IGNORECASE)
        if m:
            result["groups_number"] = int(m.group(1))
            continue

        # Generate Selection=...
        m = re.match(r"^Generate\s+Selection=(\d+)$", line, re.IGNORECASE)
        if m:
            result["generate_selection"] = int(m.group(1))
            continue

        # ColorsBox.Colors=...
        m = re.match(r"^ColorsBox\.Colors=(.+)$", line)
        if m:
            colors = m.group(1).split("|")
            result["colors_box"] = [c.strip() for c in colors if c.strip()]
            continue

        # ShadowColorsBox.Colors=...
        m = re.match(r"^ShadowColorsBox\.Colors=(.+)$", line)
        if m:
            colors = m.group(1).split("|")
            result["shadow_colors_box"] = [c.strip() for c in colors if c.strip()]
            continue

        # ColorsBox.ColorChecks=...
        m = re.match(r"^ColorsBox\.ColorChecks=(.+)$", line)
        if m:
            checks = m.group(1).split("|")
            result["color_checks"] = [c.strip() for c in checks if c.strip()]
            continue

        # ShadowColorsBox.ColorChecks=...
        m = re.match(r"^ShadowColorsBox\.ColorChecks=(.+)$", line)
        if m:
            checks = m.group(1).split("|")
            result["shadow_color_checks"] = [c.strip() for c in checks if c.strip()]
            continue

    return result


def write_hdl(hdl_path: Path, data: HdlData) -> None:
    """写入 .hdl 文件

    Args:
        hdl_path: 输出路径
        data: 与 parse_hdl 返回格式一致的数据
    """
    lines: list[str] = ["[Data]"]
    lines.append(f"Type={data.get('def_type', 0x42)}")
    lines.append(f"Shadow Type={data.get('shadow_type', 2)}")

    groups: dict[int, list[str]] = data.get("groups", {})
    for gid in sorted(groups.keys()):
        frames = groups[gid]
        line = f"Group{gid}=" + "|".join(frames) + "|"
        lines.append(line)

    shadows: dict[int, list[str]] = data.get("shadows", {})
    for gid in sorted(shadows.keys()):
        frames = shadows[gid]
        line = f"Shadow{gid}=" + "|".join(frames) + "|"
        lines.append(line)

    lines.append(f"Groups Number={data.get('groups_number', len(groups))}")
    lines.append(f"Generate Selection={data.get('generate_selection', 0)}")

    colors_box: list[str] = data.get("colors_box", [])
    if colors_box:
        lines.append("ColorsBox.Colors=" + "|".join(colors_box) + "|")
    else:
        lines.append(
            "ColorsBox.Colors=$0000FF|$FF96FF|$010303|$06090A|$FF00FF|$00FFFF|$00B4FF|$00FF00|"
        )

    shadow_colors: list[str] = data.get("shadow_colors_box", [])
    if shadow_colors:
        lines.append("ShadowColorsBox.Colors=" + "|".join(shadow_colors) + "|")
    else:
        lines.append(
            "ShadowColorsBox.Colors=$0000FF|$FF96FF|$010303|$06090A|$FF00FF|$00FFFF|$00B4FF|$00FF00|"
        )

    color_checks: list[str] = data.get("color_checks", [])
    if color_checks:
        lines.append("ColorsBox.ColorChecks=" + "|".join(color_checks) + "|")
    else:
        lines.append("ColorsBox.ColorChecks=1|0|0|0|0|0|0|0|")

    shadow_checks: list[str] = data.get("shadow_color_checks", [])
    if shadow_checks:
        lines.append("ShadowColorsBox.ColorChecks=" + "|".join(shadow_checks) + "|")
    else:
        lines.append("ShadowColorsBox.ColorChecks=1|1|1|1|1|1|1|1|")

    hdl_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


# ══════════════════════════════════════════════════════════════════
#  DEF 格式解析
# ══════════════════════════════════════════════════════════════════


class DefReader:
    """读取和解析 VCMI DEF 文件"""

    def __init__(self, path: Path):
        self.path = path
        self.name = path.stem
        self.data = path.read_bytes()

        # 解析文件头
        self.def_type = struct.unpack("<I", self.data[0:4])[0]
        self.total_blocks = struct.unpack("<I", self.data[12:16])[0]

        # 读取调色板
        self.palette = list(self.data[16 : 16 + 768])

        # 读取分组表
        pos = 784
        self.groups: dict[int, dict[str, Any]] = {}
        file_size = len(self.data)
        max_blocks = 256
        block_header_size: int | None = None

        for _ in range(min(self.total_blocks, max_blocks)):
            if pos + 8 > file_size:
                break

            block_id = struct.unpack("<I", self.data[pos : pos + 4])[0]
            total_entries = struct.unpack("<I", self.data[pos + 4 : pos + 8])[0]

            # 自动检测表头大小 (优先 16 字节: HoMM3 标准)
            if block_header_size is None:
                best_hdr = 16
                best_valid = -1
                for test_hdr in (16, 20):
                    ns = pos + test_hdr
                    os_ = ns + total_entries * 13
                    if os_ + total_entries * 4 > file_size:
                        continue
                    if ns + 12 > file_size:
                        continue
                    valid_cnt = 0
                    for j in range(total_entries):
                        o = struct.unpack(
                            "<I", self.data[os_ + j * 4 : os_ + j * 4 + 4]
                        )[0]
                        if 32 <= o < file_size:
                            valid_cnt += 1
                    name_raw = self.data[ns : ns + 13]
                    ascii_score = sum(1 for b in name_raw if 32 <= b < 127)
                    total_score = valid_cnt + ascii_score / 100.0
                    if total_score > best_valid:
                        best_valid = total_score
                        best_hdr = test_hdr
                block_header_size = best_hdr

            pos += block_header_size

            if total_entries > 10000 or total_entries == 0:
                continue

            names_size = total_entries * 13
            offsets_size = total_entries * 4
            if pos + names_size + offsets_size > file_size:
                break

            names: list[str] = []
            for _ in range(total_entries):
                name_raw = self.data[pos : pos + 13]
                name = name_raw.rstrip(b"\x00").decode("ascii", errors="replace")
                names.append(name)
                pos += 13

            offsets: list[int] = []
            for _ in range(total_entries):
                off = struct.unpack("<I", self.data[pos : pos + 4])[0]
                offsets.append(off)
                pos += 4

            valid_entries: list[dict[str, Any]] = []
            for j in range(total_entries):
                off = offsets[j]
                name = names[j]
                if off < 32 or off + 32 > file_size:
                    continue
                valid_entries.append({"name": name, "offset": off})

            if valid_entries:
                self.groups[block_id] = {"entries": valid_entries}

    @staticmethod
    def _validate_frame(
        fmt: int,
        w: int,
        h: int,
        fw: int,
        fh: int,
        frame_size: int,
        offset: int,
        file_size: int,
    ) -> bool:
        if fmt > 3:
            return False
        if w > 4096 or h > 4096 or fw > 4096 or fh > 4096:
            return False
        if frame_size < 32 and frame_size != 0:
            return False
        if offset + frame_size > file_size:
            return False
        return True

    def read_frame_data(self, offset: int) -> dict[str, Any]:
        """读取指定偏移处的帧数据"""
        data = self.data
        file_size = len(data)

        if offset + SSPRITE_DEF_SIZE > file_size:
            return self._empty_frame()

        (frame_size, fmt, fw, fh, w, h, lm, tm) = struct.unpack(
            "<IIIIIIii", data[offset : offset + SSPRITE_DEF_SIZE]
        )

        if not self._validate_frame(fmt, w, h, fw, fh, frame_size, offset, file_size):
            return self._empty_frame()

        if fmt == 1 and w > fw and h > fh:
            lm = 0
            tm = 0
            w = fw
            h = fh
            pixel_offset = offset + SSPRITE_DEF_SIZE - 16
        else:
            pixel_offset = offset + SSPRITE_DEF_SIZE

        if frame_size == 0:
            pixel_data = b""
        else:
            pixel_end = offset + frame_size
            pixel_data = data[pixel_offset:pixel_end]

        return {
            "valid": True,
            "size": frame_size,
            "format": fmt,
            "fullWidth": fw,
            "fullHeight": fh,
            "width": w,
            "height": h,
            "leftMargin": lm,
            "topMargin": tm,
            "pixel_data": pixel_data,
        }

    @staticmethod
    def _empty_frame() -> dict[str, Any]:
        return {
            "valid": False,
            "size": 0,
            "format": 0,
            "fullWidth": 0,
            "fullHeight": 0,
            "width": 0,
            "height": 0,
            "leftMargin": 0,
            "topMargin": 0,
            "pixel_data": b"",
        }

    def decode_frame_pixels(self, frame: dict[str, Any]) -> list[list[int]]:
        """解码帧的像素数据"""
        return decode_pixels(
            frame["pixel_data"],
            frame["format"],
            frame["width"],
            frame["height"],
        )

    def frame_to_png(self, frame: dict[str, Any]) -> bytes | None:
        """将帧数据渲染为 PNG 字节"""
        import io

        from PIL import Image

        if not frame.get("valid", True):
            return None

        rows = self.decode_frame_pixels(frame)
        w = frame["width"]
        h = frame["height"]

        if w == 0 or h == 0:
            return None

        fw = frame["fullWidth"]
        fh = frame["fullHeight"]
        lm = frame["leftMargin"]
        tm = frame["topMargin"]

        canvas_w = max(fw, 1)
        canvas_h = max(fh, 1)

        palette_rgba = palette_to_rgba_list(self.palette)

        bg = palette_rgba[0]
        img = Image.new("RGBA", (canvas_w, canvas_h), bg)
        px = img.load()

        for y in range(min(h, canvas_h - tm)):
            for x in range(min(w, canvas_w - lm)):
                sy = y + tm
                sx = x + lm
                if (
                    sy < canvas_h
                    and sx < canvas_w
                    and y < len(rows)
                    and x < len(rows[y])
                ):
                    idx = rows[y][x]
                    if idx < len(palette_rgba):
                        px[sx, sy] = palette_rgba[idx]

        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()

    def get_summary(self) -> str:
        """返回 DEF 文件摘要信息"""
        lines = []
        type_name = DEF_TYPES.get(self.def_type, f"UNKNOWN(0x{self.def_type:02X})")
        lines.append(f"DEF 类型:    {type_name} (0x{self.def_type:02X})")
        lines.append(f"文件大小:    {len(self.data)} 字节")
        lines.append(f"分组数:      {self.total_blocks}")
        lines.append("调色板:      256 色")

        total_frames = 0
        for gid, ginfo in sorted(self.groups.items()):
            nf = len(ginfo["entries"])
            total_frames += nf
            gname = get_group_name(self.def_type, gid)
            lines.append(f"  组 {gid:>3}: {gname:<20s} {nf:>3} 帧")

            if nf > 0:
                first_off = ginfo["entries"][0]["offset"]
                fd = self.read_frame_data(first_off)
                lines.append(
                    f"         格式={fd['format']}, "
                    f"尺寸={fd['width']}x{fd['height']}, "
                    f"全尺寸={fd['fullWidth']}x{fd['fullHeight']}, "
                    f"边距=({fd['leftMargin']},{fd['topMargin']})"
                )

        lines.append(f"总帧数:      {total_frames}")
        return "\n".join(lines)


# ══════════════════════════════════════════════════════════════════
#  DEF 格式写入
# ══════════════════════════════════════════════════════════════════


class DefWriter:
    """创建 VCMI DEF 文件"""

    def __init__(self, def_type: int = 0x47, palette: list[int] | None = None):
        self.def_type = def_type
        if palette:
            self.palette = list(palette)[:768]
        else:
            self.palette = list(STANDARD_PALETTE)[:768]
        while len(self.palette) < 768:
            self.palette.append(0)
        self.blocks: list[dict] = []

    def add_block(self, block_id: int):
        """添加一个空分组"""
        self.blocks.append({"blockID": block_id, "entries": []})

    def add_frame_from_png(
        self,
        block_index: int,
        name: str,
        png_data: bytes,
        full_w: int,
        full_h: int,
        width: int,
        height: int,
        left_margin: int,
        top_margin: int,
        dither: bool = False,
    ):
        """从 PNG 数据添加帧"""
        pixel_rows = pixels_from_palette_indexed(
            png_data, width, height, self.palette, dither=dither
        )
        self.blocks[block_index]["entries"].append(
            {
                "name": name,
                "pixel_rows": pixel_rows,
                "fullWidth": full_w,
                "fullHeight": full_h,
                "width": width,
                "height": height,
                "leftMargin": left_margin,
                "topMargin": top_margin,
            }
        )

    def add_frame_from_rows(
        self,
        block_index: int,
        name: str,
        pixel_rows: list[list[int]],
        full_w: int,
        full_h: int,
        width: int,
        height: int,
        left_margin: int,
        top_margin: int,
    ):
        """从已处理的像素行添加帧"""
        self.blocks[block_index]["entries"].append(
            {
                "name": name,
                "pixel_rows": pixel_rows,
                "fullWidth": full_w,
                "fullHeight": full_h,
                "width": width,
                "height": height,
                "leftMargin": left_margin,
                "topMargin": top_margin,
            }
        )

    def to_bytes(self) -> bytes:
        """生成完整的 DEF 文件字节流"""
        import struct as _s

        total_blocks = len(self.blocks)
        file_header_size = 16 + 768

        # 第一遍: 计算分组表大小和各帧编码数据
        block_sizes: list[int] = []
        all_entry_meta: list[list[dict[str, Any]]] = []

        for bi, block in enumerate(self.blocks):
            total_entries = len(block["entries"])
            # 16 字节块头: blockID(4) + totalEntries(4) + padding(8)
            # 与 HoMM3 原始工具 (DefPreview / H3DefTool) 兼容
            block_sizes.append(16 + total_entries * 13 + total_entries * 4)
            entry_meta: list[dict[str, Any]] = []
            for entry in block["entries"]:
                if "pixel_rows" in entry:
                    rows = entry["pixel_rows"]
                else:
                    import io

                    from PIL import Image

                    img = Image.open(io.BytesIO(entry["png_data"]))
                    w, h = img.size
                    rows = pixels_from_palette_indexed(
                        entry["png_data"], w, h, self.palette, dither=False
                    )
                    entry["width"] = w
                    entry["height"] = h
                fmt, encoded = encode_frame_pixels(rows)
                entry_meta.append(
                    {
                        "name": entry["name"],
                        "fullWidth": entry["fullWidth"],
                        "fullHeight": entry["fullHeight"],
                        "width": entry["width"],
                        "height": entry["height"],
                        "leftMargin": entry["leftMargin"],
                        "topMargin": entry["topMargin"],
                        "encoded": encoded,
                        "format": fmt,
                    }
                )
            all_entry_meta.append(entry_meta)

        total_block_table = sum(block_sizes)
        frame_data_base = file_header_size + total_block_table

        # 第二遍: 文件头 + 分组表
        # 计算画布宽高: 取所有帧最大的 fullWidth/fullHeight
        canvas_w = max(
            (m["fullWidth"] for metas in all_entry_meta for m in metas), default=0
        )
        canvas_h = max(
            (m["fullHeight"] for metas in all_entry_meta for m in metas), default=0
        )

        header = bytearray()
        header += _s.pack("<I", self.def_type)
        header += _s.pack("<II", canvas_w, canvas_h)
        header += _s.pack("<I", total_blocks)
        header += bytes(self.palette)

        block_table = bytearray()
        cumulative_frame_offset = frame_data_base

        for bi, block in enumerate(self.blocks):
            block_id = int(block["blockID"])
            total_entries = len(block["entries"])

            block_table += _s.pack("<II", block_id, total_entries)
            # 8 字节 padding = 16 字节块头 (与 DefPreview/H3DefTool 兼容)
            block_table += b"\x00" * 8

            for meta in all_entry_meta[bi]:
                name_bytes = meta["name"].encode("ascii", errors="replace")[:13]
                name_bytes = name_bytes.ljust(13, b"\x00")
                block_table += name_bytes

            for meta in all_entry_meta[bi]:
                block_table += _s.pack("<I", cumulative_frame_offset)
                cumulative_frame_offset += SSPRITE_DEF_SIZE + len(meta["encoded"])

        # 第三遍: 帧数据
        all_frame_data = bytearray()
        for bi in range(len(self.blocks)):
            for meta in all_entry_meta[bi]:
                frame_size = SSPRITE_DEF_SIZE + len(meta["encoded"])
                sd = _s.pack(
                    "<IIIIIIii",
                    frame_size,
                    meta["format"],
                    meta["fullWidth"],
                    meta["fullHeight"],
                    meta["width"],
                    meta["height"],
                    meta["leftMargin"],
                    meta["topMargin"],
                )
                all_frame_data += sd
                all_frame_data += meta["encoded"]

        return bytes(header + block_table + all_frame_data)


def _compute_basepath(def_path: Path) -> str:
    """根据 DEF 文件路径计算 VCMI basepath

    规则: 找到路径中的 Sprites 目录, 取其后的相对路径 (不含 .def 后缀)。
    例如:
      Mods/.../Sprites/abyss/battle/commander/abyssHyppoCommander.def
      → abyss/battle/commander/abyssHyppoCommander

    如果找不到 Sprites 目录, 回退到 def_path.stem。
    """
    try:
        # 在父目录链中查找名为 "Sprites" 的目录
        for parent in def_path.parents:
            if parent.name == "Sprites":
                # 计算 Sprites 之后的相对路径
                rel = def_path.parent.relative_to(parent)
                # 拼接文件名 (不含 .def)
                return str(rel / def_path.stem).replace("\\", "/")
    except (ValueError, AttributeError):
        pass
    return def_path.stem


def unpack_def(
    def_path: Path,
    output_dir: Path | None = None,
    force: bool = False,
    mode: str = "png",
    keep_pixels: bool = False,
) -> Path:
    """解包一个 DEF 文件到目录

    Args:
        def_path: .def 文件路径
        output_dir: 输出目录 (默认同目录下 def 文件名作为目录名)
        force: 覆盖已存在的目录
        mode: 解包模式: 'png' (VCMI), 'bmp' (H3DefTool)
        keep_pixels: PNG 模式下额外保存 .pixels.dat 文件

    Returns:
        输出目录路径
    """
    if output_dir is None:
        # 所有模式统一使用 def 文件名 (不含 .def 后缀) 作为目录名
        output_dir = def_path.with_name(def_path.stem)

    if output_dir.exists():
        if not force:
            print(f"  [SKIP] {_L('skip_exists', dir=str(output_dir))}")
            return output_dir
        shutil.rmtree(output_dir)

    output_dir.mkdir(parents=True, exist_ok=True)

    sys.stdout.write(
        _L("reading_file", name=def_path.name, size=def_path.stat().st_size)
    )
    sys.stdout.flush()

    try:
        reader = DefReader(def_path)
    except Exception as e:
        sys.stdout.write(_L("fail_error", err=e))
        raise

    sys.stdout.write("[OK]\n")
    sys.stdout.write(
        _L(
            "type_label",
            type=DEF_TYPES.get(reader.def_type, f"0x{reader.def_type:02X}"),
        )
    )
    sys.stdout.write(_L("group_label", n=reader.total_blocks))
    sys.stdout.flush()

    if mode == "png":
        return _unpack_to_vcmi_png(
            reader, def_path, output_dir, keep_pixels=keep_pixels
        )
    else:  # bmp
        return _unpack_to_bmp(reader, def_path, output_dir)


def _unpack_to_vcmi_png(
    reader: DefReader, def_path: Path, output_dir: Path, keep_pixels: bool = False
) -> Path:
    """解包为 VCMI 原生 PNG 格式: meta.json + {group}_{frame}.png"""

    total_frames = sum(len(g["entries"]) for g in reader.groups.values())
    sys.stdout.write(_L("total_frames", n=total_frames))
    sys.stdout.flush()

    sequences: list[dict[str, Any]] = []
    frame_count = 0
    total_groups = len(reader.groups)

    for gi, (gid, ginfo) in enumerate(sorted(reader.groups.items())):
        entries = ginfo["entries"]
        sys.stdout.write(
            _L(
                "group_progress",
                i=gi + 1,
                total=total_groups,
                gid=gid,
                nframes=len(entries),
            )
        )
        sys.stdout.flush()

        total_in_group = len(entries)
        frame_names: list[str] = []
        valid_in_group = 0  # 仅有效帧递增的计数器, 避免跳号

        for fi, entry in enumerate(entries):
            if fi % 10 == 0 or fi == total_in_group - 1:
                sys.stdout.write(
                    _L("frame_progress", gid=gid, i=fi + 1, n=total_in_group)
                )
                sys.stdout.flush()

            fd = reader.read_frame_data(entry["offset"])
            if not fd.get("valid", True):
                continue

            png_bytes = reader.frame_to_png(fd)
            if png_bytes is None:
                continue

            # 文件命名: {group_id}_{frame_index}.png (从 1 开始, 3位数字)
            valid_in_group += 1
            frame_name = f"{gid}_{valid_in_group:03d}.png"
            (output_dir / frame_name).write_bytes(png_bytes)
            frame_names.append(frame_name)
            frame_count += 1

            # --keep-pixels: 额外保存 .pixels.dat (调色板索引原始数据, 用于 round-trip 调试)
            if keep_pixels:
                rows = reader.decode_frame_pixels(fd)
                w = fd["width"]
                pixels_dat = b""
                for row in rows:
                    pixels_dat += bytes(row)
                    if len(row) < w:
                        pixels_dat += bytes([0] * (w - len(row)))
                pixels_name = frame_name.replace(".png", SUFFIX_PIXELS)
                (output_dir / pixels_name).write_bytes(pixels_dat)

            if fi == 0 or fi == total_in_group - 1:
                sys.stdout.write(
                    _L(
                        "frame_detail",
                        i=fi,
                        name=frame_name,
                        w=fd["width"],
                        h=fd["height"],
                    )
                )
                sys.stdout.flush()

        sequences.append(
            {
                "group": gid,
                "group_name": get_group_name(reader.def_type, gid),
                "frames": frame_names,
            }
        )

    # 写入 meta.json
    basepath = _compute_basepath(def_path)
    meta = {
        "basepath": basepath,
        "sequences": sequences,
        "defInfo": {
            "def_name": def_path.name,
            "def_type": reader.def_type,
            "def_type_name": DEF_TYPES.get(reader.def_type, f"0x{reader.def_type:02X}"),
            "total_blocks": reader.total_blocks,
            "palette": bytes(reader.palette).hex(),
        },
    }
    meta_clean = _clean_json(meta)
    (output_dir / "meta.json").write_text(
        json.dumps(meta_clean, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    sys.stdout.write(_L("unpack_png_ok", n=frame_count, dir=str(output_dir)))
    sys.stdout.flush()

    # 列出生成的文件
    png_files = sorted(output_dir.glob("*.png"))
    if png_files:
        total_size = sum(f.stat().st_size for f in png_files)
        sys.stdout.write(
            _L("png_file_count", n=len(png_files), size=total_size // 1024)
        )
        for p in png_files[:5]:
            sys.stdout.write(
                f"         -> {p.relative_to(output_dir)} ({p.stat().st_size // 1024} KB)\n"
            )
        if len(png_files) > 5:
            sys.stdout.write(_L("more_files", n=len(png_files) - 5))
    return output_dir


def _unpack_to_bmp(reader: DefReader, def_path: Path, output_dir: Path) -> Path:
    """解包为 H3DefTool 兼容 BMP 格式: .hdl + 8-bit BMP + Shadow/"""

    total_frames = sum(len(g["entries"]) for g in reader.groups.values())
    sys.stdout.write(_L("total_frames", n=total_frames))
    sys.stdout.flush()

    shadow_dir = output_dir / "Shadow"
    shadow_dir.mkdir(parents=True, exist_ok=True)

    hdl_groups: dict[int, list[str]] = {}
    hdl_shadows: dict[int, list[str]] = {}
    frame_count = 0
    total_groups = len(reader.groups)

    for gi, (gid, ginfo) in enumerate(sorted(reader.groups.items())):
        entries = ginfo["entries"]
        sys.stdout.write(
            _L(
                "group_progress",
                i=gi + 1,
                total=total_groups,
                gid=gid,
                nframes=len(entries),
            )
        )
        sys.stdout.flush()

        total_in_group = len(entries)
        frame_names: list[str] = []
        shadow_names: list[str] = []

        for fi, entry in enumerate(entries):
            if fi % 10 == 0 or fi == total_in_group - 1:
                sys.stdout.write(
                    _L("frame_progress", gid=gid, i=fi + 1, n=total_in_group)
                )
                sys.stdout.flush()

            fd = reader.read_frame_data(entry["offset"])
            if not fd.get("valid", True):
                continue

            png_bytes = reader.frame_to_png(fd)
            if png_bytes is None:
                continue

            # 8-bit 索引 BMP (主精灵)
            bmp_name = (
                f"{entry['name']}.bmp" if entry["name"] else f"{gid:03d}_{fi:03d}.bmp"
            )
            bmp_path = output_dir / bmp_name
            png_to_indexed_bmp(png_bytes, bmp_path, reader.palette)
            frame_names.append(bmp_name)
            frame_count += 1

            # 阴影层 (使用特殊颜色 alpha 通道生成 8-bit BMP)
            shadow_bmp_path = shadow_dir / bmp_name
            _generate_shadow_bmp(
                rows=reader.decode_frame_pixels(fd),
                palette=reader.palette,
                frame=fd,
                output_path=shadow_bmp_path,
            )
            shadow_names.append(f"Shadow\\{bmp_name}")

            if fi == 0 or fi == total_in_group - 1:
                sys.stdout.write(
                    _L(
                        "frame_detail_bmp",
                        i=fi,
                        name=bmp_name,
                        w=fd["width"],
                        h=fd["height"],
                    )
                )
                sys.stdout.flush()

        hdl_groups[gid] = frame_names
        hdl_shadows[gid] = shadow_names

    # 写入 .hdl 文件
    hdl_data: HdlData = {
        "def_type": _map_def_type_to_hdl_type(reader.def_type),
        "shadow_type": 2,
        "groups": hdl_groups,
        "shadows": hdl_shadows,
        "groups_number": len(hdl_groups),
        "generate_selection": 0,
    }
    write_hdl(output_dir / f"{reader.name}.hdl", hdl_data)

    sys.stdout.write(_L("unpack_bmp_ok", n=frame_count, dir=str(output_dir)))
    sys.stdout.flush()

    bmp_files = sorted(output_dir.glob("*.bmp"))
    if bmp_files:
        total_size = sum(f.stat().st_size for f in bmp_files)
        sys.stdout.write(
            _L("bmp_file_count", n=len(bmp_files), size=total_size // 1024)
        )
    return output_dir


def _generate_shadow_bmp(
    rows: list[list[int]], palette: list[int], frame: dict[str, Any], output_path: Path
) -> None:
    """从 DEF 帧生成阴影层的 8-bit BMP"""
    from PIL import Image

    w = frame["width"]
    h = frame["height"]
    fw = frame["fullWidth"]
    fh = frame["fullHeight"]
    lm = frame["leftMargin"]
    tm = frame["topMargin"]

    canvas_w = max(fw, 1)
    canvas_h = max(fh, 1)

    # 阴影调色板: 复用标准调色板
    shadow_pal = bytes(STANDARD_PALETTE)
    shadow_img = Image.new("P", (canvas_w, canvas_h))
    shadow_img.putpalette(shadow_pal)

    # 生成阴影像素
    pixels_data = [PAL_INDEX_TRANSPARENT] * (canvas_w * canvas_h)
    for y in range(min(h, canvas_h - tm)):
        for x in range(min(w, canvas_w - lm)):
            sy = y + tm
            sx = x + lm
            if sy < canvas_h and sx < canvas_w and y < len(rows) and x < len(rows[y]):
                idx = rows[y][x]
                if idx == PAL_INDEX_TRANSPARENT:
                    continue
                if 1 <= idx <= 4 or 6 <= idx <= 7:
                    pixels_data[sy * canvas_w + sx] = idx
                else:
                    pixels_data[sy * canvas_w + sx] = PAL_INDEX_TRANSPARENT

    shadow_img.putdata(pixels_data)
    shadow_img.save(str(output_path), format="BMP")


def _map_def_type_to_hdl_type(def_type: int) -> int:
    """将 DEF 类型映射到 HDL 的 Type 值"""
    # H3DefTool Type 值: 0=SPRITE, 1=CREATURE, 2=MAP, 3=INTERFACE
    mapping = {
        0x40: 0,  # SPELL -> SPRITE
        0x41: 0,  # SPRITE
        0x42: 1,  # CREATURE
        0x43: 2,  # MAP
        0x44: 2,  # MAP_HERO
        0x45: 2,  # TERRAIN
        0x46: 0,  # CURSOR -> SPRITE
        0x47: 3,  # INTERFACE
        0x48: 0,  # SPRITE_FRAME
        0x49: 1,  # BATTLE_HERO -> CREATURE-like
    }
    return mapping.get(def_type, 0)


# ══════════════════════════════════════════════════════════════════
#  打包逻辑 (多模式 + 自动检测)
# ══════════════════════════════════════════════════════════════════


def detect_repack_mode(unpacked_dir: Path) -> str:
    """自动检测输入目录的格式

    Returns:
        'png' - VCMI PNG 格式 (meta.json)
        'bmp' - H3DefTool BMP 格式 (.hdl)
        'unknown' - 无法识别
    """
    if (unpacked_dir / "meta.json").exists():
        try:
            meta = json.loads((unpacked_dir / "meta.json").read_text("utf-8"))
            if "basepath" in meta and "sequences" in meta:
                return "png"
        except Exception:
            pass

    hdl_files = list(unpacked_dir.glob("*.hdl"))
    if hdl_files:
        return "bmp"

    return "unknown"


def repack_def(
    unpacked_dir: Path,
    output_path: Path | None = None,
    force: bool = False,
    mode: str = "auto",
    dither: bool = False,
) -> Path:
    """从解包目录重新打包为 DEF 文件

    Args:
        unpacked_dir: 解包目录
        output_path: 输出 .def 文件路径 (默认自动生成)
        force: 覆盖已存在的文件
        mode: repack 模式: 'auto', 'png', 'bmp'
        dither: 是否使用抖动

    Returns:
        输出 .def 文件路径
    """
    if mode == "auto":
        mode = detect_repack_mode(unpacked_dir)
        if mode == "unknown":
            print(_L("unknown_format", dir=str(unpacked_dir)))
            print(_L("supported_formats"))
            raise ValueError(_L("unknown_format", dir=str(unpacked_dir)))

    if mode == "png":
        return _repack_from_meta_json(unpacked_dir, output_path, force, dither)
    elif mode == "bmp":
        return _repack_from_hdl(unpacked_dir, output_path, force, dither)
    else:
        raise ValueError(_L("unsupported_repack_mode", mode=mode))


def _repack_from_meta_json(
    unpacked_dir: Path, output_path: Path | None, force: bool, dither: bool
) -> Path:
    """从 VCMI PNG 格式 (meta.json) 打包"""
    meta_path = unpacked_dir / "meta.json"
    meta = json.loads(meta_path.read_text("utf-8"))

    def_info = meta.get("defInfo", {})
    def_type = def_info.get("def_type", 0x42)

    # 从 defInfo 恢复调色板
    palette = None
    pal_hex = def_info.get("palette", "")
    if pal_hex:
        palette = list(bytes.fromhex(pal_hex))

    writer = DefWriter(def_type=def_type, palette=palette)

    sequences = meta.get("sequences", [])
    for seq in sequences:
        gid = seq["group"]
        writer.add_block(gid)
        bi = len(writer.blocks) - 1

        for frame_name in seq.get("frames", []):
            png_path = unpacked_dir / frame_name
            if not png_path.exists():
                print(_L("frame_not_found_warn", path=str(png_path)))
                continue

            png_data = png_path.read_bytes()

            # 尝试从 defInfo 获取尺寸信息 (如果 sequences 中有)
            import io

            from PIL import Image

            img = Image.open(io.BytesIO(png_data))
            w, h = img.size

            # 默认使用图像尺寸作为全尺寸
            writer.add_frame_from_png(
                bi,
                frame_name,
                png_data,
                full_w=w,
                full_h=h,
                width=w,
                height=h,
                left_margin=0,
                top_margin=0,
                dither=dither,
            )

    def_name = def_info.get("def_name", f"{unpacked_dir.name}.def")
    if output_path is None:
        output_path = unpacked_dir.with_name(f"{Path(def_name).stem}.new.def")

    if output_path.exists() and not force:
        print(_L("skip_exists_repack", path=str(output_path)))
        return output_path

    def_bytes = writer.to_bytes()
    output_path.write_bytes(def_bytes)
    print(_L("pack_complete", path=str(output_path), size=len(def_bytes)))
    return output_path


def _repack_from_hdl(
    unpacked_dir: Path, output_path: Path | None, force: bool, dither: bool
) -> Path:
    """从 H3DefTool BMP 格式 (.hdl) 打包"""
    hdl_files = list(unpacked_dir.glob("*.hdl"))
    if not hdl_files:
        raise FileNotFoundError(_L("hdl_not_found", dir=str(unpacked_dir)))
    hdl_path = hdl_files[0]
    hdl_data = parse_hdl(hdl_path)

    # 映射 HDL Type 到 DEF type
    hdl_type: int = hdl_data.get("def_type", 2)
    def_type_mapping = {0: 0x41, 1: 0x42, 2: 0x43, 3: 0x47}
    def_type = def_type_mapping.get(hdl_type, 0x42)

    writer = DefWriter(def_type=def_type)

    groups: dict[int, list[str]] = hdl_data.get("groups", {})
    for gid in sorted(groups.keys()):
        frame_names = groups[gid]
        writer.add_block(gid)
        bi = len(writer.blocks) - 1

        for frame_name in frame_names:
            bmp_path = unpacked_dir / frame_name
            if not bmp_path.exists():
                print(_L("bmp_not_found_warn", path=str(bmp_path)))
                continue

            pixel_rows, bmp_palette, w, h = load_indexed_bmp(bmp_path)

            # 使用 BMP 自身的调色板
            writer.palette = bmp_palette[:768]
            while len(writer.palette) < 768:
                writer.palette.append(0)

            writer.add_frame_from_rows(
                bi,
                Path(frame_name).stem,
                pixel_rows,
                full_w=w,
                full_h=h,
                width=w,
                height=h,
                left_margin=0,
                top_margin=0,
            )

    if output_path is None:
        hdl_stem = hdl_path.stem
        output_path = unpacked_dir.with_name(f"{hdl_stem}.new.def")

    if output_path.exists() and not force:
        print(_L("skip_exists_repack", path=str(output_path)))
        return output_path

    def_bytes = writer.to_bytes()
    output_path.write_bytes(def_bytes)
    print(_L("pack_complete", path=str(output_path), size=len(def_bytes)))
    return output_path


# ══════════════════════════════════════════════════════════════════
#  Info (信息查看)
# ══════════════════════════════════════════════════════════════════


def show_info(def_path: Path):
    """显示 DEF 文件信息"""
    reader = DefReader(def_path)
    print(reader.get_summary())


# ══════════════════════════════════════════════════════════════════
#  路径扫描逻辑
# ══════════════════════════════════════════════════════════════════


def find_defs(path: Path) -> list[Path]:
    """递归查找所有 .def 文件"""
    if path.is_file() and path.suffix.lower() == ".def":
        return [path]
    if path.is_dir():
        return sorted(path.rglob("*.def"))
    return []


def find_unpacked_dirs(path: Path) -> list[Path]:
    """递归查找所有解包目录

    查找规则:
    - 后缀为 .def_unpacked 的目录 → 兼容旧版格式
    - 包含 meta.json (含 basepath + sequences) 的目录 → VCMI PNG 格式
    - 包含 .hdl 文件的目录 → H3DefTool BMP 格式
    """
    results: list[Path] = []

    if path.is_dir():
        if path.name.endswith(SUFFIX_UNPACKED):
            results.append(path)
            return results
        for entry in path.iterdir():
            if entry.is_dir():
                if entry.name.endswith(SUFFIX_UNPACKED):
                    results.append(entry)
                else:
                    # 检查是否为 VCMI PNG 格式或 BMP 格式目录
                    is_unpacked = False
                    meta_file = entry / "meta.json"
                    if meta_file.exists():
                        try:
                            meta = json.loads(meta_file.read_text("utf-8"))
                            if "basepath" in meta and "sequences" in meta:
                                is_unpacked = True
                        except Exception:
                            pass
                    if not is_unpacked:
                        if list(entry.glob("*.hdl")):
                            is_unpacked = True
                    if is_unpacked:
                        results.append(entry)
                    else:
                        results.extend(find_unpacked_dirs(entry))
    return results


def clean_unpacked_dirs(path: Path, dry_run: bool = False) -> int:
    """递归清理所有 .def_unpacked 目录"""
    dirs = find_unpacked_dirs(path)
    if not dirs:
        sys.stdout.write(_L("no_unpacked_dirs"))
        return 0

    total_size = 0
    for d in dirs:
        size = sum(f.stat().st_size for f in d.rglob("*") if f.is_file())
        total_size += size
        sys.stdout.write(
            _L(
                "delete_dir",
                action="[DRY-RUN]" if dry_run else "[DELETE]",
                dir=str(d),
                size=size // 1024,
            )
        )
        sys.stdout.flush()
        if not dry_run:
            shutil.rmtree(d)

    sys.stdout.write(
        _L(
            "clean_summary",
            status="DRY-RUN" if dry_run else "OK",
            n=len(dirs),
            size=total_size // 1024,
        )
    )
    sys.stdout.flush()
    return len(dirs)


def find_defs_by_unpacked(path: Path) -> list[Path]:
    """查找可以被清理的 .def 文件 (存在对应的解包目录)

    规则:
    - 与 .def 同名的 .def_unpacked 目录存在 → 可清理
    - 与 .def 同名的无后缀目录存在 (含 meta.json 或 .hdl) → 可清理
    """
    candidates: list[Path] = []
    def_files = find_defs(path)

    for def_path in def_files:
        def_stem = def_path.stem

        # 检查 .def_unpacked 目录
        unpacked_dir = def_path.with_name(def_path.name + SUFFIX_UNPACKED)
        if unpacked_dir.is_dir():
            candidates.append(def_path)
            continue

        # 检查 VCMI PNG 或 BMP 格式目录
        png_dir = def_path.with_name(def_stem)
        if png_dir.is_dir():
            if (png_dir / "meta.json").exists() or list(png_dir.glob("*.hdl")):
                candidates.append(def_path)
                continue

    return candidates


def clean_def_files(path: Path, dry_run: bool = False) -> int:
    """递归清理有对应解包目录的 .def 文件

    适用于调试场景: 保留解包目录, 删除原始 .def。
    """
    defs = find_defs_by_unpacked(path)
    if not defs:
        sys.stdout.write(_L("no_defs_to_clean"))
        return 0

    total_size = 0
    for d in defs:
        size = d.stat().st_size
        total_size += size
        sys.stdout.write(
            _L(
                "delete_dir",
                action="[DRY-RUN]" if dry_run else "[DELETE]",
                dir=str(d),
                size=size // 1024,
            )
        )
        sys.stdout.flush()
        if not dry_run:
            d.unlink()

    sys.stdout.write(
        _L(
            "clean_def_summary",
            status="DRY-RUN" if dry_run else "OK",
            n=len(defs),
            size=total_size // 1024,
        )
    )
    sys.stdout.flush()
    return len(defs)


# ══════════════════════════════════════════════════════════════════
#  Valid (验证 — round-trip 自测)
# ══════════════════════════════════════════════════════════════════


def _load_frame_pixels(file_path: Path, mode: str) -> list[list[int]] | None:
    """加载帧像素数据用于比较

    Args:
        file_path: 帧文件路径 (.png / .bmp / .pixels.dat)
        mode: 解包模式

    Returns:
        二维 int 数组 [height][width], 或 None (无法加载)
    """

    from PIL import Image

    ext = file_path.suffix.lower()

    if ext == ".png":
        try:
            img = Image.open(str(file_path))
            if img.mode != "RGBA":
                img = img.convert("RGBA")
            w, h = img.size
            if hasattr(img, "get_flattened_data"):
                pixels = list(img.get_flattened_data())
            else:
                pixels = list(img.getdata())
            # 转为 palette index (最近色匹配, 用于比较)
            pal_rgb = palette_to_rgb_list(list(STANDARD_PALETTE))
            rows: list[list[int]] = []
            for y in range(h):
                row: list[int] = []
                for x in range(w):
                    idx = y * w + x
                    if idx < len(pixels):
                        r, g, b, a = pixels[idx]
                        if a < 128:
                            row.append(PAL_INDEX_TRANSPARENT)
                        else:
                            pi = _nearest_color(r, g, b, pal_rgb)
                            row.append(pi)
                    else:
                        row.append(PAL_INDEX_TRANSPARENT)
                rows.append(row)
            return rows
        except Exception:
            return None

    elif ext == ".bmp":
        try:
            rows, _, _, _ = load_indexed_bmp(file_path)
            return rows
        except Exception:
            return None

    elif file_path.name.endswith(SUFFIX_PIXELS):
        try:
            file_path.read_bytes()
            # 没有宽高信息, 无法重建二维数组
            return None
        except Exception:
            return None

    return None


def _compare_frame_pixels(
    rows_a: list[list[int]], rows_b: list[list[int]], label: str
) -> dict[str, Any]:
    """比较两帧像素数据, 返回差异统计"""
    h_a = len(rows_a)
    h_b = len(rows_b)
    if h_a == 0 or h_b == 0:
        return {
            "match": False,
            "reason": _L("empty_frame_reason"),
            "diff_pixels": 0,
            "total_pixels": 0,
        }

    w_a = max(len(r) for r in rows_a)
    w_b = max(len(r) for r in rows_b)

    if h_a != h_b or w_a != w_b:
        return {
            "match": False,
            "reason": _L("dim_mismatch_reason", wa=w_a, ha=h_a, wb=w_b, hb=h_b),
            "diff_pixels": 0,
            "total_pixels": 0,
        }

    diff_count = 0
    total = 0
    first_diff: tuple[int, int, int, int] | None = None
    for y in range(h_a):
        for x in range(min(w_a, len(rows_b[y]))):
            if x < len(rows_a[y]):
                total += 1
                va = rows_a[y][x]
                vb = rows_b[y][x]
                if va != vb:
                    diff_count += 1
                    if first_diff is None:
                        first_diff = (x, y, va, vb)

    match_result = diff_count == 0
    return {
        "match": match_result,
        "label": label,
        "diff_pixels": diff_count,
        "total_pixels": total,
        "diff_pct": round(diff_count / total * 100, 2) if total > 0 else 0,
        "first_diff": first_diff,
        "reason": ""
        if match_result
        else _L(
            "pixel_diff_reason", count=diff_count, total=total, first_diff=first_diff
        ),
    }


def _collect_frames(unpack_dir: Path, mode: str) -> list[tuple[str, Path]]:
    """收集解包目录中的所有帧文件, 返回 [(相对路径, 绝对路径)]"""
    frames: list[tuple[str, Path]] = []

    if mode == "png":
        for f in sorted(unpack_dir.glob("*.png")):
            frames.append((f.name, f))
    elif mode == "bmp":
        for f in sorted(unpack_dir.glob("*.bmp")):
            frames.append((f.name, f))

    return frames


def valid_def(def_path: Path, mode: str = "png", keep: bool = False) -> dict[str, Any]:
    """执行 round-trip 验证: unpack → repack → unpack → 比较

    验证产物保存在原 .def 文件所在目录:
      {def_stem}-Valid.def   — 重新打包后的 def
      {def_stem}-Valid/      — 再次解包目录

    Args:
        def_path: 原始 .def 文件路径
        mode: 验证模式 ('png', 'bmp')
        keep: 保留验证产物 (-Valid.def 和 -Valid/ 目录)

    Returns:
        验证结果 dict
    """
    import shutil

    parent_dir = def_path.parent
    def_stem = def_path.stem
    valid_def_path = parent_dir / f"{def_stem}-Valid.def"
    valid_unpack_dir = parent_dir / f"{def_stem}-Valid"
    orig_unpack_dir = parent_dir / def_stem

    result: dict[str, Any] = {
        "mode": mode,
        "def_file": str(def_path),
        "total_frames": 0,
        "matched_frames": 0,
        "failed_frames": 0,
        "passed": False,
        "details": [],
    }

    orig_exists = orig_unpack_dir.exists()
    orig_mode_ok = False
    try:
        # Step 1: 确保原始解包目录存在且格式匹配
        orig_exists = orig_unpack_dir.exists()
        if orig_exists:
            if mode == "png" and (orig_unpack_dir / "meta.json").exists():
                orig_mode_ok = True
            elif mode == "bmp" and list(orig_unpack_dir.glob("*.hdl")):
                orig_mode_ok = True

        if orig_exists and orig_mode_ok:
            sys.stdout.write(_L("valid_step1_existing", dir=orig_unpack_dir.name))
            sys.stdout.flush()
            # 清理旧验证产物
            if valid_def_path.exists():
                valid_def_path.unlink()
            if valid_unpack_dir.exists():
                shutil.rmtree(valid_unpack_dir)
        else:
            if orig_exists and not orig_mode_ok:
                sys.stdout.write(
                    _L("valid_step1_mismatch", dir=orig_unpack_dir.name, mode=mode)
                )
                sys.stdout.flush()
            else:
                sys.stdout.write(_L("valid_step1_unpack", dir=orig_unpack_dir.name))
                sys.stdout.flush()
            unpack_def(def_path, output_dir=orig_unpack_dir, force=True, mode=mode)

        # Step 2: 重新打包 → {def_stem}-Valid.def
        sys.stdout.write(_L("valid_step2", file=valid_def_path.name))
        sys.stdout.flush()
        repack_mode = "bmp" if mode == "bmp" else "png"
        repack_def(
            orig_unpack_dir,
            output_path=valid_def_path,
            force=True,
            mode=repack_mode,
            dither=False,
        )

        # Step 3: 再次解包 → {def_stem}-Valid/
        sys.stdout.write(_L("valid_step3", dir=valid_unpack_dir.name))
        sys.stdout.flush()
        unpack_def(valid_def_path, output_dir=valid_unpack_dir, force=True, mode=mode)

        # Step 4: 比较帧
        frames_orig = _collect_frames(orig_unpack_dir, mode)
        frames_new_map = {
            rel: path for rel, path in _collect_frames(valid_unpack_dir, mode)
        }
        result["total_frames"] = len(frames_orig)

        for rel, path_a in frames_orig:
            path_b = frames_new_map.get(rel)
            if path_b is None:
                path_b = valid_unpack_dir / rel

            if not path_b or not path_b.exists():
                detail = {
                    "frame": rel,
                    "match": False,
                    "reason": "新目录中找不到对应帧",
                    "diff_pixels": 0,
                    "total_pixels": 0,
                    "diff_pct": 0,
                }
                result["failed_frames"] += 1
                result["details"].append(detail)
                sys.stdout.write(_L("valid_frame_not_found", rel=rel))
                sys.stdout.flush()
                continue

            pixels_a = _load_frame_pixels(path_a, mode)
            pixels_b = _load_frame_pixels(path_b, mode)

            if pixels_a is None or pixels_b is None:
                detail = {
                    "frame": rel,
                    "match": False,
                    "reason": "无法加载像素数据",
                    "diff_pixels": 0,
                    "total_pixels": 0,
                    "diff_pct": 0,
                }
                result["failed_frames"] += 1
                result["details"].append(detail)
                sys.stdout.write(_L("valid_frame_load_fail", rel=rel))
                sys.stdout.flush()
                continue

            cmp = _compare_frame_pixels(pixels_a, pixels_b, rel)
            result["details"].append(cmp)
            if cmp["match"]:
                result["matched_frames"] += 1
                if len(frames_orig) <= 10 or result["matched_frames"] % 50 == 0:
                    sys.stdout.write(_L("valid_frame_ok", rel=rel))
                    sys.stdout.flush()
            else:
                result["failed_frames"] += 1
                sys.stdout.write(_L("valid_frame_diff", rel=rel, reason=cmp["reason"]))
                sys.stdout.flush()

        result["passed"] = result["failed_frames"] == 0
        status_str = "PASS" if result["passed"] else "FAIL"
        sys.stdout.write(
            _L(
                "valid_result",
                status=status_str,
                matched=result["matched_frames"],
                total=result["total_frames"],
                failed=result["failed_frames"],
            )
        )

        if not result["passed"]:
            diff_details = [d for d in result["details"] if not d["match"]]
            for d in diff_details[:5]:
                sys.stdout.write(
                    _L(
                        "valid_diff_detail",
                        label=d.get("label", d["frame"]),
                        reason=d.get("reason", ""),
                    )
                )
            if len(diff_details) > 5:
                sys.stdout.write(_L("valid_more_diffs", n=len(diff_details) - 5))

        # 生成产物摘要
        sys.stdout.write(_L("valid_artifacts"))
        if valid_def_path.exists():
            sys.stdout.write(
                _L(
                    "valid_repacked",
                    path=str(valid_def_path),
                    size=valid_def_path.stat().st_size // 1024,
                )
            )
        if valid_unpack_dir.exists():
            png_count = len(list(valid_unpack_dir.rglob("*.*")))
            sys.stdout.write(
                _L("valid_unpacked_dir", dir=str(valid_unpack_dir), count=png_count)
            )
        sys.stdout.flush()

    except Exception as e:
        sys.stdout.write(_L("valid_exception", err=e))
        import traceback

        traceback.print_exc()
        result["passed"] = False
        result["error"] = str(e)

    finally:
        if not keep:
            # 清理验证产物
            if valid_def_path.exists():
                valid_def_path.unlink()
                sys.stdout.write(_L("valid_cleaned", name=valid_def_path.name))
            if valid_unpack_dir.exists():
                shutil.rmtree(valid_unpack_dir, ignore_errors=True)
                sys.stdout.write(_L("valid_cleaned_dir", name=valid_unpack_dir.name))
            # 如果原始解包目录是本次新建或格式不符重建的, 也清理
            if not orig_exists or not orig_mode_ok:
                if orig_unpack_dir.exists():
                    shutil.rmtree(orig_unpack_dir, ignore_errors=True)
                    sys.stdout.write(_L("valid_cleaned_dir", name=orig_unpack_dir.name))
            sys.stdout.flush()
        else:
            sys.stdout.write(
                _L("valid_kept", name=valid_def_path.name, dir=valid_unpack_dir.name)
            )
            sys.stdout.flush()

    return result


# ══════════════════════════════════════════════════════════════════
#  Test (批量 round-trip 验证测试)
# ══════════════════════════════════════════════════════════════════


def _print_test_result(
    index: int, total: int, def_path: Path, result: dict[str, Any], elapsed: float
) -> None:
    """打印单个测试结果行"""
    status = "PASS" if result["passed"] else "FAIL"
    matched = result["matched_frames"]
    total_frames = result["total_frames"]
    sys.stdout.write(
        _L(
            "test_result",
            i=index,
            total=total,
            name=def_path.name,
            status=status,
            matched=matched,
            total_frames=total_frames,
            elapsed=elapsed,
        )
    )
    if not result["passed"]:
        for detail in result.get("details", []):
            if not detail.get("match", True):
                label = detail.get("label", detail.get("frame", "?"))
                reason = detail.get("reason", "未知")
                sys.stdout.write(_L("test_diff", label=label, reason=reason))
    sys.stdout.flush()


def _print_test_summary(
    results: list[tuple[Path, dict[str, Any], float]], total_elapsed: float
) -> None:
    """打印测试汇总报告"""
    passed = sum(1 for _, r, _ in results if r["passed"])
    failed = len(results) - passed

    sys.stdout.write("─" * 70 + "\n")
    sys.stdout.write(_L("test_summary", passed=passed, total=len(results)))
    if failed:
        sys.stdout.write(_L("test_fail_count", failed=failed))
    sys.stdout.write(_L("test_elapsed", elapsed=total_elapsed))

    if failed:
        sys.stdout.write(_L("test_fail_list"))
        for def_path, r, _elapsed in results:
            if not r["passed"]:
                matched = r["matched_frames"]
                total_frames = r["total_frames"]
                sys.stdout.write(
                    _L(
                        "test_fail_item",
                        name=def_path.name,
                        matched=matched,
                        total=total_frames,
                    )
                )
    sys.stdout.flush()


def test_defs(
    input_path: Path,
    mode: str = "png",
    recursive: bool = False,
    sample: int | None = None,
    keep: bool = False,
    parallel: bool = False,
    max_workers: int = 4,
    seed: int | None = None,
) -> None:
    """批量执行 round-trip 验证测试

    Args:
        input_path: .def 文件或目录路径
        mode: 验证模式
        recursive: 是否递归扫描目录
        sample: 随机抽样数量
        keep: 保留验证产物 (传给 valid_def)
        parallel: 是否多线程并行执行
        max_workers: 并行线程数
        seed: 随机种子 (确保抽样可复现)
    """
    # 1. 收集 .def 文件
    if input_path.is_file() and input_path.suffix.lower() == ".def":
        defs = [input_path]
    elif recursive:
        defs = find_defs(input_path)
    else:
        defs = sorted(input_path.parent.glob("*.def")) if input_path.is_dir() else []
        if not defs and input_path.is_dir():
            defs = [input_path]

    if not defs:
        sys.stdout.write(_L("test_no_defs", path=str(input_path)))
        sys.stdout.flush()
        return

    # 2. 随机抽样
    total_found = len(defs)
    if sample is not None and sample < total_found:
        rng = _random.Random(seed)
        defs = rng.sample(defs, sample)

    # 3. 打印测试配置
    exec_mode = (
        _L("exec_mode_parallel", n=max_workers) if parallel else _L("exec_mode_serial")
    )
    sys.stdout.write(_L("test_scan_path", path=str(input_path)))
    sys.stdout.write(_L("test_found", total=total_found))
    if sample is not None and sample < total_found:
        sys.stdout.write(_L("test_sampled", sample=sample))
        for i, d in enumerate(defs, 1):
            try:
                rel = d.relative_to(
                    input_path.parent if input_path.is_file() else input_path
                )
            except ValueError:
                rel = d.name
            sys.stdout.write(_L("test_sample_item", i=i, path=str(rel)))
    else:
        sys.stdout.write("\n")
    sys.stdout.write(_L("test_mode", mode=mode))
    sys.stdout.write(_L("test_exec_mode", mode=exec_mode))
    sys.stdout.write("─" * 70 + "\n")
    sys.stdout.flush()

    total_start = time.time()
    all_results: list[tuple[Path, dict[str, Any], float]] = []

    if parallel:

        def _run_test(def_path: Path) -> tuple[Path, dict[str, Any], float, str]:
            buf = io.StringIO()
            with redirect_stdout(buf):
                start = time.time()
                result = valid_def(def_path, mode=mode, keep=keep)
                elapsed = time.time() - start
            return def_path, result, elapsed, buf.getvalue()

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {executor.submit(_run_test, d): d for d in defs}
            completed = 0
            for future in as_completed(futures):
                def_path, result, elapsed, _detail_out = future.result()
                all_results.append((def_path, result, elapsed))
                completed += 1
                _print_test_result(completed, len(defs), def_path, result, elapsed)
    else:
        for i, def_path in enumerate(defs, 1):
            start = time.time()
            result = valid_def(def_path, mode=mode, keep=keep)
            elapsed = time.time() - start
            _print_test_result(i, len(defs), def_path, result, elapsed)
            all_results.append((def_path, result, elapsed))

    # 4. 汇总报告
    total_elapsed = time.time() - total_start
    _print_test_summary(all_results, total_elapsed)


# ══════════════════════════════════════════════════════════════════
#  并行处理包装函数
# ══════════════════════════════════════════════════════════════════


def _process_unpack(
    def_path: Path, force: bool, mode: str, keep_pixels: bool
) -> tuple[Path, bool, float, str]:
    """unpack 的包装函数，捕获 stdout 输出用于并行执行"""
    buf = io.StringIO()
    start = time.time()
    try:
        with redirect_stdout(buf):
            unpack_def(def_path, force=force, mode=mode, keep_pixels=keep_pixels)
        elapsed = time.time() - start
        return def_path, True, elapsed, buf.getvalue()
    except Exception as e:
        elapsed = time.time() - start
        err = buf.getvalue()
        err += f"\n  [FAIL] {def_path.name}: {e}\n"
        import traceback as _tb

        err += _tb.format_exc()
        return def_path, False, elapsed, err


def _process_repack(
    unpacked_dir: Path, force: bool, mode: str, dither: bool
) -> tuple[Path, bool, float, str]:
    """repack 的包装函数，捕获 stdout 输出用于并行执行"""
    buf = io.StringIO()
    start = time.time()
    try:
        with redirect_stdout(buf):
            repack_def(unpacked_dir, force=force, mode=mode, dither=dither)
        elapsed = time.time() - start
        return unpacked_dir, True, elapsed, buf.getvalue()
    except Exception as e:
        elapsed = time.time() - start
        err = buf.getvalue()
        err += f"\n  [FAIL] {unpacked_dir.name}: {e}\n"
        import traceback as _tb

        err += _tb.format_exc()
        return unpacked_dir, False, elapsed, err


# ══════════════════════════════════════════════════════════════════
#  CLI
# ══════════════════════════════════════════════════════════════════


def parse_args() -> tuple[argparse.ArgumentParser, argparse.Namespace]:
    parser = argparse.ArgumentParser(
        description=_L("desc_main"),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=_L("epilog"),
    )

    # ── 全局参数 ──
    parser.add_argument("--lang", type=str, default=None, help=_L("help_lang_arg"))

    sub = parser.add_subparsers(dest="command", required=True)

    # ── info ──
    p_info = sub.add_parser("info", help=_L("help_info"))
    p_info.add_argument("path", type=str, help=_L("help_info_path"))

    # ── unpack ──
    p_unpack = sub.add_parser("unpack", help=_L("help_unpack"))
    p_unpack.add_argument("path", type=str, help=_L("help_unpack_path"))
    p_unpack.add_argument(
        "-o", "--output", type=str, default=None, help=_L("arg_output")
    )
    p_unpack.add_argument(
        "-r", "--recursive", action="store_true", help=_L("arg_recursive")
    )
    p_unpack.add_argument("--force", action="store_true", help=_L("arg_force"))
    p_unpack.add_argument(
        "--mode",
        type=str,
        default="png",
        choices=["png", "bmp"],
        help=_L("arg_mode_pack"),
    )
    p_unpack.add_argument(
        "--keep-pixels", action="store_true", help=_L("arg_keep_pixels")
    )
    p_unpack.add_argument(
        "-p", "--parallel", action="store_true", help=_L("arg_parallel")
    )
    p_unpack.add_argument(
        "--max-workers", type=int, default=4, help=_L("arg_max_workers")
    )

    # ── repack ──
    p_repack = sub.add_parser("repack", help=_L("help_repack"))
    p_repack.add_argument("path", type=str, help=_L("help_repack_path"))
    p_repack.add_argument(
        "-o", "--output", type=str, default=None, help=_L("arg_output")
    )
    p_repack.add_argument(
        "-r", "--recursive", action="store_true", help=_L("arg_recursive")
    )
    p_repack.add_argument("--force", action="store_true", help=_L("arg_force_file"))
    p_repack.add_argument(
        "--mode",
        type=str,
        default="auto",
        choices=["auto", "png", "bmp"],
        help=_L("arg_mode_repack"),
    )
    p_repack.add_argument("--dither", action="store_true", help=_L("arg_dither"))
    p_repack.add_argument(
        "-p", "--parallel", action="store_true", help=_L("arg_parallel_repack")
    )
    p_repack.add_argument(
        "--max-workers", type=int, default=4, help=_L("arg_max_workers")
    )

    # ── clean-unpack ──
    p_clean = sub.add_parser("clean-unpack", help=_L("help_clean_unpack"))
    p_clean.add_argument("path", type=str, help=_L("help_clean_path"))
    p_clean.add_argument("--dry-run", action="store_true", help=_L("arg_dry_run"))

    # ── clean-def ──
    p_clean_def = sub.add_parser("clean-def", help=_L("help_clean_def"))
    p_clean_def.add_argument("path", type=str, help=_L("help_clean_path"))
    p_clean_def.add_argument("--dry-run", action="store_true", help=_L("arg_dry_run"))

    # ── valid ──
    p_valid = sub.add_parser("valid", help=_L("help_valid"))
    p_valid.add_argument("path", type=str, help=_L("help_valid_path"))
    p_valid.add_argument(
        "--mode",
        type=str,
        default="png",
        choices=["png", "bmp"],
        help=_L("arg_valid_mode"),
    )
    p_valid.add_argument("--keep", action="store_true", help=_L("arg_keep"))

    # ── test ──
    p_test = sub.add_parser("test", help=_L("help_test"))
    p_test.add_argument("path", type=str, help=_L("help_test_path"))
    p_test.add_argument(
        "--mode",
        type=str,
        default="png",
        choices=["png", "bmp"],
        help=_L("arg_valid_mode"),
    )
    p_test.add_argument(
        "-r", "--recursive", action="store_true", help=_L("arg_recursive")
    )
    p_test.add_argument("--sample", type=int, default=None, help=_L("arg_sample"))
    p_test.add_argument("-k", "--keep", action="store_true", help=_L("arg_keep"))
    p_test.add_argument(
        "-p", "--parallel", action="store_true", help=_L("arg_parallel_test")
    )
    p_test.add_argument(
        "--max-workers", type=int, default=4, help=_L("arg_max_workers")
    )
    p_test.add_argument("--seed", type=int, default=None, help=_L("arg_seed"))

    # ── help ──
    p_help = sub.add_parser("help", help=_L("help_help"))
    p_help.add_argument(
        "cmd", type=str, nargs="?", default=None, help=_L("help_help_cmd")
    )

    return parser, parser.parse_args()


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(line_buffering=True)

    parser, args = parse_args()

    # ── 处理 --lang 参数 ──
    if getattr(args, "lang", None):
        _L.set_locale(args.lang)

    # ── help ── (必须在 args.path 访问之前处理)
    if args.command == "help":
        target = args.cmd
        if target:
            # 在 subparsers 中查找目标命令
            for act in parser._actions:
                if isinstance(act, argparse._SubParsersAction):
                    if target in act.choices:
                        act.choices[target].print_help()
                        return
            # 子命令不存在，列出可用命令
            sys.stdout.write(_L("unknown_cmd", cmd=target))
            sys.stdout.write(
                _L("available_cmds", cmds=", ".join(act.choices.keys())) + "\n\n"
            )
        # 无参数/未知命令均输出主帮助
        parser.print_help()
        return

    input_path = Path(args.path).resolve()

    sys.stdout.write(_L("work_dir", path=Path.cwd()))
    sys.stdout.write("\n")
    sys.stdout.write(_L("target_path", path=input_path))
    sys.stdout.flush()

    # ── info ──
    if args.command == "info":
        if not input_path.is_file():
            sys.stdout.write(_L("file_not_found", path=input_path))
            sys.exit(1)
        try:
            show_info(input_path)
        except Exception as e:
            sys.stdout.write(_L("info_fail", err=e))
            import traceback

            traceback.print_exc()
            sys.exit(1)
        return

    # ── unpack ──
    if args.command == "unpack":
        defs_to_process = (
            find_defs(input_path)
            if args.recursive
            else (
                [input_path]
                if input_path.is_file() and input_path.suffix.lower() == ".def"
                else [input_path]
            )
        )
        if not args.recursive and input_path.is_dir():
            defs_to_process = list(input_path.glob("*.def"))

        if not defs_to_process:
            sys.stdout.write(_L("def_not_found", path=input_path))
            sys.stdout.flush()
            return

        total = len(defs_to_process)
        unpack_label = "UNPACK"

        if args.parallel:
            sys.stdout.write(_L("def_found_count", n=total))
            sys.stdout.write(
                _L("exec_mode_info", mode=_L("exec_mode_parallel", n=args.max_workers))
            )
            sys.stdout.write("─" * 60 + "\n")
            sys.stdout.flush()

            success = 0
            fail = 0
            total_start = time.time()

            with ThreadPoolExecutor(max_workers=args.max_workers) as executor:
                futures = {
                    executor.submit(
                        _process_unpack,
                        d,
                        args.force,
                        args.mode,
                        getattr(args, "keep_pixels", False),
                    ): d
                    for d in defs_to_process
                }
                completed = 0
                for future in as_completed(futures):
                    def_path, ok, elapsed, detail_out = future.result()
                    completed += 1
                    status = "OK" if ok else "FAIL"
                    if ok:
                        success += 1
                    else:
                        fail += 1
                    out_dir = def_path.with_name(def_path.stem)
                    sys.stdout.write(
                        f"[{unpack_label}] ({completed}/{total}) {def_path.name} → {out_dir.name}/ [{status}] ({elapsed:.1f}s)\n"
                    )
                    if not ok:
                        sys.stdout.write(detail_out)
                    sys.stdout.flush()

            total_elapsed = time.time() - total_start
            sys.stdout.write("─" * 60 + "\n")
            sys.stdout.write(
                _L("summary_ok", label=unpack_label, ok=success, total=total)
            )
            if fail:
                sys.stdout.write(_L("summary_fail", fail=fail))
            sys.stdout.write(_L("summary_elapsed", elapsed=total_elapsed))
            sys.stdout.flush()
        else:
            for d in defs_to_process:
                try:
                    out_dir = None
                    if args.output and len(defs_to_process) == 1:
                        out_dir = Path(args.output)
                    unpack_def(
                        d,
                        output_dir=out_dir,
                        force=args.force,
                        mode=args.mode,
                        keep_pixels=getattr(args, "keep_pixels", False),
                    )
                except Exception as e:
                    sys.stdout.write(_L("generic_fail", name=d, err=e))
                    import traceback

                    traceback.print_exc()
                    sys.stdout.flush()
        return

    # ── repack ──
    if args.command == "repack":
        unpacked_dirs = (
            find_unpacked_dirs(input_path)
            if args.recursive
            else ([input_path] if input_path.is_dir() else [input_path])
        )
        if not args.recursive and input_path.is_dir():
            # 直接输入的目录: 检查是否为有效解包目录
            if (input_path / "meta.json").exists() or list(input_path.glob("*.hdl")):
                unpacked_dirs = [input_path]
            elif input_path.name.endswith(SUFFIX_UNPACKED):
                unpacked_dirs = [input_path]
            else:
                # 搜索子目录
                unpacked_dirs = list(input_path.glob(f"*{SUFFIX_UNPACKED}"))
                if not unpacked_dirs:
                    for entry in sorted(input_path.iterdir()):
                        if entry.is_dir():
                            if (entry / "meta.json").exists() or list(
                                entry.glob("*.hdl")
                            ):
                                unpacked_dirs.append(entry)

        if not unpacked_dirs:
            sys.stdout.write(_L("unpacked_not_found", path=input_path))
            sys.stdout.flush()
            return

        total = len(unpacked_dirs)
        repack_label = "REPACK"

        if args.parallel:
            sys.stdout.write(_L("unpacked_found_count", n=total))
            sys.stdout.write(
                _L("exec_mode_info", mode=_L("exec_mode_parallel", n=args.max_workers))
            )
            sys.stdout.write("─" * 60 + "\n")
            sys.stdout.flush()

            success = 0
            fail = 0
            total_start = time.time()

            with ThreadPoolExecutor(max_workers=args.max_workers) as executor:
                futures = {
                    executor.submit(
                        _process_repack, d, args.force, args.mode, args.dither
                    ): d
                    for d in unpacked_dirs
                }
                completed = 0
                for future in as_completed(futures):
                    dir_path, ok, elapsed, detail_out = future.result()
                    completed += 1
                    status = "OK" if ok else "FAIL"
                    if ok:
                        success += 1
                    else:
                        fail += 1
                    sys.stdout.write(
                        f"[{repack_label}] ({completed}/{total}) {dir_path.name}/ [{status}] ({elapsed:.1f}s)\n"
                    )
                    if not ok:
                        sys.stdout.write(detail_out)
                    sys.stdout.flush()

            total_elapsed = time.time() - total_start
            sys.stdout.write("─" * 60 + "\n")
            sys.stdout.write(
                _L("summary_ok", label=repack_label, ok=success, total=total)
            )
            if fail:
                sys.stdout.write(_L("summary_fail", fail=fail))
            sys.stdout.write(_L("summary_elapsed", elapsed=total_elapsed))
            sys.stdout.flush()
        else:
            for d in unpacked_dirs:
                try:
                    out_path = None
                    if args.output and len(unpacked_dirs) == 1:
                        out_path = Path(args.output)
                    repack_def(
                        d,
                        output_path=out_path,
                        force=args.force,
                        mode=args.mode,
                        dither=args.dither,
                    )
                except Exception as e:
                    sys.stdout.write(_L("generic_fail", name=d, err=e))
                    import traceback

                    traceback.print_exc()
                    sys.stdout.flush()
        return

    # ── clean-unpack ──
    if args.command == "clean-unpack":
        clean_unpacked_dirs(input_path, dry_run=args.dry_run)
        return

    # ── clean-def ──
    if args.command == "clean-def":
        clean_def_files(input_path, dry_run=args.dry_run)
        return

    # ── valid ──
    if args.command == "valid":
        if not input_path.is_file() or input_path.suffix.lower() != ".def":
            sys.stdout.write(_L("file_not_found", path=input_path))
            sys.exit(1)
        try:
            result = valid_def(input_path, mode=args.mode, keep=args.keep)
            if not result["passed"]:
                sys.exit(1)
        except Exception as e:
            sys.stdout.write(_L("valid_fail", err=e))
            import traceback

            traceback.print_exc()
            sys.exit(1)
        return

    # ── test ──
    if args.command == "test":
        try:
            test_defs(
                input_path,
                mode=args.mode,
                recursive=args.recursive,
                sample=args.sample,
                keep=args.keep,
                parallel=args.parallel,
                max_workers=args.max_workers,
                seed=args.seed,
            )
        except Exception as e:
            sys.stdout.write(_L("test_fail", err=e))
            import traceback

            traceback.print_exc()
            sys.exit(1)
        return


if __name__ == "__main__":
    main()
