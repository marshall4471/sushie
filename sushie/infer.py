import math
from typing import List, NamedTuple, Tuple

import pandas as pd

import jax.numpy as jnp
import jax.scipy.stats as stats
from jax import jit, lax, nn

from . import core, log, utils


def infer_sushie(
    Xs: List[jnp.ndarray],
    ys: List[jnp.ndarray],
    covar: core.ListArrayOrNone = None,
    L: int = 5,
    no_scale: bool = False,
    no_regress: bool = False,
    no_update: bool = False,
    pi: jnp.ndarray = None,
    resid_var: core.ListFloatOrNone = None,
    effect_var: core.ListFloatOrNone = None,
    rho: core.ListFloatOrNone = None,
    max_iter: int = 500,
    min_tol: float = 1e-4,
    threshold: float = 0.9,
    purity: float = 0.5,
) -> core.SushieResult:
    """The main inference function for running SuShiE.

    Args:
        Xs: genotype data for multiple ancestries.
        ys: phenotype data for multiple ancestries.
        covar: covariate data for multiple ancestries.
        L: inferred number of eQTLs for the gene, default is five.
        no_scale: do not scale the genotype and phenotype, default is to scale.
        no_regress: do not regress covariates on genotypes, default is to regress.
        no_update: do not update the effect size prior, default is to update.
        pi: the prob. prior for one SNP to be causal, default is 1 over the number of SNPs by specifying it as None.
        resid_var: prior residual variance, default is 1e-3 by specifying it as None.
        effect_var: prior effect size variance, default is 1e-3 by specifying it as None.
        rho: prior effect size correlation, default is 0.1 by specifying it as None.
        max_iter: the maximum iteration for optimization, default is 500.
        min_tol: the convergence tolerance, default is 1e-5.
        threshold: the credible set threshold, default is 0.9.
        purity: default is 0.5.

    Returns:
        SuShiE result object that contains prior, posterior, cs, and pip

    """
    if len(Xs) == len(ys):
        n_pop = len(Xs)
    else:
        raise ValueError(
            f"The number of geno ({len(Xs)}) and pheno ({len(ys)}) data does not match. Check your input."
        )

    # check x and y have the same sample size
    for idx in range(n_pop):
        if Xs[idx].shape[0] != ys[idx].shape[0]:
            raise ValueError(
                f"Ancestry {idx + 1}: The sample size of geno ({Xs[idx].shape[0]}) "
                + f"and pheno ({ys[idx].shape[0]}) data does not match. Check your input."
            )

    # check each ancestry has the same number of SNPs
    for idx in range(1, n_pop):
        if Xs[idx - 1].shape[1] != Xs[idx].shape[1]:
            raise ValueError(
                f"Ancestry {idx} and ancestry {idx} do not have "
                + f"the same number of SNPs ({Xs[idx - 1].shape[1]} vs {Xs[idx].shape[1]})."
            )

    if L <= 0:
        raise ValueError(f"Inferred L ({L}) is invalid, choose a positive L.")

    if min_tol > 0.1:
        log.logger.warning(
            f"Minimum intolerance ({min_tol}) is greater than 0.1. Inference may not be accurate."
        )

    if not 0 < threshold < 1:
        raise ValueError(
            f"CS threshold ({threshold}) is not between 0 and 1. Specify a valid one."
        )

    if not 0 < purity < 1:
        raise ValueError(
            f"Purity ({purity}) is not between 0 and 1. Specify a valid one."
        )

    if pi is not None and (pi >= 1 or pi <= 0):
        raise ValueError(
            f"Pi prior ({pi}) is not a probability (0-1). Specify a valid pi prior."
        )

    # first regress out covariates if there are any, then scale the genotype and phenotype
    if covar is not None:
        for idx in range(n_pop):
            Xs[idx], ys[idx] = utils.regress_covar(
                Xs[idx], ys[idx], covar[idx], no_regress
            )

    # center data
    for idx in range(n_pop):
        Xs[idx] -= jnp.mean(Xs[idx], axis=0)
        ys[idx] -= jnp.mean(ys[idx])
        # scale data if specified
        if not no_scale:
            Xs[idx] /= jnp.std(Xs[idx], axis=0)
            ys[idx] /= jnp.std(ys[idx])

        ys[idx] = jnp.squeeze(ys[idx])

    if resid_var is None:
        resid_var = []
        for idx in range(n_pop):
            resid_var.append(jnp.var(ys[idx], ddof=1))
    else:
        if len(resid_var) != n_pop:
            raise ValueError(
                f"Number of specified residual prior ({len(resid_var)}) does not match ancestry number ({n_pop})."
            )
        resid_var = [float(i) for i in resid_var]
        if jnp.any(jnp.array(resid_var) <= 0):
            raise ValueError(
                f"The input of residual prior ({resid_var}) is invalid (<0). Check your input."
            )

    _, n_snps = Xs[0].shape

    if n_snps < L:
        raise ValueError(
            f"The number of common SNPs across ancestries ({n_snps}) is less than inferred L ({L})."
            + "Please choose a smaller L or expand the genomic window."
        )

    param_effect_var = effect_var
    if effect_var is None:
        effect_var = [1e-3] * n_pop
    else:
        if len(effect_var) != n_pop:
            raise ValueError(
                f"Number of specified effect prior ({len(effect_var)}) does not match ancestry number ({n_pop})."
            )
        effect_var = [float(i) for i in effect_var]
        if jnp.any(jnp.array(effect_var) <= 0):
            raise ValueError(
                f"The input of effect size prior ({effect_var})is invalid (<0)."
            )

    exp_num_rho = math.comb(n_pop, 2)
    param_rho = rho
    if rho is None:
        rho = [0.1] * exp_num_rho
    else:
        if n_pop == 1:
            log.logger.info(
                "Running single-ancestry SuShiE, but --rho is specified. Will ignore."
            )

        if (len(rho) != exp_num_rho) and n_pop != 1:
            raise ValueError(
                f"Number of specified rho ({len(rho)}) does not match expected"
                + f"number {exp_num_rho}.",
            )
        rho = [float(i) for i in rho]
        # double-check the if it's invalid rho
        if jnp.any(jnp.abs(jnp.array(rho)) >= 1):
            raise ValueError(
                f"The input of rho ({rho}) is invalid (>=1 or <=-1). Check your input."
            )

    effect_covar = jnp.diag(jnp.array(effect_var))
    ct = 0
    for row in range(1, n_pop):
        for col in range(n_pop):
            if col < row:
                _two_sd = jnp.sqrt(effect_var[row] * effect_var[col])
                effect_covar = effect_covar.at[row, col].set(rho[ct] * _two_sd)
                effect_covar = effect_covar.at[col, row].set(rho[ct] * _two_sd)
                ct += 1

    if no_update:
        # if we specify no_update and rho, we want to keep rho through iterations and update variance
        if param_effect_var is None and param_rho is not None and n_pop != 1:
            prior_adjustor = core.PriorAdjustor(
                times=jnp.eye(n_pop),
                plus=effect_covar - jnp.diag(jnp.diag(effect_covar)),
            )

            log.logger.info(
                "No updates on the prior effect correlation rho while updating prior effect variance."
            )
        # if we specify no_update and effect_covar, we want to keep variance through iterations, and update rho
        elif param_effect_var is not None and param_rho is None and n_pop != 1:
            prior_adjustor = core.PriorAdjustor(
                times=jnp.ones((n_pop, n_pop)) - jnp.eye(n_pop),
                plus=effect_covar * jnp.eye(n_pop),
            )
            log.logger.info(
                "No updates on the prior effect variance while updating prior effect correlation rho."
            )
        # if we (do not specify effect_covar and rho) or (specify both effect_covar and rho)
        # nothing is updated through iterations
        else:
            prior_adjustor = core.PriorAdjustor(
                times=jnp.zeros((n_pop, n_pop)), plus=effect_covar
            )
            log.logger.info(
                "No updates on the prior effect size variance/covariance matrix."
            )
    else:
        prior_adjustor = core.PriorAdjustor(
            times=jnp.ones((n_pop, n_pop)), plus=jnp.zeros((n_pop, n_pop))
        )

    priors = core.Prior(
        pi=jnp.ones(n_snps) / float(n_snps) if pi is None else pi,
        resid_var=jnp.array(resid_var),
        # L x k x k
        effect_covar=jnp.array([effect_covar] * L),
    )

    posteriors = core.Posterior(
        alpha=jnp.zeros((L, n_snps)),
        post_mean=jnp.zeros((L, n_snps, n_pop)),
        post_mean_sq=jnp.zeros((L, n_snps, n_pop, n_pop)),
        weighted_sum_covar=jnp.zeros((L, n_pop, n_pop)),
        kl=jnp.zeros((L,)),
    )

    # since we use prior adjustor, this is really no need
    # opt_v_func = NoopOptFunc() would work
    opt_v_func = EMOptFunc() if not no_update else NoopOptFunc()

    XtXs = []
    for idx in range(n_pop):
        XtXs.append(jnp.sum(Xs[idx] ** 2, axis=0))

    elbo_last = -jnp.inf
    elbo_cur = -jnp.inf
    elbo_increase = True
    for o_iter in range(max_iter):
        prev_priors = priors
        prev_posteriors = posteriors

        priors, posteriors, elbo_cur = _update_effects(
            Xs,
            ys,
            XtXs,
            priors,
            posteriors,
            prior_adjustor,
            opt_v_func,
        )
        elbo_increase = elbo_cur < elbo_last and (
            not jnp.isclose(elbo_cur, elbo_last, atol=1e-8)
        )

        if elbo_increase or jnp.isnan(elbo_cur):
            log.logger.warning(
                f"Optimization finished after {o_iter + 1} iterations."
                + f" ELBO decreases. Final ELBO score: {elbo_cur}. Return last iteration's results."
                + " It can be precision issue,"
                + " and adding 'import jax; jax.config.update('jax_enable_x64', True)' may fix it."
                + " If this issue keeps rising for many genes, contact the developer."
            )
            priors = prev_priors
            posteriors = prev_posteriors
            elbo_increase = False
            break

        if jnp.abs(elbo_cur - elbo_last) < min_tol:
            log.logger.info(
                f"Optimization finished after {o_iter + 1} iterations. Final ELBO score: {elbo_cur}."
                + f" Reach minimum tolerance threshold {min_tol}.",
            )
            break

        if o_iter + 1 == max_iter:
            log.logger.info(
                f"Optimization finished after {o_iter + 1} iterations. Final ELBO score: {elbo_cur}."
                + f" Reach maximum iteration threshold {max_iter}.",
            )
        elbo_last = elbo_cur

    pip = _get_pip(posteriors.alpha)
    cs = _get_cs(posteriors.alpha, Xs, pip, threshold, purity)

    return core.SushieResult(priors, posteriors, pip, cs, elbo_cur, elbo_increase)


