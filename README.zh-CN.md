# HoMM3 / VCMI DEF 文件工具

解包和打包英雄无敌 3 / VCMI 的 `.def` 动画精灵文件。

## 功能

### 核心命令
- **`info`** — 查看 `.def` 文件结构信息（类型、分组、帧尺寸）
- **`unpack`** — 将 `.def` 文件解包为帧图像 + 元数据
- **`repack`** — 从帧图像 + 元数据重新打包为 `.def` 文件
- **`valid`** — round-trip 自测验证 (unpack → repack → unpack → 像素对比)
- **`test`** — 批量 round-trip 验证测试 (递归扫描/随机抽样/并行执行)
- **`clean-unpack`** — 清理所有解包目录
- **`clean-def`** — 清理有对应解包目录的 `.def` 文件

### 两种解包/打包模式

| 模式 | 参数 | 用途 | 输出格式 |
|------|------|------|----------|
| **PNG (VCMI)** | `--mode=png` (默认) | VCMI 原生动画格式 / AI 训练素材 | `meta.json` + `{group}_{frame}.png` |
| **BMP** | `--mode=bmp` | H3DefTool / DefPreview 兼容 | `.hdl` + 8-bit BMP + `Shadow/` |

### 核心特性

- 支持 4 种像素格式（格式 0~3）解码
- **Floyd-Steinberg 抖动** — 改善 AI 生成 PNG 封包时的色带问题
- **自动格式检测** — `repack` 根据目录内容自动识别格式
- **自定义调色板** — 保留原始 DEF 的调色板，支持全新生成使用标准调色板
- `.pixels.dat` **可选** — PNG 模式下 `--keep-pixels` 参数可额外保存用于调试
- **round-trip 自测** — `valid` 命令验证解包→打包→解包像素一致性
- 批量递归处理 — 支持 Mod 目录级别的批量操作

## 用法

### info
```bash
python main.py info creature.def
```

### unpack — PNG 模式 (默认)
```bash
# 解包为 VCMI 原生 PNG 格式
python main.py unpack creature.def
python main.py unpack creature.def --mode=png

# 输出结构 (目录名 = def 文件名, 不含 .def):
# creature/
# ├── meta.json           # VCMI 动画配置文件 (basepath 自动计算)
# ├── 0_001.png           # Moving 组第 1 帧
# ├── 0_002.png           # Moving 组第 2 帧
# ├── 4_001.png           # Defend 组第 1 帧
# └── ...
```

### unpack — PNG 模式 + .pixels.dat (调试用)
```bash
# 解包为 PNG 格式并额外保存 .pixels.dat (调色板索引原始数据)
python main.py unpack creature.def --mode=png --keep-pixels

# 输出结构:
# creature/
# ├── meta.json
# ├── 0_001.png
# ├── 0_001.pixels.dat    # 调色板索引原始数据 (用于 round-trip 调试)
# ├── 0_002.png
# ├── 0_002.pixels.dat
# └── ...
```

### unpack — BMP 模式 (H3DefTool 兼容)
```bash
# 解包为 8-bit 索引 BMP + .hdl
python main.py unpack creature.def --mode=bmp

# 输出结构:
# creature/
# ├── romLvl1.hdl
# ├── Shadow/
# │   ├── 000.bmp
# │   └── ...
# ├── 000.bmp
# └── ...
```

### repack (自动检测模式)
```bash
# 从 VCMI PNG 目录打包 (自动检测 meta.json)
python main.py repack creature/

# 从 BMP/HDL 目录打包 (自动检测 .hdl)
python main.py repack creature/

# 手动指定模式
python main.py repack creature/ --mode=bmp

# 启用 Floyd-Steinberg 抖动
python main.py repack creature/ --dither
```

### valid (round-trip 自测验证)
```bash
# 验证 PNG 模式
python main.py valid creature.def --mode=png

# 验证 BMP 模式
python main.py valid creature.def --mode=bmp

# 保留验证产物 (方便对比分析)
python main.py valid creature.def --mode=png --keep
```

验证流程: unpack → repack → unpack → 逐帧像素对比，输出 PASS/FAIL 报告。

