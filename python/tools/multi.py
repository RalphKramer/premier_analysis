"""
Multiprocessing-enabled versions of functions from tools.py
"""

import pandas as pd
import numpy as np
import pickle

from itertools import product
from functools import reduce

import dask.dataframe as dd
import dask.distributed as distributed

from sklearn.metrics import confusion_matrix, roc_curve
from sklearn.model_selection import StratifiedKFold, cross_val_predict
from scipy.stats import chi2, norm
from copy import deepcopy
from multiprocessing import Pool

import tools.preprocessing as tp


def get_times(df, dict, day_col=None, time_col=None, ftr_col="ftr"):
    """Gets days, hours, and minutes from index for a table"""
    # Doing the days
    dfi_orig = np.array([dict[id] for id in df.pat_key])

    # Optionally returning early if the table has no existing day or time
    if day_col is None:
        out = df[["pat_key", ftr_col]]
        out["dfi"] = dfi_orig
        out["hfi"] = out.dfi * 24
        out["mfi"] = out.hfi * 60
        return out

    # Doing the hours and minutes
    dfi = np.array(dfi_orig + df[day_col], dtype=np.uint32)
    hfi = dfi * 24
    mfi = hfi * 60
    if time_col is not None:
        p = Pool()
        times = [t for t in df[time_col]]
        hours = np.array(p.map(tp.time_to_hours, times), dtype=np.uint32)
        mins = np.array(p.map(tp.time_to_minutes, times), dtype=np.uint32)
        p.close()
        p.join()
        hfi += hours
        mfi += mins

    # Returning the new df
    out = df[["pat_key", ftr_col]]
    out["dfi"] = dfi
    out["hfi"] = hfi
    out["mfi"] = mfi

    return out


def jackknife_metrics(targets, guesses, average_by=None, weighted=True):
    # Replicates of the dataset with one row missing from each
    rows = np.array(list(range(targets.shape[0])))
    j_rows = [np.delete(rows, row) for row in rows]

    # using a pool to get the metrics across each
    inputs = [(targets[idx], guesses[idx], average_by, weighted) for idx in j_rows]
    p = Pool()
    stat_list = p.starmap(tools.clf_metrics, inputs)
    p.close()
    p.join()

    # Combining the jackknife metrics and getting their means
    scores = pd.concat(stat_list, axis=0)
    means = scores.mean()
    return scores, means


