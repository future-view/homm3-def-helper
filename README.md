# HoMM3 / VCMI DEF File Tool

Unpack and repack Heroes of Might & Magic 3 / VCMI `.def` animation sprite files.

## Features

### Core Commands
- **`info`** — Show `.def` file structure information (type, groups, frame dimensions)
- **`unpack`** — Unpack `.def` files into frame images + metadata
- **`repack`** — Repack frame images + metadata into `.def` files
- **`valid`** — Round-trip validation (unpack → repack → unpack → pixel comparison)
- **`test`** — Batch round-trip validation (recursive scanning / random sampling / parallel execution)
- **`clean-unpack`** — Clean all unpacked directories
- **`clean-def`** — Clean `.def` files that have corresponding unpacked directories

### Two Unpack/Repack Modes

| Mode | Parameter | Use Case | Output Format |
|------|-----------|----------|---------------|
| **PNG (VCMI)** | `--mode=png` (default) | VCMI native animation / AI training data | `meta.json` + `{group}_{frame}.png` |
| **BMP** | `--mode=bmp` | H3DefTool / DefPreview compatibility | `.hdl` + 8-bit BMP + `Shadow/` |

### Key Features

- Supports 4 pixel formats (format 0~3) decoding
- **Floyd-Steinberg dithering** — Reduces color banding when repacking AI-generated PNGs
- **Auto-format detection** — `repack` automatically detects format from directory contents
- **Custom palette** — Preserves original DEF palette, uses standard palette for new generation
- **`.pixels.dat` optional** — `--keep-pixels` saves raw palette index data in PNG mode for debugging
- **Round-trip self-test** — `valid` command verifies unpack→repack→unpack pixel consistency
- **Batch recursive processing** — Supports Mod directory-level batch operations

## Usage

### info
```bash
python main.py info creature.def
```

### unpack — PNG mode (default)
```bash
# Unpack to VCMI native PNG format
python main.py unpack creature.def
python main.py unpack creature.def --mode=png

# Output structure (directory name = def filename, without .def):
# creature/
# ├── meta.json           # VCMI animation config file (basepath auto-computed)
# ├── 0_001.png           # Moving group frame 1
# ├── 0_002.png           # Moving group frame 2
# ├── 4_001.png           # Defend group frame 1
# └── ...
```

### unpack — PNG mode + .pixels.dat (debugging)
```bash
# Unpack to PNG with extra .pixels.dat (raw palette index data)
python main.py unpack creature.def --mode=png --keep-pixels

# Output structure:
# creature/
# ├── meta.json
# ├── 0_001.png
# ├── 0_001.pixels.dat    # Raw palette index data (for round-trip debugging)
# ├── 0_002.png
# ├── 0_002.pixels.dat
# └── ...
```

### unpack — BMP mode (H3DefTool compatible)
```bash
# Unpack to 8-bit indexed BMP + .hdl
python main.py unpack creature.def --mode=bmp

# Output structure:
# creature/
# ├── romLvl1.hdl
# ├── Shadow/
# │   ├── 000.bmp
# │   └── ...
# ├── 000.bmp
# └── ...
```

### repack (auto-detect mode)
```bash
# Repack from VCMI PNG directory (auto-detect meta.json)
python main.py repack creature/

# Repack from BMP/HDL directory (auto-detect .hdl)
python main.py repack creature/

# Manually specify mode
python main.py repack creature/ --mode=bmp

# Enable Floyd-Steinberg dithering
python main.py repack creature/ --dither
```

### valid (round-trip self-test)
```bash
# Validate PNG mode
python main.py valid creature.def --mode=png

# Validate BMP mode
python main.py valid creature.def --mode=bmp

# Keep validation artifacts (for comparison analysis)
python main.py valid creature.def --mode=png --keep
```

Validation flow: unpack → repack → unpack → per-frame pixel comparison, outputs PASS/FAIL report.

Validation artifacts are saved in the same directory as the original `.def` file:
```
creature.def
creature/              ← Original unpacked dir (existing or newly created)
creature-Valid.def     ← Repacked def (--keep to retain)
creature-Valid/        ← Re-unpacked dir   (--keep to retain)
```

Without `--keep`, validation automatically cleans up `-Valid.def` and `-Valid/` artifacts.

### test (batch round-trip validation)

Run round-trip validation on a single `.def` file or an entire Mod directory.