验证产物保存在原 .def 文件所在目录：
```
creature.def
creature/              ← 原始解包目录 (已存在或本次创建)
creature-Valid.def     ← 重新打包后的 def (--keep 保留)
creature-Valid/        ← 再次解包目录   (--keep 保留)
```

不指定 `--keep` 时，验证完成后自动清理 `-Valid.def` 和 `-Valid/` 产物。

### test (批量 round-trip 验证测试)

对单个 `.def` 文件或整个 Mod 目录批量执行 round-trip 验证。

```bash
# 单文件测试
python main.py test creature.def --mode=png

# 递归测试整个 Mod 目录
python main.py test Mods/romantown/ --recursive --mode=png

# 递归测试 + 随机抽样 5 个 (--seed 确保可复现)
python main.py test Mods/romantown/ --recursive --sample=5 --mode=png --seed=42

# 多线程并行执行 (默认 4 线程)
python main.py test Mods/romantown/ --recursive --parallel --mode=png

# 指定线程数 + 保留验证产物
python main.py test Mods/romantown/ --recursive --parallel --max-workers=8 --keep
```

输出示例:
```
[TEST] 扫描路径: Mods/romantown/
[TEST] 发现 15 个 .def 文件
[TEST] 验证模式: png
[TEST] 执行方式: 并行 (4线程)
──────────────────────────────────────

(1/15) Krakken.def [PASS] 12/12 帧匹配 (42.3s)
(2/15) mamonth.def [FAIL] 5/8 帧匹配 (38.7s)
  [DIFF] 4_003.png: 3/25600 像素不同
...
──────────────────────────────────────
[TEST] 汇总: 14/15 通过, 1 失败 (总耗时 89.5s)
[TEST] 失败列表:
  [FAIL] mamonth.def: 5/8 帧匹配
```

### 批量处理
```bash
# 递归解包整个 Mod 目录 (串行)
python main.py unpack Mods/roman-town/ --recursive --mode=png

# 递归解包整个 Mod 目录 (并行, 4线程)
python main.py unpack Mods/roman-town/ --recursive --parallel --mode=png

# 递归解包 (并行, 8线程)
python main.py unpack Mods/roman-town/ --recursive --parallel --max-workers=8

# 递归打包 (串行)
python main.py repack Mods/roman-town/ --recursive

# 递归打包 (并行, 4线程)
python main.py repack Mods/roman-town/ --recursive --parallel

# 递归打包 (并行, 8线程 + 抖动)
python main.py repack Mods/roman-town/ --recursive --parallel --max-workers=8 --dither
```

输出示例 (并行解包):
```
[INFO] 发现 15 个 .def 文件
[INFO] 执行方式: 并行 (4线程)
────────────────────────────────────────────
[UNPACK] (1/15) Krakken.def → Krakken/ [OK] (12.3s)
[UNPACK] (2/15) mamonth.def → mamonth/ [OK] (8.7s)
...
[UNPACK] (15/15) FROSTSHOT.DEF → FROSTSHOT/ [FAIL] (0.1s)
  [FAIL] FROSTSHOT.DEF: 文件格式不支持
────────────────────────────────────────────
[UNPACK] 汇总: 14/15 成功, 1 失败 (总耗时 18.5s)
```

### 清理
```bash
# 查看会删哪些解包目录 (不实际删除)
python main.py clean-unpack Mods/roman-town/ --dry-run

# 实际清理
python main.py clean-unpack Mods/roman-town/

# 查看会删哪些 .def 文件 (保留解包目录)
python main.py clean-def Mods/roman-town/ --dry-run

# 实际清理
python main.py clean-def Mods/roman-town/
```

## VCMI 动画工作流

### 为 AI 模型训练准备素材
```bash
# 1. 解包所有生物动画为 PNG 帧
python main.py unpack Mods/ --recursive --mode=png

# 2. 训练素材可直接用于 AI 模型 (ControlNet / AnimateDiff 等)
```

### AI 生成 → DEF 封包 (vcmi-mod-generator 集成)
```bash
# stage3_5 生成 PNG 序列帧后:
# 目录结构:
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

# 3. 打包为 .def (启用抖动改善质量)
python main.py repack Content/Sprites/my-mod/ --dither
```

