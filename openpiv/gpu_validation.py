"""This module is for GPU-accelerated validation algorithms."""

from math import ceil, prod, log10

import numpy as np
# Create the PyCUDA context.
import pycuda.autoinit
import pycuda.gpuarray as gpuarray
# import pycuda.cumath as cumath
from pycuda.compiler import SourceModule

from openpiv.gpu_misc import _check_arrays, gpu_mask

# Define 32-bit types
DTYPE_i = np.int32
DTYPE_f = np.float32
DTYPE_c = np.complex64

ALLOWED_VALIDATION_METHODS = {'s2n', 'median_velocity', 'mean_velocity', 'rms_velocity'}
S2N_TOL = 2
MEDIAN_TOL = 2
MEAN_TOL = 2
RMS_TOL = 2
_BLOCK_SIZE = 64


def gpu_validation(*f_d, sig2noise_d=None, mask_d=None, validation_method='median_velocity',
                   s2n_tol=S2N_TOL, median_tol=MEDIAN_TOL, mean_tol=MEAN_TOL, rms_tol=RMS_TOL):
    """Validation for vector-fields that returns an array indicating which location need to be validated.

    Parameters
    ----------
    f_d : GPUArray
        2D float (m, n), velocity fields to be validated.
    sig2noise_d : GPUArray, optional
        2D float (m, n), signal-to-noise ratio of each velocity.
    mask_d : GPUArray
        2D int (m, n), mask for the velocity field.
    validation_method : str {'s2n', 'median_velocity', 'mean_velocity', 'rms_velocity'}, optional
        Method(s) to use for validation.
    s2n_tol : float, optional
        Minimum value for sig2noise validation.
    median_tol : float, optional
        Tolerance for median velocity validation.
    mean_tol : float, optional
        Tolerance for mean velocity validation.
    rms_tol : float, optional
        Tolerance for rms validation.

    Returns
    -------
    val_locations : GPUArray
        2D int (m, n), array of indices that need to be validated. 1s indicate locations of invalid vectors.

    """
    validation_gpu = ValidationGPU(f_d[0], mask_d, validation_method, s2n_tol, median_tol, mean_tol, rms_tol)

    return validation_gpu(*f_d, sig2noise_d=sig2noise_d)


