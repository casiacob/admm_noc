import jax.numpy as jnp
from jax import grad, hessian, jacrev
from jax import vmap, debug
from jax import lax
from admm_noc.optimal_control_problem import ADMM_OCP, Derivatives
from paroc.lqt_problem import LQT
from paroc import par_bwd_pass, par_fwd_pass
from typing import Callable
from admm_noc.costates import par_costates


def compute_derivatives(
    ocp: ADMM_OCP,
    states: jnp.ndarray,
    controls: jnp.ndarray,
    consensus: jnp.ndarray,
    dual: jnp.ndarray,
):
    def body(x, u, z, l):
        cx_k, cu_k = grad(ocp.stage_cost, (0, 1))(x, u, z, l)
        cxx_k = hessian(ocp.stage_cost, 0)(x, u, z, l)
        cuu_k = hessian(ocp.stage_cost, 1)(x, u, z, l)
        cxu_k = jacrev(jacrev(ocp.stage_cost, 0), 1)(x, u, z, l)
        fx_k = jacrev(ocp.dynamics, 0)(x, u)
        fu_k = jacrev(ocp.dynamics, 1)(x, u)
        fxx_k = jacrev(jacrev(ocp.dynamics, 0), 0)(x, u)
        fuu_k = jacrev(jacrev(ocp.dynamics, 1), 1)(x, u)
        fxu_k = jacrev(jacrev(ocp.dynamics, 0), 1)(x, u)
        return cx_k, cu_k, cxx_k, cuu_k, cxu_k, fx_k, fu_k, fxx_k, fuu_k, fxu_k

    cx, cu, cxx, cuu, cxu, fx, fu, fxx, fuu, fxu = vmap(body)(
        states[:-1], controls, consensus, dual
    )
    return Derivatives(cx, cu, cxx, cuu, cxu, fx, fu, fxx, fuu, fxu)


def compute_lqr_params(lagrange_multipliers: jnp.ndarray, d: Derivatives):
    def body(l, cu, cxx, cuu, cxu, fu, fxx, fuu, fxu):
        r = cu + fu.T @ l
        Q = cxx + jnp.tensordot(l, fxx, axes=1)
        R = cuu + jnp.tensordot(l, fuu, axes=1)
        M = cxu + jnp.tensordot(l, fxu, axes=1)
        return r, Q, R, M

    return vmap(body)(
        lagrange_multipliers[1:], d.cu, d.cxx, d.cuu, d.cxu, d.fu, d.fxx, d.fuu, d.fxu
    )


def noc_to_lqt(
    ru: jnp.ndarray,
    Q: jnp.ndarray,
    R: jnp.ndarray,
    M: jnp.ndarray,
    A: jnp.ndarray,
    B: jnp.ndarray,
):
    T = Q.shape[0]
    nx = Q.shape[1]
    nu = R.shape[1]

    def offsets(Xt, Ut, Mt, rut):
        XiM = jnp.linalg.solve(Xt, Mt)
        st = -jnp.linalg.solve(Ut - Mt.T @ XiM, rut)
        rt = -XiM @ st
        return rt, st

    r, s = vmap(offsets)(Q, R, M, ru)
    H = jnp.eye(nx)
    HT = H
    H = jnp.kron(jnp.ones((T, 1, 1)), H)
    Z = jnp.eye(nu)
    Z = jnp.kron(jnp.ones((T, 1, 1)), Z)
    XT = Q[0]
    rT = jnp.zeros(nx)
    c = jnp.zeros((T, nx))
    lqt = LQT(A, B, c, XT, HT, rT, Q, H, r, R, Z, s, M)
    return lqt


def par_solution(
    ocp: ADMM_OCP,
    x: jnp.ndarray,
    u: jnp.ndarray,
    z: jnp.ndarray,
    lamda: jnp.ndarray,
    rp: float,
):
    d = compute_derivatives(ocp, x, u, z, lamda)
    l = par_costates(ocp, x[-1], d)
    ru, Q, R, M = compute_lqr_params(l, d)
    R = R + jnp.kron(jnp.ones((R.shape[0], 1, 1)), rp * jnp.eye(R.shape[1]))
    lqt = noc_to_lqt(ru, Q, R, M, d.fx, d.fu)
    Kx_par, d_par, S_par, v_par, pred_reduction, convex_problem = par_bwd_pass(lqt)
    du_par, dx_par = par_fwd_pass(lqt, jnp.zeros(x[0].shape[0]), Kx_par, d_par)
    return dx_par, du_par, pred_reduction, convex_problem, ru


