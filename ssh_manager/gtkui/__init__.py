"""GTK 3 user-interface layer for SSH Manager.

Sub-packages:

* ``theme``       – design tokens, generated stylesheets and the theme loader.
* ``widgets``     – small reusable widgets (chips, status dots, toasts, …).
* ``dialogs``     – the dialog framework and every application dialog.
* ``views``       – composite widgets that make up the main window.
* ``controllers`` – UI logic that drives the views (no widget construction).

The package is named ``gtkui`` so it does not collide with the Textual
front-end in ``ssh_manager/ui.py``.
"""
