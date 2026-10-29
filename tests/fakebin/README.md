# Fake `ssh` / `sshpass` for GTK tests

`ssh` prints "Host key verification failed." and exits 255 (what OpenSSH does for a
changed or unknown host key); `sshpass` drops its options and runs the rest. Used by
`tests/toast_freeze_probe.py` (and so `tests/test_toast_input.py`) by putting this folder
first in `PATH`. Nothing here contacts a network.