class _LResult(NamedTuple):
    Xs: List[jnp.ndarray]
    ys: List[jnp.ndarray]
    XtXs: List[jnp.ndarray]
    priors: core.Prior
    posteriors: core.Posterior
    prior_adjustor: core.PriorAdjustor
    opt_v_func: core.AbstractOptFunc


@jit
def _update_effects(
    Xs: List[jnp.ndarray],
    ys: List[jnp.ndarray],
    XtXs: List[jnp.ndarray],
    priors: core.Prior,
    posteriors: core.Posterior,
    prior_adjustor: core.PriorAdjustor,
    opt_v_func: core.AbstractOptFunc,
) -> Tuple[core.Prior, core.Posterior, float]:
    l_dim, n_snps, n_pop = posteriors.post_mean.shape
    ns = [X.shape[0] for X in Xs]
    residual = []

    post_mean_lsum = jnp.sum(posteriors.post_mean, axis=0)
    for idx in range(n_pop):
        residual.append(ys[idx] - Xs[idx] @ post_mean_lsum[:, idx])

    init_l_result = _LResult(
        Xs=Xs,
        ys=residual,
        XtXs=XtXs,
        priors=priors,
        posteriors=posteriors,
        prior_adjustor=prior_adjustor,
        opt_v_func=opt_v_func,
    )

    l_result = lax.fori_loop(0, l_dim, _update_l, init_l_result)

    _, _, _, priors, posteriors, _, _ = l_result

    sigma2_list = []
    exp_ll = 0.0
    tr_b_s = posteriors.post_mean.T
    tr_bsq_s = jnp.einsum("nmij,ij->nmi", posteriors.post_mean_sq, jnp.eye(n_pop)).T
    for idx in range(n_pop):
        tmp_sigma2 = _erss(Xs[idx], ys[idx], tr_b_s[idx], tr_bsq_s[idx]) / ns[idx]
        sigma2_list.append(tmp_sigma2)
        exp_ll += _eloglike(Xs[idx], ys[idx], tr_b_s[idx], tr_bsq_s[idx], tmp_sigma2)

    priors = priors._replace(resid_var=jnp.array(sigma2_list))
    kl_divs = jnp.sum(posteriors.kl)
    elbo_score = exp_ll - kl_divs

    return priors, posteriors, elbo_score


