"""关系执行层的语义核对。"""
import json
import tempfile
import tracemalloc
import unittest
from pathlib import Path

from warehouse.execute import collect, execute, execute_rows
from warehouse.explain import explain, summary
from warehouse.relations import describe, scan, unnest, where

SOURCE = [{'row_id': 't1',
           'calls': [{'tool': 'chat', 'args': [1, None, 1]},
                     {'tool': 'code', 'args': []}],
           'labels': ['x', 'y']},
          {'row_id': 't2', 'calls': None, 'labels': []},
          {'row_id': 't3', 'labels': ['z']},
          {'row_id': 't4', 'calls': [None], 'labels': None}]


def plan_calls_args(outer=True):
    return (scan('t'), unnest(scan('t'), 'c', ('calls',), outer=outer),
            unnest(scan('t'), 'a', ('c', 'args'), correlation=('c',), outer=outer))


class RelatedUnnestTests(unittest.TestCase):
    def test_positions_follow_original_arrays(self):
        records = collect(SOURCE, plan_calls_args())
        t1 = [r for r in records if r['parent'] == 't1']
        # t1.calls[0] 有三个参数（含真实 null），calls[1] 为空数组。
        self.assertEqual([r['positions'] for r in t1],
                         [{'c': 0, 'a': 0}, {'c': 0, 'a': 1}, {'c': 0, 'a': 2},
                          {'c': 1, 'a': None}])
        self.assertEqual([r['values']['a'] for r in t1], [1, None, 1, None])

    def test_null_element_is_not_placeholder(self):
        records = collect(SOURCE, plan_calls_args())
        t1 = [r for r in records if r['parent'] == 't1']
        null_element = t1[1]
        self.assertEqual(null_element['positions']['a'], 1)
        self.assertFalse(null_element['placeholders']['a'])
        self.assertIsNone(null_element['reasons']['a'])

    def test_empty_array_gets_placeholder_with_reason(self):
        records = collect(SOURCE, plan_calls_args())
        t1 = [r for r in records if r['parent'] == 't1']
        empty = t1[3]
        self.assertIsNone(empty['positions']['a'])
        self.assertTrue(empty['placeholders']['a'])
        self.assertEqual(empty['reasons']['a'], 'empty')

    def test_missing_null_empty_differ(self):
        records = collect(SOURCE, plan_calls_args())
        by_parent = {r['parent']: r for r in records if r['parent'] != 't1'}
        self.assertEqual(by_parent['t2']['reasons']['c'], 'null')
        self.assertEqual(by_parent['t3']['reasons']['c'], 'missing')
        self.assertEqual(by_parent['t4']['placeholders']['c'], False)  # 真实 null 元素
        self.assertEqual(by_parent['t4']['positions']['c'], 0)
        # 外占位向下传播：t2/t3 的下层别名是占位，不是真实 null 元素。
        self.assertEqual(by_parent['t2']['reasons']['a'], 'placeholder')
        self.assertEqual(by_parent['t3']['reasons']['a'], 'placeholder')

    def test_inner_unnest_is_left(self):
        records = collect(SOURCE, plan_calls_args(outer=False))
        self.assertEqual([r['parent'] for r in records], ['t1'] * 3)

    def test_parent_ids_reference_source_rows(self):
        records = collect(SOURCE, plan_calls_args())
        ids = {row['row_id'] for row in SOURCE}
        self.assertTrue(all(r['parent'] in ids for r in records))


