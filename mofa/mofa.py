__all__ = ["Mofa"]

import numpy as np
import matplotlib.pyplot as pl
import time

from scipy.cluster.vq import kmeans
from scipy.linalg import inv
from matplotlib.patches import Ellipse


class Mofa(object):
    """
    Mixture of Factor Analyzers

    calling arguments:

    [ROSS DOCUMENT HERE]

    internal variables:

    `K`:           Number of components
    `M`:           Latent dimensionality
    `D`:           Data dimensionality
    `N`:           Number of data points
    `data`:        (N,D) array of observations
    `latents`:     (K,M,N) array of latent variables
    `latent_covs`: (K,M,M,N) array of latent covariances
    `lambdas`:     (K,M,D) array of loadings
    `psis`:        (K,D) array of diagonal variance values
    `rs`:          (K,N) array of responsibilities
    `amps`:        (K) array of component amplitudes

    """
    def __init__(self,data,K,M,
                 PPCA=False,lock_psis=False,
                 max_condition_number=1.e6,
                 init_kmeans_ppca=False):

        # required
        self.K     = K
        self.M     = M
        self.data  = np.atleast_2d(data)
        self.dataT = self.data.T # INSANE DATA DUPLICATION
        self.N     = self.data.shape[0]
        self.D     = self.data.shape[1]

        # options
        self.PPCA                 = PPCA
        self.lock_psis            = lock_psis
        self.max_condition_number = float(max_condition_number)

        # Empty arrays to be filled
        self.betas       = np.zeros((self.K,self.M,self.D))
        self.latents     = np.zeros((self.K,self.M,self.N))
        self.latent_covs = np.zeros((self.K,self.M,self.M,self.N))
        self.kmeans_rs   = np.zeros(self._data.shape[0], dtype=int)

        # Initialize
        self._initialize(init_kmeans_ppca)

    def _initialize(self,init_kmeans_ppca):

        if init_kmeans_ppca:
            pass
            #runkmeans
            #loop over compononets

        else:

            # Run K-means
            self.means = kmeans(self.data,self.K)[0]

            # Randomly assign factor loadings
            self.lambdas = np.random.randn(self.K,self.D,self.M) / \
                np.sqrt(self.max_condition_number)

            # Set (high rank) variance to variance of all data, along a dimension
            self.psis = np.tile(np.var(self.data,axis=0)[None,:],(self.K,1))

        # Set initial covs
        self.covs = np.zeros((self.K,self.D,self.D))
        self.inv_covs = 0. * self.covs
        self._update_covs()

        # Randomly assign the amplitudes.
        self.amps = np.random.rand(self.K)
        self.amps /= np.sum(self.amps)



    def run_kmeans(self, maxiter=200, tol=1e-4, verbose=True):
        """
        Run the K-means algorithm using the C extension.

        :param maxiter:
            The maximum number of iterations to try.

        :param tol:
            The tolerance on the relative change in the loss function that
            controls convergence.

        :param verbose:
            Print all the messages?

        """
        iterations = _algorithms.kmeans(self._data, self.means,
                self.kmeans_rs, tol, maxiter)

        if verbose:
            if iterations < maxiter:
                print("K-means converged after {0} iterations."
                        .format(iterations))
            else:
                print("K-means *didn't* converge after {0} iterations."
                        .format(iterations))


    def run_em(self, maxiter=400, tol=1e-4, verbose=True):
        """
        Run the EM algorithm.

        :param maxiter:
            The maximum number of iterations to try.

        :param tol:
            The tolerance on the relative change in the loss function that
            controls convergence.

        :param verbose:
            Print all the messages?

        """
        L = None
        for i in xrange(maxiter):
            self._E_step()
            newL = self.logLs.sum()
            if i == 0 and verbose:
                print("Initial NLL =", -newL)

            self._M_step_new()
            if L is None:
                L = newL
            else:
                dL = np.abs((newL - L) / L)
                assert dL > 0
                if i > 5 and dL < tol:
                    break
                L = newL

        if i < maxiter - 1:
            if verbose:
                print("EM converged after {0} iterations".format(i))
                print("Final NLL = {0}".format(-newL))
        else:
            print("Warning: EM didn't converge after {0} iterations"
                    .format(i))

    def take_EM_step(self):
        """
        Do one E step and then do one M step.  Duh!
        """
        self._E_step()
        self._M_step()

    def _E_step(self):
        """
        Expectation step.  See docs for details.
        """
        # resposibilities and likelihoods
        self.logLs, self.rs = self._calc_probs()

        for k in range(self.K):
            self.betas[k], self.latents[k], self.latent_covs[k] = \
                self._one_component_E_step(self.lambdas[k],self.inv_covs[k],
                                           self.dataT,self.means[k])



    def _one_component_E_step(self,lambdas,inv_cov,dataT,mean):
        """
        Calculate the E step for one component.
        """
        # beta
        beta = np.dot(lambdas.T,inv_cov)

        # latent values
        zeroed  = dataT - mean[:,None]
        latents = np.dot(beta,zeroed)

        # latent empirical covariance
        step1 = latents[:,None,:] * latents[None,:,:]
        step2 = np.dot(beta,lambdas)
        latent_cov = np.eye(self.M)[:,:,None] - step2[:,:,None] + step1

        return beta, latents, latent_cov

    def _one_component_M_step(self,k,rs,sumrs,dataT,
                              lambdas,latents,latent_cov,
                              PPCA):
        """
        Calculate the M step for one component.
        """
        # means
        lambdalatents = np.dot(lambdas, latents)
        means = np.sum(rs * (dataT - lambdalatents),
                        axis=1) / sumrs[k]

        # lambdas
        zeroed = dataT - means[:, None]
        lambdas = np.dot(np.dot(zeroed[:,None,:] * latents[None,:,:],rs),
                         inv(np.dot(self.latent_covs[k],rs)))

        # psis
        # hacking a floor for psis
        psis   = np.dot((zeroed - lambdalatents) * zeroed,rs) / sumrs[k]
        maxpsi = np.max(psis)
        maxlam = np.max(np.sum(lambdas * lambdas, axis=0))
        minpsi = np.max([maxpsi, maxlam]) / self.max_condition_number
        psis   = np.clip(psis, minpsi, np.Inf)
        if self.PPCA:
            psis = np.mean(psis) * np.ones(self.D)

        return means,lambdas,psis

    def _update_covs(self):
        """
        Update self.cov for responsibility, logL calc
        """
        for k in range(self.K):
            self.covs[k] = np.dot(self.lambdas[k],self.lambdas[k].T) + \
                np.diag(self.psis[k])
            self.inv_covs[k] = self._invert_cov(k)

    def _calc_probs(self):
        """
        Calculate log likelihoods, responsibilites for each datum
        under each component.
        """
        logrs = np.zeros((self.K, self.N))
        for k in range(self.K):
            logrs[k] = np.log(self.amps[k]) + self._log_multi_gauss(k, self.data)

        # here lies some ghetto log-sum-exp...
        # nothing like a little bit of overflow to make your day better!
        a = np.max(logrs, axis=0)
        L = a + np.log(np.sum(np.exp(logrs - a[None, :]), axis=0))
        logrs -= L[None, :]
        return L, np.exp(logrs)

    def _log_multi_gauss(self, k, X):
        """
        Gaussian log likelihood of the data for component k.
        """
        sgn, logdet = np.linalg.slogdet(self.covs[k])
        assert sgn > 0
        X1 = (X - self.means[k]).T
        X2 = np.dot(self.inv_covs[k], X1)
        p = -0.5 * np.sum(X1 * X2, axis=0)
        return -0.5 * np.log(2 * np.pi) * self.D - 0.5 * logdet + p

    def _invert_cov(self,k):
        """
        Calculate inverse covariance of mofa or ppca model,
        using inversion lemma
        """
        # probable slight speed up if psi kept as 1D array
        psiI = inv(np.diag(self.psis[k]))
        lam  = self.lambdas[k]
        lamT = lam.T
        step = inv(np.eye(self.M) + np.dot(lamT,np.dot(psiI,lam)))
        step = np.dot(step,np.dot(lamT,psiI))
        step = np.dot(psiI,np.dot(lam,step))

        return psiI - step

    def plot_2d_ellipses(self,d1,d2, **kwargs):
        """
        Make a 2D plot of the model projected onto axes
        d1 and d2.
        """
        for k in range(self.K):
            mean = self.means[k,(d1, d2)]
            cov = self.covs[k][((d1, d2),(d1, d2)), ((d1, d1), (d2, d2))]
            self._plot_2d_ellipse(mean, cov, **kwargs)

    def _plot_2d_ellipse(self, mu, cov, ax=None, **kwargs):
        """
        Plot the error ellipse at a point given it's covariance matrix.
        """
        # some sane defaults
        facecolor = kwargs.pop('facecolor', 'none')
        edgecolor = kwargs.pop('edgecolor', 'k')

        x, y = mu
        U, S, V = np.linalg.svd(cov)
        theta = np.degrees(np.arctan2(U[1, 0], U[0, 0]))
        ellipsePlot = Ellipse(xy=[x, y],
                              width=2 * np.sqrt(S[0]),
                              height=2 * np.sqrt(S[1]),
                              angle=theta,
                facecolor=facecolor, edgecolor=edgecolor, **kwargs)

        if ax is None:
            ax = pl.gca()
        ax.add_patch(ellipsePlot)

    def _M_step_new(self):
        """
        Maximization step.  See docs for details.

        This assumes that `_E_step()` has been run.
        """
        # MAGIC NUMBER CHECK THIS HACK!
        sumrs = np.clip(np.sum(self.rs,axis=1), 0.00, np.Inf)

        # maximize for each component
        for k in range(self.K):
            self.means[k],self.lambdas[k],self.psis[k] = \
                self._one_component_M_step(k,self.rs[k],sumrs,self.dataT,
                              self.lambdas[k],self.latents[k],self.latent_covs[k],
                              self.PPCA)
            self.amps[k] = sumrs[k] / self.N

        if self.lock_psis:
            psi = np.dot(sumrs, self.psis) / np.sum(sumrs)
            for k in range(self.K):
                self.psis[k] = psi

        self._update_covs()


    # BELOW HERE ARE DIAGONOSTIC VERSIONS OF M STEP, TO BE DELETED

    def _M_step_diagnose(self):
        """
        Maximization step.  See docs for details.

        This assumes that `_E_step()` has been run.
        """
        sumrs = np.clip(np.sum(self.rs,axis=1), 0.01, np.Inf) # MAGIC NUMBER CHECK THIS HACK!
        for k in range(self.K):
            lambdalatents = np.dot(self.lambdas[k], self.latents[k])
            meansk = np.sum(self.rs[k] * (self.dataT - lambdalatents),
                            axis=1) / sumrs[k]
            zeroed = self.dataT - self.means[k, :, None]
            lambdask = np.dot(np.dot(zeroed[:,None,:] * self.latents[k,None,:,:],
                                     self.rs[k]),
                              inv(np.dot(self.latent_covs[k],
                                         self.rs[k])))
            psisk = np.dot((zeroed - lambdalatents) * zeroed,
                           self.rs[k]) / sumrs[k]
            maxpsi = np.max(psisk)
            maxlam = np.max(np.sum(self.lambdas[k] * self.lambdas[k], axis=0))
            minpsik = np.max([maxpsi, maxlam]) / self.max_condition_number
            if np.any(psisk < 0.):
                print "HOLY CRAP; negatives in psi[%d]" % k, psisk
                self._diagnose(k)

            # only update means and lambdas *after* doing all calculations
            self.psis[k] = np.clip(psisk, minpsik, np.Inf)
            if self.PPCA:
                self.psis[k] = np.mean(self.psis[k]) * np.ones(self.D)
            self.lambdas[k] = lambdask
            self.means[k] = meansk
            self.amps[k] = sumrs[k] / self.N

        if self.lock_psis:
            psi = np.dot(sumrs, self.psis) / np.sum(sumrs)
            for k in range(self.K):
                self.psis[k] = psi

        self._update_covs()


    def _diagnose(self, k):
        """
        Find out why psis[k] has negative elements!
        """
        #print "diagnose psis[%d]: rs" % k, self.rs[k].min(), self.rs[k].max(), np.sum(self.rs[k])
        #print "diagnose psis[%d]: cov" % k, np.linalg.det(self.covs[k]), np.linalg.det(self.inv_covs[k])
        #print "diagnose psis[%d]: lambda" % k, self.lambdas[k]
        #print "diagnose psis[%d]: lambda" % k, np.sum(self.lambdas[k] * self.lambdas[k], axis=0)
        #print "diagnose psis[%d]: psi" % k, self.psis[k]
        return None