class ValidationGPU:
    """Validation for vector-fields that returns an array indicating which location need to be validated.

    Parameters
    ----------
    f_shape : GPUArray or tuple
        (ht, wd) of the fields to be validated.
    mask_d : GPUArray
        2D float, mask for the velocity field.
    validation_method : str {'s2n', 'median_velocity', 'mean_velocity', 'rms_velocity'}, optional
        Method(s) to use for validation.
    s2n_tol : float, optional
        Minimum value for sig2noise.
    median_tol : float, optional
        Tolerance for median velocity validation.
    mean_tol : float, optional
        Tolerance for mean velocity validation.
    rms_tol : float, optional
        Tolerance for rms validation.

    Methods
    -------
    __call__(*f_d)
        Returns an array indicating which indices need to be validated.

    """
    def __init__(self, f_shape, mask_d=None, validation_method='median_velocity', s2n_tol=S2N_TOL,
                 median_tol=MEDIAN_TOL, mean_tol=MEAN_TOL, rms_tol=RMS_TOL):
        self.f_shape = f_shape.shape if hasattr(f_shape, 'shape') else tuple(f_shape)
        if mask_d is not None:
            _check_arrays(mask_d, array_type=gpuarray.GPUArray, dtype=DTYPE_i, shape=self.f_shape)
        self.mask_d = mask_d
        self.validation_method = validation_method if not isinstance(validation_method, str) else (validation_method,)
        self.validation_tols = {'s2n': s2n_tol, 'median': median_tol, 'mean': mean_tol, 'rms': rms_tol}

        self._val_locations_d = None
        self._f_d = None
        self._f_neighbours_d = None
        self._f_median_d = None
        self._f_mean_d = None

        self._check_validation_methods()
        self._check_validation_tolerances()

        # Compute the median velocities to be returned.
        self._neighbours_present_d = _gpu_find_neighbours(self.f_shape, mask_d)

    def __call__(self, *f_d, sig2noise_d=None):
        """Returns an array indicating which indices need to be validated.

        Parameters
        ----------
        f_d : GPUArray
            2D float (m, n), velocity fields to be validated.
        sig2noise_d : GPUArray, optional
            2D float (m, n), signal-to-noise ratio of each velocity.

        Returns
        -------
        val_locations : GPUArray
            2D int (m, n), array of indices that need to be validated. 1s indicate locations of invalid vectors.

        """
        self.free_data()
        _check_arrays(*f_d, array_type=gpuarray.GPUArray, dtype=DTYPE_f, shape=self.f_shape)
        self._num_fields = len(f_d)
        self._f_d = f_d

        # Do the validations.
        if 's2n' in self.validation_method:
            self._s2n_validation(sig2noise_d)
        if 'median_velocity' in self.validation_method:
            self._median_validation()
        if 'mean_velocity' in self.validation_method:
            self._mean_validation()
        if 'rms_velocity' in self.validation_method:
            self._rms_validation()

        # Apply the mask to the final result.
        self._mask_val_locations()

        return self._val_locations_d

    def free_data(self):
        self._val_locations_d = None
        self._f_d = None
        self._f_neighbours_d = None
        self._f_median_d = None
        self._f_mean_d = None

    @property
    def median_d(self):
        f_median_d = self._get_median()
        if len(f_median_d) == 1:
            f_median_d = f_median_d[0]

        return f_median_d

    @property
    def mean_d(self):
        f_mean_d = self._get_mean()
        if len(f_mean_d) == 1:
            f_mean_d = f_mean_d[0]

        return f_mean_d

    def _s2n_validation(self, sig2noise_d):
        """Performs signal-to-noise validation on each field."""
        assert sig2noise_d is not None, 'signal-to-noise validation requires sig2noise_d to be passed.'
        _check_arrays(sig2noise_d, array_type=gpuarray.GPUArray, dtype=DTYPE_f, size=prod(self.f_shape))
        s2n_tol = log10(self.validation_tols['s2n'])

        sig2noise_tol_d = sig2noise_d / DTYPE_f(s2n_tol)
        self._val_locations_d = _local_validation(sig2noise_tol_d, 1, self._val_locations_d)

    def _median_validation(self):
        """Performs median validation on each field."""
        f_neighbours_d = self._get_neighbours()
        f_median_d = self._get_median()
        median_tol = self.validation_tols['median']

        for k in range(self._num_fields):
            f_median_fluc_d = _gpu_median_fluc(f_median_d[k], f_neighbours_d[k], self._neighbours_present_d)
            self._val_locations_d = _neighbour_validation(self._f_d[k], f_median_d[k], f_median_fluc_d,
                                                          median_tol, self._val_locations_d)

    def _mean_validation(self):
        """Performs mean validation on each field."""
        f_neighbours_d = self._get_neighbours()
        f_mean_d = self._get_mean()
        mean_tol = self.validation_tols['mean']

        for k in range(self._num_fields):
            f_mean_fluc_d = _gpu_mean_fluc(f_mean_d[k], f_neighbours_d[k], self._neighbours_present_d)
            self._val_locations_d = _neighbour_validation(self._f_d[k], f_mean_d[k], f_mean_fluc_d, mean_tol,
                                                          self._val_locations_d)

    def _rms_validation(self):
        """Performs RMS validation on each field."""
        f_neighbours_d = self._get_neighbours()
        f_mean_d = self._get_mean()
        rms_tol = self.validation_tols['rms']

        for k in range(self._num_fields):
            f_rms_d = _gpu_rms(f_mean_d[k], f_neighbours_d[k], self._neighbours_present_d)
            self._val_locations_d = _neighbour_validation(self._f_d[k], f_mean_d[k], f_rms_d, rms_tol,
                                                          self._val_locations_d)

    def _mask_val_locations(self):
        """Removes masked locations from the validation locations."""
        if self.mask_d is not None and self._val_locations_d is not None:
            self._val_locations_d = gpu_mask(self._val_locations_d, self.mask_d)

    def _get_neighbours(self):
        """Returns neighbouring values for each field."""
        if self._f_neighbours_d is None:
            self._f_neighbours_d = [_gpu_get_neighbours(f_d, self._neighbours_present_d) for f_d in self._f_d]

        return self._f_neighbours_d

    def _get_median(self):
        """Returns field containing median of surrounding points for each field."""
        f_neighbours_d = self._get_neighbours()

        if self._f_median_d is None:
            self._f_median_d = [_gpu_median_velocity(f_neighbours_d[k], self._neighbours_present_d)
                                for k in range(self._num_fields)]

        return self._f_median_d

    def _get_mean(self):
        """Returns field containing mean of surrounding points for each field."""
        f_neighbours_d = self._get_neighbours()

        if self._f_mean_d is None:
            self._f_mean_d = [_gpu_mean_velocity(f_neighbours_d[k], self._neighbours_present_d)
                              for k in range(self._num_fields)]

        return self._f_mean_d

    def _check_validation_methods(self):
        """Checks that input validation methods are allowed."""
        if not all([val_method in ALLOWED_VALIDATION_METHODS for val_method in self.validation_method]):
            raise ValueError(
                'Invalid validation method(s). Allowed validation methods are: {}'.format(ALLOWED_VALIDATION_METHODS))

    def _check_validation_tolerances(self):
        """Checks that input validation methods are allowed."""
        if not all([val_tol > 0 for val_tol in self.validation_tols.values()]):
            raise ValueError('Invalid validation tolerances(s). Validation tolerances must be greater than 0.')


