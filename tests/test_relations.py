"""关系计划的结构与作用域检查。"""
import unittest
from warehouse.nested import MISSING, absence, lookup, resolve
from warehouse.relations import (Filter, Scan, Unnest, describe, scan, unnest,
                                 unnest_arrays, validate, where)


class PathTests(unittest.TestCase):
    def test_absence_distinguishes_missing_null_empty(self):
        self.assertEqual(absence(MISSING), 'missing')
        self.assertEqual(absence(None), 'null')
        self.assertEqual(absence([]), 'empty')
        self.assertIsNone(absence([None]))
        self.assertIsNone(absence([0]))

    def test_resolve_alias_scope(self):
        row = {'calls': [{'args': [1]}]}
        self.assertEqual(resolve(row, {'c': {'args': [1]}}, ('c', 'args')), [1])
        self.assertEqual(resolve(row, {}, ('calls',)), row['calls'])
        self.assertIs(resolve(row, {}, ('labels',)), MISSING)

    def test_alias_beats_row_field(self):
        row = {'c': '行字段'}
        self.assertEqual(resolve(row, {'c': '别名值'}, ('c',)), '别名值')


class PlanTests(unittest.TestCase):
    def test_scan_then_unnest(self):
        plan = validate((scan('t'), unnest(scan('t'), 'c', ('calls',))))
        self.assertEqual([n.kind for n in plan], ['scan', 'unnest'])

    def test_duplicate_alias_rejected(self):
        with self.assertRaises(ValueError):
            validate((scan('t'), unnest(scan('t'), 't', ('calls',))))

    def test_correlation_required_for_outer_alias(self):
        with self.assertRaises(ValueError):
            validate((scan('t'), unnest(scan('t'), 'c', ('calls',)),
                      unnest(scan('t'), 'a', ('c', 'args'))))
        plan = validate((scan('t'), unnest(scan('t'), 'c', ('calls',)),
                         unnest(scan('t'), 'a', ('c', 'args'), correlation=('c',))))
        self.assertEqual(plan[-1].correlation, ('c',))

    def test_unbound_correlation_rejected(self):
        with self.assertRaises(ValueError):
            validate((scan('t'), unnest(scan('t'), 'a', ('x', 'args'), correlation=('x',))))

    def test_mode_must_be_explicit_and_known(self):
        with self.assertRaises(ValueError):
            unnest(scan('t'), 'c', ('calls',), mode='guess')
        self.assertEqual(unnest(scan('t'), 'c', ('calls',)).mode, 'cross')

    def test_no_unnest_after_filter(self):
        with self.assertRaises(ValueError):
            validate((scan('t'), where(('t', 'labels'), None),
                      unnest(scan('t'), 'c', ('calls',))))

    def test_plan_must_start_with_scan(self):
        with self.assertRaises(ValueError):
            validate((unnest(scan('t'), 'c', ('calls',)),))
        with self.assertRaises(ValueError):
            validate(())

    def test_offsets_validated(self):
        with self.assertRaises(ValueError):
            unnest(scan('t'), 'c', ('calls',), offsets=(0, 0))
        with self.assertRaises(ValueError):
            unnest(scan('t'), 'c', ('calls',), offsets=(-1,))
        with self.assertRaises(ValueError):
            unnest(scan('t'), 'c', ('calls',), offsets=(1,), siblings=(('l', ('labels',)),))

    def test_siblings_unique_aliases(self):
        with self.assertRaises(ValueError):
            unnest(scan('t'), 'c', ('calls',), siblings=(('c', ('labels',)),))

    def test_describe_marks_relation_and_alignment(self):
        plan = (scan('t'), unnest(scan('t'), 'c', ('calls',), outer=True),
                unnest(scan('t'), 'a', ('c', 'args'), correlation=('c',), outer=True),
                where(('c', 'tool'), 'code'))
        described = describe(plan)
        nodes = described['nodes']
        self.assertEqual(nodes[1]['mode'], 'cross')
        self.assertFalse(nodes[1]['lateral'])
        self.assertTrue(nodes[2]['lateral'])
        self.assertEqual(nodes[3]['kind'], 'filter')

    def test_unnest_arrays_keeps_relation_explicit(self):
        plan = unnest_arrays(scan('t'), (('l', ('labels',)), ('c', ('calls',))),
                             mode='zip')
        self.assertEqual(plan.mode, 'zip')
        self.assertEqual(plan.alias, 'l')
        self.assertEqual([a for a, _ in plan.siblings], ['c'])
        self.assertEqual(describe((scan('t'), plan))['aliases'], ['t', 'l', 'c'])


if __name__ == '__main__':
    unittest.main()
