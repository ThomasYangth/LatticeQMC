# coding: utf-8
"""
Created on 20 Jan 2020
author: Dylan Jones

project: LatticeQMC
version: 1.0
"""
import numpy as np
from collections import namedtuple
from scipy.special import expit
from .tools import fermi_fct, matsubara_frequencies

PoleGf = namedtuple('PoleGf', ['resids', 'poles'])


def pole_gf_moments(poles, weights, order):
    poles, weights = np.atleast_1d(*np.broadcast_arrays(poles, weights))
    order = np.asarray(order)[..., np.newaxis]
    return np.sum(weights[..., np.newaxis, :] * poles[..., np.newaxis, :]**(order-1), axis=-1)


def pole_gf_z(z, poles, weights):
    return np.sum(weights/(np.subtract.outer(z, poles)), axis=-1)


def tau2iw_ft_lin(gf_tau, beta):
    gf_tau_full_range = np.concatenate((-gf_tau[..., :-1], gf_tau), axis=-1)
    n_tau = gf_tau_full_range.shape[-1]  # pylint: disable=unsubscriptable-object
    gf_dft = np.fft.ihfft(gf_tau_full_range[..., :-1])
    d_gf_tau = gf_tau_full_range[..., 1:] - gf_tau_full_range[..., :-1]
    d_gf_dft = np.fft.ihfft(d_gf_tau)
    d_tau_iws = 2j*np.pi*np.arange(1, gf_dft.shape[-1], 2) / n_tau
    expm1 = np.expm1(d_tau_iws)
    weight1 = expm1 / d_tau_iws
    weight2 = (expm1 + 1 - weight1) / d_tau_iws
    gf_iw = weight1 * gf_dft[..., 1::2] + weight2 * d_gf_dft[..., 1::2]
    gf_iw = -beta * gf_iw
    return gf_iw


def pole_gf_from_moments(moments) -> PoleGf:
    moments = np.asarray(moments)
    n_mom = moments.shape[-1]
    if n_mom == 0:  # non-sense case, but return consistent behaviour
        return PoleGf(resids=moments.copy(), poles=np.array([]))
    poles = np.cos(.5*np.pi*np.arange(1, 2*n_mom, 2)/n_mom)
    if n_mom % 2:
        poles[n_mom//2] = 0.
    mat = np.polynomial.polynomial.polyvander(poles, deg=poles.size-1).T
    mat = mat.reshape((1,)*(moments.ndim - 1) + mat.shape)
    resid = np.linalg.solve(mat, moments)
    return PoleGf(resids=resid, poles=poles)


def pole_gf_tau(tau, poles, weights, beta):
    assert np.all((tau >= 0.) & (tau <= beta))
    poles, weights = np.atleast_1d(*np.broadcast_arrays(poles, weights))
    tau = np.asanyarray(tau)
    tau = tau.reshape(tau.shape + (1,)*poles.ndim)
    # exp(-tau*pole)*f(-pole, beta) = exp((beta-tau)*pole)*f(pole, beta)
    exponent = np.where(poles.real >= 0, -tau, beta-tau) * poles
    single_pole_tau = np.exp(exponent) * fermi_fct(-np.sign(poles.real)*poles, beta)
    return -np.sum(weights*single_pole_tau, axis=-1)


def tau2iw(gf_tau, beta, moments=None, fourier=tau2iw_ft_lin):
    tau = np.linspace(0, beta, num=gf_tau.shape[-1])
    m1 = -gf_tau[..., -1] - gf_tau[..., 0]
    if moments is None:  # = 1/z moment = jump of Gf at 0^{±}
        moments = m1[..., np.newaxis]
    else:
        if not np.allclose(m1, moments[..., 0]):
            raise Warning(f"Provided 1/z moment differs from jump. mom: {moments[..., 0]}, jump: {m1}")
    pole_gf = pole_gf_from_moments(moments)
    gf_tau = gf_tau - pole_gf_tau(tau, poles=pole_gf.poles, weights=pole_gf.resids, beta=beta)
    gf_iw = fourier(gf_tau, beta=beta)
    iws = matsubara_frequencies(range(gf_iw.shape[-1]), beta=beta)
    gf_iw += pole_gf_z(iws, poles=pole_gf.poles, weights=pole_gf.resids)
    return iws, gf_iw


def tau2iw_dft(gf_tau, beta):
    r"""Discrete Fourier transform of the real Green's function `gf_tau`.

    Fourier transformation of a fermionic imaginary-time Green's function to
    Matsubara domain.
    The Fourier integral is replaced by a Riemann sum giving a discrete
    Fourier transform (DFT).
    We assume a real Green's function `gf_tau`, which is the case for
    commutator Green's functions :math:`G_{AB}(τ) = ⟨A(τ)B⟩` with
    :math:`A = B^†`. The Fourier transform `gf_iw` is then Hermitian.

    Parameters
    ----------
    gf_tau : (..., N_tau) float np.ndarray
        The Green's function at imaginary times :math:`τ \in [0, β]`.
    beta : float
        The inverse temperature :math:`beta = 1/k_B T`.

    Returns
    -------
    gf_iw : (..., {N_iw - 1}/2) float np.ndarray
        The Fourier transform of `gf_tau` for positive fermionic Matsubara
        frequencies :math:`iω_n`.
    """
    # expand `gf_tau` to [-β, β] to get symmetric function
    gf_tau_full_range = np.concatenate((-gf_tau[..., :-1], gf_tau), axis=-1)
    dft = np.fft.ihfft(gf_tau_full_range[..., :-1])
    gf_iw = -beta * dft[..., 1::2]  # select fermionic Matsubara frequencies
    return gf_iw