def argmin_xu(
    ocp: ADMM_OCP,
    states: jnp.ndarray,
    controls: jnp.ndarray,
    consensus: jnp.ndarray,
    dual: jnp.ndarray,
):
    mu0 = 1.
    nu0 = 2.0

    def while_body(val):
        x, u, z, l, it_cnt, mu, nu, _, _ = val
        # debug.print("Iteration:    {x}", x=it_cnt)

        cost = ocp.total_cost(x, u, z, l)
        # debug.print("cost:         {x}", x=cost)
        dx, du, predicted_reduction, bp_feasible, Hu = par_solution(ocp, x, u, z, l, mu)
        Hu_norm = jnp.max(jnp.abs(Hu))

        temp_u = u + du
        temp_x = x + dx
        new_cost = ocp.total_cost(temp_x, temp_u, z, l)
        # debug.print("new cost:     {x}", x=new_cost)
        # debug.print("bp feasible:  {x}", x=bp_feasible)

        actual_reduction = new_cost - cost
        gain_ratio = actual_reduction / predicted_reduction
        # debug.print("gain ratio:   {x}", x=gain_ratio)

        accept_cond = jnp.logical_and(gain_ratio > 0, bp_feasible)
        mu = jnp.where(
            accept_cond,
            mu * jnp.maximum(1.0 / 3.0, 1.0 - (2.0 * gain_ratio - 1.0) ** 3),
            mu * nu,
        )
        nu = jnp.where(accept_cond, 2.0, 2 * nu)
        x = jnp.where(accept_cond, temp_x, x)
        u = jnp.where(accept_cond, temp_u, u)
        # debug.print("reg param:    {x}", x=mu)
        # debug.print("a red:        {x}", x=actual_reduction)
        # debug.print("p red         {x}", x=predicted_reduction)
        # debug.print("|H_u|:        {x}", x=Hu_norm)

        it_cnt = it_cnt + 1
        # debug.print("---------------------------------")
        # debug.breakpoint()
        return x, u, z, l, it_cnt, mu, nu, Hu_norm, bp_feasible

    def while_cond(val):
        _, _, _, _, it_cnt, _, _, Hu_norm, bp_feasible = val
        exit_cond = jnp.logical_and(Hu_norm < 1e-4, bp_feasible)
        # exit_cond = jnp.logical_or(exit_cond, t > 3045)
        return jnp.logical_not(exit_cond)

    (opt_x, opt_u, _, _, _, _, _, _, _,) = lax.while_loop(
        while_cond,
        while_body,
        (states, controls, consensus, dual, 0, mu0, nu0, jnp.array(1.0), jnp.bool(1.0)),
    )
    # jax.debug.breakpoint()
    return opt_x, opt_u


def argmin_z(
    ocp: ADMM_OCP, states: jnp.ndarray, controls: jnp.ndarray, dual: jnp.ndarray
):
    z = jnp.hstack((states[:-1], controls)) + 1 / ocp.penalty_parameter * dual
    return vmap(ocp.projection)(z)


def grad_ascent(
    ocp: ADMM_OCP,
    states: jnp.ndarray,
    controls: jnp.ndarray,
    consensus: jnp.ndarray,
    dual: jnp.ndarray,
):
    return dual + ocp.penalty_parameter * (
        jnp.hstack((states[:-1], controls)) - consensus
    )


def primal_residual(states: jnp.ndarray, controls: jnp.ndarray, dual: jnp.ndarray):
    return jnp.max(jnp.abs(jnp.hstack((states[:-1], controls)) - dual))


def par_admm(
    stage_cost: Callable,
    final_cost: Callable,
    dynamics: Callable,
    projection: Callable,
    states0: jnp.ndarray,
    controls0: jnp.ndarray,
    consensus0: jnp.ndarray,
    dual0: jnp.ndarray,
    penalty_param: float,
):
    def admm_stage_cost(x, u, z, l):
        y = jnp.hstack((x, u))
        sql2norm = (y - z + 1.0 / penalty_param * l).T @ (
            y - z + 1.0 / penalty_param * l
        )
        return stage_cost(x, u) + penalty_param / 2 * sql2norm

    def admm_total_cost(states, controls, consensus, dual):
        ct = vmap(admm_stage_cost)(states[:-1], controls, consensus, dual)
        cT = final_cost(states[-1])
        return cT + jnp.sum(ct)

    admm_ocp = ADMM_OCP(
        dynamics,
        projection,
        admm_stage_cost,
        final_cost,
        admm_total_cost,
        penalty_param,
    )

    def admm_iteration(val):
        x, u, z, l, _, _, it_cnt = val
        # debug.print('iteration     {x}', x=it_cnt)
        x, u = argmin_xu(admm_ocp, x, u, z, l)

        prev_z = z
        z = argmin_z(admm_ocp, x, u, l)

        l = grad_ascent(admm_ocp, x, u, z, l)

        rp_infty = primal_residual(x, u, z)
        rd_infty = jnp.max(jnp.abs(z - prev_z))
        it_cnt += 1
        # debug.print('|rp|_inf      {x}', x=rp_infty)
        # debug.print('|rd|_inf      {x}', x=rd_infty)
        # debug.print('------------------------------')
        # debug.breakpoint()
        return x, u, z, l, rp_infty, rd_infty, it_cnt

    def admm_conv(val):
        _, _, _, _, rp_infty, rd_infty, it_cnt = val
        exit_condition = jnp.logical_and(rp_infty < 1e-2, rd_infty < 1e-2)
        return jnp.logical_not(exit_condition)
        # return it_cnt < 50

    (
        opt_states,
        opt_controls,
        opt_consensus,
        opt_dual,
        _,
        _,
        iterations,
    ) = lax.while_loop(
        admm_conv,
        admm_iteration,
        (states0, controls0, consensus0, dual0, jnp.inf, jnp.inf, 0.0),
    )
    # debug.print("iterations      {x}", x=iterations)
    # debug.print("------------------------------")
    # debug.breakpoint()
    return opt_states, opt_controls, opt_consensus, opt_dual
