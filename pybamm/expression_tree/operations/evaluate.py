#
# Write a symbol to python
#
import pybamm

import numpy as np
import scipy.sparse
from collections import OrderedDict

import numbers
from platform import system
if system() != "Windows":
    import jax

    from jax.config import config
    config.update("jax_enable_x64", True)


def id_to_python_variable(symbol_id, constant=False):
    """
    This function defines the format for the python variable names used in find_symbols
    and to_python. Variable names are based on a nodes' id to make them unique
    """

    if constant:
        var_format = "const_{:05d}"
    else:
        var_format = "var_{:05d}"

    # Need to replace "-" character to make them valid python variable names
    return var_format.format(symbol_id).replace("-", "m")


def find_symbols(symbol, constant_symbols, variable_symbols, no_sparse=False):
    """
    This function converts an expression tree to a dictionary of node id's and strings
    specifying valid python code to calculate that nodes value, given y and t.

    The function distinguishes between nodes that represent constant nodes in the tree
    (e.g. a pybamm.Matrix), and those that are variable (e.g. subtrees that contain
    pybamm.StateVector). The former are put in `constant_symbols`, the latter in
    `variable_symbols`

    Note that it is important that the arguments `constant_symbols` and
    `variable_symbols` be an *ordered* dict, since the final ordering of the code lines
    are important for the calculations. A dict is specified rather than a list so that
    identical subtrees (which give identical id's) are not recalculated in the code

    Parameters
    ----------
    symbol : :class:`pybamm.Symbol`
        The symbol or expression tree to convert

    constant_symbol: collections.OrderedDict
        The output dictionary of constant symbol ids to lines of code

    variable_symbol: collections.OrderedDict
        The output dictionary of variable (with y or t) symbol ids to lines of code

    no_sparse: bool
        If True, all constants and expressions are converted to using dense matrices

    """
    if symbol.is_constant():
        value = symbol.evaluate()
        if not isinstance(value, numbers.Number):
            if no_sparse and scipy.sparse.issparse(value):
                # convert any remaining sparse matrices to our custom coo matrix
                scipy_coo = value.tocoo()
                row = jax.numpy.asarray(scipy_coo.row)
                col = jax.numpy.asarray(scipy_coo.col)
                data = jax.numpy.asarray(scipy_coo.data)
                constant_symbols[symbol.id] = JaxCooMatrix(row, col, data, value.shape)
            else:
                constant_symbols[symbol.id] = value
        return

    # process children recursively
    for child in symbol.children:
        find_symbols(child, constant_symbols, variable_symbols, no_sparse)

    # calculate the variable names that will hold the result of calculating the
    # children variables
    children_vars = []
    for child in symbol.children:
        if child.is_constant():
            child_eval = child.evaluate()
            if isinstance(child_eval, numbers.Number):
                children_vars.append(str(child_eval))
            else:
                children_vars.append(id_to_python_variable(child.id, True))
        else:
            children_vars.append(id_to_python_variable(child.id, False))

    if isinstance(symbol, pybamm.BinaryOperator):
        # Multiplication and Division need special handling for scipy sparse matrices
        # TODO: we can pass through a dummy y and t to get the type and then hardcode
        # the right line, avoiding these checks
        if isinstance(symbol, pybamm.Multiplication):
            dummy_eval_left = symbol.children[0].evaluate_for_shape()
            dummy_eval_right = symbol.children[1].evaluate_for_shape()
            if scipy.sparse.issparse(dummy_eval_left):
                if no_sparse:
                    print('calling dot_product with', dummy_eval_left, 'and',
                          dummy_eval_right)
                    symbol_str = "{0}.dot_product({1})"\
                        .format(children_vars[0], children_vars[1])
                else:
                    symbol_str = "{0}.multiply({1})"\
                        .format(children_vars[0], children_vars[1])
            elif scipy.sparse.issparse(dummy_eval_right):
                if no_sparse:
                    print('calling dot_product with', dummy_eval_right, 'and',
                          dummy_eval_left)
                    symbol_str = "{1}.dot_product({1})"\
                        .format(children_vars[0], children_vars[1])
                else:
                    symbol_str = "{1}.multiply({0})"\
                        .format(children_vars[0], children_vars[1])
            else:
                symbol_str = "{0} * {1}".format(children_vars[0], children_vars[1])
        elif isinstance(symbol, pybamm.Division):
            dummy_eval_left = symbol.children[0].evaluate_for_shape()
            if scipy.sparse.issparse(dummy_eval_left):
                if no_sparse:
                    print('calling dot_product with', dummy_eval_left, 'and 1/',
                          dummy_eval_right)
                    symbol_str = "{0}.dot_product(1/{1})"\
                        .format(children_vars[0], children_vars[1])
                else:
                    symbol_str = "{0}.multiply(1/{1})"\
                        .format(children_vars[0], children_vars[1])
            else:
                symbol_str = "{0} / {1}".format(children_vars[0], children_vars[1])

        elif isinstance(symbol, pybamm.Inner):
            dummy_eval_left = symbol.children[0].evaluate_for_shape()
            dummy_eval_right = symbol.children[1].evaluate_for_shape()
            if scipy.sparse.issparse(dummy_eval_left):
                print('XXX - found sparse on left inner')
            elif scipy.sparse.issparse(dummy_eval_right):
                print('XXX - found sparse on right inner')

            if scipy.sparse.issparse(dummy_eval_left):
                if no_sparse:
                    print('calling dot_product with', dummy_eval_left, 'and',
                          dummy_eval_right)
                    symbol_str = "{0}.dot_product({1})"\
                        .format(children_vars[0], children_vars[1])
                else:
                    symbol_str = "{0}.multiply({1})"\
                        .format(children_vars[0], children_vars[1])
            elif scipy.sparse.issparse(dummy_eval_right):
                if no_sparse:
                    print('calling dot_product with', dummy_eval_right, 'and',
                          dummy_eval_left)
                    symbol_str = "{0}.dot_product({1})"\
                        .format(children_vars[0], children_vars[1])
                else:
                    symbol_str = "{1}.multiply({0})"\
                        .format(children_vars[0], children_vars[1])
            else:
                symbol_str = "{0} * {1}".format(children_vars[0], children_vars[1])

        elif isinstance(symbol, pybamm.Minimum):
            symbol_str = "np.minimum({},{})".format(children_vars[0], children_vars[1])
        elif isinstance(symbol, pybamm.Maximum):
            symbol_str = "np.maximum({},{})".format(children_vars[0], children_vars[1])
        else:
            symbol_str = children_vars[0] + " " + symbol.name + " " + children_vars[1]

    elif isinstance(symbol, pybamm.UnaryOperator):
        # Index has a different syntax than other univariate operations
        if isinstance(symbol, pybamm.Index):
            symbol_str = "{}[{}:{}]".format(
                children_vars[0], symbol.slice.start, symbol.slice.stop
            )
        else:
            symbol_str = symbol.name + children_vars[0]

    elif isinstance(symbol, pybamm.Function):
        children_str = ""
        for child_var in children_vars:
            if children_str == "":
                children_str = child_var
            else:
                children_str += ", " + child_var
        if isinstance(symbol.function, np.ufunc):
            # write any numpy functions directly
            symbol_str = "np.{}({})".format(symbol.function.__name__, children_str)
        else:
            # unknown function, store it as a constant and call this in the
            # generated code
            constant_symbols[symbol.id] = symbol.function
            funct_var = id_to_python_variable(symbol.id, True)
            symbol_str = "{}({})".format(funct_var, children_str)

    elif isinstance(symbol, pybamm.Concatenation):

        # don't bother to concatenate if there is only a single child
        if isinstance(symbol, pybamm.NumpyConcatenation):
            if len(children_vars) > 1:
                symbol_str = "np.concatenate(({}))".format(",".join(children_vars))
            else:
                symbol_str = "{}".format(",".join(children_vars))

        elif isinstance(symbol, pybamm.SparseStack):
            if len(children_vars) > 1:
                if no_sparse:
                    print('calling jax_coo_vstack with', children_vars)
                    symbol_str = "jax_coo_vstack(({}))".format(",".join(children_vars))
                else:
                    symbol_str = "scipy.sparse.vstack(({}))".format(
                        ",".join(children_vars))
            else:
                symbol_str = "{}".format(",".join(children_vars))

        # DomainConcatenation specifies a particular ordering for the concatenation,
        # which we must follow
        elif isinstance(symbol, pybamm.DomainConcatenation):
            slice_starts = []
            all_child_vectors = []
            for i in range(symbol.secondary_dimensions_npts):
                child_vectors = []
                for child_var, slices in zip(children_vars, symbol._children_slices):
                    for child_dom, child_slice in slices.items():
                        slice_starts.append(symbol._slices[child_dom][i].start)
                        child_vectors.append(
                            "{}[{}:{}]".format(
                                child_var, child_slice[i].start, child_slice[i].stop
                            )
                        )
                all_child_vectors.extend(
                    [v for _, v in sorted(zip(slice_starts, child_vectors))]
                )
            if len(children_vars) > 1 or symbol.secondary_dimensions_npts > 1:
                symbol_str = "np.concatenate(({}))".format(",".join(all_child_vectors))
            else:
                symbol_str = "{}".format(",".join(children_vars))
        else:
            raise NotImplementedError

    # Note: we assume that y is being passed as a column vector
    elif isinstance(symbol, pybamm.StateVector):
        indices = np.argwhere(symbol.evaluation_array).reshape(-1).astype(np.int32)
        consecutive = np.all(indices[1:] - indices[:-1] == 1)
        if len(indices) == 1 or consecutive:
            symbol_str = "y[{}:{}]".format(indices[0], indices[-1] + 1)
        else:
            indices_array = pybamm.Array(indices)
            constant_symbols[indices_array.id] = indices
            index_name = id_to_python_variable(indices_array.id, True)
            symbol_str = "y[{}]".format(index_name)

    elif isinstance(symbol, pybamm.Time):
        symbol_str = "t"

    elif isinstance(symbol, pybamm.InputParameter):
        symbol_str = "inputs['{}']".format(symbol.name)

    else:
        raise NotImplementedError(
            "Not implemented for a symbol of type '{}'".format(type(symbol))
        )

    variable_symbols[symbol.id] = symbol_str


