"""Target for SYCL"""


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

from typing import Sequence, Tuple, cast

import numpy as np
from cgen import Declarator, Generable
from pymbolic import var
from pytools import memoize_method

from loopy.codegen import CodeGenerationState
from loopy.codegen.result import CodeGenerationResult
from loopy.diagnostic import LoopyError, LoopyTypeError
from loopy.kernel import LoopKernel
from loopy.kernel.array import (ArrayBase, FixedStrideArrayDimTag,
                                VectorArrayDimTag)
from loopy.kernel.data import AddressSpace, ArrayArg, ConstantArg, ImageArg
from loopy.kernel.function_interface import ScalarCallable
from loopy.target.c import (CFamilyASTBuilder, CFamilyTarget,
                            DTypeRegistryWrapper)
from loopy.target.c.codegen.expression import ExpressionToCExpressionMapper
from loopy.types import NumpyType

_SYCL_VARIABLE = {
    "handler": "handler",
    "nd_item": "item",
}

# {{{ dtype registry wrappers


class DTypeRegistryWrapperWithInt8ForBool(DTypeRegistryWrapper):
    """
    A DType registry that uses int8 for bool8 types.

    .. note::

        This sub-class is needed because compyte's type registry does
        not support type aliases.
    """

    def dtype_to_ctype(self, dtype):
        from loopy.types import NumpyType

        if isinstance(dtype, NumpyType) and dtype.dtype == np.bool8:
            return self.wrapped_registry.dtype_to_ctype(NumpyType(np.int8))
        return self.wrapped_registry.dtype_to_ctype(dtype)


class DTypeRegistryWrapperWithAtomics(DTypeRegistryWrapper):
    def get_or_register_dtype(self, names, dtype=None):
        if dtype is not None:
            from loopy.types import AtomicNumpyType, NumpyType

            if isinstance(dtype, AtomicNumpyType):
                return self.wrapped_registry.get_or_register_dtype(
                    names, NumpyType(dtype.dtype)
                )

        return self.wrapped_registry.get_or_register_dtype(names, dtype)


class DTypeRegistryWrapperWithCL1Atomics(DTypeRegistryWrapperWithAtomics):
    def dtype_to_ctype(self, dtype):
        from loopy.types import AtomicNumpyType

        if isinstance(dtype, AtomicNumpyType):
            return "volatile " + self.wrapped_registry.dtype_to_ctype(dtype)
        else:
            return self.wrapped_registry.dtype_to_ctype(dtype)


# }}}


# {{{ vector types


class vec:  # noqa
    pass


def _create_vector_types():
    field_names = ["x", "y", "z", "w"]

    vec.types = {}
    vec.names_and_dtypes = []
    vec.type_to_scalar_and_count = {}

    counts = [2, 3, 4, 8, 16]

    for base_name, base_type in [
        ("char", np.int8),
        ("uchar", np.uint8),
        ("short", np.int16),
        ("ushort", np.uint16),
        ("int", np.int32),
        ("uint", np.uint32),
        ("long", np.int64),
        ("ulong", np.uint64),
        ("float", np.float32),
        ("double", np.float64),
    ]:
        for count in counts:
            name = "%s%d" % (base_name, count)

            titles = field_names[:count]

            padded_count = count
            if count == 3:
                padded_count = 4

            names = ["s%d" % i for i in range(count)]
            while len(names) < padded_count:
                names.append("padding%d" % (len(names) - count))

            if len(titles) < len(names):
                titles.extend((len(names) - len(titles)) * [None])

            try:
                dtype = np.dtype(
                    dict(names=names, formats=[base_type] * padded_count, titles=titles)
                )
            except NotImplementedError:
                try:
                    dtype = np.dtype(
                        [((n, title), base_type) for (n, title) in zip(names, titles)]
                    )
                except TypeError:
                    dtype = np.dtype(
                        [(n, base_type) for (n, title) in zip(names, titles)]
                    )

            setattr(vec, name, dtype)

            vec.names_and_dtypes.append((name, dtype))

            vec.types[np.dtype(base_type), count] = dtype
            vec.type_to_scalar_and_count[dtype] = np.dtype(base_type), count


_create_vector_types()


def _register_vector_types(dtype_registry):
    for name, dtype in vec.names_and_dtypes:
        dtype_registry.get_or_register_dtype(name, dtype)


# }}}


# {{{ function mangler

_CL_SIMPLE_MULTI_ARG_FUNCTIONS = {
    "rsqrt": 1,
    "clamp": 3,
    "atan2": 2,
}


