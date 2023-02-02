import copy
import warnings
from typing import Callable, List, NamedTuple, Optional, Tuple

import pandas as pd

with warnings.catch_warnings():
    warnings.simplefilter("ignore")
    from pandas_plink import read_plink
    from cyvcf2 import VCF
    from bgen_reader import open_bgen
    import jax.numpy as jnp

from . import infer, log, utils

__all__ = [
    "CVData",
    "CleanData",
    "RawData",
    "read_data",
    "read_triplet",
    "read_bgen",
    "read_vcf",
    "output_cs",
    "output_alphas",
    "output_weights",
    "output_her",
    "output_corr",
    "output_cv",
    "output_numpy",
]


class CVData(NamedTuple):
    """Define the raw data object for the future inference.

    Attributes:
        train_geno: genotype data for training SuShiE weights.
        train_pheno: phenotype data for training SuShiE weights.
        valid_geno: genotype data for validating SuShiE weights.
        valid_pheno: phenotype data for validating SuShiE weights.

    """

    train_geno: List[jnp.ndarray]
    train_pheno: List[jnp.ndarray]
    valid_geno: List[jnp.ndarray]
    valid_pheno: List[jnp.ndarray]


class CleanData(NamedTuple):
    """Define the raw data object for the future inference.

    Attributes:
        geno: actual genotype data.
        pheno: phenotype data.
        covar: covariate needed to be adjusted in the inference.

    """

    geno: List[jnp.ndarray]
    pheno: List[jnp.ndarray]
    covar: utils.ListArrayOrNone


class RawData(NamedTuple):
    """Define the raw data object for the future inference.

    Attributes:
        bim: SNP information data.
        fam: individual information data.
        bed: actual genotype data.
        pheno: phenotype data.
        covar: covariate needed to be adjusted in the inference.

    """

    bim: pd.DataFrame
    fam: pd.DataFrame
    bed: jnp.ndarray
    pheno: pd.DataFrame
    covar: utils.PDOrNone


def read_data(
    pheno_paths: List[str],
    covar_paths: utils.ListStrOrNone,
    geno_paths: List[str],
    geno_func: Callable,
) -> List[RawData]:
    """Read in pheno, covar, and genotype data and convert it to raw data object.

    Args:
        pheno_paths: The path for phenotype data across ancestries.
        covar_paths: The path for covariates data across ancestries.
        geno_paths: The path for genotype data across ancestries.
        geno_func: The function to read in genotypes depending on the format.

    Returns:
        :py:obj:`List[RawData]`: A list of Raw data object (:py:obj:`RawData`).

    """
    n_pop = len(pheno_paths)

    rawData = []

    for idx in range(n_pop):
        log.logger.info(f"Ancestry {idx + 1}: Reading in genotype data.")

        tmp_bim, tmp_fam, tmp_bed = geno_func(geno_paths[idx])

        if len(tmp_bim) == 0:
            raise ValueError(
                f"Ancestry {idx + 1}: No genotype data found for ancestry at {geno_paths[idx]}."
            )
        if len(tmp_fam) == 0:
            raise ValueError(
                f"Ancestry {idx + 1}: No fam data found for ancestry at {geno_paths[idx]}."
            )

        tmp_pheno = (
            pd.read_csv(pheno_paths[idx], sep="\t", header=None, dtype={0: object})
            .rename(columns={0: "iid", 1: "pheno"})
            .reset_index(drop=True)
        )

        if len(tmp_pheno) == 0:
            raise ValueError(
                f"Ancestry {idx + 1}: No pheno data found for ancestry at {pheno_paths[idx]}."
            )

        if covar_paths is not None:
            tmp_covar = (
                pd.read_csv(covar_paths[idx], sep="\t", header=None, dtype={0: object})
                .rename(columns={0: "iid"})
                .reset_index(drop=True)
            )
        else:
            tmp_covar = None

        rawData.append(
            RawData(
                bim=tmp_bim, fam=tmp_fam, bed=tmp_bed, pheno=tmp_pheno, covar=tmp_covar
            )
        )

    return rawData


