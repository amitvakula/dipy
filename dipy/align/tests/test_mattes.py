import numpy as np
import scipy as sp
import scipy.ndimage as ndimage
from dipy.core.ndindex import ndindex
from dipy.data import get_data
import dipy.align.vector_fields as vf
from dipy.align.transforms import regtransforms
from functools import reduce
from operator import mul
from dipy.align.mattes import (MattesBase,
                               cubic_spline,
                               cubic_spline_derivative,
                               sample_domain_regular)
from numpy.testing import (assert_array_equal,
                           assert_array_almost_equal,
                           assert_almost_equal,
                           assert_equal,
                           assert_raises)

factors = {('TRANSLATION', 2): 2.0,
           ('ROTATION', 2): 0.1,
           ('RIGID', 2): 0.1,
           ('SCALING', 2): 0.01,
           ('AFFINE', 2): 0.1,
           ('TRANSLATION', 3): 2.0,
           ('ROTATION', 3): 0.1,
           ('RIGID', 3): 0.1,
           ('SCALING', 3): 0.1,
           ('AFFINE', 3): 0.1}


def create_random_image_pair(sh, nvals, seed):
    r""" Create a pair of images with an arbitrary, non-uniform joint PDF

    Parameters
    ----------
    sh : array, shape (dim,)
        the shape of the images to be created
    nvals : int
        maximum number of different values in the generated 2D images.
        The voxel intensities of the returned images will be in
        {0, 1, ..., nvals-1}

    Returns
    -------
    static : array, shape=sh
        first image in the image pair
    moving : array, shape=sh
        second image in the image pair
    """
    np.random.seed(seed)
    sz = reduce(mul, sh, 1)
    sh = tuple(sh)
    static = np.random.randint(0, nvals, sz).reshape(sh)

    # This is just a simple way of making  the distribution non-uniform
    moving = static.copy()
    moving += np.random.randint(0, nvals//2, sz).reshape(sh) - nvals//4

    # This is just a simple way of making  the distribution non-uniform
    static = moving.copy()
    static += np.random.randint(0, nvals//2, sz).reshape(sh) - nvals//4

    return static.astype(np.float64), moving.astype(np.float64)


def test_cubic_spline():
    # Cubic spline as defined in [Mattes03] eq. (3)
    #
    # [Mattes03] Mattes, D., Haynor, D. R., Vesselle, H., Lewellen, T. K.,
    #            & Eubank, W. PET-CT image registration in the chest using
    #            free-form deformations. IEEE Transactions on Medical Imaging,
    #            22(1), 120-8, 2003.
    in_list = []
    expected = []
    for epsilon in [-1e-9, 0.0, 1e-9]:
        for t in [-2.0, -1.0, 0.0, 1.0, 2.0]:
            x = t + epsilon
            in_list.append(x)
            absx = np.abs(x)
            sqrx = x * x
            if absx < 1:
                expected.append((4.0 - 6 * sqrx + 3.0 * (absx ** 3)) / 6.0)
            elif absx < 2:
                expected.append(((2 - absx) ** 3) / 6.0)
            else:
                expected.append(0.0)
    actual = cubic_spline(np.array(in_list, dtype=np.float64))
    assert_array_almost_equal(actual, np.array(expected, dtype=np.float64))


def test_cubic_spline_derivative():
    # Test derivative of the cubic spline, as defined in [Mattes03] eq. (3) by
    # comparing the analytical and numerical derivatives
    #
    # [Mattes03] Mattes, D., Haynor, D. R., Vesselle, H., Lewellen, T. K.,
    #            & Eubank, W. PET-CT image registration in the chest using
    #            free-form deformations. IEEE Transactions on Medical Imaging,
    #            22(1), 120-8, 2003.
    in_list = []
    expected = []
    for epsilon in [-1e-9, 0.0, 1e-9]:
        for t in [-2.0, -1.0, 0.0, 1.0, 2.0]:
            x = t + epsilon
            in_list.append(x)
    h = 1e-6
    in_list = np.array(in_list)
    input_h = in_list + h
    s = np.array(cubic_spline(in_list))
    s_h = np.array(cubic_spline(input_h))
    expected = (s_h - s) / h
    actual = cubic_spline_derivative(in_list)
    assert_array_almost_equal(actual, expected)


def test_mattes_base():
    # Test the simple functionality of MattesBase,
    # the gradients and computation of the joint intensity distribution
    # will be tested independently
    for nbins in [15, 30, 50]:
        for min_int in [-10.0, 0.0, 10.0]:
            for intensity_range in [0.1, 1.0, 10.0]:
                fact = 1
                max_int = min_int + intensity_range
                M = MattesBase(nbins)
                # Make a pair of 4-pixel images, introduce +/- 1 values
                # that will be excluded using a mask
                static = np.array([min_int - 1.0, min_int,
                                   max_int, max_int + 1.0])
                # Multiply by an arbitrary value (make the ranges different)
                moving = fact * np.array([min_int, min_int - 1.0,
                                          max_int + 1.0, max_int])
                # Create a mask to exclude the invalid values (beyond min and
                # max computed above)
                static_mask = np.array([0, 1, 1, 0])
                moving_mask = np.array([1, 0, 0, 1])

                M.setup(static, moving, static_mask, moving_mask)

                # Test bin_normalize_static at the boundary
                normalized = M.bin_normalize_static(min_int)
                assert_almost_equal(normalized, M.padding)
                index = M.bin_index(normalized)
                assert_equal(index, M.padding)
                normalized = M.bin_normalize_static(max_int)
                assert_almost_equal(normalized, nbins - M.padding)
                index = M.bin_index(normalized)
                assert_equal(index, nbins - 1 - M.padding)

                # Test bin_normalize_moving at the boundary
                normalized = M.bin_normalize_moving(fact * min_int)
                assert_almost_equal(normalized, M.padding)
                index = M.bin_index(normalized)
                assert_equal(index, M.padding)
                normalized = M.bin_normalize_moving(fact * max_int)
                assert_almost_equal(normalized, nbins - M.padding)
                index = M.bin_index(normalized)
                assert_equal(index, nbins - 1 - M.padding)

                # Test bin_index not at the boundary
                delta_s = (max_int - min_int) / (nbins - 2 * M.padding)
                delta_m = fact * (max_int - min_int) / (nbins - 2 * M.padding)
                for i in range(nbins - 2 * M.padding):
                    normalized = M.bin_normalize_static(min_int +
                                                        (i + 0.5) * delta_s)
                    index = M.bin_index(normalized)
                    assert_equal(index, M.padding + i)

                    normalized = M.bin_normalize_moving(fact * min_int +
                                                        (i + 0.5) * delta_m)
                    index = M.bin_index(normalized)
                    assert_equal(index, M.padding + i)


def test_mattes_densities():
    # Test the computation of the joint intensity distribution
    # using a dense and a sparse set of values
    seed = 1246592
    nbins = 32
    nr = 30
    nc = 35
    ns = 20
    nvals = 50

    for dim in [2, 3]:
        if dim == 2:
            shape = (nr, nc)
            static, moving = create_random_image_pair(shape, nvals, seed)
        else:
            shape = (ns, nr, nc)
            static, moving = create_random_image_pair(shape, nvals, seed)

        # Initialize
        mbase = MattesBase(nbins)
        mbase.setup(static, moving)
        # Get distributions computed by dense sampling
        mbase.update_pdfs_dense(static, moving)
        actual_joint_dense = mbase.joint
        actual_mmarginal_dense = mbase.mmarginal
        actual_smarginal_dense = mbase.smarginal

        # Get distributions computed by sparse sampling
        sval = static.reshape(-1)
        mval = moving.reshape(-1)
        mbase.update_pdfs_sparse(sval, mval)
        actual_joint_sparse = mbase.joint
        actual_mmarginal_sparse = mbase.mmarginal
        actual_smarginal_sparse = mbase.smarginal

        # Compute the expected joint distribution with dense sampling
        expected_joint_dense = np.zeros(shape=(nbins, nbins))
        for index in ndindex(shape):
            sv = mbase.bin_normalize_static(static[index])
            mv = mbase.bin_normalize_moving(moving[index])
            sbin = mbase.bin_index(sv)
            # The spline is centered at mv, will evaluate for all row
            spline_arg = np.array([i - mv for i in range(nbins)])
            contribution = cubic_spline(spline_arg)
            expected_joint_dense[sbin, :] += contribution

        # Compute the expected joint distribution with sparse sampling
        expected_joint_sparse = np.zeros(shape=(nbins, nbins))
        for index in range(sval.shape[0]):
            sv = mbase.bin_normalize_static(sval[index])
            mv = mbase.bin_normalize_moving(mval[index])
            sbin = mbase.bin_index(sv)
            # The spline is centered at mv, will evaluate for all row
            spline_arg = np.array([i - mv for i in range(nbins)])
            contribution = cubic_spline(spline_arg)
            expected_joint_sparse[sbin, :] += contribution

        # Verify joint distributions
        expected_joint_dense /= expected_joint_dense.sum()
        expected_joint_sparse /= expected_joint_sparse.sum()
        assert_array_almost_equal(actual_joint_dense, expected_joint_dense)
        assert_array_almost_equal(actual_joint_sparse, expected_joint_sparse)

        # Verify moving marginals
        expected_mmarginal_dense = expected_joint_dense.sum(0)
        expected_mmarginal_dense /= expected_mmarginal_dense.sum()
        expected_mmarginal_sparse = expected_joint_sparse.sum(0)
        expected_mmarginal_sparse /= expected_mmarginal_sparse.sum()
        assert_array_almost_equal(actual_mmarginal_dense,
                                  expected_mmarginal_dense)
        assert_array_almost_equal(actual_mmarginal_sparse,
                                  expected_mmarginal_sparse)

        # Verify static marginals
        expected_smarginal_dense = expected_joint_dense.sum(1)
        expected_smarginal_dense /= expected_smarginal_dense.sum()
        expected_smarginal_sparse = expected_joint_sparse.sum(1)
        expected_smarginal_sparse /= expected_smarginal_sparse.sum()
        assert_array_almost_equal(actual_smarginal_dense,
                                  expected_smarginal_dense)
        assert_array_almost_equal(actual_smarginal_sparse,
                                  expected_smarginal_sparse)


def setup_random_transform(transform, rfactor, nslices=45, sigma=1):
    r""" Creates a pair of images related to each other by an affine transform

    We warp the static image with a random transform so that the
    returned ground-truth transform will produce the static image
    when applied to the moving image. This will simply stack some copies of
    a T1 coronal slice image and add some zero slices up and down to
    reduce boundary artefacts when interpolating.

    Parameters
    ----------
    transform: instance of Transform
        defines the type of random transformation that will be created
    rfactor: float
        the factor to multiply the uniform(0,1) random noise that will be
        added to the identity parameters to create the random transform
    nslices: int
        number of slices to be stacked to form the volumes
    """
    dim = 2 if nslices == 1 else 3
    if transform.get_dim() != dim:
        raise ValueError("Transform and requested volume have different dims.")
    np.random.seed(3147702)
    zero_slices = nslices // 3

    fname = get_data('t1_coronal_slice')
    moving_slice = np.load(fname)
    moving_slice = moving_slice[40:180, 50:210]

    if nslices == 1:
        dim = 2
        moving = moving_slice
        warp_method = vf.warp_2d_affine
    else:
        dim = 3
        warp_method = vf.warp_3d_affine
        moving = np.zeros(shape=moving_slice.shape + (nslices,))
        moving[..., zero_slices:(2 * zero_slices)] = moving_slice[..., None]

    moving = sp.ndimage.filters.gaussian_filter(moving, sigma)

    moving_g2w = np.eye(dim + 1)
    mmask = np.ones_like(moving, dtype=np.int32)

    # Create a transform by slightly perturbing the identity parameters
    theta = transform.get_identity_parameters()
    n = transform.get_number_of_parameters()
    theta += np.random.rand(n) * rfactor

    M = transform.param_to_matrix(theta)
    shape = np.array(moving.shape, dtype=np.int32)
    static = np.array(warp_method(moving.astype(np.float32), shape, M))
    static = static.astype(np.float64)
    static_g2w = np.eye(dim + 1)
    smask = np.ones_like(static, dtype=np.int32)

    return static, moving, static_g2w, moving_g2w, smask, mmask, M


def test_joint_pdf_gradients_dense():
    # Compare the analytical and numerical (finite differences) gradient of the
    # joint distribution (i.e. derivatives of each histogram cell) w.r.t. the
    # transform parameters. Since the histograms are discrete partitions of the
    # image intensities, the finite difference approximation is normally not
    # very close to the analytical derivatives. Other sources of error are the
    # interpolation used when warping the images and the boundary intensities
    # introduced when interpolating outside of the image (i.e. some "zeros" are
    # introduced at the boundary which affect the numerical derivatives but is
    # not taken into account by the analytical derivatives). Thus, we need to
    # relax the verification. Instead of looking for the analytical and
    # numerical gradients to be very close to each other, we will verify that
    # they approximately point in the same direction by testing if the angle
    # they form is close to zero.
    h = 1e-4
    for ttype in factors:
        dim = ttype[1]
        if dim == 2:
            nslices = 1
            warp_method = vf.warp_2d_affine
        else:
            nslices = 45
            warp_method = vf.warp_3d_affine

        transform = regtransforms[ttype]
        factor = factors[ttype]
        theta = transform.get_identity_parameters()

        static, moving, static_g2w, moving_g2w, smask, mmask, M = \
            setup_random_transform(transform, factor, nslices, 5.0)
        metric = MattesBase(32)
        metric.setup(static, moving, smask, mmask)

        # Compute the gradient at theta with the implementation under test
        M = transform.param_to_matrix(theta)
        shape = np.array(static.shape, dtype=np.int32)

        warped = warp_method(moving.astype(np.float32), shape, M)
        warped = np.array(warped)
        metric.update_pdfs_dense(static.astype(np.float64),
                                 warped.astype(np.float64))
        # Get the joint distribution evaluated at theta
        J0 = np.copy(metric.joint)
        grid_to_space = np.eye(dim + 1)
        spacing = np.ones(dim, dtype=np.float64)
        mgrad, inside = vf.gradient(moving.astype(np.float32), moving_g2w,
                                    spacing, shape, grid_to_space)
        id = transform.get_identity_parameters()
        metric.update_gradient_dense(id, transform, static.astype(np.float64),
                                     warped.astype(np.float64), grid_to_space,
                                     mgrad, smask, mmask)
        actual = np.copy(metric.joint_grad)
        # Now we have the gradient of the joint distribution w.r.t. the
        # transform parameters

        # Compute the gradient using finite-diferences
        n = transform.get_number_of_parameters()
        expected = np.empty_like(actual)
        for i in range(n):
            dtheta = theta.copy()
            dtheta[i] += h
            # Update the joint distribution with the warped moving image
            M = transform.param_to_matrix(dtheta)
            shape = np.array(static.shape, dtype=np.int32)
            warped = warp_method(moving.astype(np.float32), shape, M)
            warped = np.array(warped)
            metric.update_pdfs_dense(static.astype(np.float64),
                                     warped.astype(np.float64))
            J1 = np.copy(metric.joint)
            expected[..., i] = (J1 - J0) / h

        # Dot product and norms of gradients of each joint histogram cell
        # i.e. the derivatives of each cell w.r.t. all parameters
        P = (expected * actual).sum(2)
        enorms = np.sqrt((expected ** 2).sum(2))
        anorms = np.sqrt((actual ** 2).sum(2))
        prodnorms = enorms * anorms
        # Cosine of angle between the expected and actual gradients.
        # Exclude very small gradients
        P[prodnorms > 1e-6] /= (prodnorms[prodnorms > 1e-6])
        P[prodnorms <= 1e-6] = 0
        # Verify that a large proportion of the gradients point almost in
        # the same direction. Disregard very small gradients
        mean_cosine = P[P != 0].mean()
        std_cosine = P[P != 0].std()
        assert(mean_cosine > 0.9)
        assert(std_cosine < 0.25)


def test_joint_pdf_gradients_sparse():
    h = 1e-4
    for ttype in factors:
        dim = ttype[1]
        if dim == 2:
            nslices = 1
            interp_method = vf.interpolate_scalar_2d
        else:
            nslices = 45
            interp_method = vf.interpolate_scalar_3d

        transform = regtransforms[ttype]
        factor = factors[ttype]
        theta = transform.get_identity_parameters()

        static, moving, static_g2w, moving_g2w, smask, mmask, M = \
            setup_random_transform(transform, factor, nslices, 5.0)
        metric = MattesBase(32)
        metric.setup(static, moving, smask, mmask)

        # Sample the fixed-image domain
        k = 3
        sigma = 0.25
        seed = 1234
        shape = np.array(static.shape, dtype=np.int32)
        samples = sample_domain_regular(k, shape, static_g2w, sigma, seed)
        samples = np.array(samples)
        samples = np.hstack((samples, np.ones(samples.shape[0])[:, None]))
        sp_to_static = np.linalg.inv(static_g2w)
        samples_static_grid = (sp_to_static.dot(samples.T).T)[..., :dim]
        intensities_static, inside = interp_method(static.astype(np.float32),
                                                   samples_static_grid)
        # The routines in vector_fields operate, mostly, with float32 because
        # they were thought to be used for non-linear registration. We may need
        # to write some float64 counterparts for affine registration, where
        # memory is not so big issue
        intensities_static = np.array(intensities_static, dtype=np.float64)

        # Compute the gradient at theta with the implementation under test
        M = transform.param_to_matrix(theta)
        sp_to_moving = np.linalg.inv(moving_g2w).dot(M)
        samples_moving_grid = (sp_to_moving.dot(samples.T).T)[..., :dim]
        intensities_moving, inside = interp_method(moving.astype(np.float32),
                                                   samples_moving_grid)
        intensities_moving = np.array(intensities_moving, dtype=np.float64)
        metric.update_pdfs_sparse(intensities_static, intensities_moving)
        # Get the joint distribution evaluated at theta
        J0 = np.copy(metric.joint)

        spacing = np.ones(dim + 1, dtype=np.float64)
        mgrad, inside = vf.sparse_gradient(moving.astype(np.float32),
                                           sp_to_moving, spacing, samples)
        metric.update_gradient_sparse(theta, transform, intensities_static,
                                      intensities_moving, samples[..., :dim],
                                      mgrad)
        # Get the gradient of the joint distribution w.r.t. the transform
        # parameters
        actual = np.copy(metric.joint_grad)

        # Compute the gradient using finite-diferences
        n = transform.get_number_of_parameters()
        expected = np.empty_like(actual)
        for i in range(n):
            dtheta = theta.copy()
            dtheta[i] += h
            # Update the joint distribution with the warped moving image
            M = transform.param_to_matrix(dtheta)
            sp_to_moving = np.linalg.inv(moving_g2w).dot(M)
            samples_moving_grid = sp_to_moving.dot(samples.T).T
            intensities_moving, inside = \
                interp_method(moving.astype(np.float32), samples_moving_grid)
            intensities_moving = np.array(intensities_moving, dtype=np.float64)
            metric.update_pdfs_sparse(intensities_static, intensities_moving)
            J1 = np.copy(metric.joint)
            expected[..., i] = (J1 - J0) / h

        # Dot product and norms of gradients of each joint histogram cell
        # i.e. the derivatives of each cell w.r.t. all parameters
        P = (expected * actual).sum(2)
        enorms = np.sqrt((expected ** 2).sum(2))
        anorms = np.sqrt((actual ** 2).sum(2))
        prodnorms = enorms*anorms
        # Cosine of angle between the expected and actual gradients.
        # Exclude very small gradients
        P[prodnorms > 1e-6] /= (prodnorms[prodnorms > 1e-6])
        P[prodnorms <= 1e-6] = 0
        # Verify that a large proportion of the gradients point almost in
        # the same direction. Disregard very small gradients
        mean_cosine = P[P != 0].mean()
        std_cosine = P[P != 0].std()
        assert(mean_cosine > 0.99)
        assert(std_cosine < 0.15)


def test_mi_gradient_dense():
    # Test the gradient of mutual information
    h = 1e-5
    for ttype in factors:
        transform = regtransforms[ttype]
        dim = ttype[1]
        if dim == 2:
            nslices = 1
            warp_method = vf.warp_2d_affine
        else:
            nslices = 45
            warp_method = vf.warp_3d_affine
        # Get data (pair of images related to each other by an known transform)
        factor = factors[ttype]
        static, moving, static_g2w, moving_g2w, smask, mmask, M = \
            setup_random_transform(transform, factor, nslices, 5.0)
        smask = None
        mmask = None

        # Prepare a MattesBase instance
        # The computation of the metric is done in 3 steps:
        # 1.Compute the joint distribution
        # 2.Compute the gradient of the joint distribution
        # 3.Compute the metric's value and gradient using results from 1 and 2
        metric = MattesBase(32)
        metric.setup(static, moving, smask, mmask)

        # 1. Update the joint distribution
        metric.update_pdfs_dense(static.astype(np.float64),
                                 moving.astype(np.float64))

        # 2. Update the joint distribution gradient (the derivative of each
        # histogram cell w.r.t. the transform parameters). This requires
        # among other things, the spatial gradient of the moving image.
        theta = transform.get_identity_parameters().copy()
        grid_to_space = np.eye(dim + 1)
        spacing = np.ones(dim, dtype=np.float64)
        shape = np.array(static.shape, dtype=np.int32)
        mgrad, inside = vf.gradient(moving.astype(np.float32), moving_g2w,
                                    spacing, shape, grid_to_space)
        metric.update_gradient_dense(theta, transform,
                                     static.astype(np.float64),
                                     moving.astype(np.float64),
                                     grid_to_space, mgrad, smask, mmask)

        # 3. Update the metric (in this case, the Mutual Information) and its
        # gradient, which is computed from the joint density and its gradient
        metric.update_mi_metric(update_gradient=True)

        # Now we can extract the value and gradient of the metric
        # This is the gradient according to the implementation under test
        val0 = metric.metric_val
        actual = np.copy(metric.metric_grad)

        # Compute the gradient using finite-diferences
        n = transform.get_number_of_parameters()
        expected = np.empty_like(actual)
        for i in range(n):
            dtheta = theta.copy()
            dtheta[i] += h

            M = transform.param_to_matrix(dtheta)
            shape = np.array(static.shape, dtype=np.int32)
            warped = np.array(warp_method(moving.astype(np.float32), shape, M))
            metric.update_pdfs_dense(static.astype(np.float64),
                                     warped.astype(np.float64))
            metric.update_mi_metric(update_gradient=False)
            val1 = metric.metric_val
            expected[i] = (val1 - val0) / h

        dp = expected.dot(actual)
        enorm = np.linalg.norm(expected)
        anorm = np.linalg.norm(actual)
        nprod = dp / (enorm * anorm)
        assert(nprod >= 0.999)


def test_mi_gradient_sparse():
    # Test the gradient of mutual information
    h = 1e-5
    for ttype in factors:
        transform = regtransforms[ttype]
        dim = ttype[1]
        if dim == 2:
            nslices = 1
            interp_method = vf.interpolate_scalar_2d
        else:
            nslices = 45
            interp_method = vf.interpolate_scalar_3d
        # Get data (pair of images related to each other by an known transform)
        factor = factors[ttype]
        static, moving, static_g2w, moving_g2w, smask, mmask, M = \
            setup_random_transform(transform, factor, nslices, 5.0)
        smask = None
        mmask = None

        # Sample static domain
        k = 3
        sigma = 0.25
        seed = 1234
        shape = np.array(static.shape, dtype=np.int32)
        samples = sample_domain_regular(k, shape, static_g2w, sigma, seed)
        samples = np.array(samples)
        samples = np.hstack((samples, np.ones(samples.shape[0])[:, None]))
        sp_to_static = np.linalg.inv(static_g2w)
        samples_static_grid = (sp_to_static.dot(samples.T).T)[..., :dim]
        intensities_static, inside = interp_method(static.astype(np.float32),
                                                   samples_static_grid)
        intensities_static = np.array(intensities_static, dtype=np.float64)

        # Prepare a MattesBase instance
        # The computation of the metric is done in 3 steps:
        # 1.Compute the joint distribution
        # 2.Compute the gradient of the joint distribution
        # 3.Compute the metric's value and gradient using results from 1 and 2
        metric = MattesBase(32)
        metric.setup(static, moving, smask, mmask)

        # 1. Update the joint distribution
        sp_to_moving = np.linalg.inv(moving_g2w)
        samples_moving_grid = (sp_to_moving.dot(samples.T).T)[..., :dim]
        intensities_moving, inside = interp_method(moving.astype(np.float32),
                                                   samples_moving_grid)
        intensities_moving = np.array(intensities_moving, dtype=np.float64)
        metric.update_pdfs_sparse(intensities_static, intensities_moving)

        # 2. Update the joint distribution gradient (the derivative of each
        # histogram cell w.r.t. the transform parameters). This requires
        # to evaluate the gradient of the moving image at the sampling points
        theta = transform.get_identity_parameters().copy()
        spacing = np.ones(dim, dtype=np.float64)
        shape = np.array(static.shape, dtype=np.int32)
        mgrad, inside = vf.sparse_gradient(moving.astype(np.float32),
                                           sp_to_moving,
                                           spacing,
                                           samples[..., :dim])
        metric.update_gradient_sparse(theta, transform, intensities_static,
                                      intensities_moving,
                                      samples[..., :dim],
                                      mgrad)

        # 3. Update the metric (in this case, the Mutual Information) and its
        # gradient, which is computed from the joint density and its gradient
        metric.update_mi_metric(update_gradient=True)

        # Now we can extract the value and gradient of the metric
        # This is the gradient according to the implementation under test
        val0 = metric.metric_val
        actual = np.copy(metric.metric_grad)

        # Compute the gradient using finite-diferences
        n = transform.get_number_of_parameters()
        expected = np.empty_like(actual)
        for i in range(n):
            dtheta = theta.copy()
            dtheta[i] += h

            M = transform.param_to_matrix(dtheta)
            shape = np.array(static.shape, dtype=np.int32)
            sp_to_moving = np.linalg.inv(moving_g2w).dot(M)
            samples_moving_grid = (sp_to_moving.dot(samples.T).T)[..., :dim]
            intensities_moving, inside =\
                interp_method(moving.astype(np.float32), samples_moving_grid)
            intensities_moving = np.array(intensities_moving, dtype=np.float64)
            metric.update_pdfs_sparse(intensities_static, intensities_moving)
            metric.update_mi_metric(update_gradient=False)
            val1 = metric.metric_val
            expected[i] = (val1 - val0) / h

        dp = expected.dot(actual)
        enorm = np.linalg.norm(expected)
        anorm = np.linalg.norm(actual)
        nprod = dp / (enorm * anorm)
        assert(nprod > 0.9999)


def test_sample_domain_regular():
    # Test 2D sampling
    shape = np.array((10, 10), dtype=np.int32)
    affine = np.eye(3)
    invalid_affine = np.eye(2)
    sigma = 0
    dim = len(shape)
    n = shape[0] * shape[1]
    k = 2
    # Verify exception is raised with invalid affine
    assert_raises(ValueError, sample_domain_regular, k, shape,
                  invalid_affine, sigma)
    samples = sample_domain_regular(k, shape, affine, sigma)
    isamples = np.array(samples, dtype=np.int32)
    indices = (isamples[:, 0] * shape[1] + isamples[:, 1])
    # Verify correct number of points sampled
    assert_array_equal(samples.shape, [n // k, dim])
    # Verify all sampled points are different
    assert_equal(len(set(indices)), len(indices))
    # Verify the sampling was regular at rate k
    assert_equal((indices % k).sum(), 0)

    # Test 3D sampling
    shape = np.array((5, 10, 10), dtype=np.int32)
    affine = np.eye(4)
    invalid_affine = np.eye(3)
    sigma = 0
    dim = len(shape)
    n = shape[0] * shape[1] * shape[2]
    k = 10
    # Verify exception is raised with invalid affine
    assert_raises(ValueError, sample_domain_regular, k, shape,
                  invalid_affine, sigma)
    samples = sample_domain_regular(k, shape, affine, sigma)
    isamples = np.array(samples, dtype=np.int32)
    indices = (isamples[:, 0] * shape[1] * shape[2] +
               isamples[:, 1] * shape[2] +
               isamples[:, 2])
    # Verify correct number of points sampled
    assert_array_equal(samples.shape, [n // k, dim])
    # Verify all sampled points are different
    assert_equal(len(set(indices)), len(indices))
    # Verify the sampling was regular at rate k
    assert_equal((indices % k).sum(), 0)
