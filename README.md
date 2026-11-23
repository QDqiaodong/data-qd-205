# data-qd-205 · 显微分割对象、像素来源与离线审阅

将已分割掩膜与强度面编译为稳定对象标签、逐像素来源、精确几何/强度和补连通拓扑；把有重叠的合成像素瓦片解析为整张画布，按对象生成可离线传输的内容寻址增量，并在断网下游库中应用增量、维护独立录入的审阅记录。

本仓主要交付为Python业务API与下游可消费的数据，不以通用CLI、报表或后台模板为产品。可观察字段、单位、身份、上限及失败边界见 [数据契约](docs/contract.md)；瓦片编译、增量编码、下游导入/审阅/恢复边界见 [流水线契约](docs/pipeline.md)。

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
```

以上命令在本地现有公共缓存上真实执行通过，无需新增下载。新机器缓存为空时，先准备匹配Python与锁定公共构建包，或在可联网安装环境移除安装命令中的offline；API、测试与样例运行不需要网络。示例创建 `run/sample.json` 与 `run/pipeline/`，目标已存在会拒绝覆盖；重复运行请先自行保留产物并选择新的演示工作副本。

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

合成样例有2个对象、9个前景像素；8像素环面积16、暴露边长24、孔洞1、平均强度22，边缘孤点被筛掉。

8×3 验收画布上，R1 仅以高优先级瓦片替换 `[2,1]` 的来源（掩膜与强度不变），增量只替换键 `[1,1]` 并撤销其审阅；R2 把该像素改为显式背景后对象断开，增量替换 `[1,1]`、新增 `[3,1]`，而数字标签从 2 变 3 的未变化键 `[5,1]` 不进增量、原审阅继续有效；高层改回 `?` 时低层桥与其来源重新可见。

安装后的样例摘要：

```json
{"accepted": 1, "foreground_pixels": 9, "objects": 2, "selected_pixels": 8}
```

已通过 43 项业务测试（含原有 6 项），覆盖瓦片优先级与问号透明、确定性摘要、分裂/合并/删除、筛选退出、跨瓦片孔洞与八连通、重放与同版本冲突、清单摘要不匹配、无变化版本推进，以及提交前/提交后两类进程中断恢复；另实际核对已有目标、符号链接目标、JSON重复键、非法业务输入和临时发布文件清理。数值和排序语义见契约，不以文件哈希代替业务结果断言。

## 当前边界与来源

不做自动分割、医疗/健康解释、图像可视化看板或跨帧跟踪。

全部输入样例原创合成，MIT许可，见 [来源](fixtures/SOURCES.md)。仅复用MIT通用构建、字段校验与原子新建IO脚手架；业务实现独立编写。当前精确公共依赖镜像准备查询结果为 `true`；这不影响已通过的宿主离线独立环境验证，也不构成容器验收记录。本次没有构建或启动容器。
