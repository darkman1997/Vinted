#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Bootstrap that restores the known-good rating-only version."""

import urllib.request

URL = "https://raw.githubusercontent.com/darkman1997/Vinted/6486d49a2dee1e2759b486ef120efc836cf8b2dc/vinted.py"
source = urllib.request.urlopen(URL, timeout=45).read().decode("utf-8", "replace")
exec(compile(source, URL, "exec"), globals(), globals())
