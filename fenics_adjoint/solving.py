import backend
import ufl
from pyadjoint.tape import get_working_tape, stop_annotating, annotate_tape, no_annotations
from pyadjoint.block import Block
from .types import Function, DirichletBC
from .types import compat
from .types.function_space import extract_subfunction

# Type dependencies

# TODO: Clean up: some inaccurate comments. Reused code. Confusing naming with dFdm when denoting the control as c.


def solve(*args, **kwargs):
    annotate = annotate_tape(kwargs)

    if annotate:
        tape = get_working_tape()
        block = SolveBlock(*args, **kwargs)
        tape.add_block(block)

    with stop_annotating():
        output = backend.solve(*args, **kwargs)

    if annotate:
        # TODO: Consider if this should be here or in the block constructor.
        #       The immediate reason output isn't added in the block constructor is because it should happen after
        #       the backend call, but the block must be constructed (add dependencies) before the backend call.
        if hasattr(args[1], "create_block_output"):
            block_output = args[1].create_block_output()
        else:
            block_output = args[1].function.create_block_output()
        block.add_output(block_output)

    return output


class SolveBlock(Block):
    def __init__(self, *args, **kwargs):
        super(SolveBlock, self).__init__()
        if isinstance(args[0], ufl.equation.Equation):
            # Variational problem.
            eq = args[0]
            self.lhs = eq.lhs
            self.rhs = eq.rhs
            self.func = args[1]
            self.kwargs = kwargs

            if len(args) > 2:
                self.bcs = args[2]
            elif "bcs" in kwargs:
                self.bcs = self.kwargs.pop("bcs")
            else:
                self.bcs = []

            # make sure self.bcs is always a list
            if self.bcs is None:
                self.bcs = []
            if not isinstance(self.bcs, list):
                self.bcs = [self.bcs]

            # User provided Jacobians J and Jp should probably only be used in forward solve?
            self.forward_kwargs = kwargs.copy()
            self.kwargs.pop("J", None)
            self.kwargs.pop("Jp", None)

            if "M" in self.kwargs:
                raise NotImplemented("Annotation of adaptive solves not implemented.")

            # Some arguments need passing to assemble:
            self.assemble_kwargs = {}
            if "solver_parameters" in kwargs and "mat_type" in kwargs["solver_parameters"]:
                self.assemble_kwargs["mat_type"] = kwargs["solver_parameters"]["mat_type"]

            #self.add_output(self.func.create_block_output())
        else:
            # Linear algebra problem.
            # TODO: Consider checking if attributes exist.
            A = args[0]
            u = args[1]
            b = args[2]

            self.lhs = A.form
            self.rhs = b.form
            self.bcs = A.bcs
            self.func = u.function

        if isinstance(self.lhs, ufl.Form) and isinstance(self.rhs, ufl.Form):
            self.linear = True
            # Add dependence on coefficients on the right hand side.
            for c in self.rhs.coefficients():
                self.add_dependency(c.get_block_output())
        else:
            self.linear = False

        for bc in self.bcs:
            self.add_dependency(bc.get_block_output())

        for c in self.lhs.coefficients():
            self.add_dependency(c.get_block_output())

    def __str__(self):
        return "{} = {}".format(str(self.lhs), str(self.rhs))

    @no_annotations
    def evaluate_adj(self):
        #t = backend.Timer("Solve:evaluate_adj")
        #t4 = backend.Timer("Solve:adj:Prolog")
        fwd_block_output = self.get_outputs()[0]
        u = fwd_block_output.get_output()
        V = u.function_space()
        adj_var = Function(V)

        if self.linear:
            tmp_u = Function(self.func.function_space()) # Replace later? Maybe save function space on initialization.
            F_form = backend.action(self.lhs, tmp_u) - self.rhs
        else:
            tmp_u = self.func
            F_form = self.lhs

        replaced_coeffs = {}
        for block_output in self.get_dependencies():
            coeff = block_output.get_output()
            if coeff in F_form.coefficients():
                replaced_coeffs[coeff] = block_output.get_saved_output()

        replaced_coeffs[tmp_u] = fwd_block_output.get_saved_output()

        F_form = backend.replace(F_form, replaced_coeffs)

        dFdu = backend.derivative(F_form, fwd_block_output.get_saved_output(), backend.TrialFunction(u.function_space()))
        dFdu_form = backend.adjoint(dFdu)
        dFdu = backend.assemble(dFdu_form, **self.assemble_kwargs)

        # Get dJdu from previous calculations.
        dJdu = fwd_block_output.get_adj_output()

        # TODO: It might make sense to move this so we don't have to do the computations above.
        if dJdu is None:
            return

        dJdu_copy = dJdu.copy()

        # Homogenize and apply boundary conditions on adj_dFdu and dJdu.
        bcs = []
        for bc in self.bcs:
            if isinstance(bc, backend.DirichletBC):
                bc = compat.create_bc(bc, homogenize=True)
            bcs.append(bc)
            bc.apply(dFdu, dJdu)

        backend.solve(dFdu, adj_var.vector(), dJdu, **self.kwargs)

        adj_var_bdy = Function(V)
        adj_var_bdy = compat.evaluate_algebra_expression(dJdu_copy -
                                                         backend.assemble(backend.action(dFdu_form, adj_var)),
                                                         adj_var_bdy)
        for block_output in self.get_dependencies():
            c = block_output.get_output()
            if c != self.func or self.linear:
                c_rep = replaced_coeffs.get(c, c)

                if isinstance(c, backend.Function):
                    dFdm = -backend.derivative(F_form, c_rep, backend.TrialFunction(c.function_space()))
                    dFdm = backend.adjoint(dFdm)
                    dFdm = dFdm*adj_var
                    dFdm = backend.assemble(dFdm, **self.assemble_kwargs)

                    block_output.add_adj_output(dFdm)
                elif isinstance(c, backend.Constant):
                    dFdm = -backend.derivative(F_form, c_rep, backend.Constant(1))
                    dFdm = backend.assemble(dFdm, **self.assemble_kwargs)

                    [bc.apply(dFdm) for bc in bcs]

                    block_output.add_adj_output(compat.inner(dFdm, adj_var.vector()))
                elif isinstance(c, backend.DirichletBC):
                    tmp_bc = compat.create_bc(c, value=extract_subfunction(adj_var_bdy, c.function_space()))
                    block_output.add_adj_output([tmp_bc])
                elif isinstance(c, backend.Expression):
                    dFdm = -backend.derivative(F_form, c_rep, backend.TrialFunction(V)) # TODO: What space to use?
                    dFdm = backend.assemble(dFdm, **self.assemble_kwargs)

                    dFdm_mat = backend.as_backend_type(dFdm).mat()

                    import numpy as np
                    bc_rows = []
                    for bc in bcs:
                        for key in bc.get_boundary_values():
                            bc_rows.append(key)

                    dFdm.zero(np.array(bc_rows, dtype=np.intc))

                    dFdm_mat.transpose(dFdm_mat)

                    block_output.add_adj_output([[dFdm*adj_var.vector(), V]])

    @no_annotations
    def evaluate_tlm(self):
        fwd_block_output = self.get_outputs()[0]
        u = fwd_block_output.get_output()
        V = u.function_space()

        if self.linear:
            tmp_u = Function(self.func.function_space()) # Replace later? Maybe save function space on initialization.
            F_form = backend.action(self.lhs, tmp_u) - self.rhs
        else:
            tmp_u = self.func
            F_form = self.lhs

        replaced_coeffs = {}
        for block_output in self.get_dependencies():
            coeff = block_output.get_output()
            if coeff in F_form.coefficients():
                replaced_coeffs[coeff] = block_output.get_saved_output()

        replaced_coeffs[tmp_u] = fwd_block_output.get_saved_output()

        F_form = backend.replace(F_form, replaced_coeffs)

        # Obtain dFdu.
        dFdu = backend.derivative(F_form, fwd_block_output.get_saved_output(), backend.TrialFunction(u.function_space()))

        dFdu = backend.assemble(dFdu, **self.assemble_kwargs)

        # Homogenize and apply boundary conditions on dFdu.
        bcs = []
        for bc in self.bcs:
            if isinstance(bc, backend.DirichletBC):
                bc = compat.create_bc(bc, homogenize=True)
            bcs.append(bc)
            bc.apply(dFdu)

        for block_output in self.get_dependencies():
            tlm_value = block_output.tlm_value
            if tlm_value is None:
                continue

            c = block_output.get_output()
            c_rep = replaced_coeffs.get(c, c)

            if c == self.func:
                continue

            if isinstance(c, backend.Function):
                #dFdm = -backend.derivative(F_form, c_rep, backend.Function(V, tlm_value))
                dFdm = -backend.derivative(F_form, c_rep, tlm_value)
                dFdm = backend.assemble(dFdm, **self.assemble_kwargs)

                # Zero out boundary values from boundary conditions as they do not depend (directly) on c.
                for bc in bcs:
                    bc.apply(dFdm)

            elif isinstance(c, backend.Constant):
                dFdm = -backend.derivative(F_form, c_rep, tlm_value)
                dFdm = backend.assemble(dFdm, **self.assemble_kwargs)

                # Zero out boundary values from boundary conditions as they do not depend (directly) on c.
                for bc in bcs:
                    bc.apply(dFdm)

            elif isinstance(c, backend.DirichletBC):
                #tmp_bc = backend.DirichletBC(V, tlm_value, c_rep.user_sub_domain())
                dFdm = backend.Function(V).vector()
                tlm_value.apply(dFdu, dFdm)

            elif isinstance(c, backend.Expression):
                dFdm = -backend.derivative(F_form, c_rep, tlm_value)
                dFdm = backend.assemble(dFdm, **self.assemble_kwargs)

                # Zero out boundary values from boundary conditions as they do not depend (directly) on c.
                for bc in bcs:
                    bc.apply(dFdm)

            dudm = Function(V)
            backend.solve(dFdu, dudm.vector(), dFdm, **self.kwargs)

            fwd_block_output.add_tlm_output(dudm)

    @no_annotations
    def evaluate_hessian(self):
        # First fetch all relevant values
        fwd_block_output = self.get_outputs()[0]
        adj_input = fwd_block_output.adj_value
        hessian_input = fwd_block_output.hessian_value
        tlm_output = fwd_block_output.tlm_value
        u = fwd_block_output.get_output()
        V = u.function_space()

        # Process the equation forms, replacing values with checkpoints,
        # and gathering lhs and rhs in one single form.
        if self.linear:
            tmp_u = Function(self.func.function_space()) # Replace later? Maybe save function space on initialization.
            F_form = backend.action(self.lhs, tmp_u) - self.rhs
        else:
            tmp_u = self.func
            F_form = self.lhs

        replaced_coeffs = {}
        for block_output in self.get_dependencies():
            coeff = block_output.get_output()
            if coeff in F_form.coefficients():
                replaced_coeffs[coeff] = block_output.get_saved_output()

        replaced_coeffs[tmp_u] = fwd_block_output.get_saved_output()
        F_form = backend.replace(F_form, replaced_coeffs)

        # Define the equation Form. This class is an initial step in refactoring
        # the SolveBlock methods.
        F = Form(F_form, transpose=True)
        F.set_boundary_conditions(self.bcs, fwd_block_output.get_saved_output())

        bcs = F.bcs

        # Using the equation Form we derive dF/du, d^2F/du^2 * du/dm * direction.
        dFdu_form = backend.derivative(F_form, fwd_block_output.get_saved_output())
        d2Fdu2 = ufl.algorithms.expand_derivatives(backend.derivative(dFdu_form, fwd_block_output.get_saved_output(), tlm_output))

        dFdu = backend.adjoint(dFdu_form)
        dFdu = backend.assemble(dFdu, **self.assemble_kwargs)

        for bc in bcs:
            bc.apply(dFdu, adj_input)

        # TODO: First-order adjoint solution should be possible to obtain from the earlier adjoint computations.
        adj_sol = backend.Function(V)
        # Solve the (first order) adjoint equation
        backend.solve(dFdu, adj_sol.vector(), adj_input, **self.kwargs)

        # Second-order adjoint (soa) solution
        adj_sol2 = backend.Function(V)

        # Start piecing together the rhs of the soa equation
        b = hessian_input
        b_form = d2Fdu2

        for bo in self.get_dependencies():
            c = bo.get_output()
            c_rep = replaced_coeffs.get(c, c)
            tlm_input = bo.tlm_value

            if c == self.func or tlm_input is None:
                continue

            if not isinstance(c, backend.DirichletBC):
                d2Fdudm = ufl.algorithms.expand_derivatives(backend.derivative(dFdu_form, c_rep, tlm_input))
                b_form += d2Fdudm

        if len(b_form.integrals()) > 0:
            b_form = backend.adjoint(b_form)
            b -= backend.assemble(backend.action(b_form, adj_sol))
        b_copy = b.copy()

        for bc in bcs:
            bc.apply(dFdu, b)

        # Solve the soa equation
        backend.solve(dFdu, adj_sol2.vector(), b, **self.kwargs)

        adj_sol2_bdy = Function(V)
        adj_sol2_bdy = compat.evaluate_algebra_expression(b_copy -
                                                          backend.assemble(backend.action(dFdu_form, adj_sol2)),
                                                          adj_sol2_bdy)

        # Iterate through every dependency to evaluate and propagate the hessian information.
        for bo in self.get_dependencies():
            c = bo.get_output()
            c_rep = replaced_coeffs.get(c, c)

            if c == self.func and not self.linear:
                continue

            # If m = DirichletBC then d^2F(u,m)/dm^2 = 0 and d^2F(u,m)/dudm = 0,
            # so we only have the term dF(u,m)/dm * adj_sol2
            if isinstance(c, backend.DirichletBC):
                tmp_bc = compat.create_bc(c, value=adj_sol2_bdy)
                #adj_output = Function(V)
                #tmp_bc.apply(adj_output.vector())

                bo.add_hessian_output([tmp_bc])
                continue

            dc = None
            if isinstance(c_rep, backend.Constant):
                dc = backend.Constant(1)
                # TODO: should this be a TrialFunction?
            else:
                dc = backend.TrialFunction(V)
            dFdm = backend.derivative(F_form, c_rep, dc)
            # TODO: Actually implement split annotations properly.
            try:
                d2Fdudm = ufl.algorithms.expand_derivatives(backend.derivative(dFdm, fwd_block_output.get_saved_output(), tlm_output))
            except ufl.log.UFLException:
                continue


            # We need to add terms from every other dependency
            # i.e. the terms d^2F/dm_1dm_2
            for bo2 in self.get_dependencies():
                c2 = bo2.get_output()
                c2_rep = replaced_coeffs.get(c2, c2)

                if isinstance(c2, backend.DirichletBC):
                    continue

                tlm_input = bo2.tlm_value
                if tlm_input is None:
                    continue

                if c2 == self.func and not self.linear:
                    continue

                d2Fdm2 = ufl.algorithms.expand_derivatives(backend.derivative(dFdm, c2_rep, tlm_input))
                if d2Fdm2.empty():
                    continue

                if len(d2Fdm2.arguments()) >= 2:
                    d2Fdm2 = backend.adjoint(d2Fdm2)

                output = backend.action(d2Fdm2, adj_sol)
                output = backend.assemble(-output)

                if isinstance(c, backend.Expression):
                    bo.add_hessian_output([(output, V)])
                else:
                    bo.add_hessian_output(output)

            if len(dFdm.arguments()) >= 2:
                dFdm = backend.adjoint(dFdm)
            output = backend.action(dFdm, adj_sol2)
            if not d2Fdudm.empty():
                if len(d2Fdudm.arguments()) >= 2:
                    d2Fdudm = backend.adjoint(d2Fdudm)
                output += backend.action(d2Fdudm, adj_sol)

            output = backend.assemble(-output)

            if isinstance(c, backend.Expression):
                bo.add_hessian_output([(output, V)])
            else:
                bo.add_hessian_output(output)

    @no_annotations
    def recompute(self):
        func = self.func
        replace_lhs_coeffs = {}
        replace_rhs_coeffs = {}
        for block_output in self.get_dependencies():
            c = block_output.output
            c_rep = block_output.get_saved_output()

            if c != c_rep:
                if c in self.lhs.coefficients():
                    replace_lhs_coeffs[c] = c_rep
                    if c == self.func:
                        func = c_rep
                        block_output.checkpoint = c_rep._ad_create_checkpoint()
                
                if self.linear and c in self.rhs.coefficients():
                    replace_rhs_coeffs[c] = c_rep

        lhs = backend.replace(self.lhs, replace_lhs_coeffs)
        
        rhs = 0
        if self.linear:
            rhs = backend.replace(self.rhs, replace_rhs_coeffs)

        backend.solve(lhs == rhs, func, self.bcs, **self.forward_kwargs)
        # Save output for use in later re-computations.
        # TODO: Consider redesigning the saving system so a new deepcopy isn't created on each forward replay.
        self.get_outputs()[0].checkpoint = func._ad_create_checkpoint()