def _update_l(l_iter: int, param: _LResult) -> _LResult:
    Xs, residual, XtXs, priors, posteriors, prior_adjustor, opt_v_func = param
    n_pop = len(Xs)
    residual_l = []

    for idx in range(n_pop):
        residual_l.append(
            residual[idx] + Xs[idx] @ posteriors.post_mean[l_iter, :, idx]
        )

    priors, posteriors = _ssr(
        Xs,
        residual_l,
        XtXs,
        priors,
        posteriors,
        prior_adjustor,
        l_iter,
        opt_v_func,
    )

    for idx in range(n_pop):
        residual[idx] = residual_l[idx] - Xs[idx] @ posteriors.post_mean[l_iter, :, idx]

    update_param = param._replace(
        ys=residual,
        priors=priors,
        posteriors=posteriors,
    )

    return update_param


def _ssr(
    Xs: List[jnp.ndarray],
    ys: List[jnp.ndarray],
    XtXs: List[jnp.ndarray],
    priors: core.Prior,
    posteriors: core.Posterior,
    prior_adjustor: core.PriorAdjustor,
    l_iter: int,
    opt_v_func: core.AbstractOptFunc,
) -> Tuple[core.Prior, core.Posterior]:
    n_pop = len(Xs)
    _, n_snps = Xs[0].shape

    beta_hat = jnp.zeros((n_snps, n_pop))
    shat2 = jnp.zeros((n_snps, n_pop))

    for idx in range(n_pop):
        Xty = Xs[idx].T @ ys[idx]
        beta_hat = beta_hat.at[:, idx].set(Xty / XtXs[idx])
        shat2 = shat2.at[:, idx].set(priors.resid_var[idx] / XtXs[idx])

    shat2 = jnp.eye(n_pop) * (shat2[:, jnp.newaxis])

    priors = opt_v_func(beta_hat, shat2, priors, posteriors, prior_adjustor, l_iter)

    _, posteriors = _compute_posterior(beta_hat, shat2, priors, posteriors, l_iter)

    return priors, posteriors


