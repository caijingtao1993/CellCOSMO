#!/usr/bin/env python
# -*- coding:utf-8 -*-
"""
@Author     : ice-melt@outlook.com
@File       : stats.py
@Time       : 2022/06/07
@Version    : 1.0
@Desc       : None
"""
import sys
import numpy as np
import scipy.stats as sp_stats



def determine_max_filtered_bcs(recovered_cells):
    """ Determine the max # of cellular barcodes to consider """
#     return float(recovered_cells) * cr_constants.FILTER_BARCODES_MAX_RECOVERED_CELLS_MULTIPLE # 6
    return float(recovered_cells) * 6


def init_barcode_filter_result():
    return {
        'filtered_bcs': 0,
        #'filtered_bcs_lb': 0,
        #'filtered_bcs_ub': 0,
        #'max_filtered_bcs': 0,
        'filtered_bcs_var': 0,
        #'filtered_bcs_cv': 0,
    }


def summarize_bootstrapped_top_n(top_n_boot):
    top_n_bcs_mean = np.mean(top_n_boot)
    #top_n_bcs_sd = np.std(top_n_boot)
    top_n_bcs_var = np.var(top_n_boot)
    result = {}
    result['filtered_bcs_var'] = top_n_bcs_var
    # comment these two lines out to avoid `ValueError: cannot convert float NaN to integer` when analyze data with low number of barcode
    #result['filtered_bcs_lb'] = round(sp_stats.norm.ppf(0.025, top_n_bcs_mean, top_n_bcs_sd))
    #result['filtered_bcs_ub'] = round(sp_stats.norm.ppf(0.975, top_n_bcs_mean, top_n_bcs_sd))
    result['filtered_bcs'] = int(round(top_n_bcs_mean))
    return result


def find_within_ordmag(x, baseline_idx):
    x_ascending = np.sort(x)
    baseline = x_ascending[-baseline_idx]
    cutoff = max(1, round(0.1 * baseline))
    # Return the index corresponding to the cutoff in descending order
    return len(x) - np.searchsorted(x_ascending, cutoff)


def filter_cellular_barcodes_ordmag(bc_counts, recovered_cells):
    """ Simply take all barcodes that are within an order of magnitude of a top barcode
        that likely represents a cell
    """
    if recovered_cells is None:
        # Modified parameter, didn't use the default value
        recovered_cells = 3000
#         recovered_cells = cr_constants.DEFAULT_RECOVERED_CELLS_PER_GEM_GROUP # 3000

    # Initialize filter result metrics
    metrics = init_barcode_filter_result()
    # determine max # of cellular barcodes to consider
    max_filtered_bcs = determine_max_filtered_bcs(recovered_cells)
    metrics['max_filtered_bcs'] = max_filtered_bcs

    nonzero_bc_counts = bc_counts[bc_counts > 0]
    if len(nonzero_bc_counts) == 0:
        msg = "WARNING: All barcodes do not have enough reads for ordmag, allowing no bcs through"
        return [], metrics, msg

#     baseline_bc_idx = int(round(float(recovered_cells) * (1 - cr_constants.ORDMAG_RECOVERED_CELLS_QUANTILE))) # Quantile=0.99
    baseline_bc_idx = int(round(float(recovered_cells) * (1 - 0.99)))  # Quantile=0.99
    baseline_bc_idx = min(baseline_bc_idx, len(nonzero_bc_counts) - 1)
    assert baseline_bc_idx < max_filtered_bcs

    # Bootstrap sampling; run algo with many random samples of the data
    top_n_boot = np.array([
        find_within_ordmag(np.random.choice(nonzero_bc_counts, len(nonzero_bc_counts)), baseline_bc_idx)
        for i in range(100)  # 100
        #         for i in range(cr_constants.ORDMAG_NUM_BOOTSTRAP_SAMPLES) # 100
    ])

    metrics.update(summarize_bootstrapped_top_n(top_n_boot))

    # Get the filtered barcodes
    top_n = metrics['filtered_bcs']
    top_bc_idx = np.sort(np.argsort(bc_counts)[::-1][:top_n])
    return top_bc_idx, metrics, None


