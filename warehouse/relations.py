"""嵌套关系的逻辑计划：扫描、逐层展开、LEFT LATERAL 与最终过滤。

计划是 ``Scan`` 开头、若干 ``Unnest``、末尾若干 ``Filter`` 的有序序列。
每个 ``Unnest`` 节点显式记录：

* ``path``/``siblings``：要展开的数组路径（可多个独立数组，也可分组对齐）；
* ``correlation``：相关展开时**显式声明**被引用的外层别名；
* ``outer``：是否为 LEFT 外展开，缺席或 ON 无匹配时补占位行；
* ``on``：本层 ON 条件，在补外占位之前逐元素判定；
* ``mode``：``cross``（各数组独立笛卡尔）或 ``zip``（按原数组位置对齐，
  最长对齐、短侧补 pad 占位）；
* ``offsets``：可选，显式给出本层按位置取哪些下标（不能由长度推断关系）。

规则由调用方明确写出；计划从不会因为两个数组长度相同就把它们按位置配对。
"""
from dataclasses import dataclass

MODES=('cross','zip')

def _tuple(path):
    if isinstance(path,str):raise ValueError('路径必须是字段名序列，不能是单个字符串')
    path=tuple(path)
    if not path:raise ValueError('路径不能为空')
    if any(not isinstance(p,str) or not p for p in path):raise ValueError('路径字段名必须是非空字符串')
    return path

@dataclass(frozen=True)
class Scan:
    """根行扫描；alias 用于后续路径引用与输出列名。"""
    alias:str
    kind='scan'
    def __post_init__(self):
        if not isinstance(self.alias,str) or not self.alias:raise ValueError('别名必须是非空字符串')

@dataclass(frozen=True)
class Filter:
    """展开完成后按类型敏感等值判断（WHERE），路径可引用任一已绑定别名。"""
    path:tuple
    equals:object
    kind='filter'
    def __post_init__(self):object.__setattr__(self,'path',_tuple(self.path))

@dataclass(frozen=True)
class Unnest:
    """一层展开。

    ``siblings`` 是同一层一起展开的额外数组，格式为 ``((别名, 路径), ...)``；
    与主路径的关系严格由 ``mode`` 决定，不看长度。
    """
    alias:str
    path:tuple
    correlation:tuple=()
    outer:bool=False
    on:object=None
    mode:str='cross'
    offsets:object=None
    siblings:tuple=()
    kind='unnest'
    def __post_init__(self):
        if not isinstance(self.alias,str) or not self.alias:raise ValueError('别名必须是非空字符串')
        object.__setattr__(self,'path',_tuple(self.path))
        object.__setattr__(self,'correlation',tuple(self.correlation))
        siblings=tuple(self.siblings)
        normalized=[]
        for item in siblings:
            if not isinstance(item,(tuple,list)) or len(item)!=2:
                raise ValueError('siblings 必须是 (别名, 路径) 对')
            alias,path=item
            if not isinstance(alias,str) or not alias:raise ValueError('别名必须是非空字符串')
            normalized.append((alias,_tuple(path)))
        object.__setattr__(self,'siblings',tuple(normalized))
        names=[self.alias]+[a for a,_ in self.siblings]
        if len(set(names))!=len(names):raise ValueError('同一展开层的别名重复')
        if self.correlation:
            if len(self.correlation)!=1:raise ValueError('correlation 只声明直接外层别名')
            if any(not isinstance(a,str) or not a for a in self.correlation):
                raise ValueError('相关别名非法')
            if self.correlation[0]!=self.path[0]:
                raise ValueError('correlation 必须指向路径首段的别名')
        if not isinstance(self.outer,bool):raise ValueError('outer 必须是布尔值')
        if self.on is not None and not isinstance(self.on,Filter):raise ValueError('on 必须是 Filter')
        if self.on is not None and not self.on.path:raise ValueError('ON 路径不能为空')
        if self.mode not in MODES:raise ValueError('多数组模式只能是 cross 或 zip，不能按长度猜测')
        if self.offsets is not None:
            if self.siblings:raise ValueError('显式 offsets 只适用于单数组展开')
            if self.mode!='cross':raise ValueError('显式 offsets 与 zip 不能同时使用')
            offsets=tuple(self.offsets)
            if any(isinstance(o,bool) or not isinstance(o,int) or o<0 for o in offsets):
                raise ValueError('offsets 必须是非负整数')
            if len(set(offsets))!=len(offsets):raise ValueError('offsets 不得重复')
            object.__setattr__(self,'offsets',offsets)

