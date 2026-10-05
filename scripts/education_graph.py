#!/usr/bin/env python3
import sys
from core import graph as implementation

if __name__ == "__main__":
    try:
        result = implementation.main()
        raise SystemExit(result if isinstance(result, int) else 0)
    except (ValueError, OSError, KeyError) as error:
        print("Error: " + str(error), file=sys.stderr)
        raise SystemExit(1)
else:
    sys.modules[__name__] = implementation