class Form(object):
    def __init__(self, form, transpose=False):
        self.form = form
        self.rank = len(form.arguments())
        self.transpose = transpose
        self._data = None

        # Boundary conditions
        self.bcs = None
        self.bc_rows = None
        self.sol_var = None
        self.bc_type = 0

    def derivative(self, coefficient, argument=None, function_space=None):
        dc = argument
        if dc is None:
            if isinstance(coefficient, backend.Constant):
                dc = backend.Constant(1)
            elif isinstance(coefficient, backend.Expression):
                dc = backend.TrialFunction(function_space)

        diff_form = ufl.algorithms.expand_derivatives(backend.derivative(self.form, coefficient, dc))
        ret = Form(diff_form, transpose=self.transpose)
        ret.bcs = self.bcs
        ret.bc_rows = self.bc_rows
        ret.sol_var = self.sol_var

        # Unintuitive way of solving this problem.
        # TODO: Consider refactoring.
        if coefficient == self.sol_var:
            ret.bc_type = self.bc_type + 1
        else:
            ret.bc_type = self.bc_type + 2

        return ret

    def transpose(self):
        transpose = False if self.transpose else True
        return Form(self.form, transpose=transpose)

    def set_boundary_conditions(self, bcs, sol_var):
        self.bcs = []
        self.bc_rows = []
        self.sol_var = sol_var
        for bc in bcs:
            if isinstance(bc, backend.DirichletBC):
                bc = compat.create_bc(bc, homogenize=True)
            self.bcs.append(bc)

            # for key in bc.get_boundary_values():
            #     self.bc_rows.append(key)

    def apply_boundary_conditions(self, data):
        import numpy
        if self.bc_type >= 2:
            if self.rank >= 2:
                data.zero(numpy.array(self.bc_rows, dtype=numpy.intc))
            else:
                [bc.apply(data) for bc in self.bcs]
        else:
            [bc.apply(data) for bc in self.bcs]

    @property
    def data(self):
        return self.compute()

    def compute(self):
        if self._data is not None:
            return self._data

        if self.form.empty():
            return None

        data = backend.assemble(self.form)

        # Apply boundary conditions here!
        if self.bcs:
            self.apply_boundary_conditions(data)

        # Transpose if needed
        if self.transpose and self.rank >= 2:
            matrix_mat = backend.as_backend_type(data).mat()
            matrix_mat.transpose(matrix_mat)

        self._data = data
        return self._data

    def __mul__(self, other):
        if self.data is None:
            return 0

        if isinstance(other, Form):
            return self.data*other

        if isinstance(other, compat.MatrixType):
            if self.rank >= 2:
                return self.data*other
            else:
                # We (almost?) always want Matrix*Vector multiplication in this case.
                return other*self.data
        elif isinstance(other, compat.VectorType):
            if self.rank >= 2:
                return self.data*other
            else:
                return self.data.inner(other)

        # If it reaches this point I have done something wrong.
        return 0
