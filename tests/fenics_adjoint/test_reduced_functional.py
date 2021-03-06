import pytest
pytest.importorskip("fenics")

from fenics import *
from fenics_adjoint import *


def test_constant():
    mesh = IntervalMesh(10, 0, 1)
    V = FunctionSpace(mesh, "Lagrange", 1)

    c = Constant(1)
    f = Function(V)
    f.vector()[:] = 1

    u = Function(V)
    v = TestFunction(V)
    bc = DirichletBC(V, Constant(1), "on_boundary")

    F = inner(grad(u), grad(v))*dx - f**2*v*dx
    solve(F == 0, u, bc)

    J = assemble(c**2*u*dx)
    Jhat = ReducedFunctional(J, c)
    assert(taylor_test(Jhat, Constant(5), Constant(1)) > 1.9)


def test_function():
    mesh = IntervalMesh(10, 0, 1)
    V = FunctionSpace(mesh, "Lagrange", 1)

    c = Constant(1)
    f = Function(V)
    f.vector()[:] = 1

    u = Function(V)
    v = TestFunction(V)
    bc = DirichletBC(V, Constant(1), "on_boundary")

    F = inner(grad(u), grad(v))*dx - f**2*v*dx
    solve(F == 0, u, bc)

    J = assemble(c**2*u*dx)
    Jhat = ReducedFunctional(J, f)
    
    h = Function(V)
    from numpy.random import rand
    h.vector()[:] = rand(V.dim())
    # Note that if you use f directly, it will not work
    # as expected since f is the control and thus the initial point in control
    # space is changed as you do the test. (Since f.vector is also assigned new values on pertubations)
    g = f.copy(deepcopy=True)

    assert(taylor_test(Jhat, g, h) > 1.9)


def test_wrt_function_dirichlet_boundary():
    mesh = UnitSquareMesh(10,10)

    V = FunctionSpace(mesh,"CG",1)
    u = TrialFunction(V)
    u_ = Function(V)
    v = TestFunction(V)

    class Up(SubDomain):
        def inside(self, x, on_boundary):
            return near(x[1], 1)

    class Down(SubDomain):
        def inside(self, x, on_boundary):
            return near(x[1], 0)

    class Left(SubDomain):
        def inside(self, x, on_boundary):
            return near(x[0], 0)

    class Right(SubDomain):
        def inside(self, x, on_boundary):
            return near(x[0], 1)

    left = Left()
    right = Right()
    up = Up()
    down = Down()

    boundary = FacetFunction("size_t", mesh)
    boundary.set_all(0)
    up.mark(boundary, 1)
    down.mark(boundary,2)
    ds = Measure("ds", subdomain_data=boundary)

    bc_func = project(Expression("sin(x[1])", degree=1), V)
    bc1 = DirichletBC(V,bc_func,left)
    bc2 = DirichletBC(V,2,right)
    bc = [bc1,bc2]

    g1 = Constant(2)
    g2 = Constant(1)
    f = Function(V)
    f.vector()[:] = 10

    a = inner(grad(u), grad(v))*dx
    L = inner(f,v)*dx + inner(g1,v)*ds(1) + inner(g2,v)*ds(2)

    solve(a==L,u_,bc)

    J = assemble(u_**2*dx)

    Jhat = ReducedFunctional(J, bc_func)
    h = Function(V)
    h.vector()[:] = 1

    g = bc_func.copy(deepcopy=True)

    assert(taylor_test(Jhat, g, h) > 1.9)


def test_time_dependent():
    # Defining the domain, 100 points from 0 to 1
    mesh = IntervalMesh(100, 0, 1)

    # Defining function space, test and trial functions
    V = FunctionSpace(mesh,"CG",1)
    u = TrialFunction(V)
    u_ = Function(V)
    v = TestFunction(V)

    # Marking the boundaries
    def left(x, on_boundary):
        return near(x[0],0)

    def right(x, on_boundary):
        return near(x[0],1)

    # Dirichlet boundary conditions
    bc_left = DirichletBC(V, 1, left)
    bc_right = DirichletBC(V, 2, right)
    bc = [bc_left, bc_right]

    # Some variables
    T = 0.5
    dt = 0.1
    f = Function(V)
    f.vector()[:] = 1

    u_1 = Function(V)
    u_1.vector()[:] = 1 

    a = u_1*u*v*dx + dt*f*inner(grad(u),grad(v))*dx
    L = u_1*v*dx

    # Time loop
    t = dt
    while t <= T:
        solve(a == L, u_, bc)
        u_1.assign(u_)
        t += dt

    J = assemble(u_1**2*dx)

    Jhat = ReducedFunctional(J, u_1)
    
    h = Function(V)
    h.vector()[:] = 1
    g = f.copy(deepcopy=True)
    assert(taylor_test(Jhat, g, h) > 1.9)

