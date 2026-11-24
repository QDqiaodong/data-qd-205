# data-qd-205 · 显微分割对象、像素来源、样本编译与离线审阅

将已分割掩膜与强度面编译为稳定对象标签、逐像素来源、精确几何/强度和补连通拓扑；把有重叠的合成像素瓦片解析为整张画布，按对象生成可离线传输的内容寻址增量，并在断网下游库中应用增量、维护独立录入的审阅记录。另一条链把多张合成纹理板按重叠裁片请求编译成训练/盲测样本：盲测并集对训练裁片预先保留，整板对象仅在整对象落入可见范围时编号，并按实际交付的训练背景拟合一份可移植的强度校准记录。配对链在同网格的两次独立整板提取间，按真实重合像素做字典序最优匹配，用对偶灵敏度分析给出逐对象配对证据，并独立交付确定的复测等级与参考分组总量，从不枚举最优方案、从不把不确定区间当成事实。

本仓主要交付为Python业务API与下游可消费的数据，不以通用CLI、报表或后台模板为产品。可观察字段、单位、身份、上限及失败边界见 [数据契约](docs/contract.md)；瓦片编译、增量编码、下游导入/审阅/恢复边界见 [流水线契约](docs/pipeline.md)；整板样本编译、盲测保留与强度校准见 [样本编译契约](docs/dataset.md)；双板配对证据与确定数据交付见 [配对交付契约](docs/correspondence.md)；区域归属、观测/缺测与批次汇总见 [区域交付契约](docs/regions.md)；抽样方案计数/序号、离线任务包、回单验收与精确总量/方差见 [抽样计量契约](docs/sampling.md)；一次性行带流的离线归档、原件下线与断网恢复见 [归档契约](docs/archive.md)。

## 独立安装与验证

Python >=3.11，实际验证CPython3.12.14、uv0.12.19。运行和测试仅标准库；构建固定setuptools80.9.0、wheel0.45.1。各仓独立 `.venv`，可以共享公共下载缓存，不共享site-packages。

从本仓根目录执行：

```sh
uv venv --offline --python 3.12 .venv
uv pip install --offline --python .venv/bin/python -r requirements-build.lock
uv pip install --offline --python .venv/bin/python --no-build-isolation --no-deps .
uv pip check --python .venv/bin/python
.venv/bin/python -I -m unittest discover -s tests -v
.venv/bin/python -I examples/run_sample.py
.venv/bin/python -I examples/run_pipeline.py
.venv/bin/python -I examples/run_dataset.py
.venv/bin/python -I examples/run_regions.py
```

以上命令在本地现有公共缓存上真实执行通过，无需新增下载。新机器缓存为空时，先准备匹配Python与锁定公共构建包，或在可联网安装环境移除安装命令中的offline；API、测试与样例运行不需要网络。示例创建 `run/sample.json`、`run/pipeline/` 与 `run/dataset/`，目标已存在会拒绝覆盖；重复运行请先自行保留产物并选择新的演示工作副本。

## 使用与实际结果

```python
from micro_objects import process
from micro_objects.io import load, publish

result = process(load("fixtures/input.json"))
publish("run/another-sample.json", result)
```

瓦片→增量→下游审阅链（合成数据、无网络）：

```python
from micro_objects import (ReviewStore, build_delta, compile_snapshot,
                           build_records, record_digest)

compiled = compile_snapshot(snapshot)                 # 整图掩膜/强度/来源面
delta = build_delta(previous_snapshot, snapshot)      # 自包含对象增量
store = ReviewStore("run/downstream/state.json", dataset_id)
receipt = store.import_delta(delta)                   # 原子导入，重放安全
store.register_review("rev-1", [1, 1],
                      record_digest(build_records(compiled)[(1, 1)]))
```

板→训练/盲测样本→强度校准链（合成数据、无网络）：