def read_triplet(path: str) -> Tuple[pd.DataFrame, pd.DataFrame, jnp.ndarray]:
    """Read in genotype data in `plink 1 <https://www.cog-genomics.org/plink/1.9/input#bed>`_ format.

    Args:
        path: The path for plink genotype data (suffix only).

    Returns:
        :py:obj:`Tuple[pd.DataFrame, pd.DataFrame, jnp.ndarray]`: A tuple of
            #. SNP information (bim; :py:obj:`pd.DataFrame`),
            #. individuals information (fam; :py:obj:`pd.DataFrame`),
            #. genotype matrix (bed; :py:obj:`jnp.ndarray`).

    """

    bim, fam, bed = read_plink(path, verbose=False)
    bim = bim[["chrom", "snp", "pos", "a0", "a1"]]
    fam = fam[["iid"]]
    # we want bed file to be nxp
    bed = bed.compute().T
    return bim, fam, bed


def read_vcf(path: str) -> Tuple[pd.DataFrame, pd.DataFrame, jnp.ndarray]:
    """Read in genotype data in `vcf <https://en.wikipedia.org/wiki/Variant_Call_Format>`_ format.

    Args:
        path: The path for vcf genotype data (full file name).

    Returns:
        :py:obj:`Tuple[pd.DataFrame, pd.DataFrame, jnp.ndarray]`: A tuple of
            #. SNP information (bim; :py:obj:`pd.DataFrame`),
            #. participants information (fam; :py:obj:`pd.DataFrame`),
            #. genotype matrix (bed; :py:obj:`jnp.ndarray`).

    """

    vcf = VCF(path, gts012=True)
    fam = pd.DataFrame(vcf.samples).rename(columns={0: "iid"})
    bim_list = []
    bed_list = []
    for var in vcf:
        # var.ALT is a list of alternative allele
        bim_list.append([var.CHROM, var.ID, var.POS, var.ALT[0], var.REF])
        tmp_bed = 2 - var.gt_types
        bed_list.append(tmp_bed)

    bim = pd.DataFrame(bim_list, columns=["chrom", "snp", "pos", "a0", "a1"])
    bed = jnp.array(bed_list).T

    return bim, fam, bed


def read_bgen(path: str) -> Tuple[pd.DataFrame, pd.DataFrame, jnp.ndarray]:
    """Read in genotype data in `bgen <https://www.well.ox.ac.uk/~gav/bgen_format/>`_ 1.3 format.

    Args:
        path: The path for bgen genotype data (full file name).

    Returns:
        :py:obj:`Tuple[pd.DataFrame, pd.DataFrame, jnp.ndarray]`: A tuple of
            #. SNP information (bim; :py:obj:`pd.DataFrame`),
            #. individuals information (fam; :py:obj:`pd.DataFrame`),
            #. genotype matrix (bed; :py:obj:`jnp.ndarray`).

    """

    bgen = open_bgen(path, verbose=False)
    fam = pd.DataFrame(bgen.samples).rename(columns={0: "iid"})
    bim = pd.DataFrame(
        data={"chrom": bgen.chromosomes, "snp": bgen.rsids, "pos": bgen.positions}
    )
    allele = (
        pd.DataFrame(bgen.allele_ids)[0]
        .str.split(",", expand=True)
        .rename(columns={0: "a0", 1: "a1"})
    )
    bim = pd.concat([bim, allele], axis=1).reset_index(drop=True)[
        ["chrom", "snp", "pos", "a0", "a1"]
    ]
    bed = jnp.einsum("ijk,k->ij", bgen.read(), jnp.array([0, 1, 2]))

    return bim, fam, bed