mod_validation = SourceModule("""
__global__ void local_validation(int *val_locations, float *f, float tol, int size)
{
    int t_idx = blockIdx.x * blockDim.x + threadIdx.x;
    if (t_idx >= size) {return;}

    val_locations[t_idx] = val_locations[t_idx] || (f[t_idx] > tol);
}

__global__ void neighbour_validation(int *val_locations, float *f, float *f_mean, float *f_fluc, float tol, int size)
{
    int t_idx = blockIdx.x * blockDim.x + threadIdx.x;
    if (t_idx >= size) {return;}

    // a small number is added to prevent singularities in uniform flow (Scarano & Westerweel, 2005)
    val_locations[t_idx] = val_locations[t_idx] || (fabsf(f[t_idx] - f_mean[t_idx]) / (f_fluc[t_idx] + 0.1f) > tol);
}
""")


def _local_validation(f_d, tol, val_locations_d=None):
    """Updates the validation list by checking if the array elements exceed the tolerance."""
    size = f_d.size

    if val_locations_d is None:
        val_locations_d = gpuarray.zeros_like(f_d, dtype=DTYPE_i)

    block_size = _BLOCK_SIZE
    grid_size = ceil(size / block_size)
    local_validation = mod_validation.get_function('local_validation')
    local_validation(val_locations_d, f_d, DTYPE_f(tol), DTYPE_i(size), block=(block_size, 1, 1), grid=(grid_size, 1))

    return val_locations_d


def _neighbour_validation(f_d, f_mean_d, f_mean_fluc_d, tol, val_locations_d=None):
    """Updates the validation list by checking if the neighbouring elements exceed the tolerance."""
    size = f_d.size

    if val_locations_d is None:
        val_locations_d = gpuarray.zeros_like(f_d, dtype=DTYPE_i)

    block_size = _BLOCK_SIZE
    grid_size = ceil(size / block_size)
    neighbour_validation = mod_validation.get_function('neighbour_validation')
    neighbour_validation(val_locations_d, f_d, f_mean_d, f_mean_fluc_d, DTYPE_f(tol), DTYPE_i(size),
                         block=(block_size, 1, 1), grid=(grid_size, 1))

    return val_locations_d