def filter_cellular_barcodes_fixed_cutoff(bc_counts, cutoff):
    nonzero_bcs = len(bc_counts[bc_counts > 0])
    top_n = min(cutoff, nonzero_bcs)
    top_bc_idx = np.sort(np.argsort(bc_counts)[::-1][:top_n])
    metrics = {
        'filtered_bcs': top_n,
        'filtered_bcs_lb': top_n,
        'filtered_bcs_ub': top_n,
        'max_filtered_bcs': 0,
        'filtered_bcs_var': 0,
        'filtered_bcs_cv': 0,
    }
    return top_bc_idx, metrics, None


def est_background_profile_bottom(matrix, bottom_frac):
    """Construct a background expression profile from the barcodes that make up the bottom b% of the data
    Args:
      matrix (scipy.sparse.csc_matrix): Feature x Barcode matrix
      bottom_frac (float): Use barcodes making up the bottom x fraction of the counts (0-1)
    Returns:
      (nz_feat (ndarray(int)), profile_p (ndarray(float)): Indices of nonzero features and background profile
    """
    assert bottom_frac >= 0 and bottom_frac <= 1
    umis_per_bc = np.ravel(np.asarray(matrix.sum(0)))
    barcode_order = np.argsort(umis_per_bc)

    cum_frac = np.cumsum(umis_per_bc[barcode_order]) / float(umis_per_bc.sum())
    max_bg_idx = np.searchsorted(cum_frac, bottom_frac, side='left')
    bg_mat = matrix[:, barcode_order[0:max_bg_idx]]

    nz_feat = np.flatnonzero(np.asarray(bg_mat.sum(1)))
    bg_profile = np.ravel(bg_mat[nz_feat, :].sum(axis=1))
    bg_profile_p = bg_profile / float(np.sum(bg_profile))
    assert np.isclose(bg_profile_p.sum(), 1)

    return (nz_feat, bg_profile_p)


def eval_multinomial_loglikelihoods(matrix, profile_p, max_mem_gb=0.1):
    """Compute the multinomial log PMF for many barcodes
    Args:
      matrix (scipy.sparse.csc_matrix): Matrix of UMI counts (feature x barcode)
      profile_p (np.ndarray(float)): Multinomial probability vector
      max_mem_gb (float): Try to bound memory usage.
    Returns:
      log_likelihoods (np.ndarray(float)): Log-likelihood for each barcode
    """
    gb_per_bc = float(matrix.shape[0] * matrix.dtype.itemsize) / (1024**3)
    bcs_per_chunk = max(1, int(round(max_mem_gb/gb_per_bc)))
    num_bcs = matrix.shape[1]

    loglk = np.zeros(num_bcs)

    for chunk_start in range(0, num_bcs, bcs_per_chunk):
        chunk = slice(chunk_start, chunk_start+bcs_per_chunk)
        matrix_chunk = matrix[:, chunk].transpose().toarray()
        n = matrix_chunk.sum(1)
        loglk[chunk] = sp_stats.multinomial.logpmf(matrix_chunk, n, p=profile_p)
    return loglk


