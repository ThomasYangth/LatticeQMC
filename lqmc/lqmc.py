# coding: utf-8
"""
Created on 15 Jan 2020

project: LatticeQMC
version: 1.0
"""
import time
import logging
import itertools
import numpy as np
from scipy.linalg import expm
from lqmc import HubbardModel, Configuration


class LatticeQMC:

    def __init__(self, model, beta, time_steps, warmup=300, sweeps=2000, det_mode=False):
        """ Initialize the Lattice Quantum Monte-Carlo solver.

        Parameters
        ----------
        model: HubbardModel
            The Hubbard model instance.
        beta: float
            The inverse temperature .math'\beta = 1/T'.
        time_steps: int
            Number of time steps from .math'0' to .math'\beta'.
        warmup int, optional
            Number of warmup sweeps.
        sweeps: int, optional
            Number of measurement sweeps.
        det_mode: bool, optional
            Flag for the calculation mode. If 'True' the slow algorithm via
            the determinants is used. The default i9s 'False' (faster).
        """
        self.model = model
        self.dtau = beta / time_steps
        self.time_steps = time_steps
        self.warm_sweeps = warmup
        self.meas_sweeps = sweeps

        self.n_sites = model.n_sites
        self.config = Configuration(self.n_sites, time_steps)

        self.det_mode = det_mode
        self.status = ""
        self.it = 0

        # Cached variables
        self.ham_kin = self.model.ham_kinetic()
        self.lamb = np.arccosh(np.exp(self.model.u * self.dtau / 2.)) if self.model.u else 1
        self.exp_k = expm(+1 * self.dtau * self.ham_kin)
        self.exp_v = np.zeros((self.n_sites, self.n_sites), dtype=np.float64)
        self.exp_k_inv = expm(+1 * self.dtau * self.ham_kin)
        self.exp_v_inv = np.zeros((self.n_sites, self.n_sites), dtype=np.float64)

    def log_iterstep(self, sweep, i, l, ratio, acc):
        logging.debug(f"{self.status} {sweep} i={i}, l={l} - ratio={ratio:.3f}, accepted={acc}")

    def loop_generator(self, sweeps):
        """ Generates the indices of the LQMC loop.

        This is mainly used to saving total iteration number and
        for hooking logging and printing events into the loops easily.

        Parameters
        ----------
        sweeps: int
            Number of sweeps of current loop.

        Yields
        -------
        indices: tuple of int
            Indices of current iteration step consisting of .term'sweep', .term'i' and .term'l'.
        """
        for sweep in range(sweeps):
            self.it = sweep
            for i, l in itertools.product(range(self.model.n_sites), range(self.config.time_steps)):
                print(f"\r{self.status} Sweep {sweep} [{i}, {l}]", end="", flush=True)
                yield sweep, i, l

    def _exp_v(self, l, sigma, inv=True):
        r""" Computes the Matrix exponential of 'V_\sigma(l)'

        To Do
        -----
        This is pretty sure the main bottle-neck
        Maybe scipy.sparse since V is diagonal?

        Parameters
        ----------
        l: int
            Time-slice index.
        sigma: int
            Spin value.

        Returns
        -------
        exp_v: (N, N) np.ndarray
        """
        if inv:
            np.fill_diagonal(self.exp_v_inv, np.exp(-1 * sigma * self.lamb * self.config[:, l]))
            return self.exp_v_inv
        else:
            np.fill_diagonal(self.exp_v, np.exp(+1 * sigma * self.lamb * self.config[:, l]))
            return self.exp_v

    def _m(self, sigma):
        r""" Computes the 'M' matrices for spin '\sigma'

        Returns
        -------
        m: (N, N) np.ndarray
        """
        lmax = self.config.time_steps - 1
        # compute prod(B)
        exp_v = self._exp_v(lmax, sigma)
        b = np.dot(exp_v, self.exp_k)
        b_prod = b
        for l in reversed(range(0, lmax)):
            exp_v = self._exp_v(l, sigma)
            b = np.dot(self.exp_k, exp_v)
            b_prod = np.dot(b_prod, b)
        # compute M matrix
        return np.eye(self.n_sites) + b_prod

    def _m_cyclic(self, sigma, l):
        r""" Computes the 'M' matrices for spin '\sigma' at time l

        Returns
        -------
        m: (N, N) np.ndarray
        """
        lmax = self.config.time_steps
        # compute prod(B)
        exp_v = self._exp_v(l, sigma)
        b = np.dot(exp_v, self.exp_k)
        b_prod = b
        for l_loop in reversed(range(0, l)):
            exp_v = self._exp_v(l_loop, sigma)
            b = np.dot(self.exp_k, exp_v)
            b_prod = np.dot(b_prod, b)
        for l_loop in reversed(range(l+1, lmax)):
            exp_v = self._exp_v(l_loop, sigma)
            b = np.dot(self.exp_k, exp_v)
            b_prod = np.dot(b_prod, b)
        # compute M matrix
        return np.eye(self.n_sites) + b_prod

    def _gf_tau(self, g_beta, sigma):
        """ Computes the Green's function for all time slices recursively.

        Returns
        -------
        gf_tau: (M, N, N) np.ndarray
            The Green's function for N sites and M time slices.
        """
        # First index of g[0, :, :] represents the time slices
        g = np.zeros((self.time_steps, self.n_sites, self.n_sites), dtype=np.float64)
        g[0, :, :] = g_beta
        for l in range(1, self.time_steps):
            exp_v = self._exp_v(l, sigma)
            exp_v_inv = self._exp_v(l, sigma, inv=True)
            b = np.dot(exp_v, self.exp_k)
            # b_inv = np.linalg.inv(b)
            b_inv = np.dot(exp_v_inv, self.exp_k_inv)
            g[l, ...] = np.dot(np.dot(b_inv, g[l - 1, ...]), b)
        return g[::-1]

    def _compute_m(self):
        """ Computes the 'M' matrices for both spins.

        Returns
        -------
        m_up: (N, N) np.ndarray
        m_dn: (N, N) np.ndarray
        """
        m_up = self._m(sigma=+1)  # compute_m(self.ham_kin, self.config, self.lamb, self.dtau, sigma=+1)
        m_dn = self._m(sigma=-1)  # compute_m(self.ham_kin, self.config, self.lamb, self.dtau, sigma=-1)
        return m_up, m_dn

    def _compute_m_cyclic(self, l):
        """ Computes the 'M' matrices for both spins at time l.

        Returns
        -------
        m_up: (N, N) np.ndarray
        m_dn: (N, N) np.ndarray
        """
        m_up = self._m_cyclic(sigma=+1, l=l)  # compute_m(self.ham_kin, self.config, self.lamb, self.dtau, sigma=+1)
        m_dn = self._m_cyclic(sigma=-1, l=l)  # compute_m(self.ham_kin, self.config, self.lamb, self.dtau, sigma=-1)
        return m_up, m_dn

    def _compute_gf_tau(self, g_beta_up, g_beta_dn):
        """ Computes the time dependend Green's function for both spins

        Returns
        -------
        gf_up: (M, N, N) np.ndarray
            The spin-up Green's function for N sites and M time slices.
        gf_dn: (M, N, N) np.ndarray
            The spin-down Green's function for N sites and M time slices.
        """
        # compute_gf_tau(self.config, self.ham_kin, g_beta_up, self.lamb, self.dtau, sigma=+1)
        # compute_gf_tau(self.config, self.ham_kin, g_beta_dn, self.lamb, self.dtau, sigma=-1)
        g_tau_up = self._gf_tau(g_beta_up, sigma=+1)
        g_tau_dn = self._gf_tau(g_beta_dn, sigma=-1)
        return g_tau_up, g_tau_dn

    def warmup_loop_det(self):
        """ Runs the slow version of the LQMC warmup-loop """
        self.status = "Warmup"
        m_up, m_dn = self._compute_m()  # Calculate M matrices for both spins
        old = np.linalg.det(m_up) * np.linalg.det(m_dn)
        old_config = self.config.copy()  # Store copy of current configuration
        # QMC loop
        for sweep, i, l in self.loop_generator(self.warm_sweeps):
            # Update Configuration
            self.config.update(i, l)

            m_up, m_dn = self._compute_m()  # Calculate M matrices for both spins
            new = np.linalg.det(m_up) * np.linalg.det(m_dn)
            ratio = new / old
            acc = np.random.rand() < ratio
            if acc:
                # Move accepted: Continue using the new configuration
                old = new
                old_config = self.config.copy()
            else:
                # Move not accepted: Revert to the old configuration
                self.config = old_config

    def measure_loop_det(self):
        r""" Runs the slow version of the LQMC measurement-loop and returns the Green's function.

        Returns
        -------
        gf_dn: (M, N, N) np.ndarray
            Measured spin-up Green's function .math'G_\uparrow(\tau)' for all M time slices.
        gf_dn: (M, N, N) np.ndarray
            Measured spin-down Green's function .math'G_\downarrow(\tau)' for all M time slices.
        """
        self.status = "Measurement"

        m_up, m_dn = self._compute_m()  # Calculate M matrices for both spins
        old = np.linalg.det(m_up) * np.linalg.det(m_dn)
        old_config = self.config.copy()  # Store copy of current configuration

        # Initialize total and temp greens functions --- old
        # gf_up, gf_dn = 0, 0
        # g_beta_up = np.linalg.inv(m_up)
        # g_beta_dn = np.linalg.inv(m_dn)
        # g_tmp_up, g_tmp_dn = self._compute_gf_tau(g_beta_up, g_beta_dn)

        # Initialize greens functions
        g_total_up = np.zeros((self.time_steps, self.n_sites, self.n_sites), dtype=np.float64)
        g_total_dn = np.zeros((self.time_steps, self.n_sites, self.n_sites), dtype=np.float64)
        g_up = np.zeros((self.time_steps, self.n_sites, self.n_sites), dtype=np.float64)
        g_dn = np.zeros((self.time_steps, self.n_sites, self.n_sites), dtype=np.float64)
        g_up[-1, :, :] = np.linalg.inv(m_up)
        g_dn[-1, :, :] = np.linalg.inv(m_dn)

        # QMC loop
        number = 0
        # Breakup of the loop_generator because of steps needed in between
        for sweep in range(self.meas_sweeps):
            # We start at time beta and go down
            for l in reversed(range(self.time_steps)):
                for i in range(self.n_sites):
                    # Update single site in time slice l
                    self.config.update(i, l)

                    # Calculate new M matrices for the updated configuration
                    m_up, m_dn = self._compute_m_cyclic(l)
                    # new weight for the ratio
                    new = np.linalg.det(m_up) * np.linalg.det(m_dn)
                    ratio = new / old
                    acc = np.random.rand() < ratio
                    # Update accepted
                    if acc:
                        # calculate updated greens function for time l
                        g_up[l, :, :] = np.linalg.inv(m_up)
                        g_dn[l, :, :] = np.linalg.inv(m_dn)
                        # update the configuration
                        old = new
                        old_config = self.config.copy()
                    # Update not accepted
                    else:
                        # return to old config by flipping the spin again
                        self.config.update(i, l)
                    # Add result to total results
                    g_total_up[l, :, :] += g_up[l, :, :]
                    g_total_dn[l, :, :] += g_dn[l, :, :]
                    number += 1
                # Update greens function for calculation at the next time slice
                # This is not yet needed, because the greens function is calculated
                # every time explicitly. If the fast updating without the inversion
                # of the M matrix is used, the wrapping step with the B matrices
                # needs to be done here.
                # If using this change back to the old _compute_m function, because
                # order of the B matrices does not matter then as they are used 
                # only within a determinant.
        # Return the normalized total green functions
        g_total_up = g_total_up / number * self.time_steps
        g_total_dn = g_total_dn / number * self.time_steps
        # These are now separated, as we use them separated anyways
        return np.array([g_total_up, g_total_dn])

        # --- old loop
        # for sweep, i, l in self.loop_generator(self.meas_sweeps):
        #     # Update Configuration
        #     self.config.update(i, l)

        #     m_up, m_dn = self._compute_m()  # Calculate M matrices for both spins
        #     new = np.linalg.det(m_up) * np.linalg.det(m_dn)
        #     ratio = new / old
        #     acc = np.random.rand() < ratio
        #     if acc:
        #         # Move accepted:
        #         # Update temp greens function and continue using the new configuration
        #         g_beta_up = np.linalg.inv(m_up)
        #         g_beta_dn = np.linalg.inv(m_dn)
        #         g_tmp_up, g_tmp_dn = self._compute_gf_tau(g_beta_up, g_beta_dn)
        #         old = new
        #         old_config = self.config.copy()
        #     else:
        #         # Move not accepted
        #         self.config = old_config

        #     # Add temp greens function to total gf after each step
        #     gf_up += g_tmp_up
        #     gf_dn += g_tmp_dn
        #     number += 1
        # # Return the normalized gfs for each spin
        # return np.array([gf_up, gf_dn]) / number

    def warmup_loop(self):
        """ Runs the fast version of the LQMC warmup-loop """
        self.status = "Warmup"
        # QMC loop
        for sweep, i, l in self.loop_generator(self.warm_sweeps):
            m_up, m_dn = self._compute_m()  # Calculate M matrices for both spins
            arg = 2 * self.lamb * self.config[i, l]
            d_up = 1 + (1 - np.linalg.inv(m_up)[i, i]) * (np.exp(-arg) - 1)
            d_dn = 1 + (1 - np.linalg.inv(m_dn)[i, i]) * (np.exp(+arg) - 1)
            ratio = d_up * d_dn
            acc = np.random.rand() < ratio
            if acc:
                self.config.update(i, l)

    def measure_loop(self):
        r""" Runs the fast version of the LQMC measurement-loop and returns the Green's function.

        Returns
        -------
        gf_dn: (M, N, N) np.ndarray
            Measured spin-up Green's function .math'G_\uparrow(\tau)' for all M time slices.
        gf_dn: (M, N, N) np.ndarray
            Measured spin-down Green's function .math'G_\downarrow(\tau)' for all M time slices.
        """
        self.status = "Measurement"
        n = self.model.n_sites
        # Initialize total and temp greens functions
        gf_up, gf_dn = 0, 0
        m_up, m_dn = self._compute_m()  # Calculate M matrices for both spins
        g_beta_up = np.linalg.inv(m_up)
        g_beta_dn = np.linalg.inv(m_dn)
        g_tmp_up, g_tmp_dn = self._compute_gf_tau(g_beta_up, g_beta_dn)

        # QMC loop
        number = 0
        for sweep, i, l in self.loop_generator(self.meas_sweeps):
            m_up, m_dn = self._compute_m()  # Calculate M matrices for both spins
            arg = 2 * self.lamb * self.config[i, l]

            d_up = 1 + (1 - np.linalg.inv(m_up)[i, i]) * (np.exp(-arg) - 1)
            d_dn = 1 + (1 - np.linalg.inv(m_dn)[i, i]) * (np.exp(+arg) - 1)
            ratio = d_up * d_dn
            acc = np.random.rand() < ratio
            if acc:
                # Move accepted: Update temp greens function and update configuration
                c_up = np.zeros(n, dtype=np.float64)
                c_dn = np.zeros(n, dtype=np.float64)
                c_up[i] = np.exp(-arg) - 1
                c_dn[i] = np.exp(+arg) - 1
                c_up = -1 * (np.exp(-arg) - 1) * g_beta_up[i, :] + c_up
                c_dn = -1 * (np.exp(+arg) - 1) * g_beta_dn[i, :] + c_dn
                b_up = g_beta_up[:, i] / (1. + c_up[i])
                b_dn = g_beta_dn[:, i] / (1. + c_dn[i])

                g_beta_up = g_beta_up - np.outer(b_up, c_up)
                g_beta_dn = g_beta_dn - np.outer(b_dn, c_dn)
                g_tmp_up, g_tmp_dn = self._compute_gf_tau(g_beta_up, g_beta_dn)

                # Update Configuration
                self.config.update(i, l)

            # Add temp greens function to total gf after each step
            gf_up += g_tmp_up
            gf_dn += g_tmp_dn
            number += 1
        # Return the normalized gfs for each spin
        return np.array([gf_up, gf_dn]) / number

    def run_lqmc(self):
        if self.det_mode:
            self.warmup_loop_det()
            gf = self.measure_loop_det()
        else:
            self.warmup_loop()
            gf = self.measure_loop()
        return gf

    def run(self):
        print("Warmup:     ", self.warm_sweeps)
        print("Measurement:", self.meas_sweeps)
        t0 = time.time()
        gf_tau = self.run_lqmc()
        t = time.time() - t0
        mins, secs = divmod(t, 60)
        print(f"\nTotal time: {int(mins):0>2}:{int(secs):0>2} min")
        print()
        return gf_tau
