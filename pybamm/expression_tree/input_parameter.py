#
# Parameter classes
#
import numpy as np
import pybamm


class InputParameter(pybamm.Symbol):
    """A node in the expression tree representing an input parameter

    This node's value can be set at the point of solving, allowing parameter estimation
    and control

    Parameters
    ----------
    name : str
        name of the node

    """

    def __init__(self, name):
        super().__init__(name)

    def new_copy(self):
        """ See :meth:`pybamm.Symbol.new_copy()`. """
        return InputParameter(self.name)

    def evaluate_for_shape(self):
        """
        Returns the scalar 'NaN' to represent the shape of a parameter.
        See :meth:`pybamm.Symbol.evaluate_for_shape()`
        """
        return np.nan

    def evaluate(self, t=None, y=None, u=None, known_evals=None):
        # u should be a dictionary
        if not isinstance(u, dict):
            raise TypeError("inputs u should be a dictionary")
        return u[self.name]

