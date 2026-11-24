# 双板对象配对与确定性数据交付契约

本文档在 [`contract.md`](contract.md) 的原 `process(data)` 契约之上新增一条数据处理链：对同一网格上两次独立的整板对象提取做配对证据分析，并独立交付**确定**的复测对象等级与参考分组总量。仅使用 Python 标准库与合成像素，不做界面、网络服务或模型训练；两侧的连通方式与筛选条件分别沿用各自 `process` 输入，整板处理口径完全不变。

## 1. 批输入与对象身份

`micro_objects.deliver_correspondence(reference, retest, reference_table, retest_table)` 一次处理一个批次。

### 1.1 两块整板

`reference`（参考侧）与 `retest`（复测侧）都是原始 `process(data)` 输入对象，各自独立调用一次 `process`。配对前只做整板级校验，不做任何图像配准：

- 两板宽、高必须分别相等；
- 两个轴的像素尺寸必须**数值等价**（`Fraction` 比较，例如 `"1"` 与 `"1.0"` 等价，`"1.0"` 与 `"1.00"` 等价），写法不同不影响结果；
- 每侧只允许通过各自原筛选（`accepted`）的整板对象参与配对，每侧最多 128 个，允许为零；
- 被筛掉对象的像素既不产生候选边，也不能被对象表登记。

对象对外身份是该对象在**整板光栅顺序**（先行后列）下的第一个前景像素 `[x, y]`，与临时数字标签、外接矩形角点均无关。输入列表次序没有业务意义。

### 1.2 两张对象表

两表都是行列表，必须**恰好覆盖**各自获选对象一次：

| 表 | 行结构 | 约束 |
| --- | --- | --- |
| `reference_table` | `{"key": [x,y], "grade": ..., "group": ...}` | `grade`、`group` 均为 1–32 个 ASCII 字母、数字、下划线或连字符；分组去重后最多 8 个 |
| `retest_table` | `{"key": [x,y], "reading": ...}` | `reading` 为 0–65535 的整数，拒绝布尔与浮点；`0` 是真实读数，不代表缺失 |

下列情况一律抛 `micro_objects.CorrespondenceError`（`ValueError` 子类）并**整批拒绝，不返回任何部分表**：缺行、重复行、键不存在、键指向被筛掉对象、布尔读数、非法字段、名称超长、分组超 8、对象超 128、原整板 `process` 错误、两板尺寸不一致或像素尺寸数值不等。读数是独立录入值，不从掩膜或强度重新计算。

## 2. 配对目标与候选证据

### 2.1 边与权重

参考对象 *i* 与复测对象 *j* 只有在共享至少一个网格前景坐标时才存在候选边，边权 `w(i,j)` 是**实际重合像素数**，不使用外接框面积。每个对象在任一方案中至多被配一次，允许落单。

### 2.2 有效方案的两层目标

有效方案（最优匹配）按字典序最大化：

1. 配对数最大；
2. 配对数并列时，全部配对边的重合像素总数最大。

两个目标都相同的所有方案同为有效方案，实现不会通过再排序偷偷选定其中一个。实现把边值编码为 `C + w`（`C > 整板像素数`），一次加权指派（匈牙利算法）同时优化两层目标，**从不枚举最优配对**。

### 2.3 可查询的逐对象证据

诊断结果中的 `object_evidence` 对每个复测对象给出：

- `candidate_reference_keys`：在**至少一个有效方案**中能与它配对的参考对象首像素列表（按参考对象光栅序排序）；仅有空间重合但不可能出现在任何有效方案里的参考对象不得出现；
- `can_be_unmatched`：是否存在它落单的有效方案；
- `delivered` 与 `reason`：等级交付判断与未交付原因（见 §3.2）。

证据由一次指派的对偶变量与等权图（equality graph）强连通分量算出：一条有限候选边属于某个最优指派，当且仅当它是紧边（对偶等式成立）且两端在同一强连通分量内；复测对象可落单，当且仅当某个表示“不配对”的虚拟行通过紧边与它同分量。逐对象证据只描述局部可能性，不承诺多条对应能同时成立。

## 3. 两张可消费交付表

两张表彼此独立：一张表因不确定而缺行，不影响另一张表交付。可消费表只含 `format` 与 `records`，不含掩膜、候选参考对象或对应证据。

### 3.1 等级表（`grade_delivery`）

以复测对象键为键，记录形如 `{"key": [x,y], "grade": "A"}`，按 `(y, x)` 光栅序排序。仅当满足全部条件时发出一条真实等级记录：

1. 该对象在**每个**有效方案中都被配对（不存在落单方案）；
2. 它在各有效方案中配到的参考对象等级是**同一个值**（参考对象身份可以不唯一）。

否则不发记录，并在诊断中给出且仅给出一个原因，原因不附带猜测等级：