def test_burgers():
    n = 30
    mesh = UnitIntervalMesh(n)
    V = FunctionSpace(mesh, "CG", 2)

    def Dt(u, u_, timestep):
        return (u - u_)/timestep

    pr = project(Expression("sin(2*pi*x[0])", degree=1), V)
    ic = Function(V)
    ic.vector()[:] = pr.vector()[:]

    u_ = Function(V)
    u = Function(V)
    v = TestFunction(V)

    nu = Constant(0.0001)

    timestep = Constant(1.0/n)

    F = (Dt(u, ic, timestep)*v
         + u*u.dx(0)*v + nu*u.dx(0)*v.dx(0))*dx
    bc = DirichletBC(V, 0.0, "on_boundary")

    t = 0.0
    solve(F == 0, u, bc)
    u_.assign(u)
    t += float(timestep)

    F = (Dt(u, u_, timestep)*v
         + u*u.dx(0)*v + nu*u.dx(0)*v.dx(0))*dx

    end = 0.2
    while (t <= end):
        solve(F == 0, u, bc)
        u_.assign(u)

        t += float(timestep)

    J = assemble(u_*u_*dx + ic*ic*dx)
    Jhat = ReducedFunctional(J, ic)

    h = Function(V)
    h.vector()[:] = 1
    g = ic.copy(deepcopy=True)
    assert(taylor_test(Jhat, g, h) > 1.9)

def test_expression():
    mesh = IntervalMesh(10, 0, 1)
    V = FunctionSpace(mesh, "CG", 1)

    bc = DirichletBC(V, Constant(1), "on_boundary")
    a = Function(V)
    a.vector()[:] = 1
    f = Expression("t*a", a=a, t=0.1, degree=1)
    f_deriv = Expression("t", t=0.1, degree=1)
    f.user_defined_derivatives = {a: f_deriv}

    u = Function(V)
    v = TestFunction(V)

    F = inner(grad(u), grad(v))*dx - f*v*dx

    t = 0.1
    dt = 0.1
    T = 0.3
    while t <= T:
        solve(F == 0, u, bc)
        t += dt
        f.t = t

    J = assemble(u**2*dx)
    Jhat = ReducedFunctional(J, a)

    h = Function(V)
    h.vector()[:] = 1
    g = a.copy(deepcopy=True)
    assert(taylor_test(Jhat, g, h) > 1.9)

def test_projection():
    mesh = UnitSquareMesh(10, 10)
    V = FunctionSpace(mesh, "CG", 1)

    bc = DirichletBC(V, Constant(1), "on_boundary")
    k = Constant(2.0)
    expr = Expression("sin(k*x[0])", k=k, degree=1)
    expr.user_defined_derivatives = {k: Expression("x[0]*cos(k*x[0])", k=k, degree=1, annotate_tape=False)}
    f = project(expr, V)

    u = TrialFunction(V)
    v = TestFunction(V)
    u_ = Function(V)

    a = inner(grad(u), grad(v))*dx
    L = f*v*dx

    solve(a == L, u_, bc)

    J = assemble(u_**2*dx)
    Jhat = ReducedFunctional(J, k)

    m = Constant(2.0)
    h = Constant(1.0)
    assert(taylor_test(Jhat, m, h) > 1.9)

def test_projection_function():
    mesh = UnitSquareMesh(10, 10)
    V = FunctionSpace(mesh, "CG", 1)

    bc = DirichletBC(V, Constant(1), "on_boundary")
    g = Function(V)
    g = project(Expression("sin(x[0])*sin(x[1])", degree=1, annotate_tape=False), V, annotate=False)
    expr = Expression("sin(g*x[0])", g=g, degree=1)
    expr.user_defined_derivatives = {g: Expression("x[0]*cos(g*x[0])", g=g, degree=1, annotate=False)}
    f = project(expr, V)

    u = TrialFunction(V)
    v = TestFunction(V)
    u_ = Function(V)

    a = inner(grad(u), grad(v))*dx
    L = f*v*dx

    solve(a == L, u_, bc)

    J = assemble(u_**2*dx)
    Jhat = ReducedFunctional(J, g)

    m = g.copy(deepcopy=True)
    h = Function(V)
    h.vector()[:] = 1
    assert(taylor_test(Jhat, m, h) > 1.9)

def test_assemble_recompute():
    mesh = UnitSquareMesh(10, 10)
    V = FunctionSpace(mesh, "CG", 1)

    v = TestFunction(V)
    u = Function(V)
    u.vector()[:] = 1

    bc = DirichletBC(V, Constant(1), "on_boundary")
    f = Function(V)
    f.vector()[:] = 2
    k = assemble(f**2*dx)
    expr = Expression("k", k=k, degree=1)
    expr.user_defined_derivatives = {k: Expression("1", degree=1, annotate_tape=False)}
    J = assemble(expr**2*dx(domain=mesh))
    Jhat = ReducedFunctional(J, f)

    m = f.copy(deepcopy=True)
    h = Function(V)
    h.vector()[:] = 1
    assert(taylor_test(Jhat, m, h) > 1.9)