mod_neighbours = SourceModule("""
__global__ void find_neighbours(int *np, int *mask, int n, int m, int size)
{
    // np : neighbours_present
    int t_idx = blockIdx.x * blockDim.x + threadIdx.x;
    if (t_idx >= size) {return;}

    int nb_idx = t_idx % 8;
    int row_idx = t_idx / 8 / n - (nb_idx < 3) + (nb_idx > 4); 
    int col_idx = t_idx / 8 % n - ((nb_idx == 0) || (nb_idx == 3) || (nb_idx == 5))
                                + ((nb_idx == 2) || (nb_idx == 4) || (nb_idx == 7));
    int in_bound = (row_idx >= 0) * (row_idx < m) * (col_idx >= 0) * (col_idx < n);

    np[t_idx] = in_bound * (!mask[(row_idx * n + col_idx) * in_bound]);
}

__global__ void get_neighbours(float *nb, int *np, float *f, int n, int size)
{
    // nb : values of the neighbouring points
    // np : 1 if there is a neighbour, 0 if no neighbour
    int t_idx = blockIdx.x * blockDim.x + threadIdx.x;
    if (t_idx >= size) {return;}
    
    int nb_idx = t_idx % 8;
    int row_idx = t_idx / 8 / n - (nb_idx < 3) + (nb_idx > 4); 
    int col_idx = t_idx / 8 % n - ((nb_idx == 0) || (nb_idx == 3) || (nb_idx == 5))
                                + ((nb_idx == 2) || (nb_idx == 4) || (nb_idx == 7));

    // get neighbouring values
    nb[t_idx] = f[(row_idx * n + col_idx) * np[t_idx]] * np[t_idx];
}
""")


def _gpu_find_neighbours(shape, mask_d=None):
    """An array that stores if a point has neighbours in a 3x3 grid surrounding it.

    Parameters
    ----------
    shape : tuple
        Int (m, n), shape of the array to find neighbours for.
    mask_d : GPUArray
        2D int (m, n), value of one where masked.

    Returns
    -------
    GPUArray
        4D (m, n, 8), value of one where the point in the field has neighbours.

    """
    m, n = shape
    size = m * n * 8
    if mask_d is None:
        mask_d = gpuarray.zeros((m, n), dtype=DTYPE_i)

    neighbours_present_d = gpuarray.empty((m, n, 8), dtype=DTYPE_i)

    block_size = _BLOCK_SIZE
    grid_size = ceil(size / block_size)
    find_neighbours = mod_neighbours.get_function('find_neighbours')
    find_neighbours(neighbours_present_d, mask_d, DTYPE_i(n), DTYPE_i(m), DTYPE_i(size), block=(block_size, 1, 1),
                    grid=(grid_size, 1))

    return neighbours_present_d


def _gpu_get_neighbours(f_d, neighbours_present_d):
    """An array that stores the values of the velocity of the surrounding neighbours.

    Parameters
    ----------
    f_d : GPUArray
        2D float (m, n), values from which to get neighbours.
    neighbours_present_d : GPUArray
        4D int (m, n, 8), value of one where a neighbour is present.

    Returns
    -------
    GPUArray
        4D float (m, n, 8), values of u and v of the neighbours of a point.

    """
    m, n = f_d.shape
    size = f_d.size * 8

    neighbours_d = gpuarray.empty((m, n, 8), dtype=DTYPE_f)

    block_size = _BLOCK_SIZE
    grid_size = ceil(size / block_size)
    get_neighbours = mod_neighbours.get_function('get_neighbours')
    get_neighbours(neighbours_d, neighbours_present_d, f_d, DTYPE_i(n), DTYPE_i(size), block=(block_size, 1, 1),
                   grid=(grid_size, 1))

    return neighbours_d