class IndependentVsZipTests(unittest.TestCase):
    def test_cross_is_cartesian(self):
        plan = (scan('t'), unnest(scan('t'), 'l', ('labels',), outer=True, mode='cross'),
                unnest(scan('t'), 'c', ('calls',), outer=True, mode='cross'))
        records = collect(SOURCE, plan)
        t1 = [r['positions'] for r in records if r['parent'] == 't1']
        self.assertEqual(len(t1), 4)
        self.assertEqual({(r['l'], r['c']) for r in t1},
                         {(0, 0), (0, 1), (1, 0), (1, 1)})

    def test_zip_aligns_by_position_not_length(self):
        plan = (scan('t'), unnest(scan('t'), 'l', ('labels',), outer=True, mode='zip',
                                 siblings=(('c', ('calls',)),)))
        records = collect(SOURCE, plan)
        t1 = [r for r in records if r['parent'] == 't1']
        self.assertEqual([r['positions'] for r in t1],
                         [{'l': 0, 'c': 0}, {'l': 1, 'c': 1}])
        t2 = [r for r in records if r['parent'] == 't2'][0]
        self.assertTrue(t2['placeholders']['l'] and t2['placeholders']['c'])
        self.assertEqual(t2['reasons']['l'], 'empty')
        self.assertEqual(t2['reasons']['c'], 'null')
        # t4: labels 为 null，calls 有一个真实 null 元素；zip 不把它当占位。
        t4 = [r for r in records if r['parent'] == 't4'][0]
        self.assertEqual(t4['positions'], {'l': None, 'c': 0})
        self.assertEqual(t4['reasons'], {'l': 'null', 'c': None})

    def test_same_length_does_not_imply_zip(self):
        plan = unnest(scan('t'), 'l', ('labels',))
        self.assertEqual(plan.mode, 'cross')
        records = collect(SOURCE, (scan('t'), plan))
        t1 = [r['positions'] for r in records if r['parent'] == 't1']
        self.assertEqual(t1, [{'l': 0}, {'l': 1}])  # calls 未被展开

    def test_offsets_explicit_alignment(self):
        plan = (scan('t'), unnest(scan('t'), 'c', ('calls',), offsets=(1, 0)))
        records = collect([SOURCE[0]], plan)
        self.assertEqual([r['positions']['c'] for r in records], [1, 0])

    def test_offsets_pad_when_out_of_range(self):
        plan = (scan('t'), unnest(scan('t'), 'c', ('calls',), offsets=(1, 0), outer=True))
        t4 = [r for r in collect(SOURCE, plan) if r['parent'] == 't4']
        self.assertEqual([(r['positions']['c'], r['placeholders']['c'], r['reasons']['c'])
                          for r in t4], [(None, True, 'padding'), (0, False, None)])


class FilterPlacementTests(unittest.TestCase):
    def test_on_applies_before_outer_placeholder(self):
        plan = (scan('t'), unnest(scan('t'), 'c', ('calls',), outer=True,
                                  on=where(('c', 'tool'), 'code')))
        records = collect(SOURCE, plan)
        by_parent = {r['parent']: r for r in records}
        self.assertEqual(by_parent['t1']['positions'], {'c': 1})
        self.assertEqual(by_parent['t1']['reasons'], {'c': None})
        # t4 的 calls[0] 是真实 null，ON 不通过 -> 占位原因为 on。
        self.assertEqual(by_parent['t4']['reasons']['c'], 'on')
        self.assertTrue(by_parent['t4']['placeholders']['c'])
        self.assertEqual(by_parent['t2']['reasons']['c'], 'null')

    def test_where_after_unnest_keeps_type_distinction(self):
        plan = (scan('t'), unnest(scan('t'), 'a', ('calls',), outer=True),
                where(('a', 'tool'), 'chat'))
        records = collect(SOURCE, plan)
        self.assertEqual({r['parent'] for r in records}, {'t1'})
        self.assertNotIn(True, [r['values'].get('a') for r in records])

    def test_where_on_placeholder_is_missing(self):
        plan = (scan('t'), unnest(scan('t'), 'c', ('calls',), outer=True),
                where(('c', 'tool'), None))
        self.assertEqual(collect(SOURCE, plan), [])

    def test_on_keeps_original_positions_after_filter(self):
        plan = (scan('t'), unnest(scan('t'), 'c', ('calls',), outer=False,
                                  on=where(('c', 'tool'), 'code')))
        records = collect(SOURCE, plan)
        self.assertEqual([r['positions']['c'] for r in records], [1])
        self.assertEqual(records[0]['values']['c']['tool'], 'code')

    def test_inner_on_failure_removes_row(self):
        plan = (scan('t'), unnest(scan('t'), 'c', ('calls',), outer=False,
                                  on=where(('c', 'tool'), 'nope')))
        self.assertEqual(collect(SOURCE, plan), [])

    def test_where_does_not_resurrect_outer_rows(self):
        plan = (scan('t'), unnest(scan('t'), 'c', ('calls',), outer=True),
                where(('c', 'tool'), 'code'))
        records = collect(SOURCE, plan)
        self.assertEqual([(r['parent'], r['positions']) for r in records],
                         [('t1', {'c': 1})])


