"""嵌套关系计划的分批流式执行。

执行器是生成器骨架：根行逐层惰性展开，批次在消费端按 ``batch_size`` 聚合，
不会先把整棵展开结果收集起来再切批。因此一行带巨大数组时，内存占用只与
``batch_size`` 和当前层的数组（输入本身）有关。

每条输出记录：

``parent``
    根行身份，可直接回查原始行。
``positions``
    各展开别名对应**原数组**中的下标；外占位或 pad 为 ``None``。ON 过滤后
    不重新编号。
``placeholders``
    各别名是否为外展开补出的占位行；真实 null 元素为 ``False``。
``reasons``
    占位原因：``missing`` / ``null`` / ``empty`` / ``on`` / ``padding`` /
    ``placeholder``（父占位向下传播）；真实元素为 ``None``。
``values``
    各别名当前绑定值；占位行绑定 ``None``。

过滤位置：ON 在补外占位**之前**、对该层每个候选组合判定；WHERE 在全部展开
之后判定。占位行不参与 ON；WHERE 中引用占位别名按缺失处理，因此不会把
外占位误当作真实 null 元素。
"""
from .nested import MISSING, absence, resolve
from .objects import encode
from .relations import Filter, Scan, validate

DEFAULT_BATCH = 64

# 占位原因
MISSING_REASON = 'missing'
NULL_REASON = 'null'
EMPTY_REASON = 'empty'
ON_REASON = 'on'
PADDING_REASON = 'padding'
PROPAGATED_REASON = 'placeholder'


def _value_of(row, node, state):
    """读取本层主数组，返回 ``(值, 缺席原因, 占位原因)``。"""
    values = state['values']
    placeholders = state['placeholders']
    if node.correlation:
        alias = node.correlation[0]
        if placeholders.get(alias):
            return MISSING, MISSING_REASON, PROPAGATED_REASON
        holder = values.get(alias, MISSING)
        if holder is MISSING:
            return MISSING, MISSING_REASON, MISSING_REASON
        if holder is None and len(node.path) > 1:
            # 穿过真实 null 元素读取下层字段：这是 null，不是字段缺席。
            return MISSING, NULL_REASON, NULL_REASON
        value = resolve({}, {alias: holder}, node.path)
    else:
        value = resolve(row, values, node.path)
    reason = absence(value)
    if reason is None and not isinstance(value, list):
        raise ValueError('展开路径不是数组')
    return value, reason, reason


def _sibling_value(row, path, state):
    value = resolve(row, state['values'], path)
    reason = absence(value)
    if reason is None and not isinstance(value, list):
        raise ValueError('展开路径不是数组')
    return value, reason


def _matches(row, state, predicate, extra=None, extra_placeholders=None):
    values = state['values']
    placeholders = state['placeholders']
    if extra:
        values = dict(values)
        values.update(extra)
    if extra_placeholders:
        placeholders = dict(placeholders)
        placeholders.update(extra_placeholders)
    path = predicate.path
    if len(path) == 1 and path[0] in placeholders and placeholders[path[0]]:
        # 占位别名按缺失处理，区别于真实 null 元素。
        return False
    value = resolve(row, values, path)
    if value is MISSING:
        return False
    # 类型敏感等值：True 不匹配 1，1 不匹配 1.0；显式 null 只匹配 None。
    return type(value) is type(predicate.equals) and value == predicate.equals


class _LayerEntry:
    __slots__ = ('values', 'positions', 'placeholders', 'reasons', 'skip_on')

    def __init__(self, values, positions, placeholders, reasons, skip_on=False):
        self.values = values
        self.positions = positions
        self.placeholders = placeholders
        self.reasons = reasons
        # 纯缺席补出的整层占位不是 ON 的候选，ON 不应再作用一次。
        self.skip_on = skip_on


def _placeholder_entry(aliases_reasons):
    values, positions, placeholders, reasons = {}, {}, {}, {}
    for alias, reason in aliases_reasons:
        values[alias] = None
        positions[alias] = None
        placeholders[alias] = True
        reasons[alias] = reason
    return _LayerEntry(values, positions, placeholders, reasons, skip_on=True)


