"""Importers that turn other tools' session exports into SSH Manager groups and hosts.

Each importer is GTK-free (parse → plan → apply choices → commit) so it can be
unit-tested without a display; the wizard dialogs live in ``gtkui/dialogs``.
"""
