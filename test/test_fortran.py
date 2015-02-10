from __future__ import division
from __future__ import absolute_import
import six
from six.moves import range

__copyright__ = "Copyright (C) 2015 Andreas Kloeckner"

__license__ = """
Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in
all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
THE SOFTWARE.
"""


import sys
import numpy as np
import loopy as lp
import pyopencl as cl
import pyopencl.clrandom  # noqa
import pytest

import logging
logger = logging.getLogger(__name__)

from pyopencl.tools import pytest_generate_tests_for_pyopencl \
        as pytest_generate_tests

__all__ = [
        "pytest_generate_tests",
        "cl"  # 'cl.create_some_context'
        ]


def test_fill(ctx_factory):
    fortran_src = """
        subroutine fill(out, a, n)
          implicit none

          real*8 a, out(n)
          integer n

          do i = 1, n
            out(i) = a
          end do
        end

        !$loopy begin transform
        !
        ! fill = lp.split_iname(fill, "i", 128,
        !     outer_tag="g.0", inner_tag="l.0")
        !
        !$loopy end transform
        """

    from loopy.frontend.fortran import f2loopy
    knl, = f2loopy(fortran_src)

    ctx = ctx_factory()

    lp.auto_test_vs_ref(knl, ctx, knl, parameters=dict(n=5, a=5))


def test_fill_const(ctx_factory):
    fortran_src = """
        subroutine fill(out, a, n)
          implicit none

          real*8 a, out(n)
          integer n

          do i = 1, n
            out(i) = 3.45
          end do
        end
        """

    from loopy.frontend.fortran import f2loopy
    knl, = f2loopy(fortran_src)

    ctx = ctx_factory()

    lp.auto_test_vs_ref(knl, ctx, knl, parameters=dict(n=5, a=5))


if __name__ == "__main__":
    if len(sys.argv) > 1:
        exec(sys.argv[1])
    else:
        from py.test.cmdline import main
        main([__file__])

# vim: foldmethod=marker