VECTOR_LITERAL_FUNCS = {
    "make_%s%d" % (name, count): (name, dtype, count)
    for name, dtype in [
        ("char", np.int8),
        ("uchar", np.uint8),
        ("short", np.int16),
        ("ushort", np.uint16),
        ("int", np.int32),
        ("uint", np.uint32),
        ("long", np.int64),
        ("ulong", np.uint64),
        ("float", np.float32),
        ("double", np.float64),
    ]
    for count in [2, 3, 4, 8, 16]
}


class SYCLCallable(ScalarCallable):
    """
    Records information about SYCL functions which are not covered by
    :class:`loopy.target.c.CMathCallable`.
    """

    def with_types(self, arg_id_to_dtype, callables_table):
        name = self.name

        # {{{ unary functions
        if name == "abs":
            for id in arg_id_to_dtype:
                if not -1 <= id <= 0:
                    raise LoopyError(f"'{name}' can take only one argument.")

            if 0 not in arg_id_to_dtype or arg_id_to_dtype[0] is None:
                return (self.copy(arg_id_to_dtype=arg_id_to_dtype), callables_table)

            dtype = arg_id_to_dtype[0].numpy_dtype

            if dtype.kind in ("u", "i"):
                # SYCL C 2.2, Section 6.13.3: abs returns *u*gentype
                from loopy.types import to_unsigned_dtype

                return (
                    self.copy(
                        name_in_target=name,
                        arg_id_to_dtype={
                            0: NumpyType(dtype),
                            -1: NumpyType(to_unsigned_dtype(dtype)),
                        },
                    ),
                    callables_table,
                )
            elif dtype.kind == "f":
                name = "fabs"
            else:
                raise LoopyTypeError(f"'{name}' does not support type {dtype}")

        # deliberately not elif: abs branch above may end up taking this.
        if name in [
            "fabs",
            "acos",
            "asin",
            "atan",
            "cos",
            "cosh",
            "sin",
            "sinh",
            "tan",
            "tanh",
            "exp",
            "log",
            "log10",
            "sqrt",
            "ceil",
            "floor",
            "erf",
            "erfc",
        ]:

            for id in arg_id_to_dtype:
                if not -1 <= id <= 0:
                    raise LoopyError(f"'{name}' can take only one argument.")

            if 0 not in arg_id_to_dtype or arg_id_to_dtype[0] is None:
                return (self.copy(arg_id_to_dtype=arg_id_to_dtype), callables_table)

            dtype = arg_id_to_dtype[0]
            dtype = dtype.numpy_dtype

            if dtype.kind in ("u", "i"):
                # ints and unsigned casted to float32
                dtype = np.float32
            elif dtype.kind == "c":
                raise LoopyTypeError(f"{name} does not support type {dtype}")

            return (
                self.copy(
                    name_in_target=name,
                    arg_id_to_dtype={0: NumpyType(dtype), -1: NumpyType(dtype)},
                ),
                callables_table,
            )

        # }}}

        # binary functions
        elif name in ["fmax", "fmin", "atan2", "copysign"]:

            for id in arg_id_to_dtype:
                if not -1 <= id <= 1:
                    # FIXME: Do we need to raise here?:
                    #   The pattern we generally follow is that if we don't find
                    #   a function, then we just return None
                    raise LoopyError("%s can take only two arguments." % name)

            if (
                0 not in arg_id_to_dtype
                or 1 not in arg_id_to_dtype
                or (arg_id_to_dtype[0] is None or arg_id_to_dtype[1] is None)
            ):
                return (self.copy(arg_id_to_dtype=arg_id_to_dtype), callables_table)

            dtype = np.find_common_type(
                [],
                [dtype.numpy_dtype for id, dtype in arg_id_to_dtype.items() if id >= 0],
            )

            if dtype.kind == "c":
                raise LoopyTypeError(f"'{name}' does not support complex numbers")

            dtype = NumpyType(dtype)
            return (
                self.copy(
                    name_in_target=name, arg_id_to_dtype={-1: dtype, 0: dtype, 1: dtype}
                ),
                callables_table,
            )

        elif name in ["max", "min"]:
            for id in arg_id_to_dtype:
                if not -1 <= id <= 1:
                    raise LoopyError("%s can take only 2 arguments." % name)
            if 0 not in arg_id_to_dtype or 1 not in arg_id_to_dtype:
                return (self.copy(arg_id_to_dtype=arg_id_to_dtype), callables_table)
            common_dtype = np.find_common_type(
                [],
                [
                    dtype.numpy_dtype
                    for id, dtype in arg_id_to_dtype.items()
                    if (id >= 0 and dtype is not None)
                ],
            )

            if common_dtype.kind in ["u", "i", "f"]:
                if common_dtype.kind == "f":
                    name = "f" + name

                dtype = NumpyType(common_dtype)
                return (
                    self.copy(
                        name_in_target=name,
                        arg_id_to_dtype={-1: dtype, 0: dtype, 1: dtype},
                    ),
                    callables_table,
                )
            else:
                # Unsupported type.
                raise LoopyError(
                    "%s function not supported for the types %s" % (name, common_dtype)
                )

        elif name == "dot":
            for id in arg_id_to_dtype:
                if not -1 <= id <= 1:
                    raise LoopyError(f"'{name}' can take only 2 arguments.")

            if (
                0 not in arg_id_to_dtype
                or 1 not in arg_id_to_dtype
                or (arg_id_to_dtype[0] is None or arg_id_to_dtype[1] is None)
            ):
                # the types provided aren't mature enough to specialize the
                # callable
                return (self.copy(arg_id_to_dtype=arg_id_to_dtype), callables_table)

            dtype = arg_id_to_dtype[0]
            scalar_dtype, offset, field_name = dtype.numpy_dtype.fields["s0"]
            return (
                self.copy(
                    name_in_target=name,
                    arg_id_to_dtype={-1: NumpyType(scalar_dtype), 0: dtype, 1: dtype},
                ),
                callables_table,
            )

        elif name == "pow":
            for id in arg_id_to_dtype:
                if not -1 <= id <= 1:
                    raise LoopyError(f"'{name}' can take only 2 arguments.")

            common_dtype = np.find_common_type(
                [],
                [
                    dtype.numpy_dtype
                    for id, dtype in arg_id_to_dtype.items()
                    if (id >= 0 and dtype is not None)
                ],
            )

            if common_dtype == np.float64:
                name = "powf64"
            elif common_dtype == np.float32:
                name = "powf32"
            else:
                raise LoopyTypeError(f"'pow' does not support type {dtype}.")

            result_dtype = NumpyType(common_dtype)

            return (
                self.copy(
                    name_in_target=name,
                    arg_id_to_dtype={
                        -1: result_dtype,
                        0: common_dtype,
                        1: common_dtype,
                    },
                ),
                callables_table,
            )

        elif name in _CL_SIMPLE_MULTI_ARG_FUNCTIONS:
            num_args = _CL_SIMPLE_MULTI_ARG_FUNCTIONS[name]
            for id in arg_id_to_dtype:
                if not -1 <= id < num_args:
                    raise LoopyError(
                        "%s can take only %d arguments." % (name, num_args)
                    )

            for i in range(num_args):
                if i not in arg_id_to_dtype or arg_id_to_dtype[i] is None:
                    # the types provided aren't mature enough to specialize the
                    # callable
                    return (self.copy(arg_id_to_dtype=arg_id_to_dtype), callables_table)

            dtype = np.find_common_type(
                [],
                [dtype.numpy_dtype for id, dtype in arg_id_to_dtype.items() if id >= 0],
            )

            if dtype.kind == "c":
                raise LoopyError("%s does not support complex numbers" % name)

            updated_arg_id_to_dtype = {
                id: NumpyType(dtype) for id in range(-1, num_args)
            }

            return (
                self.copy(name_in_target=name, arg_id_to_dtype=updated_arg_id_to_dtype),
                callables_table,
            )

        elif name in VECTOR_LITERAL_FUNCS:
            base_tp_name, dtype, count = VECTOR_LITERAL_FUNCS[name]

            for id in arg_id_to_dtype:
                if not -1 <= id < count:
                    raise LoopyError(
                        "%s can take only %d arguments." % (name, num_args)
                    )

            for i in range(count):
                if i not in arg_id_to_dtype or arg_id_to_dtype[i] is None:
                    # the types provided aren't mature enough to specialize the
                    # callable
                    return (self.copy(arg_id_to_dtype=arg_id_to_dtype), callables_table)

            updated_arg_id_to_dtype = {id: NumpyType(dtype) for id in range(count)}
            updated_arg_id_to_dtype[-1] = SYCLTarget().vector_dtype(
                NumpyType(dtype), count
            )

            return (
                self.copy(
                    name_in_target="(%s%d) " % (base_tp_name, count),
                    arg_id_to_dtype=updated_arg_id_to_dtype,
                ),
                callables_table,
            )

        # does not satisfy any of the conditions needed for specialization.
        # hence just returning a copy of the callable.
        return (self.copy(arg_id_to_dtype=arg_id_to_dtype), callables_table)