def scan(alias):return Scan(alias)
def where(path,equals):return Filter(path,equals)
def unnest(parent,alias,path,*,correlation=(),outer=False,on=None,mode='cross',offsets=None,siblings=()):
    """在 parent 之后追加一层 UNNEST/LEFT LATERAL（parent 是 Scan 或 Unnest）。"""
    if not isinstance(parent,(Scan,Unnest)):raise ValueError('parent 必须是 Scan 或 Unnest')
    if isinstance(parent,Unnest) and parent.kind=='filter':raise ValueError('Filter 之后不能再展开')
    return Unnest(alias,path,correlation=correlation,outer=outer,on=on,mode=mode,
                  offsets=offsets,siblings=siblings)

def unnest_arrays(parent,arrays,*,correlation=(),outer=False,on=None,mode='cross'):
    """一次展开多个同层数组；arrays 为 ``((别名, 路径), ...)``，首个即本层别名。"""
    arrays=tuple(arrays)
    if not arrays:raise ValueError('至少需要一个数组')
    first_alias,first_path=arrays[0]
    return unnest(parent,first_alias,first_path,correlation=correlation,outer=outer,
                  on=on,mode=mode,siblings=arrays[1:])

def validate(plan):
    """校验计划结构与作用域，返回规范化后的元组。"""
    plan=tuple(plan)
    if not plan:raise ValueError('计划不能为空')
    if not isinstance(plan[0],Scan):raise ValueError('计划必须以 Scan 开头')
    scope=[];seen=set();stage='scan';outputs=[]
    for index,node in enumerate(plan):
        if isinstance(node,Scan):
            if index!=0:raise ValueError('Scan 只能位于计划开头')
            if node.alias in seen:raise ValueError('别名重复: %s'%node.alias)
            seen.add(node.alias);scope.append(node.alias)
            stage='scan'
        elif isinstance(node,Unnest):
            if stage=='filter':raise ValueError('Filter 之后不能再展开')
            scope_set=set(scope)
            specs=[(node.alias,node.path)]+list(node.siblings)
            if node.correlation:
                if node.correlation[0] not in scope_set:
                    raise ValueError('correlation 引用了未绑定的别名: %s'%node.correlation[0])
            elif node.path[0] in scope_set:
                raise ValueError('路径引用了外层别名 %s，必须显式声明 correlation'%node.path[0])
            for alias,path in specs[1:]:
                if path[0] in scope_set and path[0] not in node.correlation:
                    raise ValueError('路径引用了外层别名 %s，必须显式声明 correlation'%path[0])
            for alias,path in specs:
                if alias in seen:raise ValueError('别名重复: %s'%alias)
                seen.add(alias);scope.append(alias);outputs.append(alias)
            stage='unnest'
        elif isinstance(node,Filter):
            stage='filter'
        else:
            raise ValueError('计划节点必须是 Scan、Unnest 或 Filter')
    return plan

def aliases(plan):
    out=[]
    for node in validate(plan):
        if isinstance(node,(Scan,Unnest)):
            out.append(node.alias)
            if isinstance(node,Unnest):out.extend(a for a,_ in node.siblings)
    return out

def _filter_dict(f):return {'path':list(f.path),'equals':f.equals}

def describe(plan):
    """把计划展开成可回查结构，明确标出每层相关/独立/按位置对齐。"""
    plan=validate(plan);nodes=[]
    for index,node in enumerate(plan):
        if isinstance(node,Scan):
            nodes.append({'index':index,'kind':'scan','alias':node.alias})
        elif isinstance(node,Filter):
            nodes.append({'index':index,'kind':'filter','where':_filter_dict(node)})
        else:
            nodes.append({'index':index,'kind':'unnest','alias':node.alias,
                          'path':list(node.path),
                          'siblings':[{'alias':a,'path':list(p)} for a,p in node.siblings],
                          'correlation':list(node.correlation) if node.correlation else None,
                          'lateral':bool(node.correlation),'outer':node.outer,
                          'mode':node.mode,
                          'offsets':list(node.offsets) if node.offsets is not None else None,
                          'on':_filter_dict(node.on) if node.on is not None else None})
    return {'nodes':nodes,'aliases':aliases(plan),
            'rule':'每层规则显式给出；不从数组长度推断关系'}