def _compute_posterior(
    beta_hat: jnp.ndarray,
    shat2: core.ArrayOrFloat,
    priors: core.Prior,
    posteriors: core.Posterior,
    l_iter: int,
) -> Tuple[core.Prior, core.Posterior]:
    n_snps, n_pop = beta_hat.shape
    # quick way to calculate the inverse instead of using linalg.inv
    inv_shat2 = jnp.eye(n_pop) * (
        1 / jnp.diagonal(shat2, axis1=1, axis2=2)[:, jnp.newaxis]
    )

    prior_covar = priors.effect_covar[l_iter]
    post_covar = jnp.linalg.inv(inv_shat2 + jnp.linalg.inv(prior_covar))
    rTZDinv = beta_hat / jnp.diagonal(shat2, axis1=1, axis2=2)

    post_mean = jnp.einsum("ijk,ik->ij", post_covar, rTZDinv)
    post_mean_sq = post_covar + jnp.einsum("ij,im->ijm", post_mean, post_mean)
    alpha = nn.softmax(
        jnp.log(priors.pi)
        - stats.multivariate_normal.logpdf(
            jnp.zeros((n_snps, n_pop)), post_mean, post_covar
        )
    )
    weighted_post_mean = post_mean * alpha[:, jnp.newaxis]
    weighted_post_mean_sq = post_mean_sq * alpha[:, jnp.newaxis, jnp.newaxis]
    # this is also the prior in our E step
    weighted_sum_covar = jnp.einsum("j,jmn->mn", alpha, post_mean_sq)
    kl_alpha = _kl_categorical(alpha, priors.pi)
    kl_betas = alpha @ _kl_mvn(post_mean, post_covar, 0.0, prior_covar)

    priors = priors._replace(
        effect_covar=priors.effect_covar.at[l_iter].set(weighted_sum_covar)
    )

    posteriors = posteriors._replace(
        alpha=posteriors.alpha.at[l_iter].set(alpha),
        post_mean=posteriors.post_mean.at[l_iter].set(weighted_post_mean),
        post_mean_sq=posteriors.post_mean_sq.at[l_iter].set(weighted_post_mean_sq),
        weighted_sum_covar=posteriors.weighted_sum_covar.at[l_iter].set(
            weighted_sum_covar
        ),
        kl=posteriors.kl.at[l_iter].set(kl_alpha + kl_betas),
    )

    return priors, posteriors


class EMOptFunc(core.AbstractOptFunc):
    def __call__(
        self,
        beta_hat: jnp.ndarray,
        shat2: core.ArrayOrFloat,
        priors: core.Prior,
        posteriors: core.Posterior,
        prior_adjustor: core.PriorAdjustor,
        l_iter: int,
    ) -> core.Prior:
        priors, _ = _compute_posterior(beta_hat, shat2, priors, posteriors, l_iter)

        return priors


