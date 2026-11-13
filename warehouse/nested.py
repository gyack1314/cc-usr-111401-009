"""嵌套路径状态和单层数组位置读取。"""
MISSING=object()

def lookup(value,path):
    for part in path:
        if not isinstance(value,dict) or part not in value:return MISSING
        value=value[part]
    return value

def elements(value,outer=False):
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
