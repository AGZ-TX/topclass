#!/usr/bin/env python3
import sys
from core import index as implementation

sys.modules[__name__] = implementation