def to_python(symbol, debug=False, no_sparse=False):
    """
    This function converts an expression tree into a dict of constant input values, and
    valid python code that acts like the tree's :func:`pybamm.Symbol.evaluate` function

    Parameters
    ----------
    symbol : :class:`pybamm.Symbol`
        The symbol to convert to python code

    debug : bool
        If set to True, the function also emits debug code

    Returns
    -------
    collections.OrderedDict:
        dict mapping node id to a constant value. Represents all the constant nodes in
        the expression tree
    str:
        valid python code that will evaluate all the variable nodes in the tree.
    no_sparse: bool
        If True, all constants and expressions are converted to using dense matrices

    """

    constant_values = OrderedDict()
    variable_symbols = OrderedDict()
    find_symbols(symbol, constant_values, variable_symbols, no_sparse)

    line_format = "{} = {}"

    if debug:
        variable_lines = [
            "print('{}'); ".format(
                line_format.format(id_to_python_variable(symbol_id, False), symbol_line)
            )
            + line_format.format(id_to_python_variable(symbol_id, False), symbol_line)
            + "; print(type({0}),{0}.shape)".format(
                id_to_python_variable(symbol_id, False)
            )
            for symbol_id, symbol_line in variable_symbols.items()
        ]
    else:
        variable_lines = [
            line_format.format(id_to_python_variable(symbol_id, False), symbol_line)
            for symbol_id, symbol_line in variable_symbols.items()
        ]

    return constant_values, "\n".join(variable_lines)