mod_median_velocity = SourceModule("""
// device-side function to swap elements of two arrays.
__device__ void swap(float *A, int a, int b)
{
    float tmp_A = A[a];
    A[a] = A[b];
    A[b] = tmp_A;
}

// device-side function to compare and swap elements of two arrays.
__device__ void compare(float *A, float *B, int a, int b)
{
    // Move non-neighbour values to end.
    if (B[a] < B[b])
    {
        swap(A, a, b);
        swap(B, a, b);
    }
    // Move greater values to right.
    else if (A[a] > A[b] && B[a] == B[b] == 1)
    {
        swap(A, a, b);
        swap(B, a, b);
    }
}

// device-side function to do an 8-wire sorting network.
__device__ void sort(float *A, float *B)
{
    compare(A, B, 0, 1);
    compare(A, B, 2, 3);
    compare(A, B, 4, 5);
    compare(A, B, 6, 7);
    compare(A, B, 0, 2);
    compare(A, B, 1, 3);
    compare(A, B, 4, 6);
    compare(A, B, 5, 7);
    compare(A, B, 1, 2);
    compare(A, B, 5, 6);
    compare(A, B, 0, 4);
    compare(A, B, 3, 7);
    compare(A, B, 1, 5);
    compare(A, B, 2, 6);
    compare(A, B, 1, 4);
    compare(A, B, 3, 6);
    compare(A, B, 2, 4);
    compare(A, B, 3, 5);
    compare(A, B, 3, 4);
}

__device__ float median(float *A, float *B)
{
    // Count the neighbouring points.
    int N = B[0] + B[1] + B[2] + B[3] + B[4] + B[5] + B[6] + B[7];

    // Return the median out of N neighbours.
    if (N % 2 == 0) {return (A[N / 2 - 1] + A[N / 2]) / 2;}
    else {return A[N / 2];}
}

__global__ void median_velocity(float *f_median, float *nb, int *np, int size)
{
    // nb : values of the neighbouring points.
    // np : 1 if there is a neighbour, 0 if no neighbour.
    int t_idx = blockIdx.x * blockDim.x + threadIdx.x;
    if (t_idx >= size) {return;}

    // Loop through neighbours to populate an array to sort.
    int i;
    int j = 0;
    float A[8];
    float B[8];
    for (i = 0; i < 8; i++) {
        A[j] = nb[t_idx * 8 + i];
        B[j++] = np[t_idx * 8 + i];
    }

    // Sort the arrays.
    sort(A, B);

    f_median[t_idx] = median(A, B);
}

__global__ void median_fluc(float *f_median_fluc, float *f_median, float *nb, int *np, int size)
{
    // nb : value of the neighbouring points
    // np : 1 if there is a neighbour, 0 if no neighbour
    int t_idx = blockIdx.x * blockDim.x + threadIdx.x;
    if (t_idx >= size) {return;}

    float f_m = f_median[t_idx];

    // Loop through neighbours to populate an array to sort.
    int i;
    int j = 0;
    float A[8];
    float B[8];
    for (i = 0; i < 8; i++) {
        A[j] = fabsf(nb[t_idx * 8 + i] - f_m);
        B[j++] = np[t_idx * 8 + i];
    }

    // Sort the arrays.
    sort(A, B);

    f_median_fluc[t_idx] = median(A, B);
}
""")


def _gpu_median_velocity(f_neighbours_d, neighbours_present_d):
    """Calculates the median velocity on a 3x3 grid around each point in a velocity field.

    Parameters
    ----------
    f_neighbours_d: GPUArray
        4D float (m, n, 8), neighbouring velocities of every point.
    neighbours_present_d: GPUArray
        4D int (m, n, 8), value of one where a neighbour is present.

    Returns
    -------
    GPUArray
        2D float (m, n), mean velocities at each point.

    """
    m, n, _ = f_neighbours_d.shape
    size = m * n

    f_median_d = gpuarray.empty((m, n), dtype=DTYPE_f)

    block_size = _BLOCK_SIZE
    grid_size = ceil(size / block_size)
    median_velocity = mod_median_velocity.get_function('median_velocity')
    median_velocity(f_median_d, f_neighbours_d, neighbours_present_d, DTYPE_i(size), block=(block_size, 1, 1),
                    grid=(grid_size, 1))

    return f_median_d