# output functions
def output_cs(
    result: List[infer.SushieResult],
    meta_pip: Optional[jnp.ndarray],
    snps: pd.DataFrame,
    output: str,
    trait: str,
    compress: bool,
    meta: bool,
    mega: bool,
) -> pd.DataFrame:
    """Output credible set (after pruning for purity) file ``*cs.tsv`` (see :ref:`csfile`).

    Args:
        result: The sushie inference result.
        meta_pip: The meta-analyzed PIPs from Meta SuShiE.
        snps: The SNP information table.
        output: The output file prefix.
        trait: The trait name better for post-hoc analysis index.
        compress: The indicator whether to compress the output files.
        meta: The indicator whether the sushie inference result is meta.
        mega: The indicator whether the sushie inference result is mega.

    Returns:
        :py:obj:`pd.DataFrame`: A data frame that outputs to the ``*cs.tsv`` file (:py:obj:`pd.DataFrame`).

    """
    cs = pd.DataFrame()

    for idx in range(len(result)):
        tmp_cs = (
            snps.merge(result[idx].cs, how="inner", on=["SNPIndex"])
            .assign(trait=trait, n_snps=snps.shape[0])
            .sort_values(
                by=["CSIndex", "alpha", "c_alpha"], ascending=[True, False, True]
            )
        )

        if meta_pip is not None:
            tmp_cs["meta_pip"] = meta_pip[tmp_cs.SNPIndex.values.astype(int)]

        if meta:
            ancestry_idx = f"ancestry_{idx + 1}"
        elif mega:
            ancestry_idx = "mega"
        else:
            ancestry_idx = "sushie"

        tmp_cs["ancestry"] = ancestry_idx
        cs = pd.concat([cs, tmp_cs], axis=0)

    # add a placeholder better for post-hoc analysis
    if cs.shape[0] == 0:
        cs = cs.append({"trait": trait}, ignore_index=True)

    file_name = f"{output}.cs.tsv.gz" if compress else f"{output}.cs.tsv"

    cs.to_csv(file_name, sep="\t", index=False)

    return cs


def output_alphas(
    result: List[infer.SushieResult],
    meta_pip: Optional[jnp.ndarray],
    snps: pd.DataFrame,
    output: str,
    trait: str,
    compress: bool,
    meta: bool,
    mega: bool,
) -> pd.DataFrame:
    """Output full credible set (before pruning for purity) file ``*alphas.tsv`` (see :ref:`alphasfile`).

    Args:
        result: The sushie inference result.
        meta_pip: The meta-analyzed PIPs from Meta SuShiE.
        snps: The SNP information table.
        output: The output file prefix.
        trait: The trait name better for post-hoc analysis index.
        compress: The indicator whether to compress the output files.
        meta: The indicator whether the sushie inference result is meta.
        mega: The indicator whether the sushie inference result is mega.

    Returns:
        :py:obj:`pd.DataFrame`: A data frame that outputs to the ``*alphas.tsv`` file (:py:obj:`pd.DataFrame`).

    """
    alphas = pd.DataFrame()

    for idx in range(len(result)):
        tmp_alphas = snps.merge(
            result[idx].alphas, how="inner", on=["SNPIndex"]
        ).assign(trait=trait, n_snps=snps.shape[0])

        if meta_pip is not None:
            tmp_alphas["meta_pip"] = meta_pip

        if meta:
            ancestry_idx = f"ancestry_{idx + 1}"
        elif mega:
            ancestry_idx = "mega"
        else:
            ancestry_idx = "sushie"

        tmp_alphas["ancestry"] = ancestry_idx
        alphas = pd.concat([alphas, tmp_alphas], axis=0)

    file_name = f"{output}.alphas.tsv.gz" if compress else f"{output}.alphas.tsv"

    alphas.to_csv(file_name, sep="\t", index=False)

    return alphas


