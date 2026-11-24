# 抽样窗口、离线任务包、回单与精确总量估计

这条链把整板 `process(data)` 得到的**获选对象**作为总体，把恰好 `K` 扇不同窗口、总工时不超过 `B` 的所有无序集合作为等概率方案。它不把窗口当对象，也不把受工时约束的组合误当成均匀抽对象。

实现仅用 Python 标准库，入口在 `micro_objects.sampling`，常用名也从 `micro_objects` 顶层导出；`micro_objects.survey` 是同义模块命名空间。合成可运行示例：

```sh
python -I examples/run_sampling.py
```

## 1. 设计入口

```python
from micro_objects import design_survey

design = design_survey(board, windows, K=2, B=3)
# 也接受小写关键字 k=2, budget=3；前四个位置参数为 board, windows, K, B。
design.scheme_count                 # 可抽方案总数 N，精确 Python int
design.N                            # 同一值的简写
design.inclusion_probability([1, 1])  # fractions.Fraction(4, 5)
design.p_i([1, 1])                    # 同义简写
design.joint_probability([1, 1], [3, 1])
design.p_ij([1, 1], [1, 1])           # 等于 p_i
```

参数：

- `board`：通常是原来传给 `process(data)` 的整板输入；入口会调用原 `process`，沿用其四/八连通、最小像素、剔边和输入边界。也接受已经执行过的 `process(data)` 结果。
- `windows`：1–32 个窗口。规范字段为：
  - `id`：1–32 位 ASCII 字母、数字、`-` 或 `_`，所有候选窗口唯一；
  - `rect`：半开矩形 `[x0, y0, x1, y1]`，必须满足 `0 <= x0 < x1 <= width`、`0 <= y0 < y1 <= height`；
  - `cost`：1–16 的整数工时。
  
  输入还接受别名字段 `window_id`、`rectangle`/`bounds`、`work_cost`/`work_minutes`/`work_hours`/`hours`/`effort`/`workload`；写出的规范字段始终是 `id`、`rect`、`cost`。不同 ID 可以使用相同矩形。
- `k`：整数 K，1–8；布尔值不是整数。
- `budget`：整数工时上限 B，1–64；布尔值不是整数。

总体必须有 1–64 个整板获选对象。每个对象以其在原板按“先行后列”扫描到的第一个前景像素为键 `[x, y]`；实现直接使用 `process` 已排序组件的 `pixels[0]`，不会使用窗口内临时编号。被整板筛选拒绝的前景不是总体。

只有对象的全部像素都在矩形内时，窗口才能测它。由于轴对齐矩形包含整个对象等价于包含对象 bbox，实现用整板对象 bbox 判定。两个部分覆盖窗口的并集能盖住对象、但没有单扇窗口完整包含它时，任何方案都测不到该对象，设计会被拒绝。

以下情况抛 `SurveyDesignError`（也是 `ValueError` 子类）：

- 板输入不合法或获选对象数不在 1–64；
- 窗口数、ID、矩形、工时、K、B 的类型、范围或字段不合法；
- 窗口 ID 重复；
- `N = 0`，即没有恰好 K 扇且工时合计不超过 B 的不同窗口集合；
- 任一总体对象在所有可抽方案中的入样数为 0。

## 2. 序号、字典序和任务包

所有窗口先按 ID 的 ASCII/Unicode 码点次序排序（允许字符均为 ASCII，因此就是 ASCII 次序）。一个方案是 K 个有序 ID 的元组；所有可行元组按 Python 元组字典序排列，序号从零开始。

```python
package = issue_task_package(design, rank=0)
save_task_package("run/sampling/task_package.json", package)

# 另一台没有板数据的机器：
loaded = load_task_package("run/sampling/task_package.json")
loaded.design.inclusion_probability([6, 1])
```

`rank` 必须是 `type(rank) is int` 且 `0 <= rank < N`；越界、负数、浮点、布尔或字符串均报错。同一规范设计和同序号一定产生同一任务包；打乱候选窗口输入顺序不会改变结果。

任务包规范结构为：

```json
{
  "format": "micro_objects.survey_task_package",
  "schema_version": 1,
  "rank": 0,
  "window_ids": ["f1", "f2"],
  "design": {
    "format": "micro_objects.survey_design",
    "schema_version": 1,
    "board_width": 8,
    "board_height": 3,
    "k": 2,
    "budget": 3,
    "scheme_count": 5,
    "objects": [
      {"key": [1, 1], "bbox": [1, 1, 2, 2]}
    ],
    "windows": [
      {
        "id": "f1",
        "rect": [1, 1, 2, 2],
        "cost": 1,
        "object_keys": [[1, 1]]
      }
    ]
  },
  "tasks": [
    {
      "window_id": "f1",
      "rect": [1, 1, 2, 2],
      "cost": 1,
      "objects": [[1, 1]]
    },
    {
      "window_id": "f2",
      "rect": [1, 1, 4, 2],
      "cost": 2,
      "objects": [[1, 1], [3, 1]]
    }
  ]
}
```

