# 瓦片编译、离线增量与下游审阅契约

本文档在 [`contract.md`](contract.md) 的原 `process(data)` 契约之上定义三条新增数据处理链：瓦片快照编译、自包含对象增量、单写者下游审阅库。全部输入输出均为合成数据，无界面、无联网服务；下游导入不读取任何瓦片，也不向上游发起请求。

## 1. 快照与画布编译

### 1.1 快照字段

| 字段 | 约束 |
| --- | --- |
| `dataset_id`、`version_id` | 1–64 个 ASCII 字母、数字、`_` 或 `-` |
| `width`、`height` | 整数 1–256（拒绝布尔） |
| `pixel_size` | 两个正十进制字符串，沿用原 `process` 口径 |
| `connectivity` | 整数 `4` 或 `8` |
| `selection` | `{"min_pixels": 1..65536, "exclude_border": bool}` |
| `tiles` | 0–32 块瓦片的列表，瓦片 ID 在快照内唯一 |

瓦片字段为 `tile_id`（同 ID 字符集）、`x`/`y`（非负整数，左上角）、`priority`（0–65535 整数）、非空等宽 `mask`（行字符串，字符仅 `0`、`1`、`?`）和同尺寸 `intensity`（0–65535 整数，拒绝布尔）。瓦片必须完整落在画布内，否则整体拒绝。

### 1.2 获胜规则与来源面

对每个画布位置，在覆盖它且掩膜字符不是 `?` 的瓦片中：

1. `priority` 最大者获胜；
2. 同级时取字典序最小的 `tile_id`；
3. `?` 不遮挡任何下层像素；
4. 获胜的 `0` 会遮掉下层的 `1`（显式背景），未被任何瓦片观测的位置才是无来源背景。

解析与瓦片输入顺序无关。`micro_objects.compile_snapshot(snapshot)` 返回：

- `mask`：每行 `0/1` 字符串，未观测位置按背景 `0` 参与处理；
- `intensity`：获胜像素的实际强度矩阵；
- `sources`：与画布同尺寸，每点为 `null`（从未观测）或
  `{"tile_id": <id>, "tile_coord": [tx, ty]}`（瓦片内零基坐标）。获胜的显式背景同样携带来源；
- `result`：对拼好的整图调用原 `micro_objects.process` 的完整结果，输入、输出、错误与度量口径（含 2,000,000 拓扑工作预算）完全不变；
- 以及回显的 `dataset_id`、`version_id`、尺寸、`pixel_size`、`connectivity`、`selection`。

连通分量、互补连通孔洞、触边判断、筛选与拓扑预算一律在拼好的整图上计算，从不先按瓦片选对象再拼接。

## 2. 确定性编码与摘要

### 2.1 规范编码

所有摘要使用同一规范 JSON 编码：

```text
canonical_bytes(x) = json.dumps(
    x, ensure_ascii=False, sort_keys=True,
    separators=(",", ":"), allow_nan=False).encode("utf-8")
digest(x) = sha256(canonical_bytes(x)).hexdigest()   # 64 位小写十六进制
```

- 记录摘要 `record_digest(record) = digest(record)`；
- 空交付清单摘要 `EMPTY_DIGEST = digest([])`（空清单仍是空 JSON 列表，不是空字节的哈希）。

### 2.2 对象键与交付记录

对象键是该对象在**整图光栅顺序**下的第一个前景像素 `[x, y]`（对外保持 `[x,y]`），不是原输出中的临时数字标签。仅交付通过原筛选的对象。记录结构：

```json
{
  "key": [1, 1],
  "pixel_size": ["1", "1"],
  "connectivity": 4,
  "pixels": [[1, 1], [2, 1], [3, 1]],
  "geometry_topology_intensity": {
    "pixel_count": 3, "bbox": [1, 1, 4, 2], "area": "3",
    "edge_perimeter": "8", "centroid": ["2", "3/2"],
    "intensity_sum": 30, "mean_intensity": "10",
    "holes": 0, "touches_border": false
  },
  "pixel_sources": [
    {"coord": [1, 1], "intensity": 10,
     "source": {"tile_id": "low", "tile_coord": [1, 1]}}
  ]
}
```

记录不含数字标签；指标块沿用原度量字段。摘要因此天然不受以下变化影响：等值的像素尺寸写法、瓦片输入顺序、无关数字标签变化。像素尺寸字符串变化、任何像素、强度、来源或几何/拓扑指标变化都会改变记录摘要。

### 2.3 清单摘要

版本交付清单是按 `(y, x)` 光栅排序的 `{"key", "record_digest"}` 列表；清单摘要为该列表的 `digest(...)`，只由对象键及其记录摘要确定。

## 3. 离线增量

`micro_objects.build_delta(previous, current)` 比较同一数据集的两个快照（已编译或原始快照均可）；首次发布传 `previous=None`。增量文档（格式标识 `micro-objects-delta/1`）字段：

| 字段 | 含义 |
| --- | --- |
| `format` | 固定格式标识 |
| `dataset_id` | 数据集 ID |
| `from_version_id` / `to_version_id` | 前后版本 ID；首版为 `null` |
| `from_manifest_digest` / `to_manifest_digest` | 两版**完整**交付清单摘要；首版前清单为 `EMPTY_DIGEST` |
| `upserts` | 新增键或同键内容变化的**完整**记录，含 `key`、`record_digest`、`record`，按光栅排序 |
| `deletes` | 离开交付集的键列表 |
| `delta_digest` | 除该字段外整个文档的规范编码摘要 |

