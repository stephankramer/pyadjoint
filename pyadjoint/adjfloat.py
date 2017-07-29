from .tape import get_working_tape, annotate_tape
from .block import Block
from .overloaded_type import OverloadedType
from numpy import generic


def annotate_operator(operator):
    """Decorate float operator like __add__, __sub__, etc.

    The provided operator is only expected to create the Block that
    corresponds to this operation. The decorator returns a wrapper code
    that checks whether annotation is needed, ensures all arguments are
    overloaded, calls the block-creating operator, and puts the Block
    on tape."""

    # the actual float operation is derived from the name of operator
    float_op = getattr(float, operator.__name__)

    def annotated_operator(self, *args):
        output = float_op(self, *args)
        if output is NotImplemented:
            return NotImplemented

        # ensure all arguments are of OverloadedType
        args = [arg if isinstance(arg, OverloadedType) else self.__class__(arg) for arg in args]

        output = self.__class__(output)
        if annotate_tape():
            block = operator(self, *args)

            tape = get_working_tape()
            tape.add_block(block)
            block.add_output(output.get_block_output())

        return output

    return annotated_operator


class AdjFloat(OverloadedType, float, generic):
    def __new__(cls, *args, **kwargs):
        return float.__new__(cls, *args)

    def __init__(self, *args, **kwargs):
        super(AdjFloat, self).__init__(*args, **kwargs)

    @annotate_operator
    def __mul__(self, other):
        return MulBlock(self, other)

    @annotate_operator
    def __rmul__(self, other):
        return MulBlock(self, other)

    @annotate_operator
    def __add__(self, other):
        return AddBlock(self, other)

    @annotate_operator
    def __radd__(self, other):
        return AddBlock(self, other)

    @annotate_operator
    def __sub__(self, other):
        return SubBlock(self, other)

    @annotate_operator
    def __rsub__(self, other):
        # NOTE: order is important here
        return SubBlock(other, self)

    @annotate_operator
    def __pow__(self, power):
        return PowBlock(self, power)

    def get_derivative(self, options={}):
        return self.__class__(self.get_adj_output())

    def adj_update_value(self, value):
        self.original_block_output.checkpoint = value

    def _ad_create_checkpoint(self):
        # Floats are immutable.
        return self

    def _ad_restore_at_checkpoint(self, checkpoint):
        return checkpoint

    def _ad_mul(self, other):
        return self*other

    def _ad_add(self, other):
        return self+other

    def _ad_dot(self, other):
        return float.__mul__(self, other)


class FloatOperatorBlock(Block):

    # the float operator annotated in this Block
    operator = None

    def __init__(self, *args):
        super(FloatOperatorBlock, self).__init__()
        # the terms are stored seperately here and added as dependencies
        # this is because get_dependencies() only returns the terms with
        # duplicates taken out; for evaluation however order and position
        # of the terms is significant
        self.terms = [arg.get_block_output() for arg in args]
        for term in self.terms:
            self.add_dependency(term)

    def recompute(self):
        self.get_outputs()[0].checkpoint = self.operator(*(term.get_saved_output() for term in self.terms))


class PowBlock(FloatOperatorBlock):

    operator = staticmethod(float.__pow__)

    def evaluate_adj(self):
        adj_input = self.get_outputs()[0].adj_value
        if adj_input is None:
            return

        base = self.terms[0]
        exponent = self.terms[1]

        base_value = base.get_saved_output()
        exponent_value = exponent.get_saved_output()

        base_adj = adj_input*exponent_value*base_value**(exponent_value-1)
        base.add_adj_output(base_adj)

        from numpy import log
        exponent_adj = adj_input*base_value**exponent_value*log(base_value)
        exponent.add_adj_output(exponent_adj)


class AddBlock(FloatOperatorBlock):

    operator = staticmethod(float.__add__)

    def evaluate_adj(self):
        adj_input = self.get_outputs()[0].get_adj_output()
        if adj_input is None:
            return

        for term in self.terms:
            term.add_adj_output(adj_input)


class SubBlock(FloatOperatorBlock):

    operator = staticmethod(float.__sub__)

    def evaluate_adj(self):
        adj_input = self.get_outputs()[0].get_adj_output()
        if adj_input is None:
            return

        self.terms[0].add_adj_output(adj_input)
        self.terms[1].add_adj_output(-adj_input)


class MulBlock(FloatOperatorBlock):

    operator = staticmethod(float.__mul__)

    def evaluate_adj(self):
        adj_input = self.get_outputs()[0].get_adj_output()
        if adj_input is None:
            return

        self.terms[0].add_adj_output(adj_input * self.terms[1].get_saved_output())
        self.terms[1].add_adj_output(adj_input * self.terms[0].get_saved_output())