class BatchTests(unittest.TestCase):
    def test_lazy_single_huge_array(self):
        class Counting(list):
            def __init__(self, data):
                super().__init__(data)
                self.iterated = 0
            def __iter__(self):
                for item in super().__iter__():
                    self.iterated += 1
                    yield item

        values = Counting(list(range(200000)))
        rows = [{'row_id': 'big', 'args': values}]
        plan = (scan('t'), unnest(scan('t'), 'a', ('args',), outer=True))
        tracemalloc.start()
        iterator = execute(rows, plan, batch_size=4)
        first = next(iterator)
        second = next(iterator)
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        self.assertEqual(len(first), 4)
        self.assertEqual(len(second), 4)
        self.assertEqual(values.iterated, 8)
        self.assertLess(peak, 4 * 1024 * 1024)
        plan_positions = [r['positions']['a'] for batch in (first, second) for r in batch]
        self.assertEqual(plan_positions, [0, 1, 2, 3, 4, 5, 6, 7])

    def test_byte_budget_splits_batches(self):
        plan = (scan('t'), unnest(scan('t'), 'a', ('calls',), outer=True))
        batches = list(execute(SOURCE, plan, batch_size=100, max_bytes=600))
        self.assertGreater(len(batches), 1)
        for batch in batches:
            self.assertLessEqual(len(json.dumps(batch, ensure_ascii=False).encode()), 700)

    def test_batch_size_validated(self):
        with self.assertRaises(ValueError):
            list(execute(SOURCE, (scan('t'),), batch_size=0))

    def test_rows_are_checked(self):
        with self.assertRaises(ValueError):
            list(execute([{'value': 1}], (scan('t'),)))

    def test_execute_rows_matches_collect(self):
        plan = plan_calls_args()
        self.assertEqual(list(execute_rows(SOURCE, plan, batch_size=3)),
                         collect(SOURCE, plan))


class ExplainTests(unittest.TestCase):
    def test_explain_reports_cross_layer_and_duplicates(self):
        result = explain(SOURCE, plan_calls_args())
        summary_ = summary(result)
        self.assertEqual(summary_['parents'], 4)
        lateral = [layer for layer in summary_['layers']
                   if layer['kind'] == 'unnest' and layer.get('relation') == 'lateral']
        self.assertEqual(lateral[0]['referenced'], ['c'])
        self.assertEqual(lateral[0]['unresolved'], [])
        duplicates = {(d['alias'], d['value']): d['positions']
                      for d in summary_['duplicates']}
        self.assertEqual(duplicates[('a', '1')], [0, 2])
        self.assertEqual(summary_['per_parent']['t1']['leaves'], 4)

    def test_explain_ignores_placeholder_in_duplicates(self):
        result = explain(SOURCE, plan_calls_args())
        for duplicate in result['duplicates']:
            self.assertTrue(all(position is not None for position in duplicate['positions']))

    def test_explain_summary_is_serializable(self):
        result = summary(explain(SOURCE, plan_calls_args()))
        self.assertIn('t1', result['per_parent'])
        json.dumps(result, ensure_ascii=False)


if __name__ == '__main__':
    unittest.main()
