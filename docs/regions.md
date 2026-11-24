# 区域归属与批次交付契约

区域链在**不改变整板 `process(data)` 语义**的前提下，把已通过原筛选的整板对象像素归属到全批共享的空间区域目录，并交付标本-区域记录与跨标本汇总。区域栅格是组织学归属层：它不是 mask，不参与连通、孔洞、触边或筛选；区域边界也不是图像边界。

公开入口：

```python
from micro_objects import deliver_regions

delivery = deliver_regions(region_catalog, specimens)
```

`attribute_regions`、`deliver_region_batch`、`build_region_delivery` 与 `compile_region_delivery` 是同一入口的别名。入口也接受一个批次字典：`{"region_catalog": [...], "specimens": [...]}`。

本能力只用 Python 标准库；没有界面、网络服务或真实图像导入。

## 输入

### `region_catalog`

有序列表，长度 1–64；输入顺序没有业务意义。每个 `region_id` 必须是：

- 字符串；
- 长度 1–64；
- 仅含 ASCII 字母、数字、下划线 `_` 或连字符 `-`；
- 全批唯一。

也接受仅含 `region_id`（或 `id`）一个字段的目录项字典。

### `specimens`

列表长度 1–32，每个标本是字典，规范字段如下：

| 字段 | 要求 |
| --- | --- |
| `specimen_id` | 非空字符串，批次内唯一；稳定排序按字符串码点顺序。 |
| `data` | 原样传给 `micro_objects.process(data)` 的整板数据；板宽高仍为 1–256，所有原错误语义保留。也允许把 `mask`、`intensity`、`pixel_size`、`connectivity`、`selection` 五个字段直接平铺在标本字典中。 |
| `region_grid` | 与 `process(data)` 整板结果同尺寸的列表；外层行数等于高，每行列数等于宽。每格为目录中的 `region_id` 或 JSON `null`（Python `None`）。 |
| `observed_regions` | 该标本确实采集到的目录区域 ID 列表；可以为空列表。顺序无意义，重复 ID 按同一已观测区域去重。 |

兼容字段名：整板数据也可用 `board`；栅格也可用 `region_raster`、`region_mask`、`region_map` 或 `grid`。这些别名只是调用便利，JSON 文档字段以下文规范名为准。

校验规则：

1. 标本 ID、目录 ID 不得重复。
2. `observed_regions` 中的 ID 必须在目录中。
3. 栅格非空值必须是合法目录 ID，并且必须在该标本的 `observed_regions` 中。
4. 每个 `observed_regions` 声明的区域必须至少在栅格中出现一格。
5. 未观测区域不得出现在栅格中。
6. 栅格尺寸必须与整板一致。
7. 非法字段、布尔冒充整数，以及 `process(data)` 拒绝的整板数据均令整批失败。

任何校验失败都抛 `RegionDeliveryError`（是 `ValueError` 子类；`RegionError` 等为别名）。入口是纯函数：成功时才构造并返回完整 `RegionDelivery`，失败时没有部分返回。需要落盘时可使用原子的 `micro_objects.io.publish`，或使用会先做守恒校验的 `micro_objects.io.save_region_delivery` / `load_region_delivery`。

## 稳定排序与对象身份

- 目录、区域记录和汇总按 `region_id` 的 Unicode/ASCII 字典序排序。
- 标本按 `specimen_id` 排序。
- 标本内对象按原整板行优先首像素顺序（即原 `label` 顺序）排列。
- 全局对象记录按 `(specimen_id, first_y, first_x)` 排序。

获选对象键固定为：

```text
[specimen_id, first_x, first_y]
```

`first_x`、`first_y` 取自原 `process(data)` 对象 `pixels[0]`；`pixels` 已按 `(y, x)` 排序。不得使用临时 label、区域内首像素或质心代替。输出中的 `label` 只是保留的原整板度量字段，不是跨区域身份。

## 顶层交付

`RegionDelivery` 是 `dict` 子类，规范键：

| 键 | 内容 |
| --- | --- |
| `format` | `micro-objects-region-delivery/1`。 |
| `region_catalog` | 排序后的全部目录 ID。 |
| `specimens` | 排序后的标本交付记录。 |
| `objects` | 所有标本中通过原筛选的对象归属记录，全局确定排序。 |
| `object_keys` | 与 `objects` 同序的对象键。 |
| `object_region_contributions` | 所有像素数大于零的对象-区域贡献记录。 |
| `specimen_region_records` | 全部“标本 × 目录区域”记录，共 `标本数 × 区域数` 条。 |
| `region_summaries` | 每个目录区域一条跨标本汇总，按 `region_id` 排序。 |
| `complete_region_ids` / `incomplete_region_ids` | 完整/缺测区域 ID 列表。 |
| `null_allocation` | 全批获选像素落在 `null` 格的总数和强度和。 |
| `summary` | 标本数、区域数、对象数、贡献数、完整区域数和 `null` 总量。 |

Python 字典还支持 `delivery.specimen(sid)`、`delivery.region_summary(rid)`、`delivery.object_record(key)` 和 `delivery.verify_conservation()`。

## 标本与对象记录

### 标本记录 `specimens[]`

| 键 | 含义 |
| --- | --- |
| `specimen_id` | 标本 ID。 |
| `observed_regions` / `missing_regions` | 排序后的已观测/未观测目录区域。 |
| `process_result` | `process(data)` 的原样完整结果；连通、孔洞、触边、筛选和错误语义均以此为准。 |
| `objects` | 仅含通过原筛选对象的区域归属记录。 |
| `object_keys` | 本标本获选对象键。 |
| `object_region_contributions` | 本标本所有正像素对象-区域贡献。 |
| `region_records` | 本标本每个目录区域一条记录。 |
| `null_allocation` | 本标本获选像素落在 `null` 格的 `pixel_count`、`intensity_sum`、`area`、`physical_area`。 |
| `null_pixel_count`、`null_intensity_sum`、`null_area` | 同一 `null_allocation` 总量的直接标量字段。 |