比较规则：

- 键相同且记录摘要相同：不发送该键；
- 新键或同键摘要变化：发送完整记录；
- 旧键在新版不存在：发送删除；
- 分裂与合并只按键与记录内容比较，不按重叠面积猜测审阅归属；
- 没有对象变化也可以发布新版本：两个清单摘要相同、`upserts`/`deletes` 均为空，版本 ID 仍必须推进，且不同版本 ID 即使清单相同也不能互相代替。

增量是可保存、可重新读取的自包含 JSON（见 `micro_objects.io.save_delta/load_delta`），携带数据集、前后版本标识与两版完整清单摘要。`micro_objects.parse_delta(payload)` 对外部字节做严格结构校验：字段集合、ID、摘要形状与 `delta_digest`、每条记录与其 `record_digest`、键与记录首像素、重复/冲突条目、首版不得删除且必须从空清单出发、首版目标清单摘要可在无任何本地状态下完整复算。任何不一致抛 `micro_objects.DeltaError`。

## 4. 下游审阅库

### 4.1 公开入口

```python
from micro_objects import (
    ReviewStore, DatasetMismatch, StaleVersion,
    DeltaRejected, ReviewRejected, parse_delta, record_digest,
)

store = ReviewStore("path/state.json", "demo-dataset")  # 不存在则新建并绑定数据集
store.import_delta(payload)            # 导入（校验、应用、出回执一体）
store.current_version()                # 当前头版本 ID，未导入时为 None
store.current_manifest_digest()        # 当前完整清单摘要
store.list_object_keys()               # 光栅排序的全部对象键
store.get_object([1, 1])               # 当前交付记录（深拷贝）
store.has_object([1, 1])
store.register_review("rev-1", [1, 1], record_digest_value)
store.list_reviews(include_revoked=False)
store.get_review("rev-1")              # 含登记时记录快照
store.review_history("rev-1")
store.receipt("R1")                    # 已成功导入版本的持久回执
```

库以单写者运行，状态是单个 JSON 文件，不需要也不支持多写者、机器掉电场景。

### 4.2 导入规则与拒绝行为

非重放导入只有在以下条件全部满足时才执行，任何不合法增量都整体拒绝、不留部分更新：

1. 增量通过 `parse_delta` 结构与摘要校验，否则抛 `DeltaRejected`；
2. `dataset_id` 与本库绑定一致，否则抛 `DatasetMismatch`；
3. `from_version_id` 等于当前头、`from_manifest_digest` 等于当前清单摘要，否则抛 `StaleVersion`；
4. 不允许无变化的替换（upsert 与现记录摘要相同）、删除不存在的键、同键重复或 upsert/delete 冲突，否则抛 `DeltaRejected`；
5. 应用全部变更后复算清单摘要，必须等于 `to_manifest_digest`，否则抛 `DeltaRejected`。

### 4.3 重放与同版本冲突

以目标版本 ID 识别一次导入。若该目标版本已成功导入：

- 同一增量（`delta_digest` 相同）重复送达：**先于**前版过期判断，原样返回初次回执（前后版本、增删改键、当时撤销的审阅 ID），即使当前头已到更晚版本，也不倒退当前状态、不重复撤销；
- 同一目标版本 ID 送来不同内容（`delta_digest` 不同）：抛 `DeltaRejected`，已存回执不被覆盖。

### 4.4 审阅生命周期

- `register_review(review_id, key, record_digest_value)` 仅在：审阅 ID 未使用、对象仍在交付集、摘要与当前记录匹配、该对象没有有效审阅时成功，否则抛 `ReviewRejected`；
- 审阅条目记录审阅 ID、对象键、记录摘要、登记时版本、登记时完整记录快照（含旧像素来源）；
- 替换或删除一个已审阅对象时，其有效审阅在同一次导入中被撤销，条目标记 `active=false` 与 `revoked_at_version=<目标版本>`，撤销的审阅 ID 进入回执；未变化对象的审阅继续有效；
- 对象消失后再出现、或证据改回以前的值，都不会自动恢复旧审阅，必须重新登记；
- `review_history` 始终能读到登记时的记录与撤销版本。

### 4.5 持久化与恢复边界

一次提交只做一次原子操作：写临时文件 → `flush` → `fsync` → `os.replace` 覆盖状态文件。版本头、对象变更、审阅撤销与成功回执在同一状态文件中一起生效。

- **提交生效前中断**（`os.replace` 未执行）：重开只见旧状态，临时文件已清理，可原样重试；
- **提交已生效但调用方收到回执前中断**（替换已落盘、回执尚未返回）：内存态会立即与磁盘对齐并抛出 `StoreError` 提示重试；重开或同实例重试均被识别为重放，返回持久化的原回执，不重复撤销、不改写当前头。

不要求支持多写者并发或机器掉电；单进程内的中断（异常、kill）满足上述边界。损坏或形状不符的状态文件在打开时抛 `StoreError`。
