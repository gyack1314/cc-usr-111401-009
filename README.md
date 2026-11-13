# 嵌套调用湖表资料工具

Python 3.11 项目，用于读取、保存及核对协作资料仓的本地资料。不可变内容通过 SHA256 寻址，目录引用单独保存。现有领域接口位于 warehouse/nested.py，资料契约见 docs/contracts.md，合成输入位于 data/source.json。warehouse/objects.py 负责内容校验，catalog.py 负责本地引用更新，snapshot.py 提供完整行快照基础读取。

已有读取流程使用临时目录，不改变源资料。基础接口输出相应对象身份、行或引用摘要。

嵌套关系读取层在既有 `nested.py` 之上新增：

- `warehouse/relations.py`：显式关系计划。逐层相关展开用 `correlation` 声明，
  独立数组用 `mode='cross'` 笛卡尔展开，需要按位置对齐时显式写
  `mode='zip'` 或 `offsets`；计划不会因长度相同而猜测配对。
- `warehouse/execute.py`：生成器式分批执行，输出 `parent`、各层原数组
  `positions`、`placeholders` 与 `reasons`。ON 在补外占位前判定，WHERE 在
  展开后判定，保留正确的外连接结果；一行带巨大数组时不会把整棵展开结果
  先装进内存。
- `warehouse/explain.py`：慢速关系解释，核对跨层引用、重复值与空扩展。

契约细节见 docs/contracts.md。

运行与开发检查：

```sh
python -m unittest discover -s tests -v
python -m compileall -q warehouse tests
python -m warehouse
```
