from typing import Tuple

from glimix_core.lmm import LMM
from numpy_sugar.linalg import economic_qs
from scipy import stats

import jax.numpy as jnp


def ols(X: jnp.ndarray, y: jnp.ndarray) -> Tuple[jnp.ndarray, jnp.ndarray, jnp.ndarray]:
    """Perform ordinary linear regression.

    Args:
        X: n x p matrix for independent variables with no intercept vector.
        y: n x m matrix for dependent variables. If m > 1, then perform m ordinary regression parallel.

    Returns:
        residual: regression residual   s
        adj_r: adjusted r squared
        p_val: p values for betas
    """
    n_samples, n_features = X.shape
    X_inter = jnp.append(jnp.ones((n_samples, 1)), X, axis=1)
    n_features += 1
    y = jnp.reshape(y, (len(y), -1))
    XtX_inv = jnp.linalg.inv(X_inter.T @ X_inter)
    betas = XtX_inv @ X_inter.T @ y
    residual = y - X_inter @ betas
    rss = jnp.sum(residual ** 2, axis=0)
    sigma_sq = rss / (n_samples - n_features)
    t_scores = betas / jnp.sqrt(
        jnp.diagonal(XtX_inv)[:, jnp.newaxis] @ sigma_sq[jnp.newaxis, :]
    )
    r_sq = 1 - rss / jnp.sum((y - jnp.mean(y)) ** 2)
    adj_r = 1 - (1 - r_sq) * (n_samples - 1) / (n_samples - n_features)
    p_value = jnp.array(2 * stats.t.sf(abs(t_scores), df=(n_samples - n_features)))

    return residual, adj_r, p_value


def regress_covar(
    X: jnp.ndarray, y: jnp.ndarray, covar: jnp.ndarray, no_regress: bool
) -> Tuple[jnp.ndarray, jnp.ndarray]:
    y, _, _ = ols(covar, y)
    if not no_regress:
        X, _, _ = ols(covar, X)

    return X, y


def estimate_her(
    X: jnp.ndarray, y: jnp.ndarray, covar: jnp.ndarray = None
) -> Tuple[float, float, float, float, float]:
    """Calculate proportion of gene expression variation explained by genotypes (cis-heritability).

    Args:
        X: n x p matrix for independent variables with no intercept vector.
        y: n x 1 vector for gene expression.
        covar: n x m vector for covariates or None.

    Returns:
        g: genetic variance
        h2g_w_v: narrow-sense heritability including the fixed effect variance
        h2g_wo_v: narrow-sense heritability including the fixed effect variance
        lrt_stats: LRT test statistics for narrow-sense heritability
        p_value: LRT p-value for narrow-sense heritability
    """
    n, p = X.shape

    if covar is None:
        covar = jnp.ones(n)

    GRM = jnp.dot(X, X.T) / p
    GRM = GRM / jnp.diag(GRM).mean()
    QS = economic_qs(GRM)
    method = LMM(y, covar, QS, restricted=True)
    method.fit(verbose=False)

    g = method.scale * (1 - method.delta)
    e = method.scale * method.delta
    v = jnp.var(method.mean())
    h2g_w_v = g / (v + g + e)
    h2g_wo_v = g / (g + e)
    alt_lk = method.lml()
    method.delta = 1
    method.fix("delta")
    method.fit(verbose=False)
    null_lk = method.lml()
    lrt_stats = -2 * (null_lk - alt_lk)
    p_value = stats.chi2.sf(lrt_stats, 1) / 2

    return g, h2g_w_v, h2g_wo_v, lrt_stats, p_value