```bash
# Single file test
python main.py test creature.def --mode=png

# Recursive test on entire Mod directory
python main.py test Mods/romantown/ --recursive --mode=png

# Recursive test + random sample 5 (--seed ensures reproducibility)
python main.py test Mods/romantown/ --recursive --sample=5 --mode=png --seed=42

# Multi-threaded parallel execution (default 4 threads)
python main.py test Mods/romantown/ --recursive --parallel --mode=png

# Specify thread count + keep validation artifacts
python main.py test Mods/romantown/ --recursive --parallel --max-workers=8 --keep
```

Output example:
```
[TEST] Scanning path: Mods/romantown/
[TEST] Found 15 .def files
[TEST] Validation mode: png
[TEST] Execution mode: parallel (4 threads)
──────────────────────────────────────

(1/15) Krakken.def [PASS] 12/12 frames matched (42.3s)
(2/15) mamonth.def [FAIL] 5/8 frames matched (38.7s)
  [DIFF] 4_003.png: 3/25600 pixels differ
...
──────────────────────────────────────
[TEST] Summary: 14/15 passed, 1 failed (total elapsed 89.5s)
[TEST] Failed list:
  [FAIL] mamonth.def: 5/8 frames matched
```

### Batch Processing
```bash
# Recursive unpack entire Mod directory (serial)
python main.py unpack Mods/roman-town/ --recursive --mode=png

# Recursive unpack entire Mod directory (parallel, 4 threads)
python main.py unpack Mods/roman-town/ --recursive --parallel --mode=png

# Recursive unpack (parallel, 8 threads)
python main.py unpack Mods/roman-town/ --recursive --parallel --max-workers=8

# Recursive repack (serial)
python main.py repack Mods/roman-town/ --recursive

# Recursive repack (parallel, 4 threads)
python main.py repack Mods/roman-town/ --recursive --parallel

# Recursive repack (parallel, 8 threads + dithering)
python main.py repack Mods/roman-town/ --recursive --parallel --max-workers=8 --dither
```

Output example (parallel unpack):
```
[INFO] Found 15 .def files
[INFO] Execution mode: parallel (4 threads)
────────────────────────────────────────────
[UNPACK] (1/15) Krakken.def → Krakken/ [OK] (12.3s)
[UNPACK] (2/15) mamonth.def → mamonth/ [OK] (8.7s)
...
[UNPACK] (15/15) FROSTSHOT.DEF → FROSTSHOT/ [FAIL] (0.1s)
[UNPACK] Summary: 14/15 succeeded, 1 failed (total elapsed 18.5s)
```

### Cleanup
```bash
# Preview which unpacked directories would be deleted (no actual deletion)
python main.py clean-unpack Mods/roman-town/ --dry-run

# Actual cleanup
python main.py clean-unpack Mods/roman-town/

# Preview which .def files would be cleaned (keep unpacked dirs)
python main.py clean-def Mods/roman-town/ --dry-run

# Actual cleanup
python main.py clean-def Mods/roman-town/
```

## VCMI Animation Workflow

### Prepare Data for AI Model Training
```bash
# 1. Unpack all creature animations to PNG frames
python main.py unpack Mods/ --recursive --mode=png

# 2. Training data ready for AI models (ControlNet / AnimateDiff, etc.)
```

### AI Generation → DEF Packaging (vcmi-mod-generator Integration)
```bash
# After stage3_5 generates PNG sequence frames:
# Directory structure:
# Content/Sprites/{mod_id}/battle/{cid}/
# ├── meta.json
# │   {
# │     "basepath": "my-mod/battle/creatures/myCreature",
# │     "sequences": [
# │       {"group": 0, "frames": ["0_001.png", "0_002.png", ...]},
# │       {"group": 4, "frames": ["4_001.png", ...]}
# │     ],
# │     "defInfo": {"def_type": 66, ...}
# │   }
# ├── 0_001.png
# ├── 0_002.png
# └── ...

# 3. Repack to .def (enable dithering to improve quality)
python main.py repack Content/Sprites/my-mod/ --dither
```

## basepath Auto-computation

When unpacking in VCMI PNG mode, the `basepath` field in `meta.json` is automatically computed from the DEF file path:

1. Look for the `Sprites` directory in the path
2. Take the relative path after `Sprites` + filename (without `.def`)
3. If `Sprites` directory is not found, fall back to filename