def simulate_multinomial_loglikelihoods(profile_p, umis_per_bc,
                                        num_sims=1000, jump=1000,
                                        n_sample_feature_block=1000000, verbose=False):
    """Simulate draws from a multinomial distribution for various values of N.
       Uses the approximation from Lun et al. ( https://www.biorxiv.org/content/biorxiv/early/2018/04/04/234872.full.pdf )
    Args:
      profile_p (np.ndarray(float)): Probability of observing each feature.
      umis_per_bc (np.ndarray(int)): UMI counts per barcode (multinomial N).
      num_sims (int): Number of simulations per distinct N value.
      jump (int): Vectorize the sampling if the gap between two distinct Ns exceeds this.
      n_sample_feature_block (int): Vectorize this many feature samplings at a time.
    Returns:
      (distinct_ns (np.ndarray(int)), log_likelihoods (np.ndarray(float)):
      distinct_ns is an array containing the distinct N values that were simulated.
      log_likelihoods is a len(distinct_ns) x num_sims matrix containing the
        simulated log likelihoods.
    """
    distinct_n = np.flatnonzero(np.bincount(umis_per_bc))

    loglk = np.zeros((len(distinct_n), num_sims), dtype=float)
    num_all_n = np.max(distinct_n) - np.min(distinct_n)
    if verbose:
        print('Number of distinct N supplied: %d' % len(distinct_n))
        print('Range of N: %d' % num_all_n)
        print('Number of features: %d' % len(profile_p))

    sampled_features = np.random.choice(len(profile_p), size=n_sample_feature_block, p=profile_p, replace=True)
    k = 0

    log_profile_p = np.log(profile_p)

    for sim_idx in range(num_sims):
        if verbose and sim_idx % 100 == 99:
            sys.stdout.write('.')
            sys.stdout.flush()
        curr_counts = np.ravel(sp_stats.multinomial.rvs(distinct_n[0], profile_p, size=1))

        curr_loglk = sp_stats.multinomial.logpmf(curr_counts, distinct_n[0], p=profile_p)

        loglk[0, sim_idx] = curr_loglk

        for i in range(1, len(distinct_n)):
            step = distinct_n[i] - distinct_n[i-1]
            if step >= jump:
                # Instead of iterating for each n, sample the intermediate ns all at once
                curr_counts += np.ravel(sp_stats.multinomial.rvs(step, profile_p, size=1))
                curr_loglk = sp_stats.multinomial.logpmf(curr_counts, distinct_n[i], p=profile_p)
                assert not np.isnan(curr_loglk)
            else:
                # Iteratively sample between the two distinct values of n
                for n in range(distinct_n[i-1]+1, distinct_n[i]+1):
                    j = sampled_features[k]
                    k += 1
                    if k >= n_sample_feature_block:
                        # Amortize this operation
                        sampled_features = np.random.choice(
                            len(profile_p), size=n_sample_feature_block, p=profile_p, replace=True)
                        k = 0
                    curr_counts[j] += 1
                    curr_loglk += log_profile_p[j] + np.log(float(n)/curr_counts[j])

            loglk[i, sim_idx] = curr_loglk

    if verbose:
        sys.stdout.write('\n')

    return distinct_n, loglk


def compute_ambient_pvalues(umis_per_bc, obs_loglk, sim_n, sim_loglk):
    """Compute p-values for observed multinomial log-likelihoods
    Args:
      umis_per_bc (nd.array(int)): UMI counts per barcode
      obs_loglk (nd.array(float)): Observed log-likelihoods of each barcode deriving from an ambient profile
      sim_n (nd.array(int)): Multinomial N for simulated log-likelihoods
      sim_loglk (nd.array(float)): Simulated log-likelihoods of shape (len(sim_n), num_simulations)
    Returns:
      pvalues (nd.array(float)): p-values
    """
    assert len(umis_per_bc) == len(obs_loglk)
    assert sim_loglk.shape[0] == len(sim_n)

    # Find the index of the simulated N for each barcode
    sim_n_idx = np.searchsorted(sim_n, umis_per_bc)
    num_sims = sim_loglk.shape[1]

    num_barcodes = len(umis_per_bc)

    pvalues = np.zeros(num_barcodes)

    for i in range(num_barcodes):
        num_lower_loglk = np.sum(sim_loglk[sim_n_idx[i], :] < obs_loglk[i])
        pvalues[i] = float(1 + num_lower_loglk) / (1 + num_sims)
    return pvalues
