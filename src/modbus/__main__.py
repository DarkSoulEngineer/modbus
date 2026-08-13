"""Allow ``python -m modbus`` in addition to the ``modbus`` launcher."""

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