def _enumerate_entries(node, value, reason):
    """单数组候选，产出 ``(位置, 值, 占位, 原因)``；真实 null 元素不是占位。

    ``offsets`` 显式指定按哪些原数组下标展开。某个下标在本行数组越界时，
    outer 为真按 padding 补位，否则跳过该下标；越界不会让整条查询失败。
    """
    if node.offsets is not None:
        if reason is not None:
            return
        for offset in node.offsets:
            if offset < len(value):
                yield offset, value[offset], False, None
            elif node.outer:
                yield None, None, True, PADDING_REASON
        return
    if reason is not None:
        return
    for position, item in enumerate(value):
        yield position, item, False, None


def _layer_specs(row, node, state):
    specs = [(node.alias, _value_of(row, node, state))]
    for alias, path in node.siblings:
        value, reason = _sibling_value(row, path, state)
        specs.append((alias, (value, reason, reason)))
    return specs


def _cross_candidates(row, node, specs, state):
    """独立数组的笛卡尔展开，全程惰性。

    每个数组单独决定候选：有元素则逐元素；缺席/为空时，本层 outer 给它一个
    占位候选（使父行保留），否则该层没有任何候选。数组之间只做笛卡尔积，
    绝不按长度配对。
    """
    if len(specs) == 1:
        alias, (value, reason, _) = specs[0]
        for position, item, placeholder, note in _enumerate_entries(node, value, reason):
            yield _LayerEntry({alias: item}, {alias: position},
                              {alias: placeholder}, {alias: note},
                              skip_on=placeholder)
        return

    def product(index, values, positions, placeholders, reasons):
        if index == len(specs):
            yield _LayerEntry(dict(values), dict(positions),
                              dict(placeholders), dict(reasons))
            return
        alias, (value, reason, propagated) = specs[index]
        if reason is not None:
            if not node.outer:
                return
            values[alias] = None
            positions[alias] = None
            placeholders[alias] = True
            reasons[alias] = propagated
            yield from product(index + 1, values, positions, placeholders, reasons)
            del values[alias], positions[alias], placeholders[alias], reasons[alias]
            return
        for position, item, placeholder, note in _enumerate_entries(node, value, reason):
            values[alias] = item
            positions[alias] = position
            placeholders[alias] = placeholder
            reasons[alias] = note
            yield from product(index + 1, values, positions, placeholders, reasons)
        del values[alias], positions[alias], placeholders[alias], reasons[alias]

    yield from product(0, {}, {}, {}, {})


def _zip_candidates(row, node, specs, state):
    """按原数组位置对齐（最长对齐，短侧 pad 占位），绝不按长度推断关系。"""
    width = max((len(value) for _, (value, reason, _) in specs if reason is None), default=0)
    if width == 0:
        if node.outer:
            yield _placeholder_entry([(a, reason or EMPTY_REASON)
                                      for a, (_, reason, _) in specs])
        return
    for index in range(width):
        values, positions, placeholders, reasons = {}, {}, {}, {}
        for alias, (value, reason, _) in specs:
            if reason is None and index < len(value):
                values[alias] = value[index]
                positions[alias] = index
                placeholders[alias] = False
                reasons[alias] = None
            else:
                values[alias] = None
                positions[alias] = None
                placeholders[alias] = True
                reasons[alias] = reason if reason is not None else PADDING_REASON
        yield _LayerEntry(values, positions, placeholders, reasons)


def _walk(plan, index, row, state):
    """递归展开；只在所有层都处理完后产出一条结果记录。

    中间状态（只绑定了部分别名）不对外输出，避免看起来像「缺列的行」；
    每一层的原数组位置仍完整保留在最终记录的 ``positions`` 里。
    """
    if index >= len(plan):
        yield _record(row, state), state
        return
    node = plan[index]
    if isinstance(node, Filter):
        # 末尾过滤由 execute 统一处理；这里把当前状态作为结果返回。
        yield _record(row, state), state
        return
    specs = _layer_specs(row, node, state)
    if node.mode == 'zip':
        candidates = _zip_candidates(row, node, specs, state)
    else:
        candidates = _cross_candidates(row, node, specs, state)

    produced = False
    real = candidates if node.on is None else _apply_on(row, node, candidates, state, specs)
    for entry in real:
        produced = True
        child = {
            'values': _merged(state['values'], entry.values),
            'positions': _merged(state['positions'], entry.positions),
            'placeholders': _merged(state['placeholders'], entry.placeholders),
            'reasons': _merged(state['reasons'], entry.reasons),
        }
        yield from _walk(plan, index + 1, row, child)
    if not produced and node.outer:
        entry = _placeholder_entry(_placeholder_reasons(node, specs, state))
        child = {
            'values': _merged(state['values'], entry.values),
            'positions': _merged(state['positions'], entry.positions),
            'placeholders': _merged(state['placeholders'], entry.placeholders),
            'reasons': _merged(state['reasons'], entry.reasons),
        }
        yield from _walk(plan, index + 1, row, child)