### 对象记录 `objects[]`

对象记录首先展开原 `process(data)` 对象的全部度量字段，包括 `pixel_count`、`area`、`intensity_sum`、`bbox`、`centroid`、`holes`、`touches_border`、`accepted` 和 `rejection_reasons`，另加：

- `specimen_id`；
- `object_key`、`object_id`/`key`（同键）、`first_x`、`first_y`、`first_pixel: [first_x, first_y]`；
- `region_contributions`：该对象在各区域的正像素贡献，按 `region_id` 排序；
- `unassigned`：该对象落在 `null` 格的部分；
- `unassigned_pixel_count`、`null_pixel_count`、`unassigned_intensity_sum`、`null_intensity_sum`、`unassigned_area`、`null_area`。

一件整板对象始终只有一条对象记录。跨多个区域只产生多条贡献，不复制或拆分对象。

### 对象-区域贡献 `region_contributions[]`

| 字段 | 含义 |
| --- | --- |
| `object_key` | `[specimen_id, first_x, first_y]`。 |
| `specimen_id` / `region_id` | 标本和区域冗余键，便于展开表消费。 |
| `pixel_count` | 该对象落在该区域栅格中的像素数，恒大于零。 |
| `intensity_sum` | 这些像素的原始强度整数和。 |
| `area` / `physical_area` | 这些像素的物理面积，精确 `Fraction` 字符串。 |
| `ratio` / `fraction` | `pixel_count / 整对象 pixel_count` 的精确分数，例如 `"2/3"`。 |
| `ratio_numerator` / `ratio_denominator` | 同一比例的最简整数分子、分母。 |

`unassigned` 使用同一组计数、强度、面积和比例字段；即使为零也输出。未分配部分不是背景，也不能丢弃。

## 标本-区域记录

每个“标本 × 目录区域”都有一条 `region_records[]` 记录，按 `region_id` 排序。

已观测区域（即使没有任何获选前景像素）：

```json
{
  "observed": true,
  "status": "observed",
  "missing": false,
  "pixel_count": 0,
  "area": "0",
  "physical_area": "0",
  "intensity_sum": 0,
  "object_count": 0,
  "object_region_count": 0,
  "contribution_count": 0
}
```

未观测区域标记为缺测，数值字段为 JSON `null`，不能伪造成零：

```json
{
  "observed": false,
  "status": "missing",
  "missing": true,
  "pixel_count": null,
  "area": null,
  "physical_area": null,
  "intensity_sum": null,
  "object_count": null,
  "object_region_count": null,
  "contribution_count": null
}
```

这里的 `object_count` 是**对象-区域贡献数**（`object_region_count` / `contribution_count` 为同义字段），不是跨标本去重后的生物对象数。

## 跨标本区域汇总 `region_summaries[]`

每个目录区域一条：

| 键 | 含义 |
| --- | --- |
| `region_id` | 区域 ID。 |
| `observed_specimen_ids` / `missing_specimen_ids` | 排序后的已观测/缺测标本 ID，二者划分整批标本。 |
| `observed_specimen_count` / `missing_specimen_count` / `total_specimen_count` | 对应计数。 |
| `complete` | 仅当批次中每个标本都观测该区域（缺测列表为空）时为 `true`。 |
| `pixel_count` | 仅对已观测标本的标本-区域像素数求和。 |
| `intensity_sum` | 仅对已观测标本的强度和求和。 |
| `area` / `physical_area` | 仅对已观测标本的面积精确求和。 |
| `object_count` / `object_region_count` / `contribution_count` | 对象-区域贡献数之和。 |

`complete=false` 时仍交付已观测部分的数值；空的已观测集合之和为整数零/`"0"`，但 `missing_specimen_ids` 必须保留。消费端不能把该部分和误读为全批完整总量。

## 精确表示与守恒

所有计数和强度和均为 Python `int` / JSON 整数。物理面积和比例不使用二进制浮点：

- `area` 沿用 `process(data)` 的 `Fraction` 字符串口径，可用 `fractions.Fraction(area)` 往返解析；
- `ratio` 是最简分数字符串，如 `"2/3"`，并另给整数分子/分母；
- 小数 `pixel_size`（如 `["0.1", "0.2"]`）产生 `"1/25"`、`"1/50"` 这样的精确面积。

对每个获选对象 `o`，恒有：

```text
Σ contribution.pixel_count + unassigned.pixel_count = o.pixel_count
Σ contribution.intensity_sum + unassigned.intensity_sum = o.intensity_sum
Σ Fraction(contribution.area) + Fraction(unassigned.area) = Fraction(o.area)
Σ Fraction(contribution.ratio) + Fraction(unassigned.ratio) = 1
```

每个标本-区域记录等于该标本在该区域的全部贡献之和；每个跨标本汇总等于全部已观测标本-区域记录之和。`verify_region_delivery(delivery)` 或 `delivery.verify_conservation()` 会核对这些等式。

## 示例与测试

合成示例：

```sh
.venv/bin/python -I examples/run_regions.py
```

它创建被 `.gitignore` 忽略的 `run/regions/region-delivery.json`，演示验收板的 `2/3`、`1/3` 贡献、第二标本的观测零值、B 区域缺测，以及补观测后的完整标志。

区域专项自动化测试：

```sh
.venv/bin/python -I -m unittest tests.test_regions -v
```

全量测试：

```sh
.venv/bin/python -I -m unittest discover -s tests -v
```
