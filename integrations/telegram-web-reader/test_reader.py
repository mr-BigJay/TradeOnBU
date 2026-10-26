import unittest
from reader import connect, record

class StorageTests(unittest.TestCase):
    def setUp(self):
        self.db = connect(':memory:')
    def tearDown(self):
        self.db.close()
    def test_baseline_duplicate_new_and_edit(self):
        a = {'id':'1', 'text':'synthetic message'}
        self.assertEqual(record(self.db,[a],True)['baseline'],1)
        self.assertFalse(any(record(self.db,[a]).values()))
        self.assertEqual(record(self.db,[dict(a,text='changed')])['edited'],1)
        self.assertEqual(record(self.db,[dict(a,id='2')])['new'],1)
    def test_restart_is_baseline(self):
        self.assertEqual(record(self.db,[{'id':'9','text':'old'}],True)['baseline'],1)
    def test_unsupported_and_invalid(self):
        self.assertEqual(record(self.db,[{'id':'1','text':''}])['unsupported'],1)
        self.assertFalse(any(record(self.db,[{'id':'','text':'x'}]).values()))
    def test_retention(self):
        record(self.db,[{'id':'1','text':'old'}],now=0)
        record(self.db,[],now=8*86400)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM messages').fetchone()[0],0)
    def test_bound(self):
        record(self.db,[{'id':str(i),'text':'x'} for i in range(510)])
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM messages').fetchone()[0],500)

if __name__ == '__main__': unittest.main()