## basepath 自动计算

在 VCMI PNG 模式下解包时，`meta.json` 中的 `basepath` 字段自动根据 DEF 文件路径计算：

1. 在路径中查找 `Sprites` 目录
2. 取 `Sprites` 之后的相对路径 + 文件名（不含 `.def`）
3. 如果找不到 `Sprites` 目录，回退到文件名

```
Input:  Mods/abyss-town/content/Sprites/abyss/battle/commander/abyssHyppoCommander.def
Output: abyss/battle/commander/abyssHyppoCommander
```

## DEF 文件格式

本工具兼容 VCMI 使用的 DEF 格式（与 HoMM3 原版一致）：

```
偏移     内容
0-3      类型标识 (0x47=INTERFACE, 0x42=CREATURE 等)
4-11     宽/高 (已废弃)
12-15    totalBlocks (分组数)
16-783   256 色调色板 (RGB x 256)
784+     分组表: blockID + 帧数 + 帧名(13字节) + 帧偏移(4字节)
帧数据   SSpriteDef(32字节) + 像素数据(格式0~3)
```

### 像素格式

| 格式 | 说明 |
|------|------|
| 0 | 未压缩，直接像素数据 |
| 1 | RLE 压缩 (VSIDE)，每行有偏移表 |
| 2 | 紧凑段编码 |
| 3 | 类似格式 2，不同行偏移计算 |

### 特殊颜色

调色板前 8 个索引（0~7）在 HoMM3 中有特殊含义：

| 索引 | 用途 | Alpha |
|:----:|------|:-----:|
| 0 | 透明 | 0 |
| 1 | 阴影边框 | 64 |
| 2 | 阴影边框 (战争迷雾) | 64 |
| 3 | 阴影主体 | 128 |
| 4 | 阴影主体 | 128 |
| 5 | 选择/所有者标记 | 0 |
| 6 | 阴影主体 (选择之下) | 128 |
| 7 | 阴影边框 (选择之下) | 64 |

## 依赖

- Python >= 3.10
- `Pillow` — 图像处理

```bash
# 安装项目 + 开发工具（推荐）
pip install -r requirements.txt

# 或仅安装项目运行时
pip install -e .
```

## 开发指南

### 依赖安装

```bash
# 一键安装所有依赖 (项目 + 开发工具，推荐)
pip install -r requirements.txt

# 或分别安装:
pip install -e .               # 仅项目运行时 (Pillow)
pip install -e ".[dev]"        # + 开发工具 (ruff, pre-commit)
```

所有依赖声明统一在 `pyproject.toml` 的 `[project.optional-dependencies]` dev 分组中管理。

### 代码质量检查

本项目使用 [Ruff](https://github.com/astral-sh/ruff) 进行代码检查与格式化：

```bash
# 检查代码问题
ruff check .

# 自动修复 (格式化、未用 import、旧语法升级等)
ruff check --fix .

# 代码格式化
ruff format .
```

所有配置位于项目根目录的 `pyproject.toml` 中。

### pre-commit hooks (推荐)

安装后每次 `git commit` 前自动运行检查和格式化：

```bash
pre-commit install
```

配置位于 `.pre-commit-config.yaml`。

### CI/CD

代码推送时会自动运行 GitHub Actions 流水线：

- **Lint** — Ruff 检查和格式化校验
- **Test** — 多 Python 版本测试

配置位于 `.github/workflows/`。

---

## 项目结构

```
homm3-def-helper/
├── main.py                # 工具主入口 (含 test 批量验证子命令)
├── pyproject.toml         # 项目配置 (Ruff 规则、依赖声明)
├── .pre-commit-config.yaml # pre-commit 钩子配置
├── .github/
│   └── workflows/
│       ├── lint.yml       # Ruff 检查 CI
│       └── test.yml       # 多版本测试 CI
├── README.md              # 本文件 (中文)
├── README.en.md           # 英文文档
├── docs/
│   └── Features.MD        # 功能设计文档
├── langs/
│   ├── zh_CN.json         # 中文翻译
│   └── en_US.json         # 英文翻译
└── references/
    └── defs/              # 参考 DEF 文件和测试用例
```
