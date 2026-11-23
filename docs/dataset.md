# 整板样本编译、盲测保留与强度校准契约

本文档在 [`contract.md`](contract.md) 的原 `process(data)` 契约之上新增一条数据处理链：把 1–8 张带实例掩膜的合成纹理板按重叠裁片请求编译成训练/盲测样本，并拟合一份批次级强度校准记录。不训练模型、不做界面、不联网，仅使用 Python 标准库；整板连通与筛选仍由原 `micro_objects.process` 在整图上完成，裁片内从不重做连通或边缘筛选。

## 1. 批次输入

`micro_objects.compile_dataset(boards, requests)` 一次编译一个批次。

### 1.1 板

`boards` 为 1–8 项列表，每项是 `{"board_id", "data"}`：

| 字段 | 约束 |
| --- | --- |
| `board_id` | 1–64 个 ASCII 字母、数字、`_` 或 `-`，批次内唯一 |
| `data` | `process(data)` 的原始输入对象（`mask`、`intensity`、`pixel_size`、`connectivity`、`selection`），按原契约严格校验 |

每张板独立调用一次 `process`，确定整板对象、首像素身份与原筛选结果。整板数据错误包装为 `DatasetCompileError`（仍是 `ValueError`）并整体拒绝该批次。

### 1.2 裁片请求

`requests` 为 1–1024 项列表，每项字段集合固定为 `request_id`、`board_id`、`rect`、`kind`：

| 字段 | 约束 |
| --- | --- |
| `request_id` | 同上 ID 字符集，**全批唯一**（跨板也不得重复） |
| `board_id` | 必须引用批次内存在的板 |
| `rect` | `[x0, y0, x1, y1]` 整数列表，左闭右开的整板像素矩形；非空且完全位于该板上，坐标拒绝布尔 |
| `kind` | 字符串常量 `POSITIVE`（`"positive"`）、`NEGATIVE`（`"negative"`）、`BLIND`（`"blind"`）之一 |

约束汇总：至少一个盲测请求和一个训练请求；请求矩形允许任意重叠，同范围不同请求 ID 合法。坏字段、重复 ID、未知板、越界矩形、整板数据错误、缺少盲测或训练请求均抛 `DatasetCompileError`。板与请求的输入顺序不改变任何请求 ID 的结果或校准记录。

### 1.3 盲测保留区

每张板上所有 `BLIND` 矩形的**并集**在任何发出判断之前预先保留，与其他裁片是否发出无关：

- 训练裁片落在保留并集内的格为「训练保留格」：强度输出 `null`、监督固定 `-1`，不转交该格的原始掩膜、强度或任何未隐藏的派生值；
- 盲测裁片可以读取其整个请求窗口，保留边界不是人为掩膜，不因此遮挡盲测裁片；
- 重叠盲测矩形自然合并，只按并集保留。

## 2. 样本语义

样本是与请求矩形同尺寸的两张栅格：校准强度栅格（保留格为 `null`）与监督栅格。监督取值：背景 `0`、忽略 `-1`、正整数为该裁片内的实例编号。

### 2.1 可见范围与对象完整性

对训练裁片，可见范围 = 请求矩形减去盲测并集；对盲测裁片，整个窗口均可见。

一个整板对象只有同时满足以下条件才能在该裁片中获得正编号：

1. 通过该板的**原筛选**（`accepted`）；
2. 其**全部像素**都落在该裁片的可见范围（矩形内且不被保留）。

否则其在裁片内可见的前景一律标 `-1`（强度仍正常给出）。裁片内不重新做连通分析，也不使用临时数字标签充当实例身份。

### 2.2 编号与身份对应

编号从 1 起，按各整板对象首像素（整板光栅顺序的第一个前景像素 `[x, y]`）的行优先次序赋号。每个样本的 `objects` 字段返回编号与对象身份的对应：

```json
{"label": 1, "board_id": "b0", "first_pixel": [6, 1]}
```

身份是板 ID 与整板首像素，与裁片内新连通块、原过程的临时数字标签均无关。

### 2.3 发出规则

- 正例：至少有一个完整获选对象才发出，否则记录原因 `no_complete_object`；
- 负例：必须有可见背景，且不能有任何可见前景——即使该前景未通过整板筛选或会被标为忽略。两问题同时存在时先报告 `visible_foreground`；只有前景缺失之外的纯保留窗口才报告 `no_visible_background`；
- 盲测：总是发出，允许全忽略。