class EvaluatorPython:
    """
    Converts a pybamm expression tree into pure python code that will calculate the
    result of calling `evaluate(t, y)` on the given expression tree.

    Parameters
    ----------

    symbol : :class:`pybamm.Symbol`
        The symbol to convert to python code


    """

    def __init__(self, symbol):
        constants, python_str = pybamm.to_python(symbol, debug=False)

        # extract constants in generated function
        for i, symbol_id in enumerate(constants.keys()):
            const_name = id_to_python_variable(symbol_id, True)
            python_str = '{} = constants[{}]\n'.format(const_name, i) + python_str

        # constants passed in as an ordered dict, convert to list
        self._constants = list(constants.values())

        # indent code
        python_str = '   ' + python_str
        python_str = python_str.replace('\n', '\n   ')

        # add function def to first line
        python_str = 'def evaluate(constants, t=None, y=None, '\
            'y_dot=None, inputs=None, known_evals=None):\n' + python_str

        # calculate the final variable that will output the result of calling `evaluate`
        # on `symbol`
        result_var = id_to_python_variable(symbol.id, symbol.is_constant())
        if symbol.is_constant():
            result_value = symbol.evaluate()

        # add return line
        if symbol.is_constant() and isinstance(result_value, numbers.Number):
            python_str = python_str + '\n   return ' + str(result_value)
        else:
            python_str = python_str + '\n   return ' + result_var

        # store a copy of examine_jaxpr
        python_str = python_str + \
            '\nself._evaluate = evaluate'

        self._python_str = python_str
        self._symbol = symbol

        # compile and run the generated python code,
        compiled_function = compile(
            python_str, result_var, "exec"
        )
        exec(compiled_function)

    def evaluate(self, t=None, y=None, y_dot=None, inputs=None, known_evals=None):
        """
        Acts as a drop-in replacement for :func:`pybamm.Symbol.evaluate`
        """
        # generated code assumes y is a column vector
        if y is not None and y.ndim == 1:
            y = y.reshape(-1, 1)

        result = self._evaluate(self._constants, t, y, y_dot, inputs, known_evals)

        # don't need known_evals, but need to reproduce Symbol.evaluate signature
        if known_evals is not None:
            return result, known_evals
        else:
            return result