def _apply_on(row, node, candidates, state, specs):
    for entry in candidates:
        if entry.skip_on or _matches(row, state, node.on, entry.values, entry.placeholders):
            yield entry


def _placeholder_reasons(node, specs, state):
    reasons = []
    for alias, (_, reason, propagated) in specs:
        if reason is None:
            reasons.append((alias, ON_REASON))
        else:
            reasons.append((alias, propagated or reason))
    return reasons


def _merged(left, right):
    merged = dict(left)
    merged.update(right)
    return merged


def _record(row, state):
    values = dict(state['values'])
    if state['positions']:
        # 父行已可通过 parent 回查；展开后不再把整个根行（可能含巨大数组）
        # 复制进每个输出记录。
        values.pop(state.get('scan_alias'), None)
    return {
        'parent': row.get('row_id'),
        'positions': dict(state['positions']),
        'placeholders': dict(state['placeholders']),
        'reasons': dict(state['reasons']),
        'values': values,
    }


def _rows(plan, row):
    scan = plan[0]
    state = {'values': {scan.alias: row}, 'positions': {}, 'placeholders': {},
             'reasons': {}, 'scan_alias': scan.alias}
    yield from _walk(plan, 1, row, state)


def _passes_where(plan, row, state):
    for node in plan:
        if isinstance(node, Filter) and not _matches(row, state, node):
            return False
    return True


def execute(row_source, plan, batch_size=DEFAULT_BATCH, max_bytes=None):
    """按批产出 ``list[record]``；每批最多 ``batch_size`` 条，输入惰性推进。

    ``max_bytes`` 是单批的字节预算：某条记录编码后本身超出预算时抛出
    ``ValueError``，因为无法在不截断语义的前提下产出；批次本身在加入一条
    超预算记录之前就会提交，不会为了凑批而无限增长。预算只约束输出，不要求
    先把全部展开结果收集起来。
    """
    plan = validate(plan)
    if isinstance(batch_size, bool) or not isinstance(batch_size, int) or batch_size < 1:
        raise ValueError('batch_size 必须是正整数')
    if max_bytes is not None and (isinstance(max_bytes, bool) or not isinstance(max_bytes, int)
                                  or max_bytes < 0):
        raise ValueError('max_bytes 必须是非负整数')
    batch = []
    batch_bytes = 0
    for row in row_source:
        if not isinstance(row, dict) or not isinstance(row.get('row_id'), str):
            raise ValueError('行缺少身份')
        for record, state in _rows(plan, row):
            if not _passes_where(plan, row, state):
                continue
            record_bytes = len(encode(record)) if max_bytes is not None else 0
            if max_bytes is not None and record_bytes > max_bytes:
                raise ValueError('单条记录超出字节预算，无法在不截断语义下产出')
            if batch and max_bytes is not None and batch_bytes + record_bytes > max_bytes:
                yield batch
                batch = []
                batch_bytes = 0
            batch.append(record)
            batch_bytes += record_bytes
            if len(batch) >= batch_size:
                yield batch
                batch = []
                batch_bytes = 0
    if batch:
        yield batch


def execute_rows(row_source, plan, batch_size=DEFAULT_BATCH, max_bytes=None):
    """逐条产出记录，便于调用方自己流式合并。"""
    for batch in execute(row_source, plan, batch_size, max_bytes):
        yield from batch


def collect(row_source, plan, batch_size=DEFAULT_BATCH, max_bytes=None):
    """小规模核对用：收集全部记录。大数组场景应使用 ``execute``。"""
    out = []
    for batch in execute(row_source, plan, batch_size, max_bytes):
        out.extend(batch)
    return out