def _gpu_median_fluc(f_median_d, f_neighbours_d, neighbours_present_d):
    """Calculates the magnitude of the median velocity fluctuations on a 3x3 grid around each point in a velocity field.

    Parameters
    ----------
    f_median_d : GPUArray
        2D float (m, n), mean velocities around each point.
    f_neighbours_d : GPUArray
        4D float (m, n, 8), neighbouring velocities of every point.
    neighbours_present_d : GPUArray
        4D int  (m, n, 8), value of one where a neighbour is present.

    Returns
    -------
    GPUArray
        2D float (m, n), RMS velocities at each point.

    """
    m, n = f_median_d.shape
    size = f_median_d.size

    f_median_fluc_d = gpuarray.empty((m, n), dtype=DTYPE_f)

    block_size = _BLOCK_SIZE
    grid_size = ceil(size / block_size)
    median_u_fluc = mod_median_velocity.get_function('median_fluc')
    median_u_fluc(f_median_fluc_d, f_median_d, f_neighbours_d, neighbours_present_d, DTYPE_i(size),
                  block=(block_size, 1, 1), grid=(grid_size, 1))

    return f_median_fluc_d


mod_mean_velocity = SourceModule("""
__device__ float num_neighbours(int *np, int t_idx)
{
    float denominator = np[t_idx * 8 + 0] + np[t_idx * 8 + 1] + np[t_idx * 8 + 2] + np[t_idx * 8 + 3]
                        + np[t_idx * 8 + 4] + np[t_idx * 8 + 5] + np[t_idx * 8 + 6] + np[t_idx * 8 + 7];
    return denominator + (denominator == 0.0f);
}


__global__ void mean_velocity(float *f_mean, float *nb, int *np, int size)
{
    // n : value of neighbours.
    // np : neighbours present.
    int t_idx = blockIdx.x * blockDim.x + threadIdx.x;
    if (t_idx >= size) {return;}

    // Sum terms of the mean.
    float numerator = nb[t_idx * 8 + 0] + nb[t_idx * 8 + 1] + nb[t_idx * 8 + 2] + nb[t_idx * 8 + 3]
                        + nb[t_idx * 8 + 4] + nb[t_idx * 8 + 5] + nb[t_idx * 8 + 6] + nb[t_idx * 8 + 7];

    // Mean is normalized by number of terms summed.
    float denominator = num_neighbours(np, t_idx);
    f_mean[t_idx] = numerator / denominator;
}

__global__ void mean_fluc(float *f_fluc, float *f_mean, float *nb, int *np, int size)
{
    // nb : value of the neighbouring points.
    // np : 1 if there is a neighbour, 0 if no neighbour.
    int t_idx = blockIdx.x * blockDim.x + threadIdx.x;
    if (t_idx >= size) {return;}

    // Sum terms of the mean fluctuations.
    float f_m = f_mean[t_idx];
    float numerator = fabsf(nb[t_idx * 8 + 0] - f_m) + fabsf(nb[t_idx * 8 + 1] - f_m)
                      + fabsf(nb[t_idx * 8 + 2] - f_m) + fabsf(nb[t_idx * 8 + 3] - f_m)
                      + fabsf(nb[t_idx * 8 + 4] - f_m) + fabsf(nb[t_idx * 8 + 5] - f_m)
                      + fabsf(nb[t_idx * 8 + 6] - f_m) + fabsf(nb[t_idx * 8 + 7] - f_m);

    // Mean fluctuation is normalized by number of terms summed.
    float denominator = num_neighbours(np, t_idx);
    f_fluc[t_idx] = numerator / denominator;
}

__global__ void rms(float *f_rms, float *f_mean, float *nb, int *np, int size)
{
    // nb : value of the neighbouring points
    // np : 1 if there is a neighbour, 0 if no neighbour
    int t_idx = blockIdx.x * blockDim.x + threadIdx.x;
    if (t_idx >= size) {return;}

    // Sum terms of the rms fluctuations.
    float f_m = f_mean[t_idx];
    float numerator = (powf(nb[t_idx * 8 + 0] - f_m, 2) + powf(nb[t_idx * 8 + 1] - f_m, 2)
                       + powf(nb[t_idx * 8 + 2] - f_m, 2) + powf(nb[t_idx * 8 + 3] - f_m, 2)
                       + powf(nb[t_idx * 8 + 4] - f_m, 2) + powf(nb[t_idx * 8 + 5] - f_m, 2)
                       + powf(nb[t_idx * 8 + 6] - f_m, 2) + powf(nb[t_idx * 8 + 7] - f_m, 2));

    // RMS is normalized by number of terms summed.
    float denominator = num_neighbours(np, t_idx);
    f_rms[t_idx] = sqrtf(numerator / denominator);

}
""")


