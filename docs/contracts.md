# 资料与既有接口约定

nested.py 使用MISSING表示路径缺席，与None不同。数组原位置从0起；LEFT展开遇MISSING、null或空数组时各产生一个有reason的外占位，元素本身为null则是有位置的真实元素。

多数组模式只有cross和zip：cross是独立数组的笛卡尔展开，zip按原数组位置对齐、最长对齐、短侧补pad占位。模式必须显式给出，不能由长度相同推断关系。

## 关系计划（relations.py）

计划是 `Scan` 开头、若干 `Unnest`、末尾若干 `Filter` 的有序序列。

- `alias` 是词法作用域标识。路径首段命中已绑定别名时从该别名值继续，否则从根行字段读取；别名优先于同名行字段。
- `correlation` 显式声明本层引用的直接外层别名。引用外层别名的路径若未声明 correlation，计划校验失败。
- `outer=True` 表示 LEFT 展开：数组缺席（missing/null/empty）或 ON 全部失败时，该层补一条占位记录。
- `on` 是本层条件，在补外占位之前、对每个候选组合判定；占位不是 ON 的候选。
- `mode='cross'|'zip'` 明确本层多数组关系；默认 cross，绝不按长度猜测。`siblings` 给出同层一起展开的数组。
- `offsets=(...)` 可显式按原数组下标对齐；不能与 zip 或 siblings 同时使用。下标越界时，outer 按 `padding` 补位，否则跳过该下标。
- `Filter` 是展开完成后的 WHERE，可引用任一已绑定别名。

计划校验拒绝：非 Scan 开头、Scan 不在首位、重复别名、Filter 之后继续展开、引用未绑定别名的 correlation、引用外层别名却未声明 correlation、非 cross/zip 模式。

## 执行层（execute.py）

`execute(row_source, plan, batch_size=64, max_bytes=None)` 是生成器，按批产出记录列表。展开逐层惰性进行，不会先收集整棵结果再切批；一行带巨大数组时，内存与 batch_size（及输入数组本身）相关。`max_bytes` 限制单批输出字节，单条记录本身超限会报错，无法在不截断语义下产出。

每条记录：

- `parent`：根行身份，可回查原始行。
- `positions`：各展开别名在原数组中的下标。ON 过滤后不重新编号；外占位与 pad 为 None。
- `placeholders`：True 表示该别名是补出的占位行；真实 null 元素为 False。
- `reasons`：占位的具体原因 `missing`/`null`/`empty`/`on`/`padding`/`placeholder`（父占位向下传播）；真实元素为 None。
- `values`：各别名绑定值；占位为 None。根行绑定值不重复塞入每条展开记录。

外占位向下传播时不按真实 null 元素处理：相关子路径解析为 missing，WHERE 引用占位别名也不命中。

## 关系解释（explain.py）

`explain(rows, plan)` 为小规模核对复用同一执行器，逐条列出每层是 independent/lateral、cross/zip、引用与未解析别名、每个父行的叶子数与占位数、同父行重复值及其原位置。`summary` 去掉记录明细便于打印。大数组请直接用 `execute` 流式读取。

所有资料均为合成，字段、身份及状态按本文解释。源码中的 inspect 入口只读取并核对现有基础接口；高级查询通过 relations/execute/explain 显式构造计划。
