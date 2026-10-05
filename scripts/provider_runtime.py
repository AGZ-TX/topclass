#!/usr/bin/env python3
import sys
from core import provider as implementation

sys.modules[__name__] = implementation
