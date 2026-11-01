# Files written by SSH Manager before the rename to Ophion

Generated from commit b4a4aeb (the last "SSH Manager" version) with its own code, so the tests prove the
renamed app still opens what users already have. Never regenerate them with newer code.

- `keybox.json` (master password `pre-rename-master`), `passwords.json` (keyring entry ssh-manager/web-01),
  `macros.enc` (one macro, "greet")
- `export-plain.sshm`, `export-encrypted.sshm` (passphrase `pre-rename-export`)
- `openssh-export.conf` (the OpenSSH export, old header), `ssh_config-with-include` (the old Include marker)