```python
from micro_objects import (POSITIVE, NEGATIVE, BLIND, compile_dataset,
                           apply_calibration)
from micro_objects.io import save_calibration, load_calibration

compiled = compile_dataset(boards, requests)          # 整批校验、发出判断、一次拟合
compiled.not_emitted                                  # 未发出训练请求及原因
sample = compiled.get_sample("p1")                    # 按需取一片，不预展开裁片
save_calibration("run/dataset/calibration.json",      # 独立可移植校准记录
                 compiled.calibration)
record = load_calibration("run/dataset/calibration.json")
apply_calibration(record, [[0, None, 20, 65535]])     # 无掩膜新矩阵，不重新拟合
```

合成样例有2个对象、9个前景像素；8像素环面积16、暴露边长24、孔洞1、平均强度22，边缘孤点被筛掉。

双板配对与确定交付链（合成数据、无网络）：

```python
from micro_objects import deliver_correspondence
from micro_objects.io import save_grade_delivery, save_group_delivery

result = deliver_correspondence(reference_board, retest_board,
                                reference_table, retest_table)
result['grade_delivery']      # 只含确定等级的复测对象，无候选、无证据
result['group_delivery']      # 只含上下界一致的确定分组总量（含0）
result['diagnostics']         # 逐对象候选/落单证据、未交付原因、分组区间
save_grade_delivery('run/pair/grades.json', result['grade_delivery'])
save_group_delivery('run/pair/groups.json', result['group_delivery'])
```

区域归属与批次交付链（合成数据、无网络）：

```python
from micro_objects import deliver_regions

delivery = deliver_regions(
    ['A', 'B'],
    [{
        'specimen_id': 's1',
        'data': process_input,       # 原样传给 process(data)
        'region_grid': region_grid,  # 同尺寸；每格是 'A'/'B'/None
        'observed_regions': ['A', 'B'],
    }],
)
obj = delivery.specimen('s1')['objects'][0]
obj['object_key']                    # ['s1', first_x, first_y]
obj['region_contributions']          # 每区域正像素贡献，比例为精确分数字符串
delivery['region_summaries']         # 已观测/缺测标本、部分和与 complete
delivery.verify_conservation()       # 像素、强度、面积精确守恒核对
```

验收合成板上，3 像素整板对象的首像素为 `[1,1]`，区域 A/B 分别贡献 `2/3`（强度和 30）与 `1/3`（强度和 30）；把中间格的区域归属改为 `null` 不改变整板对象度量，只增加 1 像素、强度和 20 的未分配部分。已观测但无获选前景的区域是有效零记录，未观测区域的数值字段为 `null`，跨标本汇总保留缺测清单和 `complete=false`。

窗口抽样→离线任务包→回单统计链（合成数据、无网络）：

```python
from micro_objects import (design_survey, issue_task_package,
                           save_task_package, load_task_package,
                           evaluate_receipt)

design = design_survey(board, windows, k=2, budget=3)
design.scheme_count                         # 全部可行 K 窗方案数 N
design.inclusion_probability([1, 1])        # Fraction(4, 5)，无浮点
package = issue_task_package(design, rank=0) # ASCII ID 字典序的零基序号
save_task_package("run/sampling/task.json", package)

loaded = load_task_package("run/sampling/task.json")  # 不需原板
estimate = evaluate_receipt(loaded, receipt)
estimate["total_estimate"]                  # Fraction(15)
estimate["variance_estimate"]               # Fraction(55, 3) 或 None
```

设计端用预算子集动态规划计数和定位序号，不枚举全部方案；任务包自带全部候选窗口、对象键/bbox、K、B、N 等设计信息，但不含原掩膜或强度。回单严格核对每窗对象、重复行和跨窗冲突；概率与估计以 `Fraction`/`"n/d"` 精确表示。字段、错误层级和复杂度见 [抽样计量契约](docs/sampling.md)，可运行 `python -I examples/run_sampling.py`。

