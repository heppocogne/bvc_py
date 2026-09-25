# 例外の階層と終了コード(M1-1、設計書 3.7節、仕様書 2.2節)。

import unittest

from bvc import errors as E


class TestErrors(unittest.TestCase):
    def test_exit_codes(self):
        cases = {
            E.BvcError: 1,
            E.UsageError: 2,
            E.SafetyAbort: 3,
            E.CannotMove: 4,
            E.IntegrityError: 1,
            E.UnsupportedFormat: 1,
            E.RevisionError: 1,
        }
        for cls, code in cases.items():
            with self.subTest(cls=cls.__name__):
                self.assertEqual(cls.exit_code, code)

    def test_subclasses_inherit_exit_code(self):
        # I-5: 細分したクラスは親のサブクラスで、exit_code を引き継ぐ
        cases = {
            E.SafetyAbort: [E.MissingFiles, E.FileBusy, E.FileChanging, E.Locked,
                            E.PinnedCommit, E.DiskFull],
            E.IntegrityError: [E.CorruptData, E.BrokenVersion, E.UnsafePath],
        }
        for parent, children in cases.items():
            for cls in children:
                with self.subTest(cls=cls.__name__):
                    self.assertTrue(issubclass(cls, parent))
                    self.assertEqual(cls.exit_code, parent.exit_code)
                    self.assertIsNot(cls.__dict__.get("exit_code"), 0)

    def test_all_are_bvc_errors(self):
        for name in dir(E):
            obj = getattr(E, name)
            if isinstance(obj, type) and issubclass(obj, Exception):
                self.assertTrue(issubclass(obj, E.BvcError), name)

    def test_message_and_details(self):
        e = E.Locked("使用中", path="x", info={"pid": 1})
        self.assertEqual(str(e), "使用中")
        self.assertEqual(e.message, "使用中")
        self.assertEqual(e.details, {"path": "x", "info": {"pid": 1}})


if __name__ == "__main__":
    unittest.main()
