"""File transfer for FTP, FTPS and SFTP hosts: backends, listing parsers and settings (no GTK).

* :mod:`.backend` — the ``RemoteFileBackend`` interface, ``RemoteEntry`` and errors;
* :mod:`.ftp` — FTP and FTPS (explicit / implicit) on ``ftplib`` + ``ssl``;
* :mod:`.sftp` — SFTP on ``paramiko``;
* :mod:`.listing` — ``MLSD`` and ``LIST`` (Unix, DOS, …) parsers;
* :mod:`.settings` — ``TransferSettings`` (``host.metadata["transfer"]``);
* :mod:`.pins` — trusted certificate fingerprints.
"""