def get_sycl_callables():
    """
    Returns an instance of :class:`InKernelCallable` if the function defined by
    *identifier* is known in SYCL.
    """
    sycl_function_ids = (
        {
            "max",
            "min",
            "dot",
            "pow",
            "abs",
            "acos",
            "asin",
            "atan",
            "cos",
            "cosh",
            "sin",
            "sinh",
            "pow",
            "atan2",
            "tanh",
            "exp",
            "log",
            "log10",
            "sqrt",
            "ceil",
            "floor",
            "max",
            "min",
            "fmax",
            "fmin",
            "fabs",
            "tan",
            "erf",
            "erfc",
        }
        | set(_CL_SIMPLE_MULTI_ARG_FUNCTIONS)
        | set(VECTOR_LITERAL_FUNCS)
    )

    return {id_: SYCLCallable(name=id_) for id_ in sycl_function_ids}


# }}}


# {{{ symbol mangler


def sycl_symbol_mangler(kernel, name):
    # FIXME: should be more picky about exact names
    if name.startswith("FLT_"):
        return NumpyType(np.dtype(np.float32)), name
    elif name.startswith("DBL_"):
        return NumpyType(np.dtype(np.float64)), name
    elif name.startswith("M_"):
        if name.endswith("_F"):
            return NumpyType(np.dtype(np.float32)), name
        else:
            return NumpyType(np.dtype(np.float64)), name
    elif name == "INFINITY":
        return NumpyType(np.dtype(np.float32)), name
    elif name.startswith("INT_"):
        return NumpyType(np.dtype(np.int32)), name
    elif name.startswith("LONG_"):
        return NumpyType(np.dtype(np.int64)), name
    elif name == "HUGE_VAL":
        return NumpyType(np.dtype(np.float64)), name
    else:
        return None


