# homm3-def-helper 功能设计

## 简述

1. 支持def文件自动化解包与包装，取代**defPreview**和**H3DefTool**这种GUI工具
2. 支持def文件转成VCMI的PNG动画格式
3. 支持VCMI的PNG动画格式转成def文件
4. 支持def文件的信息查看
5. 支持round-trip自测验证（valid命令）
6. 最终满足VCMI模组全AI生成流程

## info命令

> 查看def文件详细信息

信息输出包括：

```
[INFO] 工作目录: ./vcmi-mod-ai-creation
[INFO] 目标路径: ../Mods/roman-town/content/Sprites/roman-town/battle/creatures/romLvl1.def
DEF 类型:    CREATURE (0x42)
文件大小:    1059828 字节
分组数:      21
调色板:      256 色
  组   0: Moving                 6 帧
         格式=1, 尺寸=82x97, 全尺寸=450x400, 边距=(160,153)
  组   1: Mouse Over            43 帧
         格式=1, 尺寸=67x104, 全尺寸=450x400, 边距=(161,160)
  组   2: Standing              20 帧
         格式=1, 尺寸=64x105, 全尺寸=450x400, 边距=(157,159)
总帧数:      292
```

## unpack命令

> 将Def文件解包成目录
> 支持指定目录，支持递归操作，支持解包模式 `--mode`
> 假设def文件所在目录是 "Mods/roman-town/content/Sprites/roman-town/battle/creatures/romLvl1.def"

输出目录统一使用 def 文件名（不含 .def 后缀），不附加额外后缀。

### 解包模式

#### --mode=png (默认)

全新VCMI的动画格式，由JSON配置+PNG图片即可实现动画。

取def内的组ID作为文件名前缀(例如Moving的组ID为0，那么前缀为"0_")，存放各个动作的png透明图片，并且通过meta.json配置文件来管理。

basepath根据def文件在Sprites目录下的相对路径自动计算。
例如 `.../Sprites/abyss/battle/commander/romLvl1.def` → `abyss/battle/commander/romLvl1`

生成目录 "romLvl1"。

目录文件如下：
```
.
|-- 0_001.png
|-- 0_{更多序号}.png
|-- 4_001.png
|-- 4_{更多序号}.png
`-- meta.json
```

meta.json文件内容如下：
```json
{
  // VCMI模组配置(必要)
  // basepath = Sprites 目录之后的相对路径
  "basepath": "roman-town/battle/creatures/romLvl1",
  "sequences": [
    {
      "group": 0,
      "group_name": "Moving",
      "frames": [
        "0_001.png",
        "0_002.png",
        "0_003.png"
      ]
    },
    {
      "group": 4,
      "group_name": "Defend",
      "frames": [
        "4_001.png",
        "4_002.png",
        "4_003.png"
      ]
    }
    // ...更多分组动画
  ],
  // 原始def信息(VCMI不会使用，仅记录一些原始信息)
  "defInfo": {
    "def_name": "romLvl1.def",
    "def_type": 66,
    "def_type_name": "CREATURE",
    "total_blocks": 21,
    "palette": "hex编码的调色板",
    "basepath": "roman-town/battle/creatures/romLvl1"
  }
}
```

#### --keep-pixels 可选 (PNG 模式)

在 PNG 模式下额外保存 `.pixels.dat` 文件，包含调色板索引原始数据，可用于 round-trip 精度调试。

```
.
|-- meta.json
|-- 0_001.png
|-- 0_001.pixels.dat
|-- 0_002.png
|-- 0_002.pixels.dat
`-- ...
```

#### --mode=bmp

历史homm3的动画格式，需要兼容 **DefPreview** 与 **H3DefTool** 的工程目录模式。

生成8-bit索引BMP + 阴影层(S/) + .hdl 清单文件。

生成目录 "romLvl1"。

目录文件如下：
```
.
|-- Shadow
|   |-- 000.bmp
|   |-- 001.bmp
|   `-- {更多图片}
|-- romLvl1.hdl
|-- 000.bmp
|-- 001.bmp
`-- {更多图片}
```

.hdl文件跟**DefPreview**导出的文件保持一致。

## repack命令

> 将符合规则的目录，包装为def文件
> 支持指定目录, 支持指定输出文件路径，支持包装模式

### 自动检测模式 (--mode=auto, 默认)

检测指定目录下的内容自动选择包装模式：

| 目录内容 | 匹配模式 |
|---------|---------|
| 包含 meta.json (含 basepath + sequences 字段) | PNG → DEF |
| 包含 .hdl 文件 | BMP → DEF |

### 手动指定模式

--mode=png   强制按 PNG 模式打包
--mode=bmp   强制按 BMP 模式打包

### 抖动优化

--dither 参数启用 Floyd-Steinberg 误差扩散抖动，在将AI生成的RGBA PNG转为8-bit索引时大幅减少色带。

## valid命令 (round-trip 验证)

> 自测验证解包和包装是否符合预期
> 流程: unpack → repack → unpack → 逐帧像素对比

### 用法

```bash
# 验证 PNG 模式
python main.py valid creature.def --mode=png

# 验证 BMP 模式
python main.py valid creature.def --mode=bmp

# 保留临时目录 (方便调试)
python main.py valid creature.def --mode=png --keep
```

### 验证流程

```
原始 .def
   ↓ unpack (指定模式)
解包目录 A
   ↓ repack (自动检测)
新 .def
   ↓ unpack (同模式)
解包目录 B
   ↓ 逐帧加载像素数据 → 比较 palette index
验证报告: PASS/FAIL (匹配帧数 / 总帧数)
```

### 验证标准

- PNG模式: 比较各帧PNG的RGBA像素值，通过最近色匹配转为调色板索引后对比
- BMP模式: 比较各帧BMP文件的8-bit索引像素值

## clean-unpack命令

> 检测Def文件所在目录是否有解包的目录，符合则清理掉解包目录
> 支持指定目录，支持递归操作，支持dry-run操作(查看会删哪些目录（不实际删除）)

该命令用于调试用途，清理无用的解包后的目录，只保留原来的def文件。

清理解包目录的必要前提，确保跟def文件同名的目录存在，并且目录下有符合要求的*.json文件或者*.hdl文件。

## clean-def命令

> 检测Def文件所在目录是否有解包的目录，符合则清理掉def文件
> 支持指定目录，支持递归操作，支持dry-run操作(查看会删哪些def文件（不实际删除）)

该命令用于调试用途，清理无用的def文件，只保留符合的解包目录。

清理def文件的必要前提，确保跟def文件同名的目录存在，并且目录下有符合要求的*.json文件或者*.hdl文件。

## 关于basepath的计算

basepath 是 VCMI 动画配置中的关键字段，指向 Sprites 目录后的相对路径。

自动计算规则：
1. 在 DEF 文件路径的父目录链中查找名为 "Sprites" 的目录
2. 取 "Sprites" 之后的相对路径 + 文件名(不含 .def)
3. 如果找不到 "Sprites" 目录，回退到文件名(不含 .def)

示例：
```
Input:  Mods/abyss-town/content/Sprites/abyss/battle/commander/abyssHyppoCommander.def
Output: abyss/battle/commander/abyssHyppoCommander
```
