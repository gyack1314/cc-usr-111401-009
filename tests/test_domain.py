"""领域资料及现有接口核对。"""
import json,tempfile,unittest
from pathlib import Path
from warehouse.objects import ObjectStore
from warehouse.nested import inspect

class DomainTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.store=ObjectStore(self.root/'objects')
        self.data=json.loads(Path('data/source.json').read_text(encoding='utf-8'))
    def test_existing_reader(self):
        result=inspect(self.data,self.store,self.root)
        self.assertTrue(result)

    def test_null_element_and_missing(self):
        from warehouse.nested import lookup,elements,MISSING
        self.assertIs(lookup({},['calls']),MISSING)
        self.assertEqual(list(elements([None],True)),[{'position':0,'value':None,'placeholder':False,'reason':None}])
        self.assertEqual(list(elements([],True))[0]['reason'],'empty')
        self.assertEqual(list(elements(None,False)),[])