# }}}


# {{{ preamble generator


def sycl_preamble_generator(preamble_info):

    from loopy.tools import remove_common_indentation

    kernel = preamble_info.kernel

    yield (
        "00_declare_gid_lid",
        remove_common_indentation(
            """
                #define lid(N) ((%(idx_ctype)s) %(item)s.get_local_id(N))
                #define gid(N) ((%(idx_ctype)s) %(item)s.get_group_id(N))
                """
            % dict(
                idx_ctype=kernel.target.dtype_to_typename(kernel.index_dtype),
                item=_SYCL_VARIABLE["nd_item"],
            )
        ),
    )

    for func in preamble_info.seen_functions:
        if func.name == "pow" and func.c_name == "powf32":
            yield (
                "08_clpowf32",
                """
                inline float powf32(float x, float y) {
                return pow(x, y);
                }""",
            )

        if func.name == "pow" and func.c_name == "powf64":
            yield (
                "08_clpowf64",
                """
                inline double powf64(double x, double y) {
                return pow(x, y);
                }""",
            )


# }}}


# {{{ expression mapper


class ExpressionToSYCLCExpressionMapper(ExpressionToCExpressionMapper):
    def wrap_in_typecast_lazy(self, actual_dtype, needed_dtype, s):
        if needed_dtype.dtype.kind == "b" and actual_dtype().dtype.kind == "f":
            # CL does not perform implicit conversion from float-type to a bool.
            from pymbolic.primitives import Comparison

            return Comparison(s, "!=", 0)

        return super().wrap_in_typecast_lazy(actual_dtype, needed_dtype, s)

    def map_group_hw_index(self, expr, type_context):
        return var("item.get_global_id")(expr.axis)

    def map_local_hw_index(self, expr, type_context):
        return var("item.get_local_id")(expr.axis)


# }}}


# {{{ target


class SYCLTarget(CFamilyTarget):
    """A target for the SYCL C heterogeneous compute programming language."""

    def split_kernel_at_global_barriers(self):
        return True

    def get_device_ast_builder(self):
        return SYCLCASTBuilder(self)

    @memoize_method
    def get_dtype_registry(self):
        from loopy.target.c.compyte.dtypes import (
            DTypeRegistry, fill_registry_with_opencl_c_types)

        result = DTypeRegistry()
        fill_registry_with_opencl_c_types(result)

        _register_vector_types(result)

        return result

    def is_vector_dtype(self, dtype):
        return isinstance(dtype, NumpyType) and dtype.numpy_dtype in list(
            vec.types.values()
        )

    def vector_dtype(self, base, count):
        return NumpyType(vec.types[base.numpy_dtype, count])


# }}}


# {{{ ast builder


