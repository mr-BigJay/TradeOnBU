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

class ExportTests(unittest.TestCase):
    def test_only_fresh_new_exports_and_edit_cancels(self):
        db=connect(':memory:')
        record(db,[{'id':'1','text':'history','timestamp':99}],True,now=100,export=True)
        self.assertEqual(db.execute('SELECT COUNT(*) FROM exports').fetchone()[0],0)
        record(db,[{'id':'2','text':'new','timestamp':100}],now=101,export=True)
        self.assertEqual(db.execute('SELECT COUNT(*) FROM exports').fetchone()[0],1)
        record(db,[{'id':'3','text':'old','timestamp':1}],now=300,export=True)
        self.assertEqual(db.execute('SELECT COUNT(*) FROM exports').fetchone()[0],1)
        record(db,[{'id':'2','text':'edited','timestamp':100}],now=301,export=True)
        import json
        self.assertEqual(json.loads(db.execute('SELECT payload FROM exports').fetchone()[0])['kind'],'edited')
        db.close()

class PowerSavingTests(unittest.TestCase):
    def test_enable_and_idempotent(self):
        from unittest.mock import MagicMock
        from reader import enable_power_saving
        for initially_on in (True, False):
            page = MagicMock()
            menu = MagicMock(); menu.count.return_value = 1
            toggle = MagicMock(); toggle.is_checked.side_effect = [initially_on, True]
            label = MagicMock()
            def locate(selector):
                if selector.startswith('#column-left'): return menu
                if '#input-power-saving-all' in selector: return toggle
                if selector.endswith(' label'): return label
                return MagicMock()
            page.locator.side_effect = locate
            self.assertTrue(enable_power_saving(page))
            self.assertEqual(label.filter.return_value.click.call_count, 0 if initially_on else 1)
    def test_ambiguous_menu_stops(self):
        from unittest.mock import MagicMock
        from reader import enable_power_saving
        page=MagicMock(); page.locator.return_value.count.return_value=2
        with self.assertRaisesRegex(RuntimeError,'menu_not_unique'): enable_power_saving(page)
        page.locator.return_value.click.assert_not_called()
    def test_unchecked_after_click_stops(self):
        from unittest.mock import MagicMock
        from reader import enable_power_saving
        page=MagicMock(); page.locator.return_value.count.return_value=1
        page.locator.return_value.is_checked.return_value=False
        with self.assertRaisesRegex(RuntimeError,'power_saving_not_checked'): enable_power_saving(page)

class IndependentArchiveTests(unittest.TestCase):
    def test_fresh_reply_is_archived_with_review_metadata(self):
        import json, time
        db=connect(':memory:')
        try:
            record(db,[{'id':'998','text':'synthetic reply','timestamp':time.time(),'has_reply':True}],False,export=True)
            payload=json.loads(db.execute('SELECT payload FROM exports').fetchone()[0])
            self.assertTrue(payload['has_reply'])
            self.assertEqual(payload['kind'],'new')
        finally:
            db.close()
