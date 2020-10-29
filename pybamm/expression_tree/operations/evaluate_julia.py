#
# Write a symbol to Julia
#
import pybamm

import numpy as np
import scipy.sparse
import re
from collections import OrderedDict

import numbers


def id_to_julia_variable(symbol_id, constant=False):
    """
    This function defines the format for the julia variable names used in find_symbols
    and to_julia. Variable names are based on a nodes' id to make them unique
    """

    if constant:
        var_format = "const_{:05d}"
    else:
        var_format = "cache_{:05d}"

    # Need to replace "-" character to make them valid julia variable names
    return var_format.format(symbol_id).replace("-", "m")


def find_symbols(symbol, constant_symbols, variable_symbols, variable_symbol_sizes):
    """
    This function converts an expression tree to a dictionary of node id's and strings
    specifying valid julia code to calculate that nodes value, given y and t.

    The function distinguishes between nodes that represent constant nodes in the tree
    (e.g. a pybamm.Matrix), and those that are variable (e.g. subtrees that contain
    pybamm.StateVector). The former are put in `constant_symbols`, the latter in
    `variable_symbols`

    Note that it is important that the arguments `constant_symbols` and
    `variable_symbols` be and *ordered* dict, since the final ordering of the code lines
    are important for the calculations. A dict is specified rather than a list so that
    identical subtrees (which give identical id's) are not recalculated in the code

    Parameters
    ----------
    symbol : :class:`pybamm.Symbol`
        The symbol or expression tree to convert

    constant_symbol : collections.OrderedDict
        The output dictionary of constant symbol ids to lines of code

    variable_symbol : collections.OrderedDict
        The output dictionary of variable (with y or t) symbol ids to lines of code

    variable_symbol_sizes : collections.OrderedDict
        The output dictionary of variable (with y or t) symbol ids to size of that
        variable, for caching

    """
    if symbol.is_constant():
        value = symbol.evaluate()
        if not isinstance(value, numbers.Number):
            if scipy.sparse.issparse(value):
                # Create Julia SparseArray
                row, col, data = scipy.sparse.find(value)
                m, n = value.shape
                # Set print options large enough to avoid ellipsis
                # at least as big is len(row) = len(col) = len(data)
                np.set_printoptions(
                    threshold=max(np.get_printoptions()["threshold"], len(row) + 10)
                )
                # add 1 to correct for 1-indexing in Julia
                # use array2string so that commas are included
                constant_symbols[symbol.id] = "sparse({}, {}, {}, {}, {})".format(
                    np.array2string(row + 1, separator=","),
                    np.array2string(col + 1, separator=","),
                    np.array2string(data, separator=","),
                    m,
                    n,
                )
            elif value.shape == (1, 1):
                # Extract value if array has only one entry
                constant_symbols[symbol.id] = value[0, 0]
            elif value.shape[1] == 1:
                # Set print options large enough to avoid ellipsis
                # at least as big as len(row) = len(col) = len(data)
                np.set_printoptions(
                    threshold=max(
                        np.get_printoptions()["threshold"], value.shape[0] + 10
                    )
                )
                # Flatten a 1D array
                constant_symbols[symbol.id] = np.array2string(
                    value.flatten(), separator=","
                )
            else:
                constant_symbols[symbol.id] = value
        return

    # process children recursively
    for child in symbol.children:
        find_symbols(child, constant_symbols, variable_symbols, variable_symbol_sizes)

    # calculate the variable names that will hold the result of calculating the
    # children variables
    children_vars = []
    for child in symbol.children:
        if child.is_constant():
            child_eval = child.evaluate()
            if isinstance(child_eval, numbers.Number):
                children_vars.append(str(child_eval))
            else:
                children_vars.append(id_to_julia_variable(child.id, True))
        else:
            children_vars.append(id_to_julia_variable(child.id, False))

    if isinstance(symbol, pybamm.BinaryOperator):
        # Multiplication and Division need special handling for scipy sparse matrices
        # TODO: we can pass through a dummy y and t to get the type and then hardcode
        # the right line, avoiding these checks
        if isinstance(symbol, pybamm.MatrixMultiplication):
            symbol_str = "{0} * {1}".format(children_vars[0], children_vars[1])
        elif isinstance(symbol, pybamm.Inner):
            symbol_str = "{0} .* {1}".format(children_vars[0], children_vars[1])
        elif isinstance(symbol, pybamm.Minimum):
            symbol_str = "np.minimum({},{})".format(children_vars[0], children_vars[1])
        elif isinstance(symbol, pybamm.Maximum):
            symbol_str = "np.maximum({},{})".format(children_vars[0], children_vars[1])
        elif isinstance(symbol, pybamm.Power):
            # julia uses ^ instead of ** for power
            # include dot for elementwise operations
            symbol_str = children_vars[0] + " .^ " + children_vars[1]
        else:
            # all other operations use the same symbol
            # include dot: all other operations should be elementwise
            symbol_str = children_vars[0] + " ." + symbol.name + " " + children_vars[1]

    elif isinstance(symbol, pybamm.UnaryOperator):
        # Index has a different syntax than other univariate operations
        if isinstance(symbol, pybamm.Index):
            # Because of how julia indexing works, add 1 to the start, but not to the
            # stop
            symbol_str = "{}[{}:{}]".format(
                children_vars[0], symbol.slice.start + 1, symbol.slice.stop
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
        # write functions directly
        julia_name = symbol.julia_name
        # add a . to allow elementwise operations
        symbol_str = "{}.({})".format(julia_name, children_str)

    elif isinstance(symbol, pybamm.Concatenation):

        # don't bother to concatenate if there is only a single child
        if isinstance(symbol, (pybamm.NumpyConcatenation, pybamm.SparseStack)):
            # return a list of the children variables, which will be converted to a
            # line by line assignment
            symbol_str = children_vars
            # if len(children_vars) == 1:
            #     symbol_str = children_vars[0]
            # else:
            #     symbol_str = "vcat({})".format(",".join(children_vars))

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
                            "@view {}[{}:{}]".format(
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
        # add 1 since julia uses 1-indexing
        indices += 1
        consecutive = np.all(indices[1:] - indices[:-1] == 1)
        if len(indices) == 1:
            symbol_str = "@view y[{}]".format(indices[0])
        elif consecutive:
            # julia does include the final value
            symbol_str = "@view y[{}:{}]".format(indices[0], indices[-1])
        else:
            indices_array = pybamm.Array(indices)
            constant_symbols[indices_array.id] = indices
            index_name = id_to_julia_variable(indices_array.id, True)
            symbol_str = "@view y[{}]".format(index_name)

    elif isinstance(symbol, pybamm.Time):
        symbol_str = "t"

    elif isinstance(symbol, pybamm.InputParameter):
        symbol_str = "inputs['{}']".format(symbol.name)

    elif isinstance(symbol, pybamm.Variable):
        # No need to do anything if a Variable is found
        return

    else:
        raise NotImplementedError(
            "Conversion to Julia not implemented for a symbol of type '{}'".format(
                type(symbol)
            )
        )

    variable_symbols[symbol.id] = symbol_str

    # Save the size of the variable
    symbol_shape = symbol.shape
    if symbol_shape[1] != 1:
        raise ValueError("expected column vector")
    variable_symbol_sizes[symbol.id] = symbol_shape[0]


def to_julia(symbol, debug=False):
    """
    This function converts an expression tree into a dict of constant input values, and
    valid julia code that acts like the tree's :func:`pybamm.Symbol.evaluate` function

    Parameters
    ----------
    symbol : :class:`pybamm.Symbol`
        The symbol to convert to julia code

    Returns
    -------
    constant_values : collections.OrderedDict
        dict mapping node id to a constant value. Represents all the constant nodes in
        the expression tree
    str
        valid julia code that will evaluate all the variable nodes in the tree.

    """

    constant_values = OrderedDict()
    variable_symbols = OrderedDict()
    variable_symbol_sizes = OrderedDict()
    find_symbols(symbol, constant_values, variable_symbols, variable_symbol_sizes)

    # line_format = "{} .= {}"

    # if debug:
    #     variable_lines = [
    #         "print('{}'); ".format(
    #             line_format.format(id_to_julia_variable(symbol_id, False), symbol_line)
    #         )
    #         + line_format.format(id_to_julia_variable(symbol_id, False), symbol_line)
    #         + "; print(type({0}),{0}.shape)".format(
    #             id_to_julia_variable(symbol_id, False)
    #         )
    #         for symbol_id, symbol_line in variable_symbols.items()
    #     ]
    # else:
    #     variable_lines = [
    #         line_format.format(id_to_julia_variable(symbol_id, False), symbol_line)
    #         for symbol_id, symbol_line in variable_symbols.items()
    #     ]

    return constant_values, variable_symbols, variable_symbol_sizes


def get_julia_function(symbol, funcname="f"):
    """
    Converts a pybamm expression tree into pure julia code that will calculate the
    result of calling `evaluate(t, y)` on the given expression tree.

    Parameters
    ----------
    symbol : :class:`pybamm.Symbol`
        The symbol to convert to julia code

    Returns
    -------
    julia_str : str
        String of julia code, to be evaluated by ``julia.Main.eval``

    """

    constants, var_symbols, var_symbol_sizes = to_julia(symbol, debug=False)

    # extract constants in generated function
    const_and_cache_str = "const cs=(\n"
    for symbol_id, const_value in constants.items():
        const_name = id_to_julia_variable(symbol_id, True)
        const_and_cache_str += "   {} = {},\n".format(const_name, const_value)

    # Pop (get and remove) items from the dictionary of symbols one by one
    # If they are simple operations (@view, .+, .-, .*, ./), replace all future
    # occurences instead of assigning them. This "inlining" speeds up the computation
    inlineable_symbols = ["@view", ".+", ".-", ".*", "./"]
    var_str = ""
    while var_symbols:
        var_symbol_id, symbol_line = var_symbols.popitem(last=False)
        julia_var = id_to_julia_variable(var_symbol_id, False)
        # use mul! for matrix multiplications (requires LinearAlgebra library)
        if " * " in symbol_line:
            symbol_line = symbol_line.replace(" * ", ", ")
            var_str += "mul!({}, {})\n".format(julia_var, symbol_line)
        else:
            # inline operation if it can be inlined
            if any(x in symbol_line for x in inlineable_symbols):
                # replace all other occurrences of the variable
                # in the dictionary with the symbol line
                for key, value in var_symbols.items():
                    if not ("mul!" in value and not "@view" in symbol_line):
                        var_symbols[key] = value.replace(julia_var, symbol_line)

            # otherwise assign
            else:
                var_str += "{} .= {}\n".format(julia_var, symbol_line)
    # add "c." to constant and cache names
    var_str = var_str.replace("const", "c.const")
    var_str = var_str.replace("cache", "c.cache")
    # indent code
    var_str = "   " + var_str
    var_str = var_str.replace("\n", "\n   ")

    # add the cache variables to the cache NamedTuple
    for var_symbol_id, var_symbol_size in var_symbol_sizes.items():
        # Skip caching the result variable since this is provided as dy
        # Also skip caching the result variable if it doesn't appear in the var_str,
        # since it has been inlined and does not need to be assigned to
        julia_var = id_to_julia_variable(var_symbol_id, False)
        if var_symbol_id != symbol.id and julia_var in var_str:
            cache_name = id_to_julia_variable(var_symbol_id, False)
            const_and_cache_str += "   {} = zeros({}),\n".format(
                cache_name, var_symbol_size
            )

    # close the constants and cache string
    const_and_cache_str += ")\n"

    # add function def and sparse arrays to first line
    imports = "begin\nusing SparseArrays, LinearAlgebra\n\n"
    julia_str = (
        imports
        + const_and_cache_str
        + f"\nfunction {funcname}_with_consts(dy, y, p, t, c)\n"
        + var_str
    )

    # calculate the final variable that will output the result
    result_var = id_to_julia_variable(symbol.id, symbol.is_constant())
    if symbol.is_constant():
        result_value = symbol.evaluate()

    # assign the return variable
    if symbol.is_constant() and isinstance(result_value, numbers.Number):
        julia_str = julia_str + "\n   dy .= " + str(result_value) + "\n"
    else:
        julia_str = julia_str.replace("c." + result_var, "dy")

    # close the function
    julia_str += "end\n\n"
    julia_str = julia_str.replace("\n   end", "\nend")

    # Return the function with the cached variables passed in
    julia_str += f"{funcname}(dy, y, p, t) = {funcname}_with_consts(dy, y, p, t, cs)\n"

    # close the "begin"
    julia_str += "end"

    return julia_str


def get_julia_mtk_model(model):
    """
    Converts a pybamm model into a Julia ModelingToolkit model

    Parameters
    ----------
    model : :class:`pybamm.BaseModel`
        The model to be converted

    Returns
    -------
    mtk_str : str
        String of julia code representing a model in MTK,
        to be evaluated by ``julia.Main.eval``
    """
    mtk_str = "begin\n"
    # Define parameters
    # Makes a line of the form '@parameters t a b c d'
    mtk_str += "@parameters t"
    for param in model.input_parameters:
        mtk_str += f" {param.name}"
    mtk_str += "\n"

    # Define variables
    variables = {**model.rhs, **model.algebraic}.keys()
    variable_id_to_number = {var.id: f"x{i+1}" for i, var in enumerate(variables)}

    # Add a comment with the variable names
    for var in variables:
        mtk_str += f"# {var.name} -> {variable_id_to_number[var.id]}\n"
    # Makes a line of the form '@variables x1(t) x2(t)'
    mtk_str += "@variables"
    for var in variable_id_to_number.values():
        mtk_str += f" {var}(t)"
    mtk_str += "\n"

    # Define derivatives
    mtk_str += "@derivatives D'~t\n\n"

    # Define equations
    all_eqns_str = ""
    all_constants_str = ""
    all_julia_str = ""
    for var, eqn in {**model.rhs, **model.algebraic}.items():
        constants, julia_str = to_julia(eqn, debug=False)

        # extract constants in generated function
        for eqn_id, const_value in constants.items():
            const_name = id_to_julia_variable(eqn_id, True)
            all_constants_str += "{} = {}\n".format(const_name, const_value)

        # add a comment labeling the equation, and the equation itself
        if julia_str == "":
            all_julia_str = ""
        else:
            all_julia_str += f"# '{var.name}' equation\n" + julia_str + "\n"

        # calculate the final variable that will output the result
        result_var = id_to_julia_variable(eqn.id, eqn.is_constant())
        if eqn.is_constant():
            result_value = eqn.evaluate()

        # define the variable that goes into the equation
        if eqn.is_constant() and isinstance(result_value, numbers.Number):
            eqn_str = str(result_value)
        else:
            eqn_str = result_var

        if var in model.rhs:
            all_eqns_str += f"   D({variable_id_to_number[var.id]}) ~ {eqn_str},\n"
        elif var in model.algebraic:
            all_eqns_str += f"   0 ~ {eqn_str},\n"

    # Replace variables in the julia strings that correspond to pybamm variables with
    # their julia equivalent
    for var_id, julia_id in variable_id_to_number.items():
        all_julia_str = all_julia_str.replace(
            id_to_julia_variable(var_id, False), julia_id
        )

    # Replace parameters in the julia strings in the form "inputs[name]"
    # with just "name"
    for param in model.input_parameters:
        # Replace 'var_id' with 'paran.name'
        all_julia_str = all_julia_str.replace(
            id_to_julia_variable(param.id, False), param.name
        )
        # Remove the line where the variable is re-defined
        all_julia_str = all_julia_str.replace(
            f"{param.name} = inputs['{param.name}']\n", ""
        )

    # Update the MTK string
    mtk_str += all_constants_str + all_julia_str + "\n" + f"eqs = [\n{all_eqns_str}]\n"

    # Create ODESystem
    mtk_str += "sys = ODESystem(eqs, t)\n\n"

    # Create initial conditions
    all_ics_str = ""
    all_constants_str = ""
    all_julia_str = ""
    for var, eqn in model.initial_conditions.items():
        constants, julia_str = to_julia(eqn, debug=False)

        # extract constants in generated function
        for eqn_id, const_value in constants.items():
            const_name = id_to_julia_variable(eqn_id, True)
            all_constants_str += "{} = {}\n".format(const_name, const_value)

        # add a comment labeling the equation, and the equation itself
        if julia_str == "":
            all_julia_str = ""
        else:
            all_julia_str += f"# '{var.name}' initial conditions\n" + julia_str + "\n"

        # calculate the final variable that will output the result
        result_var = id_to_julia_variable(eqn.id, eqn.is_constant())
        if eqn.is_constant():
            result_value = eqn.evaluate()

        # define the variable that goes into the equation
        if eqn.is_constant() and isinstance(result_value, numbers.Number):
            eqn_str = str(result_value)
        else:
            raise pybamm.ModelError

        all_ics_str += f"   {variable_id_to_number[var.id]} => {eqn_str},\n"

    mtk_str += all_constants_str + all_julia_str + "\n" + f"u0 = [\n{all_ics_str}]\n"

    mtk_str += "end\n"

    return mtk_str