| 原因常量 | 值 | 触发条件 |
| --- | --- | --- |
| `NO_MATCH` | `"no_match"` | 没有任何可行参考对象（证据候选为空） |
| `POSSIBLY_UNMATCHED` | `"possibly_unmatched"` | 有可行参考对象，同时存在它落单的有效方案 |
| `GRADE_CONFLICT` | `"grade_conflict"` | 不会落单，但可行参考对象的等级不止一个值 |

### 3.2 分组表（`group_delivery`）

以参考侧**已登记**的每个分组为键（即使组内对象从未配对也保留）。在一个完整有效方案里，某组总量是该组所有配到复测对象的参考对象各自收到的复测读数之和；组内无人配到时总量为 0。

- 诊断 `group_bounds` 给出每组在所有完整有效方案上分别可达的 `min_total` 与 `max_total`；下界与上界各自由某个完整方案见证，**不承诺不同组的极值能在同一方案同时达到**。
- 分组表只在 `min_total == max_total` 时发出确定记录 `{"group": ..., "total": n}`（包括确定的 0）；上下界不一致的组只留在诊断里，不进可消费表。
- 极值在等权图上对每组各做一次最小化、一次最大化指派得到，不把逐对象局部极值相加，也不只累计等级表已发出的读数。配对层的两个优化目标先于分组极值：等权图只包含最优匹配可用的紧边，不可能为凑极值退回较差对应。

## 4. 文档格式、排序与离线读回

### 4.1 结果结构

```json
{
  "format": "micro-objects-correspondence/1",
  "grade_delivery": {
    "format": "micro-objects-grade-delivery/1",
    "records": [{"key": [1, 1], "grade": "A"}]
  },
  "group_delivery": {
    "format": "micro-objects-group-delivery/1",
    "records": [{"group": "g1", "total": 13}]
  },
  "diagnostics": {
    "format": "micro-objects-correspondence-diagnostics/1",
    "optimal_matching": {
      "pair_count": 2,
      "overlap_pixels": 2,
      "candidate_edge_count": 4,
      "allowed_edge_count": 4
    },
    "object_evidence": [
      {"key": [1, 1],
       "candidate_reference_keys": [[1, 1], [3, 1]],
       "can_be_unmatched": false,
       "delivered": true,
       "reason": null}
    ],
    "group_bounds": [
      {"group": "g1", "min_total": 4, "max_total": 9}
    ]
  }
}
```

稳定键与排序：等级记录按 `(y, x)`、候选参考键按参考对象光栅序、分组记录与分组区间按分组名字典序。输入表重排、等值像素尺寸写法变化都不改变任何交付内容。

### 4.2 保存与无掩膜读回

两张可消费表可分别保存为 JSON，且能被不持有掩膜或对应证据的调用方读取：

```python
from micro_objects import (deliver_correspondence,
                           parse_grade_delivery, parse_group_delivery)
from micro_objects.io import (save_grade_delivery, load_grade_delivery,
                              save_group_delivery, load_group_delivery)

result = deliver_correspondence(reference, retest,
                                reference_table, retest_table)
save_grade_delivery('grades.json', result['grade_delivery'])
save_group_delivery('groups.json', result['group_delivery'])
grades = load_grade_delivery('grades.json')     # 严格校验 + 规范化排序
groups = load_group_delivery('groups.json')
```

写入使用与增量文档相同的规范 JSON（`sort_keys`、紧凑分隔、UTF-8、原子 `os.replace`）。`parse_grade_delivery` / `parse_group_delivery` 对读回字节做严格结构校验（字段集合、格式标识、键形状、名称字符、非负总量），非法文档抛 `CorrespondenceError`。诊断结果不是可消费表，没有对应的独立读回入口。

## 5. 复杂度与空间边界

设两侧对象总数为 *N*、候选边数为 *E*、组数为 *G*。

- 整板 `process` 之后：扫描像素建立候选边为线性于两侧前景像素总量；
- 稠密图（每个真实格都是候选）使用 `n = max(nr, nt)` 方阵；含缺失边的图使用 `nr + nt` 显式虚拟顶点方阵，非候选格取有限匹配不可能达到的惩罚代价，保证有限完美匹配总存在且惩罚格不进入最优解；
- 主指派、等权图强连通分量、每组两次极值指派共 `O((G+1)·N³)` 个多项式精确步骤，矩阵构建与排序带 `log` 因子，总体不超过 `O((G+1)·N³·log(N+2))`；
- 额外空间为 `O(N² + E)`（指派矩阵、等权图、候选边集合），不包括已读入像素与返回值。

不枚举任何最优配对族，也不会对每条候选边重做完整加权优化；256×256、每侧 128 条单像素宽线、16,384 条候选边的稠密批次在普通环境下约 1 秒内完成，且无需保存任一整族方案，空间不随有效方案数增长。
