# -*- coding: utf-8 -*-
"""Штатный крючок покрытия: дочерний python стартует замер при COVERAGE_PROCESS_START."""
try:  # без установленного coverage файл обязан быть безвредным
    import coverage

    coverage.process_startup()
except ImportError:  # обычный прогон без покрытия
    pass
