#!/bin/sh
# wwf-recorder entrypoint: capture is PID 1 (its death restarts the machine,
# policy=always); the fold sidecar runs beside it and can die without ever
# touching capture (its own retry loop; refolds on next boot regardless).
python3 -u /fold.py &
exec python3 -u /recorder.py
