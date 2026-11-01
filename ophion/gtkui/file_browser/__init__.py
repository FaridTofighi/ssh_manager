"""The file-browser session tab for FTP, FTPS and SFTP hosts (GTK).

* :mod:`.worker` — one background thread per connection;
* :mod:`.pane` — one side (local or remote): toolbar, path bar, filter, file list;
* :mod:`.tab` — :class:`FileBrowserTab`, a :class:`SessionPage` with both panes.
"""