# Calculates bootstrap confidence intervals for an estimator
class boot_cis:
    def __init__(
        self,
        targets,
        guesses,
        sample_by=None,
        n=100,
        a=0.05,
        method="bca",
        interpolation="nearest",
        average_by=None,
        weighted=True,
        mcnemar=False,
        seed=10221983,
    ):
        # Converting everything to NumPy arrays, just in case
        stype = type(pd.Series())
        if type(sample_by) == stype:
            sample_by = sample_by.values
        if type(targets) == stype:
            targets = targets.values
        if type(guesses) == stype:
            guesses = guesses.values

        # Getting the point estimates
        stat = tools.clf_metrics(
            targets, guesses, average_by=average_by, weighted=weighted, mcnemar=mcnemar
        ).transpose()

        # Pulling out the column names to pass to the bootstrap dataframes
        colnames = list(stat.index.values)

        # Making an empty holder for the output
        scores = pd.DataFrame(np.zeros(shape=(n, stat.shape[0])), columns=colnames)

        # Setting the seed
        if seed is None:
            seed = np.random.randint(0, 1e6, 1)
        np.random.seed(seed)
        seeds = np.random.randint(0, 1e6, n)

        # Generating the bootstrap samples and metrics
        p = Pool()
        boot_input = [(targets, sample_by, None, seed) for seed in seeds]
        boots = p.starmap(tools.boot_sample, boot_input)

        if average_by is not None:
            inputs = [
                (targets[boot], guesses[boot], average_by[boot], weighted)
                for boot in boots
            ]
        else:
            inputs = [(targets[boot], guesses[boot]) for boot in boots]

        # Getting the bootstrapped metrics from the Pool
        p_output = p.starmap(tools.clf_metrics, inputs)
        scores = pd.concat(p_output, axis=0)
        p.close()
        p.join()

        # Calculating the confidence intervals
        lower = (a / 2) * 100
        upper = 100 - lower

        # Making sure a valid method was chosen
        methods = ["pct", "diff", "bca"]
        assert method in methods, "Method must be pct, diff, or bca."

        # Calculating the CIs with method #1: the percentiles of the
        # bootstrapped statistics
        if method == "pct":
            cis = np.nanpercentile(
                scores, q=(lower, upper), interpolation=interpolation, axis=0
            )
            cis = pd.DataFrame(
                cis.transpose(), columns=["lower", "upper"], index=colnames
            )

        # Or with method #2: the percentiles of the difference between the
        # obesrved statistics and the bootstrapped statistics
        elif method == "diff":
            stat_vals = stat.transpose().values.ravel()
            diffs = stat_vals - scores
            percents = np.nanpercentile(
                diffs, q=(lower, upper), interpolation=interpolation, axis=0
            )
            lower_bound = pd.Series(stat_vals + percents[0])
            upper_bound = pd.Series(stat_vals + percents[1])
            cis = pd.concat([lower_bound, upper_bound], axis=1)
            cis = cis.set_index(stat.index)

        # Or with method #3: the bias-corrected and accelerated bootstrap
        elif method == "bca":
            # Calculating the bias-correction factor
            stat_vals = stat.transpose().values.ravel()
            n_less = np.sum(scores < stat_vals, axis=0)
            p_less = n_less / n
            z0 = norm.ppf(p_less)

            # Fixing infs in z0
            z0[np.where(np.isinf(z0))[0]] = 0.0

            # Estiamating the acceleration factor
            j = jackknife_metrics(targets, guesses)
            diffs = j[1] - j[0]
            numer = np.sum(np.power(diffs, 3))
            denom = 6 * np.power(np.sum(np.power(diffs, 2)), 3 / 2)

            # Getting rid of 0s in the denominator
            zeros = np.where(denom == 0)[0]
            for z in zeros:
                denom[z] += 1e-6

            # Finishing up the acceleration parameter
            acc = numer / denom
            self.jack = j

            # Calculating the bounds for the confidence intervals
            zl = norm.ppf(a / 2)
            zu = norm.ppf(1 - (a / 2))
            lterm = (z0 + zl) / (1 - acc * (z0 + zl))
            uterm = (z0 + zu) / (1 - acc * (z0 + zu))
            lower_q = norm.cdf(z0 + lterm) * 100
            upper_q = norm.cdf(z0 + uterm) * 100
            self.lower_q = lower_q
            self.upper_q = upper_q

            # Returning the CIs based on the adjusted quintiles
            cis = [
                np.nanpercentile(
                    scores.iloc[:, i],
                    q=(lower_q[i], upper_q[i]),
                    interpolation=interpolation,
                    axis=0,
                )
                for i in range(len(lower_q))
            ]
            cis = pd.DataFrame(cis, columns=["lower", "upper"], index=colnames)

        # Putting the stats with the lower and upper estimates
        cis = pd.concat([stat, cis], axis=1)
        cis.columns = ["stat", "lower", "upper"]

        # Passing the results back up to the class
        self.cis = cis
        self.scores = scores

        return


def boot_roc(targets, scores, sample_by=None, n=1000, seed=10221983):
    # Generating the seeds
    np.random.seed(seed)
    seeds = np.random.randint(1, 1e7, n)

    # Getting the indices for the bootstrap samples
    p = Pool()
    boot_input = [(targets, sample_by, None, seed) for seed in seeds]
    boots = p.starmap(tools.boot_sample, boot_input)

    # Getting the ROC curves
    roc_input = [(targets[boot], scores[boot]) for boot in boots]
    rocs = p.starmap(roc_curve, roc_input)

    return rocs


def dask_merge_all(df_list, **kwargs):

    out = reduce(lambda x, y: x.join(y, **kwargs), df_list)
    return out