空窗口仍出现在 `window_ids` 和 `tasks` 中，其 `"objects": []`。包内的 `design` 含全部候选窗口、K、B、N、对象键和 bbox，足以在统计端重算概率；不能只靠本次抽中的窗口反推设计。包中不含原始掩膜、强度矩阵或像素强度和。bbox 只是派生几何边界。

`load_task_package(path)` / `parse_task_package(doc)` 会检查：

- JSON 结构、格式和版本；
- 非法字段、重复 JSON 键、布尔冒充整数；
- 对象键唯一且在板尺寸/bbox 内；
- 窗口 ID、矩形、工时和对象引用；
- 窗口列出的对象与按 bbox 完整包含关系重算的结果一致；
- `scheme_count` 与动态规划重算值一致；
- N>0、每个对象至少有一个可行方案能测到；
- rank 未越界，且 `window_ids`/`tasks` 正是该 rank 的规范方案。

不合规包抛 `TaskPackageError`。保存前也会重新严格校验。这里检查结构与业务可用性，不把它当成对抗恶意篡改的签名安全边界。`save_task_package`、`load_task_package`、`save_survey_estimate` 和 `load_survey_estimate` 也可从 `micro_objects.io` 导入。

## 3. 回单验收

规范回单：

```json
{
  "format": "micro_objects.survey_receipt",
  "schema_version": 1,
  "records": [
    {
      "window_id": "f1",
      "readings": [
        {"object_key": [1, 1], "reading": 4}
      ]
    },
    {
      "window_id": "f2",
      "readings": [
        {"object_key": [1, 1], "reading": 4},
        {"object_key": [3, 1], "reading": 8}
      ]
    }
  ]
}
```

`reading` 必须是 `type(value) is int` 且 `0 <= value <= 65535`；`0` 有效，`true` 不是 `1`。读数是计量工位交回值，代码不会用板像素强度或强度和替代或核对它。

为方便调用方，行也可写成 `[[x, y], reading]`；别名字段包括记录的 `id`、`rows`/`objects`/`measurements`，行的 `key`/`object`、`value`/`y`/`measurement`。

统计入口：

```python
estimate = evaluate_receipt(
    "run/sampling/task_package.json",
    "run/sampling/receipt.json",
)
# 也可直接传 TaskPackage/dict 和 receipt dict/path。
```

验收规则：

- 必须恰有每个抽中窗口的一条记录；缺窗口、未知窗口、重复窗口均拒绝；
- 每份记录必须恰有任务要求的对象；缺行、多行/错窗对象、重复对象均拒绝；
- 非法读数、非法类型或非法字段均拒绝；
- 空窗口必须有记录且 `"readings": []`；
- 同一对象被多扇抽中窗口覆盖时，每份相关回单都必须有它；读数必须完全一致；
- 读数冲突抛 `ReceiptConflictError`（`ReceiptError` 的子类），整批退回，不会求平均，也不返回部分估计；
- 记录顺序和对象行顺序不影响验收或估计。

`validate_receipt(package, receipt)` 只做验收并返回 `(readings_by_object, per_window)`；`build_receipt(package, readings_by_window)` 可从示例数据构造规范回单。

## 4. 概率和估计的精确表示

设全部可行方案数为 N。对对象 i：

- `p_i = C_i / N`，其中 `C_i` 是至少包含一扇能完整测 i 的窗口的可行方案数；
- `p_ij = C_ij / N`，其中 `C_ij` 是 i、j 都至少被一扇抽中窗口测到的方案数，可以由同一扇或不同窗口完成；
- `p_ii = p_i`。

Python API 一律返回 `fractions.Fraction`，没有浮点近似：

```python
design.inclusion_probability(key)       # Fraction
design.joint_probability(key_a, key_b)  # Fraction
design.joint_probability((key_a, key_b))
```

总量估计为 Horvitz–Thompson 和：

\[
\hat Y=\sum_{i\in S}\frac{y_i}{p_i},
\]

其中 S 是验收后按对象去重的实测对象集合。同一对象在多份回单中一致时只使用一次。

只有当**总体中任意两个不同对象**都满足 `p_ij > 0` 时才给方差。这个设计级条件可通过 `design.variance_supported` 或 `design.unsupported_pairs()` 核对；即使零联合概率的对象对没有出现在本次回单中，也不支持方差。