def _gpu_mean_velocity(f_neighbours_d, neighbours_present_d):
    """Calculates the mean velocity on a 3x3 grid around each point in a velocity field.

    Parameters
    ----------
    f_neighbours_d: GPUArray
        4D float (m, n, 8), neighbouring velocities of every point.
    neighbours_present_d: GPUArray
        4D int (m, n, 8), value of one where a neighbour is present.

    Returns
    -------
    GPUArray
        2D float (m, n), mean velocities at each point.

    """
    m, n, _ = f_neighbours_d.shape
    size = m * n

    f_mean_d = gpuarray.empty((m, n), dtype=DTYPE_f)

    block_size = _BLOCK_SIZE
    grid_size = ceil(size / block_size)
    mean_velocity = mod_mean_velocity.get_function('mean_velocity')
    mean_velocity(f_mean_d, f_neighbours_d, neighbours_present_d, DTYPE_i(size), block=(block_size, 1, 1),
                  grid=(grid_size, 1))

    return f_mean_d


mod_mean_fluc = SourceModule("""

""")


def _gpu_mean_fluc(f_mean_d, f_neighbours_d, neighbours_present_d):
    """Calculates the magnitude of the mean velocity fluctuations on a 3x3 grid around each point in a velocity field.

    Parameters
    ----------
    f_mean_d: GPUArray
        2D float (m, n), mean velocities around each point.
    f_neighbours_d : GPUArray
        4D float (m, n, 8), neighbouring velocities of every point.
    neighbours_present_d : GPUArray
        4D int (m, n, 8), value of one where a neighbour is present.

    Returns
    -------
    GPUArray
        2D float (m, n), rms velocities at each point.

    """
    m, n = f_mean_d.shape
    size = f_mean_d.size

    f_fluc_d = gpuarray.empty((m, n), dtype=DTYPE_f)

    block_size = _BLOCK_SIZE
    grid_size = ceil(size / block_size)
    mean_fluc = mod_mean_velocity.get_function('mean_fluc')
    mean_fluc(f_fluc_d, f_mean_d, f_neighbours_d, neighbours_present_d, DTYPE_i(size), block=(block_size, 1, 1),
              grid=(grid_size, 1))

    return f_fluc_d


def _gpu_rms(f_mean_d, neighbours_d, neighbours_present_d):
    """Calculates the rms velocity in a 3x3 grid around each point in a velocity field.

    Parameters
    ----------
    f_mean_d : GPUArray
        2D float (m, n), mean velocities around each point.
    neighbours_d : GPUArray
        4D float (m, n, 8), neighbouring velocities of every point.
    neighbours_present_d : GPUArray
        4D int (m, n, 8), value of one where a neighbour is present.

    Returns
    -------
    GPUArray
        2D float (m, n), RMS velocities at each point.

    """
    m, n = f_mean_d.shape
    size = f_mean_d.size

    f_rms_d = gpuarray.empty((m, n), dtype=DTYPE_f)

    block_size = _BLOCK_SIZE
    grid_size = ceil(size / block_size)
    u_rms = mod_mean_velocity.get_function('rms')
    u_rms(f_rms_d, f_mean_d, neighbours_d, neighbours_present_d, DTYPE_i(size), block=(block_size, 1, 1),
          grid=(grid_size, 1))

    return f_rms_d
