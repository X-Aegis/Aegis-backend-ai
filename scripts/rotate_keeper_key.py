"""scripts/rotate_keeper_key.py

Quarterly rotation of the keeper bot signing key (BK-11).

Run from cron on the first day of each quarter (idempotent — a no-op on any
other day, and a no-op if the current quarter's KMS alias is already active)::

    0 3 1 1,4,7,10 *  cd /srv/aegis && .venv/bin/python scripts/rotate_keeper_key.py

For SIGNING_BACKEND=aws_kms the printed ``new_ciphertext`` is the seed re-wrapped
under the new quarterly key — store it as ADMIN_SECRET_KEY_CIPHERTEXT before the
old key is disabled. See docs/key-management-runbook.md.
"""

import json
import os
import sys

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.key_manager import rotate_signing_key


def main() -> int:
    result = rotate_signing_key(actor="cron")
    print(json.dumps(result, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