支持方差时，对实测对象的所有有序对（含对角线）求和：

\[
\widehat{\operatorname{Var}}(\hat Y)
=
\sum_{i\in S}\sum_{j\in S}
\frac{p_{ij}-p_i p_j}{p_{ij}}
\frac{y_i}{p_i}
\frac{y_j}{p_j}.
\]

计算保留精确 `Fraction`，负项自然保留，不截断为零。设计不支持时：

- `total_estimate` 仍正常交付；
- `variance_supported is False`；
- `variance_estimate is None`；
- `variance_unavailable_reason == "design_has_zero_joint_inclusion_probability_pair"`；
- `unsupported_object_pairs` 列出所有零联合概率对象对。

没有任何实测对象时，总量按空和为 `Fraction(0, 1)`；若设计支持方差，方差也按空前/有序对空和为 `0`。若设计本身存在零联合概率对，即使样本为空，方差仍为 `None`。

`SurveyEstimate` 字典中的估计值是 `Fraction`。调用 `estimate.to_document()` 或 `save_survey_estimate(path, estimate)` 时，Fraction 编码为规范精确字符串：整数为 `"15"`，分数为 `"55/3"`。`load_survey_estimate(path)` 会用 `Fraction(text)` 无损读回。`design.probability_document()` 可生成包含全部 p_i 和 p_ij 字符串的可选报表，但统计调用方通常只需按需调用概率方法，不必预先写整张表。

## 5. 错误类型

所有新错误都继承 `micro_objects.SurveyError(ValueError)`：

| 类型 | 触发场景 |
| --- | --- |
| `SurveyDesignError` | 板、窗口、K、B、序号或设计可行性不合法 |
| `TaskPackageError` | 任务包 JSON/字段/一致性/可用性检查失败 |
| `ReceiptError` | 回单缺窗、多窗、错对象、缺行、多行、重复行或读数非法 |
| `ReceiptConflictError` | 同一对象的多份读数冲突；是 `ReceiptError` 子类 |

错误不携带“可消费的部分估计”；`evaluate_receipt` 要么返回完整 `SurveyEstimate`，要么抛异常。

## 6. 不计全部方案的算法与复杂度

记：

- `m`：候选窗口数，`1 <= m <= 32`；
- `K`：每方案窗口数，`1 <= K <= 8`；
- `B`：工时上限，`1 <= B <= 64`；
- `n`：获选对象数，`1 <= n <= 64`；
- `WH`：原板像素数。

实现使用两类 0/1 子集动态规划，不调用 `itertools.combinations` 来计数、定位序号或计算概率：

1. **后缀预算表** `F(pos,k,L)`：从第 `pos` 个及之后的规范排序窗口中选恰好 `k` 扇、工时至多 `L` 的方案数。  
   时间 `O(m K B)`，空间 `O(m K B)`。它一次建好 N，并使序号定位中每个候选前缀块的大小可在 `O(1)` 时间取得。
2. **允许窗口掩码上的精确工时 DP**：计算“所有窗口都不覆盖对象 i”或“只保留 i、j 都不覆盖的窗口”等限制集合中的可行方案数。单次时间 `O(m K B)`、临时空间 `O(K B)`，结果整数按窗口掩码缓存。

各操作的边界：

- 整板处理和连通/筛选：沿用 `process`，为原板像素/拓扑工作量级，内存含整板标签面；
- 对象—窗口完整包含匹配：`O(mn)`；
- 设计入口：额外 `O(m K B + n m K B)`，因为要检查 N 和每个 p_i；空间 `O(m K B + mn + n)`；
- 序号定位/发包：设计已建表后为 `O(mK)` 次候选扫描；序列化任务包大小 `O(mn + Kn)`，不包含方案列表；
- 保存/读包：写盘内容 `O(mn)`；读包重新验证设计，时间 `O(mn + n m K B + mK)`，空间 `O(m K B + mn)`；
- 回单验收：若回单行数为 R，`O(R + Kn)` 量级；
- 离线统计：设计级方差支持检查最坏要询问全部对象对，时间 `O(n² m K B)`，掩码计数缓存最多保存 `O(n²)` 个整数；通过该检查后，实测 s<=n 个对象的估计求和为 `O(s²)` 次精确分数运算（计数通常命中缓存）。

因此准备、发包、保存包和离线读包都不会把最多 `C(32,8)=10,518,300` 个方案放进内存或写进任务包。32 窗口规模测试直接核对 `N=C(32,8)`、首尾序号和一对概率，不枚举全部方案。统计端只接收任务包/回单；加载任务包不会读取原板，也不存在向编译端请求数据的调用。
