"""慢速关系解释：用执行器核对跨层引用、重复值与空扩展。

``explain`` 不另写一套展开算法，而是复用同一份执行器逐条收集小规模数据，
把每一步的判定摆出来，便于与 ``execute`` 的实际输出对查。它会显式报告：

* 每层用了相关（lateral）还是独立（cross）还是按位置对齐（zip）；
* 计划中引用了哪些外层别名，以及这些别名是否确实在作用域内；
* 每个父行产生多少条叶子、其中多少是占位；
* 出现重复值的父行/别名/位置（去重后仍保留全部位置，不做合并）。

它只用于核对小数据；大数组请使用 ``execute`` 流式读取。
"""
from .execute import collect
from .relations import Filter, Scan, Unnest, validate


def _terminal(record, aliases):
    """叶子记录：所有展开别名都已绑定（外占位也算绑定）。"""
    return all(alias in record['positions'] for alias in aliases)


def _leaf_stats(records, aliases):
    stats = {}
    for record in records:
        if not _terminal(record, aliases):
            continue
        parent = record['parent']
        item = stats.setdefault(parent, {'leaves': 0, 'placeholders': 0, 'aliases': {}})
        item['leaves'] += 1
        if any(record['placeholders'].get(alias) for alias in aliases):
            item['placeholders'] += 1
        for alias in aliases:
            bucket = item['aliases'].setdefault(alias, {'positions': [], 'placeholders': 0})
            bucket['positions'].append(record['positions'].get(alias))
            if record['placeholders'].get(alias):
                bucket['placeholders'] += 1
    return stats


def _duplicates(records, aliases):
    """同一父行、同一别名下重复出现的真实值（含真实 null 元素）。

    只有带原数组位置的记录参与；占位与中间层记录不算重复值。
    """
    seen = {}
    for record in records:
        if not _terminal(record, aliases):
            continue
        for alias in aliases:
            if alias not in record['positions'] or record['placeholders'].get(alias):
                continue
            key = repr(record['values'].get(alias))
            seen.setdefault((record['parent'], alias, key), []).append(
                record['positions'].get(alias))
    return [{'parent': parent, 'alias': alias, 'value': key,
             'positions': positions}
            for (parent, alias, key), positions in seen.items() if len(positions) > 1]


def explain(rows, plan):
    """返回可回查的解释结构；rows 为行序列，plan 为关系计划。"""
    plan = validate(plan)
    rows = list(rows)
    records = collect(rows, plan)
    aliases = [node.alias for node in plan if isinstance(node, Unnest)]
    layers = []
    scope = []
    for index, node in enumerate(plan):
        if isinstance(node, Scan):
            scope.append(node.alias)
            layers.append({'index': index, 'kind': 'scan', 'alias': node.alias})
        elif isinstance(node, Unnest):
            referenced = list(node.correlation)
            unresolved = [a for a in referenced if a not in scope]
            layers.append({
                'index': index, 'kind': 'unnest', 'alias': node.alias,
                'path': list(node.path),
                'siblings': [{'alias': a, 'path': list(p)} for a, p in node.siblings],
                'relation': 'lateral' if node.correlation else 'independent',
                'alignment': node.mode,
                'referenced': referenced,
                'unresolved': unresolved,
                'outer': node.outer,
                'on': None if node.on is None else
                      {'path': list(node.on.path), 'equals': node.on.equals},
            })
            scope.append(node.alias)
            scope.extend(a for a, _ in node.siblings)
        else:
            layers.append({'index': index, 'kind': 'where',
                           'path': list(node.path), 'equals': node.equals})
    return {
        'layers': layers,
        'parents': len([row for row in rows]),
        'leaves': len([r for r in records if _terminal(r, aliases)]),
        'per_parent': _leaf_stats(records, aliases),
        'duplicates': _duplicates(records, aliases),
        'records': records,
    }


def summary(result):
    """把解释结果压缩成稳定的可打印摘要（不含全部记录）。"""
    return {
        'layers': result['layers'],
        'parents': result['parents'],
        'leaves': result['leaves'],
        'per_parent': result['per_parent'],
        'duplicates': result['duplicates'],
    }