编译结果的 `not_emitted` 属性按请求 ID 告知未发出的训练请求及原因（常量 `NO_COMPLETE_OBJECT`、`VISIBLE_FOREGROUND`、`NO_VISIBLE_BACKGROUND`）。盲测请求不出现在该映射中。

## 3. 校准拟合与应用

### 3.1 一份批次记录

一个批次只拟合一份校准记录。拟合集合严格定义为：**训练侧实际发出的所有裁片中监督为 0 的格**，按「板 ID、整板坐标」去重，每格的原始强度计一次；不同板的同坐标独立计数。

以下格不得参与：未发出裁片独占的背景、盲测格、忽略格（保留格与残缺前景）以及任何前景。不以全部可见训练矩形或全部非盲测像素代替真正交付的背景。

取该多重集按值排序后的**下中位数**（偶数个取中间较小值，即索引 `(n-1)//2`）。没有任何训练背景时整批编译失败，包括盲测在内不返回半套样本。

记录为独立、可保存、可读回的数据文档：

```json
{"format": "micro-objects-calibration/1", "baseline": 20, "background_cells": 3}
```

`background_cells` 是参与拟合的去重格数，`baseline` 是下中位数整数。

### 3.2 样本校准与新矩阵入口

所有发出的样本（含盲测）用同一记录计算「原强度 − 基线」，保留负值、不作截断；`null` 保持 `null`。

`micro_objects.apply_calibration(record, matrix)` 是给只拿到新强度矩阵的下游的公开入口：

- `matrix` 为非空等宽行列表，单元格是原强度范围 0–65535 的整数（拒绝布尔）或 `null`；
- 不要求掩膜、不在新矩阵上重新拟合；
- 整数输出 `value - baseline`（可为负），`null` 原样透传；
- 记录或矩阵形状/取值不合法抛 `DatasetCompileError`。

`parse_calibration(record)` 对读回的记录做严格结构校验（字段集合、格式标识、基线 0–65535、格数为正）。`micro_objects.io.save_calibration(path, record)` 与 `load_calibration(path)` 以规范 JSON 原子写入/读取该记录。

## 4. 读取接口与内存边界

### 4.1 公开入口

```python
from micro_objects import (
    compile_dataset, apply_calibration, parse_calibration,
    POSITIVE, NEGATIVE, BLIND,
    NO_COMPLETE_OBJECT, VISIBLE_FOREGROUND, NO_VISIBLE_BACKGROUND,
)
from micro_objects.io import save_calibration, load_calibration

compiled = compile_dataset(boards, requests)   # 先整批校验、发出判断与拟合
compiled.request_ids()                          # 全部请求 ID（排序）
compiled.emitted_ids()                          # 可读取样本的请求 ID（排序）
compiled.not_emitted                            # {"request_id": reason}
compiled.calibration                            # 校准记录的深拷贝
sample = compiled.get_sample("p1")              # 按 ID 取一片
compiled.storage_stats()                        # 保留数据量（输入口径）
```

`get_sample(request_id)`：未知 ID 抛 `KeyError`；已知但未发出的训练请求抛 `DatasetCompileError`；盲测与已发出训练请求返回全新的独立列表，字段为 `request_id`、`board_id`、`kind`、`rect`、`intensity`、`supervision`、`objects`。同一成功批次可按任意顺序多次读取。

### 4.2 不可变性

编译时即从输入构建冻结的整板平面与不可变元组；调用者之后修改传入的板、请求或任何已返回样本，都不能改变后续读取结果。整板原图与编译内部工作数据不作为训练样本的一部分交付。

### 4.3 内存随输入增长而非随裁片面积增长

编译端保留：每板一份冻结强度面、标签面、对象像素集合与盲测并集位图（`bytearray`），以及请求记录（至多 1024 条）。这些数据随**整板总像素数与请求数**增长，不随全部裁片面积之和增长——即使重叠请求的输出格总量接近 6700 万，也不预展开任何裁片栅格（`storage_stats()["retained_crop_grids"] == 0`）。`get_sample` 按需渲染，调用期间只增加一个裁片大小的临时空间；不包括调用方自己另存的输出。本模块不实现联网服务，也不持久化整个批次，仅校准记录可单独存档。