# Dask-enabled preprocessing class
class parquets_dask(object):

    # HACK:
    # Yeah, yeah. It's not great.
    # Could also pass in during init or just abstract away even more
    # but this is the extent of my py-fu
    final_names = ["vitals", "bill", "gen_lab", "proc", "diag", "lab_res"]
    feat_prefix = ["vtl", "bill", "genl", "proc", "dx", "lbrs"]
    time_cols = [
        ["observation_day_number", "observation_time_of_day"],
        ["serv_day"],
        ["collection_day_number", "collection_time_of_day"],
        ["proc_day"],
        None,
        ["spec_day_number", "spec_time_of_day"],
    ]

    text_cols = [
        "lab_test",
        "std_chg_desc",
        "lab_test_loinc_desc",
        "icd_code",
        "icd_code",
        "text",
    ]
    num_col = ["test_result_numeric_value", None, "numeric_value", None, None, None]

    df_arg_names = ["df", "text_col", "feature_prefix", "num_col", "time_cols"]

    def __init__(
        self,
        data_dir="data/data/",
        dask_client=None,
        agg_lvl="dfi"
    ):

        # Start Dask client
        self.client = dask_client
        print(self.client)

        # Specifying some columns to pull
        genlab_cols = [
            "collection_day_number",
            "collection_time_of_day",
            "lab_test_loinc_desc",
            "numeric_value",
        ]
        vital_cols = [
            "observation_day_number",
            "observation_time_of_day",
            "lab_test",
            "test_result_numeric_value",
        ]
        bill_cols = ["std_chg_desc", "serv_day"]
        lab_res_cols = [
            "spec_day_number",
            "spec_time_of_day",
            "test",
            "observation",
        ]

        # Pulling in the visit tables
        self.pat = dd.read_parquet(data_dir + "vw_covid_pat/", index="pat_key")
        self.id = dd.read_parquet(data_dir + "vw_covid_id/", index="pat_key")

        # Pulling the lab and vitals
        genlab = dd.read_parquet(
            data_dir + "vw_covid_genlab/", columns=genlab_cols, index="pat_key"
        )
        hx_genlab = dd.read_parquet(
            data_dir + "vw_covid_hx_genlab/", columns=genlab_cols, index="pat_key"
        )
        lab_res = dd.read_parquet(
            data_dir + "vw_covid_lab_res/", columns=lab_res_cols, index="pat_key"
        )

        hx_lab_res = dd.read_parquet(
            data_dir + "vw_covid_hx_lab_res/", columns=lab_res_cols, index="pat_key"
        )
        vitals = dd.read_parquet(
            data_dir + "vw_covid_vitals/", columns=vital_cols, index="pat_key"
        )
        hx_vitals = dd.read_parquet(
            data_dir + "vw_covid_hx_vitals/", columns=vital_cols, index="pat_key"
        )

        # Concatenating the current and historical labs and vitals
        self._genlab = dd.concat(
            [genlab, hx_genlab], axis=0, interleave_partitions=True
        )
        self._vitals = dd.concat(
            [vitals, hx_vitals], axis=0, interleave_partitions=True
        )
        self._lab_res = dd.concat(
            [lab_res, hx_lab_res], axis=0, interleave_partitions=True
        )

        # Pulling in the billing tables
        bill_lab = dd.read_parquet(
            data_dir + "vw_covid_bill_lab/", columns=bill_cols, index="pat_key"
        )
        bill_pharm = dd.read_parquet(
            data_dir + "vw_covid_bill_pharm/", columns=bill_cols, index="pat_key"
        )
        bill_oth = dd.read_parquet(
            data_dir + "vw_covid_bill_oth/", columns=bill_cols, index="pat_key"
        )
        hx_bill = dd.read_parquet(
            data_dir + "vw_covid_hx_bill/", columns=bill_cols, index="pat_key"
        )
        self._bill = dd.concat(
            [bill_lab, bill_pharm, bill_oth, hx_bill],
            axis=0,
            interleave_partitions=True,
        )

        # Pulling in the additional diagnosis and procedure tables
        pat_diag = dd.read_parquet(data_dir + "vw_covid_paticd_diag/", index="pat_key")
        pat_proc = dd.read_parquet(data_dir + "vw_covid_paticd_proc/", index="pat_key")
        add_diag = dd.read_parquet(
            data_dir + "vw_covid_additional_paticd_" + "diag/", index="pat_key"
        )
        add_proc = dd.read_parquet(
            data_dir + "vw_covid_additional_paticd_" + "proc/", index="pat_key"
        )
        self._diag = dd.concat([pat_diag, add_diag], axis=0, interleave_partitions=True)
        self._proc = dd.concat([pat_proc, add_proc], axis=0, interleave_partitions=True)

        # And any extras
        self.icd = dd.read_parquet(data_dir + "icdcode/")

        # Fixing lab_res
        self._lab_res["text"] = (
            self._lab_res["test"].astype(str)
            + " "
            + self._lab_res["observation"].astype(str)
        )

        # Compute all the needed arguments for df_to_feature
        self.df_kwargs = self.compute_kwargs()

        # Save agg level information

        # Quick lookup for multiplier based on agg_lvl
        # NOTE: we will use these values to transform
        # day count to the appropriate time unit agg_lvl.
        # and also use them to transform the time (in seconds)
        # pulled from a timestamp to the appropriate time unit
        day_vals = [1, 24, 1440]
        sec_vals = [60 * 60 * 24, 60 * 60, 60]
        agg_lvls = ["dfi", "hfi", "mfi"]

        self.from_days = dict(zip(agg_lvls, day_vals))
        self.from_seconds = dict(zip(agg_lvls, sec_vals))

        self.agg_level = agg_lvl

        # Compute timing from index for each visit
        self.visit_timing = self.client.compute(self.get_visit_timing(self.id))

        # Pull id as a pandas dataframe since it's not too large
        self.id = self.client.compute(self.id)

        # Compute an H:M:S lookup table
        # (which is quick and prevents us from parsing or using string ops)
        time_stamps = [
            "{:02d}:{:02d}:{:02d}".format(a, b, c)
            for a, b, c in product(range(24), range(60), range(60))
        ]

        self.time_dict = dict(zip(time_stamps, range(len(time_stamps))))

        return

    def compute_kwargs(self):
        out = [
            dict(zip(self.df_arg_names, a))
            for a in zip(
                [self._vitals, self._bill, self._genlab, self._proc, self._diag, self._lab_res],
                self.text_cols,
                self.feat_prefix,
                self.num_col,
                self.time_cols,
            )
        ]

        return out

    def all_df_to_feat(self, pkl_file=None, force_compute=False):

        out = []
        code_dicts = []

        # Quantize numeric output, rename text column to "text"
        # NOTE: This is persistent
        out = [self.df_to_features(**kw) for kw in self.df_kwargs]

        # Compute all feature values for each table for lookup table
        code_dicts = [self.col_to_features(a["text"], b["feature_prefix"]) for a, b in zip(out, self.df_kwargs)]
       
       # Convert long feature name to condensed value computed in code dict
        out = [self.condense_features(a, "text", b) for a, b in zip(out, code_dicts)]

        # Combining the feature dicts and saving to disk
        ftr_dict = dict(
            zip(
                tp.flatten([d.keys() for d in code_dicts]),
                tp.flatten([d.values() for d in code_dicts]),
            )
        )

        # Write all dictionaries to pickle
        if pkl_file is not None:
            with open(pkl_file, "wb") as file:
                pickle.dump(ftr_dict, file)

        # return dict of dask dataframes which contain the slimmed down data
        # NOTE: These still have to be evaluated, but
        # the hope is keeping it as a task graph will keep memory overhead low
        # until it's absolutely necessary to read in
        return dict(zip(self.final_names, out)), ftr_dict

    def condense_features(self, df, text_col = "text", code_dict = None):
        df["ftr"] = df[text_col].map({k: v for v, k in code_dict.items()})

        df = df.drop(text_col, axis=1)

        return self.client.persist(df)

    def col_to_features(self, text, feature_prefix):

        unique_codes = self.client.compute(text.unique(), sync=True)
        n_codes = len(unique_codes)
        ftr_codes = ["".join([feature_prefix, str(i)]) for i in range(n_codes)]
        code_dict = dict(zip(ftr_codes, unique_codes))

        return code_dict

    def df_to_features(
        self,
        df,
        text_col,
        feature_prefix,
        num_col=None,
        time_cols=None,
        buckets=5,
        slim=True,
    ):

        # Pulling out the text
        df[text_col] = df[text_col].astype(str)

        # Optionally quantizing the numeric column
        if num_col is not None:
        # BUG: Transform triggers a shuffle which is very
        # computationally intensive. If there were a faster
        # way to have the data indexed by the text col first and
        # then reindex by pat_key, that would be ideal.
        # any operation where groupby uses a non-index is costly
            df["q"] = (
                df.groupby(text_col)[num_col]
                .transform(
                    pd.qcut, q=buckets, labels=False, duplicates="drop", meta=("q", "f")
                )
                .reset_index(drop=True)
            )

            df[text_col] += " q" + df["q"].astype(str)

        # Return full set (as pandas DF)
        if not slim:
            return df

        # Rename text_col to "text" for further downstream processing
        df = df.rename(columns={text_col: "text"})

        out_cols = ["text"]

        if time_cols is not None:
            out_cols += time_cols

        # Return as a dask lazy Df and a persistent dict with the features
        out = df[out_cols]

        return self.client.persist(out)

    def get_visit_timing(self, id_table, day_col="days_from_index"):

        out = id_table[day_col].to_frame()

        out[self.agg_level] = out[day_col] * self.from_days[self.agg_level]

        out = out.drop(day_col, axis=1)

        return out

    def get_timing(
        self, df, day_col=None, end_of_visit=False, time_col=None, ftr_col="ftr"
    ):
        """Compute timing for each feature based on the granularity specified.

        Arguments:

        df: Dask or pandas dataframe
            Data where time and feature columns can be found

        day_col: str (default: None)
            Name of column in df which contains days from record index

        end_of_visit: bool (default: False)
            Should the feature values be appended to the last day in the visit?

        time_col: str (default: None)
            Optional column name in df which contains intra-day timing

        ftr_col: str (default: "ftr")
            Name of feature column to aggregate
        """

        # Compute which cols we will have
        out_cols = [col for col in [ftr_col, day_col, time_col] if col is not None]

        # Merge in timing for visit which was already computed
        # make sure to index by pat_key so we can keep this efficient.
        # out: dask df with [out_cols], agg_lvl
        out = df[out_cols]
        out = out.join(self.visit_timing.result(), how="left", on="pat_key")

        # If we want this to occur at the end of the visit, add visit LOS to our timing
        # that was computed in get_visit_timing
        if end_of_visit:
            out = out.join(self.id.result()["los"].to_frame(), how="left", on="pat_key")

            out[self.agg_level] += out["los"] * self.from_days[self.agg_level]
            out = out.drop("los", axis=1)

        # If we have no other timing information, the visit timing is
        # all we can add, so return as-is
        if day_col is None:
            return out

        # Add day column contribution to the timing
        out[self.agg_level] += out[day_col] * self.from_days[self.agg_level]

        # We don't need the day col anymore
        out = out.drop(day_col, axis=1)

        if time_col is not None:
            # Using a seconds in a day dictionary lookup
            # map HMS strings to dict to convert to seconds
            # then aggregate to appropriate agg_level

            out[self.agg_level] += (
                out[time_col].map(self.time_dict) / self.from_seconds[self.agg_level]
            )

            # Done with the time col
            out = out.drop(time_col, axis=1)

        return out

    def reset_agg_level(self, new_level):
        """Helper function to reset aggregation level during runtime
        in the unlikely event that you'd like to recompute at a different
        level of time aggregation without re-running the whole pipeline.
        """
        # Update agg level
        self.agg_level = new_level

        # Recompute visit timing based on new agg level
        self.visit_timing = self.get_visit_timing(self.id)

        return None

    def agg_features(self, df, as_str=True, ftr_col="ftr", out_col="ftrs"):
        """Aggregate feature column to token columns by time step + id"""

        grouped = df[[self.agg_level, ftr_col]].groupby([self.agg_level, df.index])
        agged = grouped.agg(list).rename(columns={ftr_col: out_col})

        # We might want to keep as list-of-lists instead of concatenating
        if as_str:
            agged[out_col] = agged[out_col].map(lambda x: " ".join(x))

        return self.client.persist(agged)