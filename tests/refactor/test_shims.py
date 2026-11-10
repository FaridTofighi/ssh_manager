"""Moved modules (R6): the old ``ophion.gtkui`` paths are shims that hand out the same objects."""

import unittest


class I18nShimTests(unittest.TestCase):
    def test_gtkui_i18n_is_the_neutral_module(self) -> None:
        import ophion.gtkui.i18n as old
        import ophion.i18n as new

        self.assertIs(old._, new._)
        self.assertIs(old.ngettext, new.ngettext)
        self.assertIs(old._translation, new._translation)
        self.assertEqual(old.DOMAIN, "ssh-manager")
        self.assertIs(old.DOMAIN, new.DOMAIN)


class ValidationShimTests(unittest.TestCase):
    def test_gtkui_validation_is_the_neutral_module(self) -> None:
        import ophion.gtkui.validation as old
        import ophion.validation as new

        names = [name for name in vars(new) if name.startswith("validat") or name in ("Validator", "_HOST_LABEL", "_")]
        self.assertIn("validators_for", names)
        for name in names:
            with self.subTest(name=name):
                self.assertIs(getattr(old, name), getattr(new, name))
        self.assertIs(new._, __import__("ophion.i18n", fromlist=["_"])._)


if __name__ == "__main__":
    unittest.main()