def output_weights(
    result: List[infer.SushieResult],
    meta_pip: Optional[jnp.ndarray],
    snps: pd.DataFrame,
    output: str,
    trait: str,
    compress: bool,
    meta: bool,
    mega: bool,
) -> pd.DataFrame:
    """Output prediction weights file ``*weights.tsv`` (see :ref:`weightsfile`).

    Args:
        result: The sushie inference result.
        meta_pip: The meta-analyzed PIPs from Meta SuShiE.
        snps: The SNP information table.
        output: The output file prefix.
        trait: The trait name better for post-hoc analysis index.
        compress: The indicator whether to compress the output files.
        meta: The indicator whether the sushie inference result is meta.
        mega: The indicator whether the sushie inference result is meta.

    Returns:
        :py:obj:`pd.DataFrame`: A data frame that outputs to the ``*weights.tsv`` file (:py:obj:`pd.DataFrame`).

    """

    n_pop = len(result[0].priors.resid_var)
    weights = copy.deepcopy(snps).assign(trait=trait)

    for idx in range(len(result)):
        if meta:
            cname_idx = [f"ancestry{idx + 1}_single_weight"]
            cname_pip = f"ancestry{idx + 1}_single_pip"
            cname_cs = f"ancestry{idx + 1}_in_cs"
        elif mega:
            cname_idx = ["mega_weight"]
            cname_pip = "mega_pip"
            cname_cs = "mega_in_cs"
        else:
            cname_idx = [f"ancestry{jdx + 1}_sushie_weight" for jdx in range(n_pop)]
            cname_pip = "sushie_pip"
            cname_cs = "sushie_in_cs"

        tmp_weights = pd.DataFrame(
            data=jnp.sum(result[idx].posteriors.post_mean, axis=0),
            columns=cname_idx,
        )

        tmp_weights[cname_pip] = result[idx].pip
        weights = pd.concat([weights, tmp_weights], axis=1)
        weights[cname_cs] = (
            weights["SNPIndex"].isin(result[idx].cs["SNPIndex"].tolist()).astype(int)
        )

    if meta_pip is not None:
        weights["meta_pip"] = meta_pip
        tmp_cs = (weights["ancestry1_in_cs"] == 0) * 1
        for idx in range(1, len(result)):
            tmp_cs = tmp_cs * ((weights[f"ancestry{idx + 1}_in_cs"] == 0) * 1)
        weights["meta_in_cs"] = 1 - tmp_cs

    file_name = f"{output}.weights.tsv.gz" if compress else f"{output}.weights.tsv"

    weights.to_csv(file_name, sep="\t", index=False)

    return weights


def output_her(
    result: List[infer.SushieResult],
    data: CleanData,
    output: str,
    trait: str,
    compress: bool,
) -> pd.DataFrame:
    """Output heritability estimation file ``*her.tsv`` (see :ref:`herfile`).

    Args:
        result: The sushie inference result.
        data: The clean data that are used to estimate traits' heritability.
        output: The output file prefix.
        trait: The trait name better for post-hoc analysis index.
        compress: The indicator whether to compress the output files.

    Returns:
        :py:obj:`pd.DataFrame`: A data frame that outputs to the ``*her.tsv`` file (:py:obj:`pd.DataFrame`).

    """

    n_pop = len(data.geno)

    her_result = []
    for idx in range(n_pop):
        if data.covar is None:
            tmp_covar = None
        else:
            tmp_covar = data.covar[idx]
        tmp_her_result = utils.estimate_her(data.geno[idx], data.pheno[idx], tmp_covar)
        her_result.append(tmp_her_result)

    est_her = (
        pd.DataFrame(
            data=her_result,
            columns=["genetic_var", "h2g_w_v", "h2g_wo_v", "lrt_stats", "p_value"],
            index=[idx + 1 for idx in range(n_pop)],
        )
        .reset_index(names="ancestry")
        .assign(trait=trait)
    )

    # only output h2g that has credible sets
    SNPIndex = result[0].cs.SNPIndex.values.astype(int)

    shared_col = [
        "s_genetic_var",
        "s_h2g_w_v",
        "s_h2g_wo_v",
        "s_lrt_stats",
        "s_p_value",
    ]

    est_shared_her = pd.DataFrame(
        columns=shared_col, index=[idx + 1 for idx in range(n_pop)]
    ).reset_index(names="ancestry")

    if len(SNPIndex) != 0:
        for idx in range(n_pop):
            if data.covar is None:
                tmp_covar = None
            else:
                tmp_covar = data.covar[idx]

            est_shared_her.iloc[idx, 1:6] = utils.estimate_her(
                data.geno[idx][:, SNPIndex], data.pheno[idx], tmp_covar
            )

    est_her = est_her.merge(est_shared_her, how="left", on="ancestry")

    if est_her.shape[0] == 0:
        est_her = est_her.append({"trait": trait}, ignore_index=True)

    file_name = f"{output}.her.tsv.gz" if compress else f"{output}.her.tsv"

    est_her.to_csv(file_name, sep="\t", index=False)

    return est_her


