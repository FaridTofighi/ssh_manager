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


if __name__ == "__main__":
    unittest.main()