class JaxCooMatrix:
    def __init__(self, row, col, data, shape):
        self.row = row
        self.col = col
        self.data = data
        self.shape = shape
        self.nnz = len(data)

    @jax.partial(jax.jit, static_argnums=(0,))
    def dot_product(self, b):
        print('XXXXXXdot_product', self.shape, b.shape)
        # assume b is a column vector
        result = jax.numpy.zeros((self.shape[0], 1), dtype=b.dtype)
        result = result.at[self.row].add(self.data.reshape(-1, 1) * b[self.col])
        return result

    @jax.partial(jax.jit, static_argnums=(0,))
    def __matmul__(self, b):
        print('XXXXXXmultiply', self.shape, b.shape, b, self.row.shape, self.col.shape,
              self.data.shape)
        result = jax.numpy.zeros((self.shape[0], 1), dtype=b.dtype)
        result = result.at[self.row].add(self.data.reshape(-1, 1) * b[self.col])
        return result.reshape(self.shape[0])

def jax_coo_vstack(blocks):
    M = len(blocks)
    N = 1

    block_mask = np.zeros((M, N), dtype=bool)
    brow_lengths = np.zeros(M, dtype=np.int64)
    bcol_lengths = np.zeros(N, dtype=np.int64)

    nnz = sum(block.nnz for block in blocks[block_mask])

    # assume all the same type
    dtype = blocks[0].data.dtype
    idx_dtype = blocks[0].row.dtype

    row_offsets = np.append(0, np.cumsum(brow_lengths))
    col_offsets = np.append(0, np.cumsum(bcol_lengths))

    shape = (row_offsets[-1], col_offsets[-1])

    data = np.empty(nnz, dtype=dtype)
    row = np.empty(nnz, dtype=idx_dtype)
    col = np.empty(nnz, dtype=idx_dtype)

    nnz = 0
    ii, jj = np.nonzero(block_mask)
    for i, j in zip(ii, jj):
        B = blocks[i, j]
        idx = slice(nnz, nnz + B.nnz)
        data[idx] = B.data
        row[idx] = B.row + row_offsets[i]
        col[idx] = B.col + col_offsets[j]
        nnz += B.nnz

    print('jax_coo_vstack', blocks, row, col, data, shape)

    return JaxCooMatrix(row, col, data, shape)