```
Input:  Mods/abyss-town/content/Sprites/abyss/battle/commander/abyssHyppoCommander.def
Output: abyss/battle/commander/abyssHyppoCommander
```

## DEF File Format

This tool is compatible with the DEF format used by VCMI (same as original HoMM3):

```
Offset     Content
0-3        Type identifier (0x47=INTERFACE, 0x42=CREATURE, etc.)
4-11       Width/Height (deprecated)
12-15      totalBlocks (number of groups)
16-783     256-color palette (RGB x 256)
784+       Group table: blockID + frame count + frame names (13 bytes) + frame offsets (4 bytes)
Frame data SSpriteDef(32 bytes) + pixel data (format 0~3)
```

### Pixel Formats

| Format | Description |
|--------|-------------|
| 0 | Uncompressed, raw pixel data |
| 1 | RLE compressed (VSIDE), per-row offset table |
| 2 | Compact segment encoding |
| 3 | Similar to format 2, different row offset calculation |

### Special Colors

The first 8 palette indices (0~7) have special meaning in HoMM3:

| Index | Usage | Alpha |
|:-----:|-------|:-----:|
| 0 | Transparent | 0 |
| 1 | Shadow edge | 64 |
| 2 | Shadow edge (fog of war) | 64 |
| 3 | Shadow body | 128 |
| 4 | Shadow body | 128 |
| 5 | Selection/owner marker | 0 |
| 6 | Shadow body (under selection) | 128 |
| 7 | Shadow edge (under selection) | 64 |

## Multi-language Support

The tool automatically detects your system language and displays messages in the appropriate language:

- **Chinese system** → Chinese output (zh_CN)
- **Other systems** → English output (en_US)

**Override language:**
```bash
python main.py --lang=en_US unpack Mods/ --recursive
python main.py --lang=zh_CN info creature.def
```

**Environment variable:**
```bash
export HOMM3_LANG=en_US  # Linux/macOS
set HOMM3_LANG=en_US     # Windows CMD
$env:HOMM3_LANG="en_US"  # Windows PowerShell
```

**Translation files** are located in `langs/`:
- `langs/zh_CN.json` — Chinese
- `langs/en_US.json` — English

To add a new language, copy `en_US.json` to `{locale}.json`, translate all `messages` values, and submit a PR.

## Dependencies

- Python >= 3.10
- `Pillow` — Image processing

```bash
# Install project + dev tools (recommended)
pip install -r requirements.txt

# Or runtime only
pip install -e .
```

## Development Guide

### Install Dependencies

```bash
# One command to install everything (project + dev tools, recommended)
pip install -r requirements.txt

# Or install separately:
pip install -e .               # Runtime only (Pillow)
pip install -e ".[dev]"        # + Dev tools (ruff, pre-commit)
```

All dependencies are managed centrally in `pyproject.toml` under `[project.optional-dependencies] dev`.

### Code Quality

This project uses [Ruff](https://github.com/astral-sh/ruff) for code checking and formatting:

```bash
# Check for issues
ruff check .

# Auto-fix (formatting, unused imports, syntax upgrades, etc.)
ruff check --fix .

# Code formatting
ruff format .
```

Configuration is in `pyproject.toml`.

### pre-commit hooks (recommended)

Runs checks automatically before each `git commit`:

```bash
pre-commit install
```

Configuration is in `.pre-commit-config.yaml`.

### CI/CD

GitHub Actions pipelines run on every push:

- **Lint** — Ruff checking and formatting validation
- **Test** — Multi Python version testing

Configuration is in `.github/workflows/`.

---

## Project Structure

```
homm3-def-helper/
├── main.py                # Main entry point (includes test batch validation)
├── pyproject.toml         # Project config (Ruff rules, dependencies)
├── .pre-commit-config.yaml # pre-commit hook configuration
├── .github/
│   └── workflows/
│       ├── lint.yml       # Ruff check CI
│       └── test.yml       # Multi-version test CI
├── README.md              # This file (English)
├── README.zh-CN.md        # Chinese documentation
├── langs/
│   ├── zh_CN.json         # Chinese translations
│   └── en_US.json         # English translations
├── docs/
│   └── Features.MD        # Feature design document
└── references/
    └── defs/              # Reference DEF files and test cases
```
