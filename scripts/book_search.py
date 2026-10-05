#!/usr/bin/env python3
import sys
from core import search as implementation

sys.modules[__name__] = implementation