class NoopOptFunc(core.AbstractOptFunc):
    def __call__(
        self,
        beta_hat: jnp.ndarray,
        shat2: core.ArrayOrFloat,
        priors: core.Prior,
        posteriors: core.Posterior,
        prior_adjustor: core.PriorAdjustor,
        l_iter: int,
    ) -> core.Prior:
        priors, _ = _compute_posterior(beta_hat, shat2, priors, posteriors, l_iter)
        priors = priors._replace(
            effect_covar=priors.effect_covar.at[l_iter].set(
                priors.effect_covar[l_iter] * prior_adjustor.times + prior_adjustor.plus
            )
        )
        return priors


def _get_pip(alpha: jnp.ndarray) -> jnp.ndarray:
    pip = 1 - jnp.exp(jnp.sum(jnp.log1p(-alpha), axis=0))
    return pip


def _get_cs(
    alpha: jnp.ndarray,
    Xs: List[jnp.ndarray],
    pip: jnp.ndarray,
    threshold: float = 0.9,
    purity: float = 0.5,
) -> pd.DataFrame:
    n_l, _ = alpha.shape
    t_alpha = pd.DataFrame(alpha.T).reset_index()

    # ld is always pxp, so it can be converted to jnp.array
    ld = jnp.array([x.T @ x / x.shape[0] for x in Xs])
    cs = pd.DataFrame(columns=["CSIndex", "SNPIndex", "alpha", "c_alpha"])

    for idx in range(n_l):
        # select original index and alpha
        tmp_pd = (
            t_alpha[["index", idx]]
            .sort_values(idx, ascending=False)
            .reset_index(drop=True)
        )
        tmp_pd["csum"] = tmp_pd[[idx]].cumsum()
        n_row = tmp_pd[tmp_pd.csum < threshold].shape[0]

        # if select rows less than total rows, n_row + 1
        if n_row == tmp_pd.shape[0]:
            select_idx = jnp.arange(n_row)
        else:
            select_idx = jnp.arange(n_row + 1)
        tmp_pd = (
            tmp_pd.iloc[select_idx, :]
            .assign(CSIndex=idx + 1)
            .rename(columns={"csum": "c_alpha", "index": "SNPIndex", idx: "alpha"})
        )

        # check the impurity
        snp_idx = tmp_pd.SNPIndex.values.astype("int64")

        min_corr = jnp.min(jnp.abs(ld[:, snp_idx][:, :, snp_idx]))
        if min_corr > purity:
            cs = pd.concat([cs, tmp_pd], ignore_index=True)

    cs["pip"] = pip[cs.SNPIndex.values.astype(int)]

    return cs


def _eloglike(
    X: jnp.ndarray,
    y: jnp.ndarray,
    beta: jnp.ndarray,
    beta_sq: jnp.ndarray,
    sigma_sq: core.ArrayOrFloat,
) -> core.ArrayOrFloat:
    n, _ = X.shape
    norm_term = -(0.5 * n) * jnp.log(2 * jnp.pi * sigma_sq)
    quad_term = -(0.5 / sigma_sq) * _erss(X, y, beta, beta_sq)

    return norm_term + quad_term


def _kl_categorical(
    alpha: jnp.ndarray,
    pi: jnp.ndarray,
) -> float:
    return jnp.nansum(alpha * (jnp.log(alpha) - jnp.log(pi)))


def _kl_mvn(
    m0: jnp.ndarray,
    sigma0: jnp.ndarray,
    m1: float,
    sigma1: jnp.ndarray,
) -> float:
    # https://en.wikipedia.org/wiki/Kullback%E2%80%93Leibler_divergence
    k, _ = sigma1.shape

    p1 = (
        jnp.trace(
            jnp.einsum("ij,kjm->kim", jnp.linalg.inv(sigma1), sigma0), axis1=1, axis2=2
        )
        - k
    )
    p2 = jnp.einsum("ij,jm,im->i", (m1 - m0), jnp.linalg.inv(sigma1), (m1 - m0))

    _, sld1 = jnp.linalg.slogdet(sigma1)
    _, sld0 = jnp.linalg.slogdet(sigma0)

    p3 = sld1 - sld0

    return 0.5 * (p1 + p2 + p3)


def _erss(
    X: jnp.ndarray, y: jnp.ndarray, beta: jnp.ndarray, beta_sq: jnp.ndarray
) -> core.ArrayOrFloat:
    mu_li = X @ beta
    mu2_li = (X ** 2) @ beta_sq

    term_1 = jnp.sum((y - jnp.sum(mu_li, axis=1)) ** 2)
    term_2 = jnp.sum(mu2_li - (mu_li ** 2))

    return term_1 + term_2