class SYCLCASTBuilder(CFamilyASTBuilder):
    # {{{ library

    @property
    def known_callables(self):
        callables = super().known_callables
        callables.update(get_sycl_callables())
        return callables

    def symbol_manglers(self):
        return super().symbol_manglers() + [sycl_symbol_mangler]

    def preamble_generators(self):

        return super().preamble_generators() + [sycl_preamble_generator]

    # }}}

    # {{{ top-level codegen
    def get_buffer_arg_declarator(self, arg: ArrayArg, is_written: bool) -> Declarator:
        from cgen.sycl import Buffer

        arg_decl = Buffer(
            self.get_array_base_declarator(arg),
            self.target.dtype_to_typename(arg.dtype),
        )

        return arg_decl

    def get_function_definition(
        self,
        codegen_state: CodeGenerationState,
        codegen_result: CodeGenerationResult,
        schedule_index: int,
        function_decl: Generable,
        function_body: Generable,
    ) -> Generable:
        kernel = codegen_state.kernel
        assert kernel.linearization is not None

        from cgen import Block, FunctionBody, Initializer, Line
        from cgen import \
            Module as Collection  # Post-mid-2016 cgens have 'Collection', too.

        result = []

        from loopy.kernel.data import AddressSpace
        from loopy.schedule import CallKernel
        from loopy.target.c import generate_array_literal

        # We only need to write declarations for global variables with
        # the first device program. `is_first_dev_prog` determines
        # whether this is the first device program in the schedule.
        is_first_dev_prog = codegen_state.is_generating_device_code
        for i in range(schedule_index):
            if isinstance(kernel.linearization[i], CallKernel):
                is_first_dev_prog = False
                break
        if is_first_dev_prog:
            for tv in sorted(
                kernel.temporary_variables.values(), key=lambda key_tv: key_tv.name
            ):

                if tv.address_space == AddressSpace.GLOBAL and (
                    tv.initializer is not None
                ):
                    assert tv.read_only

                    decl = self.wrap_global_constant(
                        self.get_temporary_var_declarator(codegen_state, tv)
                    )

                    if tv.initializer is not None:
                        decl = Initializer(
                            decl,
                            generate_array_literal(codegen_state, tv, tv.initializer),
                        )

                    result.append(decl)
        kernel = codegen_state.kernel
        from cgen.sycl import SYCLparallel_for, SYCLQueueSubmit

        from loopy.schedule import get_insn_ids_for_block_at

        global_sizes, local_sizes = kernel.get_grid_sizes_for_insn_ids_as_exprs(
            get_insn_ids_for_block_at(
                codegen_state.kernel.linearization, schedule_index
            ),
            codegen_state.callables_table,
        )

        from loopy.schedule import CallKernel

        assert codegen_state.kernel.linearization is not None
        subkernel_name = cast(
            CallKernel, codegen_state.kernel.linearization[schedule_index]
        ).kernel_name

        if codegen_state.is_entrypoint:
            # subkernel launches occur only as part of entrypoint kernels for now
            from loopy.schedule.tools import get_subkernel_arg_info

            skai = get_subkernel_arg_info(kernel, subkernel_name)
            passed_names = skai.passed_names
            written_names = skai.written_names
        accessors = []
        for arg_name in passed_names:
            acc = self.buffer_to_accessor(
                kernel,
                arg_name,
                _SYCL_VARIABLE["handler"],
                is_written=arg_name in written_names,
            )
            if acc != None:
                accessors.append(acc)

        function_body = Block(
            accessors
            + [
                SYCLparallel_for(
                    function_body,
                    len(global_sizes),
                    _SYCL_VARIABLE["handler"],
                    _SYCL_VARIABLE["nd_item"],
                )
            ]
        )
        function_body = Block(
            [SYCLQueueSubmit(function_body, _SYCL_VARIABLE["handler"])]
        )
        fbody = FunctionBody(function_decl, function_body)
        if not result:
            return fbody
        else:
            return Collection(result + [Line(), fbody])

    def get_buffer_to_accessor(
        self, arg: ArrayArg, is_written: bool, handler: str
    ) -> Declarator:
        from cgen import Assign, Value
        from cgen.sycl import SYCLGetAccessor

        arg_decl = Assign(
            f"auto {arg.name}", SYCLGetAccessor(arg.name, is_written, handler)
        )
        return arg_decl

    def buffer_to_accessor(
        self, kernel: LoopKernel, passed_name: str, handler: str, is_written: bool
    ):
        var_descr = kernel.get_var_descriptor(passed_name)
        if isinstance(var_descr, ArrayArg):
            return self.get_buffer_to_accessor(var_descr, is_written, handler)
        return None

    def arg_to_cgen_declarator(
        self, kernel: LoopKernel, passed_name: str, is_written: bool
    ) -> Declarator:
        from loopy.kernel.data import (AddressSpace, ArrayArg, ConstantArg,
                                       ImageArg, TemporaryVariable, ValueArg)

        if passed_name in kernel.all_inames():
            assert not is_written
            return self.get_value_arg_declaraotor(
                passed_name, kernel.index_dtype, is_written
            )
        var_descr = kernel.get_var_descriptor(passed_name)
        if isinstance(var_descr, ValueArg):
            assert var_descr.dtype is not None
            return self.get_value_arg_declaraotor(
                var_descr.name, var_descr.dtype, is_written
            )
        elif isinstance(var_descr, ArrayArg):
            return self.get_buffer_arg_declarator(var_descr, is_written)
        elif isinstance(var_descr, TemporaryVariable):
            return self.get_temporary_arg_decl(var_descr, is_written)
        elif isinstance(var_descr, ConstantArg):
            return self.get_constant_arg_declarator(var_descr)
        elif isinstance(var_descr, ImageArg):
            return self.get_image_arg_declarator(var_descr, is_written)
        else:
            raise ValueError(
                f"unexpected type of argument '{passed_name}': " f"'{type(var_descr)}'"
            )

    def get_function_declaration(
        self,
        codegen_state: CodeGenerationState,
        codegen_result: CodeGenerationResult,
        schedule_index: int,
    ) -> Tuple[Sequence[Tuple[str, str]], Generable]:
        kernel = codegen_state.kernel
        from loopy.schedule import CallKernel

        assert codegen_state.kernel.linearization is not None
        subkernel_name = cast(
            CallKernel, codegen_state.kernel.linearization[schedule_index]
        ).kernel_name

        from cgen import FunctionDeclaration, Value

        name = codegen_result.current_program(codegen_state).name
        if self.target.fortran_abi:
            name += "_"

        if codegen_state.is_entrypoint:
            name = Value("void", name)

            # subkernel launches occur only as part of entrypoint kernels for now
            from loopy.schedule.tools import get_subkernel_arg_info

            skai = get_subkernel_arg_info(kernel, subkernel_name)
            passed_names = skai.passed_names
            written_names = skai.written_names
        else:
            name = Value("static void", name)
            passed_names = [arg.name for arg in kernel.args]
            written_names = kernel.get_written_variables()
        from loopy.target.c import FunctionDeclarationWrapper

        preambles, fdecl = [], FunctionDeclarationWrapper(
            FunctionDeclaration(
                name,
                [
                    self.arg_to_cgen_declarator(
                        kernel, arg_name, is_written=arg_name in written_names
                    )
                    for arg_name in passed_names
                ],
            )
        )

        preambles = self.required_work_group_size(
            codegen_state, schedule_index, preambles
        )
        assert isinstance(fdecl, FunctionDeclarationWrapper)
        if not codegen_state.is_entrypoint:
            # auxiliary kernels need not mention sycl speicific qualifiers
            # for a functions signature
            return preambles, fdecl

        return preambles, FunctionDeclarationWrapper(
            self._wrap_kernel_decl(codegen_state, schedule_index, fdecl.subdecl)
        )

    def _wrap_kernel_decl(
        self, codegen_state: CodeGenerationState, schedule_index: int, fdecl: Declarator
    ) -> Declarator:
        from cgen.sycl import SYCLKernel

        from loopy.schedule import get_insn_ids_for_block_at

        global_size, _ = codegen_state.kernel.get_grid_sizes_for_insn_ids_as_exprs(
            get_insn_ids_for_block_at(
                codegen_state.kernel.linearization, schedule_index
            ),
            codegen_state.callables_table,
        )
        fdecl = SYCLKernel(fdecl, len(global_size))
        return fdecl

    def required_work_group_size(
        self, codegen_state: CodeGenerationState, schedule_index: int, preambles
    ):
        from cgen.sycl import SYCLRequiredWorkGroupSize

        from loopy.schedule import get_insn_ids_for_block_at

        _, local_sizes = codegen_state.kernel.get_grid_sizes_for_insn_ids_as_exprs(
            get_insn_ids_for_block_at(
                codegen_state.kernel.linearization, schedule_index
            ),
            codegen_state.callables_table,
        )

        from loopy.symbolic import get_dependencies

        if not get_dependencies(local_sizes):
            preambles = [
                (
                    "10_request_work_group_size",
                    str(SYCLRequiredWorkGroupSize(local_sizes)),
                )
            ]
        return preambles

    def generate_top_of_body(self, codegen_state):
        from loopy.kernel.data import ImageArg

        if any(isinstance(arg, ImageArg) for arg in codegen_state.kernel.args):
            from cgen import Const, Initializer, Value

            return [
                Initializer(
                    Const(Value("sampler_t", "loopy_sampler")),
                    "CLK_NORMALIZED_COORDS_FALSE | CLK_ADDRESS_CLAMP "
                    "| CLK_FILTER_NEAREST",
                )
            ]

        return []

    # }}}

    def get_expression_to_c_expression_mapper(self, codegen_state):
        return ExpressionToSYCLCExpressionMapper(codegen_state)

    def add_vector_access(self, access_expr, index):
        # The 'int' avoids an 'L' suffix for long ints.
        return access_expr.attr("s%s" % hex(int(index))[2:])

    def emit_barrier(self, synchronization_kind, mem_kind, comment):
        """
        :arg kind: ``"local"`` or ``"global"``
        :return: a :class:`loopy.codegen.GeneratedInstruction`.
        """
        if synchronization_kind == "local":
            if comment:
                comment = " /* %s */" % comment

            mem_kind = mem_kind.upper()

            from cgen import Statement

            return Statement(f"group_barrier(item.get_group()){comment}")
        elif synchronization_kind == "global":
            raise LoopyError("SYCL does not have global barriers")
        else:
            raise LoopyError("unknown barrier kind")

    # {{{ declarators

    def wrap_decl_for_address_space(
        self, decl: Declarator, address_space: AddressSpace
    ) -> Declarator:
        from cgen.opencl import CLGlobal, CLLocal

        if address_space == AddressSpace.GLOBAL:
            return CLGlobal(decl)
        elif address_space == AddressSpace.LOCAL:
            return CLLocal(decl)
        elif address_space == AddressSpace.PRIVATE:
            return decl
        else:
            raise ValueError(
                "unexpected temporary variable address space: %s" % address_space
            )

    def wrap_global_constant(self, decl: Declarator) -> Declarator:
        from cgen.opencl import CLConstant, CLGlobal

        assert isinstance(decl, CLGlobal)
        decl = decl.subdecl

        return CLConstant(decl)

    # duplicated in CUDA, update there if updating here
    def get_array_base_declarator(self, ary: ArrayBase) -> Declarator:
        dtype = ary.dtype

        vec_size = ary.vector_size(self.target)
        if vec_size > 1:
            dtype = self.target.vector_dtype(dtype, vec_size)

        if ary.dim_tags:
            for dim_tag in ary.dim_tags:
                if isinstance(dim_tag, (FixedStrideArrayDimTag, VectorArrayDimTag)):
                    # we're OK with those
                    pass

                else:
                    raise NotImplementedError(
                        f"{type(self).__name__} does not understand axis tag "
                        f"'{type(dim_tag)}."
                    )

        from loopy.target.c import POD

        return POD(self, dtype, ary.name)

    def get_constant_arg_declarator(self, arg: ConstantArg) -> Declarator:
        from cgen import RestrictPointer
        from cgen.opencl import CLConstant

        # constant *is* an address space as far as CL is concerned, do not re-wrap
        return CLConstant(RestrictPointer(self.get_array_base_declarator(arg)))

    def get_image_arg_declarator(self, arg: ImageArg, is_written: bool) -> Declarator:
        if is_written:
            mode = "w"
        else:
            mode = "r"

        from cgen.opencl import CLImage

        return CLImage(arg.num_target_axes(), mode, arg.name)

    # }}}

    # {{{ atomics

    def emit_atomic_init(
        self,
        codegen_state,
        lhs_atomicity,
        lhs_var,
        lhs_expr,
        rhs_expr,
        lhs_dtype,
        rhs_type_context,
    ):
        # for the CL1 flavor, this is as simple as a regular update with whatever
        # the RHS value is...

        return self.emit_atomic_update(
            codegen_state,
            lhs_atomicity,
            lhs_var,
            lhs_expr,
            rhs_expr,
            lhs_dtype,
            rhs_type_context,
        )

    def emit_atomic_update(
        self,
        codegen_state,
        lhs_atomicity,
        lhs_var,
        lhs_expr,
        rhs_expr,
        lhs_dtype,
        rhs_type_context,
    ):
        from pymbolic.mapper.stringifier import PREC_NONE

        # FIXME: Could detect operations, generate atomic_{add,...} when
        # appropriate.

        if isinstance(lhs_dtype, NumpyType) and lhs_dtype.numpy_dtype in [
            np.int32,
            np.int64,
            np.float32,
            np.float64,
        ]:
            from cgen import Assign, Block, DoWhile

            from loopy.target.c import POD

            old_val_var = codegen_state.var_name_generator("loopy_old_val")
            new_val_var = codegen_state.var_name_generator("loopy_new_val")

            from loopy.kernel.data import AddressSpace, TemporaryVariable

            ecm = codegen_state.expression_to_code_mapper.with_assignments(
                {
                    old_val_var: TemporaryVariable(old_val_var, lhs_dtype, shape=()),
                    new_val_var: TemporaryVariable(new_val_var, lhs_dtype, shape=()),
                }
            )

            lhs_expr_code = ecm(lhs_expr, prec=PREC_NONE, type_context=None)

            from pymbolic import var
            from pymbolic.mapper.substitutor import make_subst_func

            from loopy.symbolic import SubstitutionMapper

            subst = SubstitutionMapper(make_subst_func({lhs_expr: var(old_val_var)}))
            rhs_expr_code = ecm(
                subst(rhs_expr),
                prec=PREC_NONE,
                type_context=rhs_type_context,
                needed_dtype=lhs_dtype,
            )

            if lhs_dtype.numpy_dtype.itemsize == 4:
                func_name = "atomic_cmpxchg"
            elif lhs_dtype.numpy_dtype.itemsize == 8:
                func_name = "atom_cmpxchg"
            else:
                raise LoopyError("unexpected atomic size")

            cast_str = ""
            old_val = old_val_var
            new_val = new_val_var

            if lhs_dtype.numpy_dtype.kind == "f":
                if lhs_dtype.numpy_dtype == np.float32:
                    ctype = "int"
                elif lhs_dtype.numpy_dtype == np.float64:
                    ctype = "long"
                else:
                    raise AssertionError()

                from loopy.kernel.data import ArrayArg, TemporaryVariable

                if (
                    isinstance(lhs_var, ArrayArg)
                    and lhs_var.address_space == AddressSpace.GLOBAL
                ):
                    var_kind = ""
                elif (
                    isinstance(lhs_var, ArrayArg)
                    and lhs_var.address_space == AddressSpace.LOCAL
                ):
                    var_kind = "__local"
                elif (
                    isinstance(lhs_var, TemporaryVariable)
                    and lhs_var.address_space == AddressSpace.LOCAL
                ):
                    var_kind = "__local"
                elif (
                    isinstance(lhs_var, TemporaryVariable)
                    and lhs_var.address_space == AddressSpace.GLOBAL
                ):
                    var_kind = ""
                else:
                    raise LoopyError(
                        "unexpected kind of variable '%s' in "
                        "atomic operation: '%s'"
                        % (lhs_var.name, type(lhs_var).__name__)
                    )

                old_val = "*(%s *) &" % ctype + old_val
                new_val = "*(%s *) &" % ctype + new_val
                cast_str = f"({var_kind} {ctype} *) "

            return Block(
                [
                    POD(self, NumpyType(lhs_dtype.dtype), old_val_var),
                    POD(self, NumpyType(lhs_dtype.dtype), new_val_var),
                    DoWhile(
                        "%(func_name)s("
                        "%(cast_str)s&(%(lhs_expr)s), "
                        "%(old_val)s, "
                        "%(new_val)s"
                        ") != %(old_val)s"
                        % {
                            "func_name": func_name,
                            "cast_str": cast_str,
                            "lhs_expr": lhs_expr_code,
                            "old_val": old_val,
                            "new_val": new_val,
                        },
                        Block(
                            [
                                Assign(old_val_var, lhs_expr_code),
                                Assign(new_val_var, rhs_expr_code),
                            ]
                        ),
                    ),
                ]
            )
        else:
            raise NotImplementedError("atomic update for '%s'" % lhs_dtype)

    # }}}


# }}}


# {{{ volatile mem acccess target


class VolatileMemExpressionToSYCLCExpressionMapper(ExpressionToSYCLCExpressionMapper):
    def make_subscript(self, array, base_expr, subscript):
        registry = self.codegen_state.ast_builder.target.get_dtype_registry()

        from loopy.kernel.data import AddressSpace

        if array.address_space == AddressSpace.GLOBAL:
            aspace = " "
        elif array.address_space == AddressSpace.LOCAL:
            aspace = "__local "
        elif array.address_space == AddressSpace.PRIVATE:
            aspace = ""
        else:
            raise ValueError("unexpected value of address space")

        from pymbolic import var

        return var(
            "(%s volatile %s *) "
            % (
                registry.dtype_to_ctype(array.dtype),
                aspace,
            )
        )(base_expr)[subscript]


class VolatileMemSYCLCASTBuilder(SYCLCASTBuilder):
    def get_expression_to_c_expression_mapper(self, codegen_state):
        return VolatileMemExpressionToSYCLCExpressionMapper(codegen_state)


class VolatileMemSYCLTarget(SYCLTarget):
    def get_device_ast_builder(self):
        return VolatileMemSYCLCASTBuilder(self)


# }}}

# vim: foldmethod=marker
