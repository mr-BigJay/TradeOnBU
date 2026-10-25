import importlib.util
import unittest
from pathlib import Path
from types import SimpleNamespace

spec = importlib.util.spec_from_file_location(
    "check", Path(__file__).with_name("telegram_channel_check.py")
)
check = importlib.util.module_from_spec(spec)
spec.loader.exec_module(check)


class CheckTests(unittest.IsolatedAsyncioTestCase):
    def test_protection_and_membership(self):
        report = check.channel_report(SimpleNamespace(
            id=check.CHANNEL_ID, title=check.EXPECTED_TITLE,
            noforwards=True, left=False, broadcast=True,
        ))
        self.assertTrue(report["content_protected"])
        self.assertTrue(report["member"])
        self.assertFalse(report["message_content_tested"])
        self.assertEqual(report["openai_requests"], 0)

    def test_wrong_id_is_rejected(self):
        with self.assertRaises(ValueError):
            check.channel_report(SimpleNamespace(id=123))

    async def test_only_target_entity_refreshed_and_no_content_read(self):
        class Dialog:
            is_channel = True
            entity = SimpleNamespace(id=check.CHANNEL_ID)
            input_entity = "target"

            @property
            def message(self):
                raise AssertionError("Message content must not be read")

        class Client:
            async def iter_dialogs(self):
                yield SimpleNamespace(is_channel=False)
                yield SimpleNamespace(is_channel=True, entity=SimpleNamespace(id=1))
                yield Dialog()

            async def get_entity(self, peer):
                assert peer == "target"
                return SimpleNamespace(id=check.CHANNEL_ID, title=check.EXPECTED_TITLE,
                                       noforwards=True, left=False, broadcast=True)

        self.assertTrue((await check.inspect_channel(Client()))["content_protected"])

    async def test_channel_not_found(self):
        class Client:
            async def iter_dialogs(self):
                if False:
                    yield

        self.assertFalse((await check.inspect_channel(Client()))["channel_found"])


if __name__ == "__main__":
    unittest.main()
