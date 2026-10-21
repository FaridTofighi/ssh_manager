# FreeRDP client fixtures

- `help-3.15.txt`: the real output of `xfreerdp3 /help` from Debian's
  `freerdp3-x11` 3.15.0 (the binary path replaced by `xfreerdp3`).
- `help-2.11.txt`: FreeRDP 2.11.7's option table (`client/common/cmdline.h`)
  rendered with the first-column rules of its help printer
  (`freerdp_client_print_command_line_args`); descriptions are replaced by
  `(option)`. Only the option column is used by the parser.