5×7 验收板上两条参考竖线与两条复测横线各重合一格、两种交换配对同为有效方案：同等级同组时两条复测等级照发、组总量确定为 13；右竖线换组后等级仍照发但两组区间均为 4–9、不交付总量；右竖线换独立等级后两条等级均不发（`grade_conflict`）而组总量 13 照常交付；再加一条复测横线后三条复测线均可能落单，组区间为 13–29，既不发等级也不发总量。256×256 板上 128 条竖线与 128 条横线构成 16,384 条候选边的稠密图，对应不唯一但无对象可落单，128 条等级与组总量 128 全数交付，且不保存任何一族完整方案。

8×3 验收画布上，R1 仅以高优先级瓦片替换 `[2,1]` 的来源（掩膜与强度不变），增量只替换键 `[1,1]` 并撤销其审阅；R2 把该像素改为显式背景后对象断开，增量替换 `[1,1]`、新增 `[3,1]`，而数字标签从 2 变 3 的未变化键 `[5,1]` 不进增量、原审阅继续有效；高层改回 `?` 时低层桥与其来源重新可见。

9×3 样本编译验收板上，前景仅在 y=1 的 x=1、2、6、7（四连通、最小对象 2 像素、不剔边）。盲测窗口 `[8,9)` 时三片正例全部发出，训练背景去重后 4 格、下中位数基线 10，盲测格 x=8 校准后为 190；盲测左界移到 `[7,9)` 后第三片因对象不再完整而不发出（`no_complete_object`），其独占背景 x=5 退出拟合，基线变为 20、计数 3，盲测 x=8 变为 180、残缺对象格标 -1。读回该记录对 `[[0, null, 20, 65535]]` 得到 `[[-20, null, 0, 65515]]`。

安装后的样例摘要：

```json
{"accepted": 1, "foreground_pixels": 9, "objects": 2, "selected_pixels": 8}
```

已通过 100 项业务测试（含原有 72 项），覆盖瓦片优先级与问号透明、确定性摘要、分裂/合并/删除、筛选退出、跨瓦片孔洞与八连通、重放与同版本冲突、清单摘要不匹配、无变化版本推进，以及提交前/提交后两类进程中断恢复；21 项覆盖盲测并集保留与训练隐藏、完整/裁断实例编号与首像素身份、正例/负例/盲测发出规则与原因优先级、按实际交付背景的下中位数校准与记录读回、无训练背景整批拒绝、输入校验、调用方数据修改后的重复取样，以及大量大范围重叠请求下不预展开任何裁片栅格的内存边界（保留量只随整板像素与请求数增长）。新增 28 项覆盖双层目标最优匹配、对偶灵敏度证据（含仅有空间重合但不进任何最优方案的边）、三种未交付原因、分组上下界与确定零总量、筛选排除与整批拒绝、输入重排与等值像素尺寸、两表独立保存后无掩膜读回，以及 256×256 稠密 128×128 候选图的全量交付与性能。新增 17 项区域契约测试覆盖跨区对象与 `null` 守恒、观测零值/缺测空值/补观测完整、仅含被筛对象的区域、区域边界切过触边对象、8连通对角跨区、全 `null` 对象、分数物理面积、输入重排、重复身份、栅格/声明不一致、尺寸与坏板输入、批次上限、原子保存/读回，以及确定性小板守恒；新增 16 项抽样计量测试覆盖验收三例的精确概率/总量/方差、跨窗读数冲突、并集假覆盖、筛选前景不进任务、空窗口/零读数、无方案、输入重排、越界序号、回单顺序与各类缺错/重复行、布尔整数拒绝、小板全组合独立枚举，以及 32 窗口首尾序号和组合数规模核对；当前全量 `unittest` 共 148 项并全部通过。数值和排序语义见契约，不以文件哈希代替业务结果断言。

## 当前边界与来源

不做自动分割、医疗/健康解释、图像可视化看板或跨帧跟踪。

全部输入样例原创合成，MIT许可，见 [来源](fixtures/SOURCES.md)。仅复用MIT通用构建、字段校验与原子新建IO脚手架；业务实现独立编写。当前精确公共依赖镜像准备查询结果为 `true`；这不影响已通过的宿主离线独立环境验证，也不构成容器验收记录。本次没有构建或启动容器。