class EvaluatorJax:
    """
    Converts a pybamm expression tree into pure python code that will calculate the
    result of calling `evaluate(t, y)` on the given expression tree. The resultant code
    is compiled with JAX

    Limitations: JAX currently does not work on expressions involving sparse matrices,
    so any sparse matrices and operations involved sparse matrices are converted to
    their dense equivilents before compilation

    Parameters
    ----------

    symbol : :class:`pybamm.Symbol`
        The symbol to convert to python code


    """

    def __init__(self, symbol):
        constants, python_str = pybamm.to_python(symbol, debug=False, no_sparse=True)

        # replace numpy function calls to jax numpy calls
        python_str = python_str.replace('np.', 'jax.numpy.')

        # convert all numpy constants to device vectors
        for symbol_id in constants:
            if isinstance(constants[symbol_id], np.ndarray):
                constants[symbol_id] = jax.device_put(constants[symbol_id])

        # extract constants in generated function
        for i, symbol_id in enumerate(constants.keys()):
            const_name = id_to_python_variable(symbol_id, True)
            python_str = '{} = constants[{}]\n'.format(const_name, i) + python_str

        # constants passed in as an ordered dict, convert to list
        self._constants = list(constants.values())

        # indent code
        python_str = '   ' + python_str
        python_str = python_str.replace('\n', '\n   ')

        # add function def to first line
        python_str = 'def evaluate_jax(constants, t=None, y=None, '\
            'y_dot=None, inputs=None, known_evals=None):\n' + python_str

        # calculate the final variable that will output the result of calling `evaluate`
        # on `symbol`
        result_var = id_to_python_variable(symbol.id, symbol.is_constant())
        if symbol.is_constant():
            result_value = symbol.evaluate()

        # add return line
        if symbol.is_constant() and isinstance(result_value, numbers.Number):
            python_str = python_str + '\n   return ' + str(result_value)
        else:
            python_str = python_str + '\n   return ' + result_var

        # store a copy of examine_jaxpr
        python_str = python_str + \
            '\nself._evaluate_jax = evaluate_jax'

        # compile and run the generated python code,
        compiled_function = compile(
            python_str, result_var, "exec"
        )
        exec(compiled_function)

        self._jit_evaluate = jax.jit(self._evaluate_jax, static_argnums=(0, 4, 5))

        # store a jit version of evaluate_jax's jacobian
        jacobian_evaluate = jax.jacfwd(self._evaluate_jax, argnums=2)
        self._jac_evaluate = jax.jit(jacobian_evaluate, static_argnums=(0, 4, 5))

    def get_jacobian(self):
        return EvaluatorJaxJacobian(self._jac_evaluate, self._constants)

    def debug(self, t=None, y=None, y_dot=None, inputs=None, known_evals=None):
        # generated code assumes y is a column vector
        if y is not None and y.ndim == 1:
            y = y.reshape(-1, 1)

        # execute code
        jaxpr = jax.make_jaxpr(self._evaluate_jax)(
            self._constants, t, y, y_dot, inputs, known_evals
        ).jaxpr
        print("invars:", jaxpr.invars)
        print("outvars:", jaxpr.outvars)
        print("constvars:", jaxpr.constvars)
        for eqn in jaxpr.eqns:
            print("equation:", eqn.invars, eqn.primitive, eqn.outvars, eqn.params)
        print()
        print("jaxpr:", jaxpr)

    def evaluate(self, t=None, y=None, y_dot=None, inputs=None, known_evals=None):
        """
        Acts as a drop-in replacement for :func:`pybamm.Symbol.evaluate`
        """
        # generated code assumes y is a column vector
        if y is not None and y.ndim == 1:
            y = y.reshape(-1, 1)

        result = self._jit_evaluate(self._constants, t, y, y_dot, inputs, known_evals)

        # don't need known_evals, but need to reproduce Symbol.evaluate signature
        if known_evals is not None:
            return result, known_evals
        else:
            return result


class EvaluatorJaxJacobian:
    def __init__(self, jac_evaluate, constants):
        self._jac_evaluate = jac_evaluate
        self._constants = constants

    def evaluate(self, t=None, y=None, y_dot=None, inputs=None, known_evals=None):
        """
        Acts as a drop-in replacement for :func:`pybamm.Symbol.evaluate`
        """
        # generated code assumes y is a column vector
        if y is not None and y.ndim == 1:
            y = y.reshape(-1, 1)

        # execute code
        result = self._jac_evaluate(self._constants, t, y, y_dot, inputs, known_evals)
        result = result.reshape(result.shape[0], -1)

        # don't need known_evals, but need to reproduce Symbol.evaluate signature
        if known_evals is not None:
            return result, known_evals
        else:
            return result