def output_corr(
    result: List[infer.SushieResult],
    output: str,
    trait: str,
    compress: bool,
) -> pd.DataFrame:
    """Output effect size correlation file ``*corr.tsv`` (see :ref:`corrfile`).

    Args:
        result: The sushie inference result.
        output: The output file prefix.
        trait: The trait name better for post-hoc analysis index.
        compress: The indicator whether to compress the output files.

    Returns:
        :py:obj:`pd.DataFrame`: A data frame that outputs to the ``*corr.tsv`` file (:py:obj:`pd.DataFrame`).

    """

    n_pop = len(result[0].priors.resid_var)

    CSIndex = jnp.unique(result[0].cs.CSIndex.values.astype(int))
    # only output after-purity CS
    corr_cs_only = jnp.transpose(result[0].posteriors.weighted_sum_covar[CSIndex - 1])
    corr = pd.DataFrame(data={"trait": trait, "CSIndex": CSIndex})
    for idx in range(n_pop):
        _var = corr_cs_only[idx, idx]
        tmp_pd = pd.DataFrame(data={f"ancestry{idx + 1}_est_var": _var})
        corr = pd.concat([corr, tmp_pd], axis=1)
        for jdx in range(idx + 1, n_pop):
            _covar = corr_cs_only[idx, jdx]
            _var1 = corr_cs_only[idx, idx]
            _var2 = corr_cs_only[jdx, jdx]
            _corr = _covar / jnp.sqrt(_var1 * _var2)
            tmp_pd_covar = pd.DataFrame(
                data={f"ancestry{idx + 1}_ancestry{jdx + 1}_est_covar": _covar}
            )
            tmp_pd_corr = pd.DataFrame(
                data={f"ancestry{idx + 1}_ancestry{jdx + 1}_est_corr": _corr}
            )
            corr = pd.concat([corr, tmp_pd_covar, tmp_pd_corr], axis=1)

    if corr.shape[0] == 0:
        corr = corr.append({"trait": trait}, ignore_index=True)

    file_name = f"{output}.corr.tsv.gz" if compress else f"{output}.corr.tsv"

    corr.to_csv(file_name, sep="\t", index=False)

    return corr


def output_cv(
    cv_res: List,
    sample_size: List[int],
    output: str,
    trait: str,
    compress: bool,
) -> pd.DataFrame:
    """Output cross validation file ``*cv.tsv`` for
        future `FUSION <http://gusevlab.org/projects/fusion/>`_ pipline (see :ref:`cvfile`).

    Args:
        cv_res: The cross-validation result (adjusted :math:`r^2` and corresponding :math:`p` values).
        sample_size: The sample size for the SuShiE inference.
        output: The output file prefix.
        trait: The trait name better for post-hoc analysis index.
        compress: The indicator whether to compress the output files.

    Returns:
        :py:obj:`pd.DataFrame`: A data frame that outputs to the ``*cv.tsv`` file (:py:obj:`pd.DataFrame`).

    """

    cv_r2 = (
        pd.DataFrame(
            data=cv_res,
            index=[idx + 1 for idx in range(len(sample_size))],
            columns=["rsq", "p_value"],
        )
        .reset_index(names="ancestry")
        .assign(N=sample_size, trait=trait)
    )

    if cv_r2.shape[0] == 0:
        cv_r2 = cv_r2.append({"trait": trait}, ignore_index=True)

    file_name = f"{output}.cv.tsv.gz" if compress else f"{output}.cv.tsv"

    cv_r2.to_csv(file_name, sep="\t", index=False)

    return cv_r2


def output_numpy(
    result: List[infer.SushieResult], snps: pd.DataFrame, output: str
) -> None:
    """Output all results in ``*.npy`` file (no compress option) (see :ref:`npyfile`).

    Args:
        result: The sushie inference result.
        snps: The SNP information
        output: The output file prefix.

    Returns:
        :py:obj:`None`: This function returns nothing (:py:obj:`None`:).

    """
    jnp.save(f"{output}.all.results.npy", [snps, result])

    return None
