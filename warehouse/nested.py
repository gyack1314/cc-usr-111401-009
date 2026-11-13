"""嵌套路径状态和单层数组位置读取。

原有 ``lookup``/``elements`` 接口保持不变；新增 ``absence`` 与 ``resolve``
用于查询执行层区分「路径缺席」「显式 null」「空数组」以及解析带别名的路径。
MISSING 仍是独立的哨兵值，不等于 None。
"""
MISSING=object()

def lookup(value,path):
    for part in path:
        if not isinstance(value,dict) or part not in value:return MISSING
        value=value[part]
    return value

def absence(value):
    """返回非数组值的缺席原因；正常（非空）数组返回 None。

    ``MISSING`` -> ``'missing'``，``None`` -> ``'null'``，空列表 -> ``'empty'``。
    这三个状态都不同于「数组中的 null 元素」：后者有原数组位置，且不是占位行。
    """
    if value is MISSING:return 'missing'
    if value is None:return 'null'
    if isinstance(value,list) and not value:return 'empty'
    return None

def resolve(row,bindings,path):
    """按词法作用域解析路径。

    路径首段若命中已绑定的别名，则从别名值继续；否则从原始行字段开始。
    别名优先于同名的行字段。只有路径首段会按别名解析，后续段只做字段读取。
    """
    path=tuple(path)
    if not path:return MISSING
    head,rest=path[0],path[1:]
    if head in bindings:
        value=bindings[head]
    else:
        if not isinstance(row,dict) or head not in row:return MISSING
        value=row[head]
    return lookup(value,rest)

def elements(value,outer=False):
    """逐层枚举数组元素。

    数组元素（包括显式 null 元素）始终带有原数组位置；MISSING、None、空数组
    在 outer=True 时各产生一个 position=None 的占位行，以便与真实 null 元素区分。
    outer=False 时这三种缺席不产生任何行。
    """
    if value is MISSING:reason='missing'
    elif value is None:reason='null'
    elif not isinstance(value,list):raise ValueError('展开路径不是数组')
    elif not value:reason='empty'
    else:
        for i,item in enumerate(value):yield {'position':i,'value':item,'placeholder':False,'reason':None}
        return
    if outer:yield {'position':None,'value':None,'placeholder':True,'reason':reason}

def inspect(data,store,root):
    rows=[]
    for row in data['rows']:
        for entry in elements(lookup(row,['calls']),True):rows.append(dict(parent=row['row_id'],**entry))
    return {'path':['calls'],'rows':rows,'parents':len(data['rows'])}
