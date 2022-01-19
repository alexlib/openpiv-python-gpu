"""This module is dedicated to advanced algorithms for PIV image analysis with NVIDIA GPU Support.

Note that all data must 32-bit at most to be stored on GPUs. All identifiers ending with '_d' exist on the GPU and not
the CPU. The GPU is referred to as the device, and therefore "_d" signifies that it is a device variable. Please adhere
to this standard as it makes developing and debugging much easier.

"""

import pycuda.autoinit
import pycuda.gpuarray as gpuarray
import pycuda.cumath as cumath
import skcuda.fft as cu_fft
import skcuda.misc as cu_misc
import numpy as np
import numpy.ma as ma
import logging
import nvidia_smi
from pycuda.compiler import SourceModule
from scipy.fft import fftshift
from math import sqrt
from openpiv.gpu_validation import gpu_validation
from openpiv.smoothn import smoothn as smoothn

# Define 32-bit types
DTYPE_i = np.int32
DTYPE_f = np.float32

# initialize the skcuda library
cu_misc.init()


class GPUCorrelation:
    def __init__(self, frame_a_d, frame_b_d, nfft_x=None):
        """A class representing the cross correlation function.

        Parameters
        ----------
        frame_a_d, frame_b_d : GPUArray
            2D int, image pair
        nfft_x : int or None
            window size for fft

        Methods
        -------
        __call__(window_size, extended_size=None, d_shift=None, d_strain=None)
            returns the peaks of the correlation windows
        sig2noise_ratio(method='peak2peak', width=2)
            returns the signal-to-noise ratio of the correlation peaks

        """
        _check_inputs(frame_a_d, frame_b_d, array_type=gpuarray.GPUArray, dtype=DTYPE_i, dim=2)
        if nfft_x is None:
            self.nfft = 2
        else:
            assert (self.nfft & (self.nfft - 1)) == 0, 'nfft must be power of 2'
            self.nfft = nfft_x
        self.frame_a_d = frame_a_d
        self.frame_b_d = frame_b_d
        self.peak_row = None
        self.peak_col = None
        self.frame_shape = DTYPE_i(frame_a_d.shape)

    def __call__(self, window_size, overlap_ratio, extended_size=None, d_shift=None, d_strain=None):
        """Returns the pixel peaks using the specified correlation method.

        Parameters
        ----------
        window_size : int
            size of the interrogation window
        overlap_ratio : float
            overlap between interrogation windows
        extended_size : int
            extended window size to search in the second frame
        d_shift : GPUArray
            2D ([dx, dy]), dx and dy are 1D arrays of the x-y shift at each interrogation window of the second image.
            This is using the x-y convention of this code where x is the row and y is the column.
        d_strain : GPUArray
            2D strain tensor. First dimension is (u_x, u_y, v_x, v_y)

        Returns
        -------
        row_sp, col_sp : ndarray
            3D float, locations of the subpixel peaks.

        """
        assert window_size >= 8, "Window size is too small."
        assert window_size % 8 == 0, "Window size must be a multiple of 8."
        self.window_size = window_size
        self.overlap_ratio = overlap_ratio
        self.spacing = self.window_size * overlap_ratio
        # TODO remove unnecessary type casts
        self.extended_size = DTYPE_i(extended_size) if extended_size is not None else DTYPE_i(window_size)
        self.fft_size = DTYPE_i(self.extended_size * self.nfft)
        # TODO shouldn't call this more than once
        self.n_row, self.n_col = DTYPE_i(get_field_shape(self.frame_shape, self.window_size, self.overlap_ratio))
        self.n_windows = self.n_row * self.n_col

        # Return stack of all IWs
        win_a_d, win_b_d = self._iw_arrange(self.frame_a_d, self.frame_b_d, d_shift, d_strain)

        # normalize array by computing the norm of each IW
        win_a_norm_d, win_b_norm_d = self._normalize_intensity(win_a_d, win_b_d)

        # zero pad arrays
        win_a_zp_d, win_b_zp_d = self._zero_pad(win_a_norm_d, win_b_norm_d)

        # correlate Windows
        self.data = self._correlate_windows(win_a_zp_d, win_b_zp_d)

        # get first peak of correlation function
        self.peak_row, self.peak_col, self.corr_max1 = self._find_peak(self.data)

        # get the subpixel location
        row_sp, col_sp = self._subpixel_peak_location()

        # TODO this could be GPU array --would be faster?
        # reshape to field window coordinates
        i_peak = row_sp.reshape((self.n_row, self.n_col)) - self.fft_size / 2
        j_peak = col_sp.reshape((self.n_row, self.n_col)) - self.fft_size / 2

        return i_peak, j_peak

    def _iw_arrange(self, frame_a_d, frame_b_d, shift_d, strain_d):
        """Creates a 3D array stack of all the interrogation windows.

        This is necessary to do the FFTs all at once on the GPU. This populates interrogation windows from the origin of the image.

        Parameters
        -----------
        frame_a_d, frame_b_d : GPUArray
            2D int, image pair
        shift_d : GPUArray
            3D float, shift of the second window
        strain_d : GPUArray
            4D float, strain rate tensor. First dimension is (u_x, u_y, v_x, v_y)

        Returns
        -------
        win_a_d, win_b_d : GPUArray
            3D float, all interrogation windows stacked on each other.

        """
        _check_inputs(frame_a_d, frame_b_d, array_type=gpuarray.GPUArray, dtype=DTYPE_i, dim=2)
        ht, wd = self.frame_shape
        spacing = DTYPE_i(self.window_size * self.overlap_ratio)
        diff = DTYPE_i(spacing - self.extended_size / 2)

        # create GPU arrays to store the window data
        win_a_d = gpuarray.zeros((self.n_windows, self.extended_size, self.extended_size), dtype=DTYPE_f)
        win_b_d = gpuarray.zeros((self.n_windows, self.extended_size, self.extended_size), dtype=DTYPE_f)

        mod_ws = SourceModule("""
            __global__ void window_slice(int *input, float *output, int ws, int spacing, int diff, int n_col, int wd, int ht)
        {
            // x blocks are windows; y and z blocks are x and y dimensions, respectively
            int ind_i = blockIdx.x;
            int ind_x = blockIdx.y * blockDim.x + threadIdx.x;
            int ind_y = blockIdx.z * blockDim.y + threadIdx.y;
            
            // do the mapping
            int x = (ind_i % n_col) * spacing + diff + ind_x;
            int y = (ind_i / n_col) * spacing + diff + ind_y;
            
            // find limits of domain
            int outside_range = (x >= 0 && x < wd && y >= 0 && y < ht);

            // indices of new array to map to
            int w_range = ind_i * ws * ws + ws * ind_y + ind_x;
            
            // apply the mapping
            output[w_range] = input[(y * wd + x) * outside_range] * outside_range;
        }

            __global__ void window_slice_deform(int *input, float *output, float *shift, float *strain, float f, int ws, int spacing, int diff, int n_col, int num_window, int wd, int ht)
        {
            // f : factor to apply to the shift and strain tensors
            // wd : width (number of columns in the full image)
            // h : height (number of rows in the full image)

            // x blocks are windows; y and z blocks are x and y dimensions, respectively
            int ind_i = blockIdx.x;  // window index
            int ind_x = blockIdx.y * blockDim.x + threadIdx.x;
            int ind_y = blockIdx.z * blockDim.y + threadIdx.y;

            // Loop through each interrogation window and apply the shift and deformation.
            // get the shift values
            float dx = shift[ind_i] * f;
            float dy = shift[num_window + ind_i] * f;

            // get the strain tensor values
            float u_x = strain[ind_i] * f;
            float u_y = strain[num_window + ind_i] * f;
            float v_x = strain[2 * num_window + ind_i] * f;
            float v_y = strain[3 * num_window + ind_i] * f;

            // compute the window vector
            float r_x = ind_x - ws / 2 + 0.5;  // r_x = x - x_c
            float r_y = ind_y - ws / 2 + 0.5;  // r_y = y - y_c

            // apply deformation operation
            float x_shift = ind_x + dx + r_x * u_x + r_y * u_y;  // r * du + dx
            float y_shift = ind_y + dy + r_x * v_x + r_y * v_y;  // r * dv + dy

            // do the mapping
            float x = (ind_i % n_col) * spacing + x_shift + diff;
            float y = (ind_i / n_col) * spacing + y_shift + diff;

            // do bilinear interpolation
            int x2 = ceilf(x);
            int x1 = floorf(x);
            int y2 = ceilf(y);
            int y1 = floorf(y);

            // prevent divide-by-zero
            if (x2 == x1) {x2 = x1 + 1;}
            if (y2 == y1) {y2 = y2 + 1;}

            // find limits of domain
            int outside_range = (x1 >= 0 && x2 < wd && y1 >= 0 && y2 < ht);

            // terms of the bilinear interpolation. multiply by outside_range to avoid index error.
            float f11 = input[(y1 * wd + x1) * outside_range];
            float f21 = input[(y1 * wd + x2) * outside_range];
            float f12 = input[(y2 * wd + x1) * outside_range];
            float f22 = input[(y2 * wd + x2) * outside_range];

            // indices of image to map to
            int w_range = ind_i * ws * ws + ws * ind_y + ind_x;

            // Apply the mapping. Multiply by outside_range to set values outside the window to zero.
            output[w_range] = (f11 * (x2 - x) * (y2 - y) + f21 * (x - x1) * (y2 - y) + f12 * (x2 - x) * (y - y1) + f22 * (x - x1) * (y - y1)) * outside_range;
        }
        """)
        block_size = 8
        grid_size = int(self.extended_size / block_size)

        # slice windows
        if shift_d is not None:
            # use translating windows
            # TODO this might be redundant
            if strain_d is None:
                strain_d = gpuarray.zeros((4, self.n_row, self.n_col), dtype=DTYPE_f)

            # factors to apply the symmetric shift
            shift_factor_a = DTYPE_f(-0.5)
            shift_factor_b = DTYPE_f(0.5)

            # shift frames and deform
            window_slice_deform = mod_ws.get_function("window_slice_deform")
            window_slice_deform(frame_a_d, win_a_d, shift_d, strain_d, shift_factor_a, self.extended_size, spacing,
                                diff,
                                self.n_col, self.n_windows, wd, ht, block=(block_size, block_size, 1),
                                grid=(int(self.n_windows), grid_size, grid_size))
            window_slice_deform(frame_b_d, win_b_d, shift_d, strain_d, shift_factor_b, self.extended_size, spacing,
                                diff,
                                self.n_col, self.n_windows, wd, ht, block=(block_size, block_size, 1),
                                grid=(int(self.n_windows), grid_size, grid_size))

        else:
            # use non-translating windows
            window_slice_deform = mod_ws.get_function("window_slice")
            window_slice_deform(frame_a_d, win_a_d, self.extended_size, spacing, diff, self.n_col, wd, ht,
                                block=(block_size, block_size, 1), grid=(int(self.n_windows), grid_size, grid_size))
            window_slice_deform(frame_b_d, win_b_d, self.extended_size, spacing, diff, self.n_col, wd, ht,
                                block=(block_size, block_size, 1), grid=(int(self.n_windows), grid_size, grid_size))

        return win_a_d, win_b_d

    def _normalize_intensity(self, win_a_d, win_b_d):
        """Remove the mean from each IW of a 3D stack of IWs.

        Parameters
        ----------
        win_a_d, win_b_d : GPUArray
            3D float, stack of first IWs.

        Returns
        -------
        win_a_norm_d, win_b_norm_d : GPUArray
            3D float, the normalized intensities in the windows.

        """
        # define GPU arrays to store window data
        win_a_norm_d = gpuarray.zeros((self.n_windows, self.extended_size, self.extended_size), dtype=DTYPE_f)
        win_b_norm_d = gpuarray.zeros((self.n_windows, self.extended_size, self.extended_size), dtype=DTYPE_f)

        # number of pixels in each interrogation window
        iw_size = DTYPE_i(self.extended_size * self.extended_size)

        # get mean of each IW using skcuda
        mean_a_d = cu_misc.mean(win_a_d.reshape(self.n_windows, iw_size), axis=1)
        mean_b_d = cu_misc.mean(win_b_d.reshape(self.n_windows, iw_size), axis=1)

        mod_norm = SourceModule("""
            __global__ void normalize(float *array, float *array_norm, float *mean, int iw_size)
        {
            // global thread id for 1D grid of 2D blocks
            int thread_idx = blockIdx.x * (blockDim.x * blockDim.y) + threadIdx.y * blockDim.x + threadIdx.x;

            // indices for mean matrix
            int mean_idx = thread_idx / iw_size;

            array_norm[thread_idx] = array[thread_idx] - mean[mean_idx];
        }
        
            __global__ void smart_normalize(float *array, float *array_norm, float *mean, float *mean_ratio, int iw_size)
        {
            // global thread id for 1D grid of 2D blocks
            int thread_idx = blockIdx.x * (blockDim.x * blockDim.y) + threadIdx.y * blockDim.x + threadIdx.x;

            // indices for mean matrix
            int mean_idx = thread_idx / iw_size;

            array_norm[thread_idx] = array[thread_idx] * mean_ratio[mean_idx] - mean[mean_idx];
        }
        """)
        block_size = 8
        grid_size = int(win_a_d.size / block_size ** 2)
        normalize = mod_norm.get_function('normalize')
        normalize(win_a_d, win_a_norm_d, mean_a_d, iw_size, block=(block_size, block_size, 1), grid=(grid_size, 1))
        normalize(win_b_d, win_b_norm_d, mean_b_d, iw_size, block=(block_size, block_size, 1), grid=(grid_size, 1))

        return win_a_norm_d, win_b_norm_d

    def _zero_pad(self, win_a_norm_d, win_b_norm_d):
        """Function that zero-pads an 3D stack of arrays for use with the skcuda FFT function.

        If extended size is passed, then the second window

        Parameters
        ----------
        win_a_norm_d, win_b_norm_d : GPUArray
            3D float, arrays to be zero padded.

        Returns
        -------
        win_a_zp_d, win_b_zp_d : GPUArray
            3D float, windows which have been zero-padded.

        """
        # compute the window extension
        s0_a = DTYPE_i((self.extended_size - self.window_size) / 2)
        s1_a = DTYPE_i(self.extended_size - s0_a)
        s0_b = DTYPE_i(0)
        s1_b = self.extended_size

        # define GPU arrays to store the window data
        win_a_zp_d = gpuarray.zeros([self.n_windows, self.fft_size, self.fft_size], dtype=DTYPE_f)
        win_b_zp_d = gpuarray.zeros([self.n_windows, self.fft_size, self.fft_size], dtype=DTYPE_f)

        mod_zp = SourceModule("""
            __global__ void zero_pad(float *array_zp, float *array, int fft_size, int window_size, int s0, int s1)
            {
                // index, x blocks are windows; y and z blocks are x and y dimensions, respectively
                int ind_i = blockIdx.x;
                int ind_x = blockIdx.y * blockDim.x + threadIdx.x;
                int ind_y = blockIdx.z * blockDim.y + threadIdx.y;
                
                // don't copy if out of range
                if (ind_x < s0 || ind_x >= s1 || ind_y < s0 || ind_y >= s1) {return;}

                // get range of values to map
                int arr_range = ind_i * window_size * window_size + window_size * ind_y + ind_x;
                int zp_range = ind_i * fft_size * fft_size + fft_size * ind_y + ind_x;

                // apply the map
                array_zp[zp_range] = array[arr_range];
            }
        """)
        block_size = 8
        grid_size = int(self.extended_size / block_size)
        zero_pad = mod_zp.get_function('zero_pad')
        zero_pad(win_a_zp_d, win_a_norm_d, self.fft_size, self.extended_size, s0_a, s1_a,
                 block=(block_size, block_size, 1), grid=(int(self.n_windows), grid_size, grid_size))
        zero_pad(win_b_zp_d, win_b_norm_d, self.fft_size, self.extended_size, s0_b, s1_b,
                 block=(block_size, block_size, 1), grid=(int(self.n_windows), grid_size, grid_size))

        return win_a_zp_d, win_b_zp_d

    def _correlate_windows(self, win_a_zp_d, win_b_zp_d):
        """Compute correlation function between two interrogation windows.

        The correlation function can be computed by using the correlation theorem to speed up the computation.

        Parameters
        ----------
        win_a_zp_d, win_b_zp_d : GPUArray
            3D float, zero-padded correlation windows.

        Returns
        -------
        ndarray
            2D, output of the correlation function.

        """
        # FFT size
        win_h = self.fft_size
        win_w = self.fft_size

        # allocate space on gpu for FFTs
        win_i_fft_d = gpuarray.empty((self.n_windows, win_h, win_w), DTYPE_f)
        win_fft_d = gpuarray.empty((self.n_windows, win_h, win_w // 2 + 1), np.complex64)
        search_area_fft_d = gpuarray.empty((self.n_windows, win_h, win_w // 2 + 1), np.complex64)

        # forward FFTs
        plan_forward = cu_fft.Plan((win_h, win_w), DTYPE_f, np.complex64, self.n_windows)
        cu_fft.fft(win_a_zp_d, win_fft_d, plan_forward)
        cu_fft.fft(win_b_zp_d, search_area_fft_d, plan_forward)

        # multiply the FFTs
        win_fft_d = win_fft_d.conj()
        tmp_d = cu_misc.multiply(search_area_fft_d, win_fft_d)

        # inverse transform
        plan_inverse = cu_fft.Plan((win_h, win_w), np.complex64, DTYPE_f, self.n_windows)
        cu_fft.ifft(tmp_d, win_i_fft_d, plan_inverse, True)

        # transfer back to cpu to do FFTshift
        # possible to do this on GPU?
        corr = fftshift(win_i_fft_d.get().real, axes=(1, 2))

        return corr

    def _find_peak(self, corr):
        """Find the row and column of the highest peak in correlation function

        Parameters
        ----------
        corr : ndarray
            array that is image of the correlation function

        Returns
        -------
        ind : array - 1D int
            flattened index of corr peak
        row : array - 1D int
            row position of corr peak
        col : array - 1D int
            column position of corr peak

        """
        # Reshape matrix
        corr_reshape = corr.reshape(self.n_windows, self.fft_size ** 2)

        # Get index and value of peak
        max_idx = np.argmax(corr_reshape, axis=1)
        maximum = corr_reshape[range(self.n_windows), max_idx]

        # row and column information of peak
        row = max_idx // self.fft_size
        col = max_idx % self.fft_size

        # return the center if the correlation peak is zero (same as cython code above)
        w = int(self.fft_size / 2)
        corr_idx = np.asarray((corr_reshape[range(self.n_windows), max_idx] < 0.1)).nonzero()
        row[corr_idx] = w
        col[corr_idx] = w

        return row, col, maximum

    def _find_second_peak(self, width):
        """Find the value of the second-largest peak.

        The second-largest peak is the height of the peak in the region outside a "width * width" submatrix around
        the first correlation peak.

        Parameters
        ----------
        width : int
            the half size of the region around the first correlation peak to ignore for finding the second peak.

        Returns
        -------
        corr_max2 : int
            the value of the second correlation peak.

        """
        # create a masked view of the self.data array
        tmp = self.data.view(ma.MaskedArray)

        # set (width x width) square sub-matrix around the first correlation peak as masked
        for i in range(-width, width + 1):
            for j in range(-width, width + 1):
                row_idx = self.peak_row + i
                col_idx = self.peak_col + j
                idx = (row_idx >= 0) & (row_idx < self.fft_size) & (col_idx >= 0) & (col_idx < self.fft_size)
                tmp[idx, row_idx[idx], col_idx[idx]] = ma.masked

        row2, col2, corr_max2 = self._find_peak(tmp)

        return corr_max2

    def _subpixel_peak_location(self):
        """Find subpixel peak approximation using Gaussian method.

        Returns
        -------
        row_sp, col_sp : ndarray
            2D float, location of peak to subpixel accuracy

        """
        # TODO subtract the nfft half-width before this step. This should only be for subpixel approximation.
        # Define small number to replace zeros and get rid of warnings in calculations
        small = 1e-20

        # cast to float
        corr_c = self.data.astype(DTYPE_f)
        row_c = self.peak_row.astype(DTYPE_f)
        col_c = self.peak_col.astype(DTYPE_f)

        # move boundary peaks inward one node.
        row_tmp = np.copy(self.peak_row)
        row_tmp[row_tmp < 1] = 1
        row_tmp[row_tmp > self.fft_size - 2] = self.fft_size - 2
        col_tmp = np.copy(self.peak_col)
        col_tmp[col_tmp < 1] = 1
        col_tmp[col_tmp > self.fft_size - 2] = self.fft_size - 2

        # initialize arrays
        c = corr_c[range(self.n_windows), row_tmp, col_tmp]
        cl = corr_c[range(self.n_windows), row_tmp - 1, col_tmp]
        cr = corr_c[range(self.n_windows), row_tmp + 1, col_tmp]
        cd = corr_c[range(self.n_windows), row_tmp, col_tmp - 1]
        cu = corr_c[range(self.n_windows), row_tmp, col_tmp + 1]

        # get rid of values that are zero or lower
        non_zero = np.array(c > 0, dtype=DTYPE_f)
        c[c <= 0] = small
        cl[cl <= 0] = small
        cr[cr <= 0] = small
        cd[cd <= 0] = small
        cu[cu <= 0] = small

        # do subpixel approximation. Add small to avoid zero divide.
        row_sp = row_c + ((np.log(cl) - np.log(cr))
                          / (2 * np.log(cl) - 4 * np.log(c) + 2 * np.log(cr) + small)) * non_zero
        col_sp = col_c + ((np.log(cd) - np.log(cu))
                          / (2 * np.log(cd) - 4 * np.log(c) + 2 * np.log(cu) + small)) * non_zero

        return row_sp, col_sp

    def sig2noise_ratio(self, method='peak2peak', width=2):
        """Computes the signal-to-noise ratio.

        The signal-to-noise ratio is computed from the correlation map with one of two available method. It is a measure
        of the quality of the matching between two interrogation windows.

        Parameters
        ----------
        method : string
            the method for evaluating the signal to noise ratio value from
            the correlation map. Can be `peak2peak`, `peak2mean` or None
            if no evaluation should be made.
        width : int, optional
            the half size of the region around the first
            correlation peak to ignore for finding the second
            peak. [default: 2]. Only used if ``sig2noise_method==peak2peak``.

        Returns
        -------
        ndarray
            2D float, the signal-to-noise ratio from the correlation map for each vector.

        """
        # compute signal-to-noise ratio by the chosen method
        if method == 'peak2peak':
            corr_max2 = self._find_second_peak(width=width)
        elif method == 'peak2mean':
            corr_max2 = self.data.mean()
        else:
            raise ValueError('wrong sig2noise_method')

        # get rid on divide by zero
        corr_max2[corr_max2 == 0.0] = 1e-20

        # get signal to noise ratio
        sig2noise = self.corr_max1 / corr_max2

        # get rid of nan values. Set sig2noise to zero
        sig2noise[np.isnan(sig2noise)] = 0.0

        # if the image is lacking particles, it will correlate to very low value, but not zero
        # return zero, since we have no signal.
        sig2noise[self.corr_max1 < 1e-3] = 0.0

        # if the first peak is on the borders, the correlation map is wrong
        # return zero, since we have no signal.
        sig2noise[np.array(self.peak_row == 0) * np.array(self.peak_row == self.data.shape[1]) * np.array(
            self.peak_col == 0) * np.array(self.peak_col == self.data.shape[2])] = 0.0

        return sig2noise.reshape(self.n_row, self.n_col)


def gpu_extended_search_area(frame_a, frame_b,
                             window_size,
                             overlap_ratio,
                             dt,
                             search_area_size,
                             **kwargs
                             ):
    """The implementation of the one-step direct correlation with the same size windows.

    Support for extended search area of the second window has yet to be implimetned. This module is meant to be used
    with an iterative method to cope with the loss of pairs due to particle movement out of the search area.

    This function is an adaptation of the original extended_search_area_piv function rewritten with PyCuda and CUDA-C to run on an NVIDIA GPU.

    References
    ----------
        Particle-Imaging Techniques for Experimental Fluid Mechanics Annual Review of Fluid Mechanics
            Vol. 23: 261-304 (Volume publication date January 1991)
            DOI: 10.1146/annurev.fl.23.010191.001401

    Parameters
    ----------
    frame_a, frame_b : ndarray
        2D int, grey levels of the first and second frames.
    window_size : int
        The size of the (square) interrogation window for the first frame.
    search_area_size : int
        The size of the (square) interrogation window for the second frame.
    overlap_ratio : float
        The ratio of overlap between two windows (between 0 and 1)
    dt : float
        Time delay separating the two frames.

    Returns
    -------
    u, v : ndarray
        2D, the u and v velocity components, in pixels/seconds.

    Other Parameters
    ----------------
    subpixel_method : {'gaussian', 'centroid', 'parabolic'}
        Method to estimate subpixel location of the peak. Gaussian is default if correlation map is positive. Centroid replaces default if correlation map is negative.
    width : int
        Half size of the region around the first correlation peak to ignore for finding the second peak. Default is 2. Only used if sig2noise_method==peak2peak.
    nfft_x : int
        The size of the 2D FFT in x-direction. The default of 2 x windows_a.shape[0] is recommended.

    Example
    --------
    >>> u, v = gpu_extended_search_area(frame_a, frame_b, window_size=16, overlap_ratio=0.5, search_area_size=32, dt=1)

    """
    # Extract the parameters
    nfft_x = kwargs['nfft_x'] if 'nfft_x' in kwargs else None
    overlap = int(overlap_ratio * window_size)

    # cast images as floats and sent to gpu
    frame_a_d = gpuarray.to_gpu(frame_a.astype(DTYPE_i))
    frame_b_d = gpuarray.to_gpu(frame_b.astype(DTYPE_i))

    # Get correlation function
    corr = GPUCorrelation(frame_a_d, frame_b_d, nfft_x)

    # Get window displacement to subpixel accuracy
    sp_i, sp_j = corr(window_size, overlap_ratio, search_area_size)

    # reshape the peaks
    i_peak = np.reshape(sp_i, (corr.n_row, corr.n_col))
    j_peak = np.reshape(sp_j, (corr.n_row, corr.n_col))

    # calculate velocity fields
    u = j_peak / dt
    v = -i_peak / dt

    # Free gpu memory
    frame_a_d.gpudata.free()
    frame_b_d.gpudata.free()

    return u, v


def gpu_piv(frame_a, frame_b,
            mask=None,
            window_size_iters=(1, 2),
            min_window_size=16,
            overlap_ratio=0.5,
            dt=1,
            deform=True,
            smooth=True,
            nb_validation_iter=1,
            validation_method='median_velocity',
            trust_1st_iter=True,
            **kwargs):
    """An iterative GPU-accelerated algorithm that uses translation and deformation of interrogation windows.

    At every iteration, the estimate of the displacement and gradient are used to shift and deform the interrogation
    windows used in the next iteration. One or more iterations can be performed before the the estimated velocity is
    interpolated onto a finer mesh. This is done until the final mesh and number of iterations is met.

    Algorithm Details
    -----------------
    Only window sizes that are multiples of 8 are supported now, and the minimum window size is 8.
    Windows are shifted symmetrically to reduce bias errors.
    The displacement obtained after each correlation is the residual displacement dc.
    The new displacement is computed by dx = dpx + dcx and dy = dpy + dcy.
    Validation is done by any combination of signal-to-noise ratio, mean, median
    Smoothn can be used between iterations to improve the estimate and replace missing values.

    References
    ----------
    Scarano F, Riethmuller ML (1999) Iterative multigrid approach in PIV image processing with discrete window offset.
        Exp Fluids 26:513–523
    Meunier, P., & Leweke, T. (2003). Analysis and treatment of errors due to high velocity gradients in particle image velocimetry.
        Experiments in fluids, 35(5), 408-421.
    Garcia, D. (2010). Robust smoothing of gridded data in one and higher dimensions with missing values.
        Computational statistics & data analysis, 54(4), 1167-1178.

    Parameters
    ----------
    frame_a, frame_b : ndarray
        2D int, integers containing grey levels of the first and second frames.
    mask : ndarray or None
        2D, int, array of integers with values 0 for the background, 1 for the flow-field. If the center of a window is on a 0 value the velocity is set to 0.
    window_size_iters : tuple or int
        Number of iterations performed at each window size
    min_window_size : tuple or int
        Length of the sides of the square deformation. Only supports multiples of 8.
    overlap_ratio : float
        Ratio of overlap between two windows (between 0 and 1).
    dt : float
        Time delay separating the two frames.
    deform : bool
        Whether to deform the windows by the velocity gradient at each iteration.
    smooth : bool
        Whether to smooth the intermediate fields.
    nb_validation_iter : int
        Number of iterations per validation cycle.
    validation_method : {tuple, 's2n', 'median_velocity', 'mean_velocity', 'rms_velocity'}
        Method used for validation. Only the mean velocity method is implemented now. The default tolerance is 2 for median validation.
    trust_1st_iter : bool
        With a first window size following the 1/4 rule, the 1st iteration can be trusted and the value should be 1.

    Returns
    -------
    x, y : ndarray
        2D, Coordinates where the PIV-velocity fields have been computed.
    u, v : ndarray
        2D, Velocity fields in pixel/time units.
    mask : ndarray
        2D, the boolean values (True for vectors interpolated from previous iteration).
    s2n : ndarray
        2D, the signal to noise ratio of the final velocity field.

    Other Parameters
    ----------------
    s2n_tol, median_tol, mean_tol, median_tol, rms_tol : float
        Tolerance of the validation methods.
    smoothing_par : float
        Smoothing parameter to pass to Smoothn to apply to the intermediate velocity fields.
    extend_ratio : float
        Ratio the extended search area to use on the first iteration. If not specified, extended search will not be used.
    subpixel_method : {'gaussian', 'centroid', 'parabolic'}
        Method to estimate subpixel location of the peak. Gaussian is default if correlation map is positive. Centroid replaces default if correlation map is negative.
    return_sig2noise : bool
        Sets whether to return the signal-to-noise ratio. Not returning the signal-to-noise speeds up computation significantly, which is default behaviour.
    sig2noise_method : {'peak2peak', 'peak2mean'}
        Method of signal-to-noise-ratio measurement.
    s2n_width : int
        Half size of the region around the first correlation peak to ignore for finding the second peak. Default is 2. Only used if sig2noise_method==peak2peak.
    nfftx : int
        The size of the 2D FFT in x-direction. The default of 2 x windows_a.shape[0] is recommended.

    Example
    -------
    >>> x, y, u, v, mask, s2n = gpu_piv(frame_a, frame_b, mask=None, window_size_iters=(1, 2), min_window_size=16, overlap_ratio=0.5, dt=1, deform=True, smooth=True, nb_validation_iter=2, validation_method='median_velocity', median_tol=2)

    """
    piv_gpu = PIVGPU(frame_a.shape, window_size_iters, min_window_size, overlap_ratio, dt, mask, deform, smooth,
                     nb_validation_iter, validation_method, trust_1st_iter, **kwargs)

    return_sig2noise = kwargs['return_sig2noise'] if 'return_sig2noise' in kwargs else False
    x, y = piv_gpu.coords
    u, v = piv_gpu(frame_a, frame_b)
    mask = piv_gpu.mask
    s2n = piv_gpu.s2n if return_sig2noise else None
    return x, y, u, v, mask, s2n


class PIVGPU:
    """This class is the object-oriented implementation of the GPU PIV function.

    Parameters
    ----------
    frame_shape : tuple
        (ht, wd) of the image series
    window_size_iters : tuple or int
        Number of iterations performed at each window size
    min_window_size : tuple or int
        Length of the sides of the square deformation. Only support multiples of 8.
    overlap_ratio : float
        the ratio of overlap between two windows (between 0 and 1).
    dt : float
        Time delay separating the two frames.
    mask : ndarray
        2D, float. Array of integers with values 0 for the background, 1 for the flow-field. If the center of a window is on a 0 value the velocity is set to 0.
    deform : bool
        Whether to deform the windows by the velocity gradient at each iteration.
    smooth : bool
        Whether to smooth the intermediate fields.
    nb_validation_iter : int
        Number of iterations per validation cycle.
    validation_method : {tuple, 's2n', 'median_velocity', 'mean_velocity', 'rms_velocity'}
        Method used for validation. Only the mean velocity method is implemented now. The default tolerance is 2 for median validation.
    trust_1st_iter : bool
        With a first window size following the 1/4 rule, the 1st iteration can be trusted and the value should be 1.

    Other Parameters
    ----------------
    s2n_tol, median_tol, mean_tol, median_tol, rms_tol : float
        Tolerance of the validation methods.
    smoothing_par : float
        Smoothing parameter to pass to smoothn to apply to the intermediate velocity fields. Default is 0.5.
    extend_ratio : float
        Ratio the extended search area to use on the first iteration. If not specified, extended search will not be used.
    subpixel_method : {'gaussian', 'centroid', 'parabolic'}
        Method to estimate subpixel location of the peak. Gaussian is default if correlation map is positive. Centroid replaces default if correlation map is negative.
    sig2noise_method : {'peak2peak', 'peak2mean'}
        Method of signal-to-noise-ratio measurement.
    s2n_width : int
        Half size of the region around the first correlation peak to ignore for finding the second peak. Default is 2. Only used if sig2noise_method==peak2peak.
    nfft_x : int
        The size of the 2D FFT in x-direction. The default of 2 x windows_a.shape[0] is recommended.

    Attributes
    ----------
    coords : ndarray
        2D, Coordinates where the PIV-velocity fields have been computed.
    mask : ndarray
        2D, the boolean values (True for vectors interpolated from previous iteration).
    s2n : ndarray
        2D, the signal to noise ratio of the final velocity field.

    Methods
    -------
    __call__(frame_a, frame_b)
        Main method to process image pairs.

    """

    def __init__(self,
                 frame_shape,
                 window_size_iters=(1, 2),
                 min_window_size=16,
                 overlap_ratio=0.5,
                 dt=1,
                 mask=None,
                 deform=True,
                 smooth=True,
                 nb_validation_iter=1,
                 validation_method='median_velocity',
                 trust_1st_iter=False,
                 **kwargs):

        # input checks
        ht, wd = frame_shape
        dt = DTYPE_f(dt)
        ws_iters = (window_size_iters,) if type(window_size_iters) == int else window_size_iters
        num_ws = len(ws_iters)
        self.overlap_ratio = overlap_ratio
        self.dt = dt
        self.deform = deform
        self.smooth = smooth
        self.nb_iter_max = nb_iter_max = sum(ws_iters)
        self.nb_validation_iter = nb_validation_iter
        self.trust_1st_iter = trust_1st_iter

        # windows sizes
        self.ws = np.asarray(
            [np.power(2, num_ws - i - 1) * min_window_size for i in range(num_ws) for _ in range(ws_iters[i])],
            dtype=DTYPE_i)

        # TODO These are terrible for understanding. Try a dictionary instead.
        # validation method
        self.val_tols = [None, None, None, None]
        val_methods = validation_method if type(validation_method) == str else (validation_method,)
        if 's2n' in val_methods:
            self.val_tols[0] = kwargs['s2n_tol'] if 's2n_tol' in kwargs else 1.2  # default tolerance
        if 'median_velocity' in val_methods:
            self.val_tols[1] = kwargs['median_tol'] if 'median_tol' in kwargs else 2  # default tolerance
        if 'mean_velocity' in val_methods:
            self.val_tols[2] = kwargs['mean_tol'] if 'mean_tol' in kwargs else 2  # default tolerance
        if 'rms_velocity' in val_methods:
            self.val_tols[3] = kwargs['rms_tol'] if 'rms_tol' in kwargs else 2  # default tolerance

        # other parameters
        self.smoothing_par = kwargs['smoothing_par'] if 'smoothing_par' in kwargs else 0.5
        self.sig2noise_method = kwargs['sig2noise_method'] if 'sig2noise_method' in kwargs else 'peak2peak'
        self.s2n_width = kwargs['s2n_width'] if 's2n_width' in kwargs else 2
        self.nfft_x = kwargs['nfft_x'] if 'nfft_x' in kwargs else None
        self.extend_ratio = kwargs['extend_ratio'] if 'extend_ratio' in kwargs else None
        self.im_mask = gpuarray.to_gpu(mask.astype(DTYPE_i)) if mask is not None else None
        self.corr = None
        self.sig2noise = None
        # TODO reduce the size of this definition
        self.n_row = np.zeros(nb_iter_max, dtype=DTYPE_i)
        self.n_col = np.zeros(nb_iter_max, dtype=DTYPE_i)
        self.spacing = np.zeros(nb_iter_max, dtype=DTYPE_i)

        # overlap init
        for K in range(nb_iter_max):
            self.spacing[K] = self.ws[K] - int(self.ws[K] * overlap_ratio)

        # n_col and n_row init
        for K in range(nb_iter_max):
            self.n_row[K] = (ht - self.spacing[K]) // self.spacing[K]
            self.n_col[K] = (wd - self.spacing[K]) // self.spacing[K]

        # initialize x and y
        # TODO make a pythonic object to store x, y and mask
        x = np.zeros([nb_iter_max, self.n_row[nb_iter_max - 1], self.n_col[nb_iter_max - 1]], dtype=DTYPE_f)
        y = np.zeros([nb_iter_max, self.n_row[nb_iter_max - 1], self.n_col[nb_iter_max - 1]], dtype=DTYPE_f)
        mask_array = np.zeros([nb_iter_max, self.n_row[nb_iter_max - 1], self.n_col[nb_iter_max - 1]], dtype=DTYPE_f)

        for K in range(nb_iter_max):
            x[K, :, 0] = self.spacing[K]  # init x on first column
            y[K, 0, :] = self.spacing[K]  # init y on first row

            # init x on subsequent columns
            for J in range(1, self.n_col[K]):
                x[K, :, J] = x[K, 0, J - 1] + self.spacing[K]
            # init y on subsequent rows
            for I in range(1, self.n_row[K]):
                y[K, I, :] = y[K, I - 1, 0] + self.spacing[K]
        self.x = x[-1, :, :]
        self.y = y[-1, ::-1, :]

        # create the mask arrays for each iteration
        if mask is not None:
            assert mask.shape == (ht, wd), 'Mask is not same shape as image!'
            for K in range(nb_iter_max):
                x_idx = x[K, :, :].astype(DTYPE_i)
                y_idx = y[K, :, :].astype(DTYPE_i)
                mask_array[K, :, :] = mask[y_idx, x_idx].astype(DTYPE_f)
        else:
            mask_array[:, :, :] = 1
        self.mask = mask_array[-1, :, :]

        # move arrays to gpu
        self.x_d = gpuarray.to_gpu(x)
        self.y_d = gpuarray.to_gpu(y)
        self.mask_d = gpuarray.to_gpu(mask_array)

    def __call__(self, frame_a, frame_b):
        """Processes an image pair.

        Parameters
        ----------
        frame_a, frame_b : ndarray
            2D int, integers containing grey levels of the first and second frames.

        Returns
        -------
        u : array
            2D, the u velocity component, in pixels/seconds.
        v : array
            2D, the v velocity component, in pixels/seconds.

        """
        x_d = self.x_d
        y_d = self.y_d
        mask_d = self.mask_d
        u_d = None
        v_d = None
        u_previous_d = None
        v_previous_d = None
        shift_d = None
        strain_d = None
        dp_x_d = None
        dp_y_d = None

        # send masked frames to device
        frame_a_d, frame_b_d = self._mask_image(frame_a, frame_b)

        # create the correlation object
        self.corr = GPUCorrelation(frame_a_d, frame_b_d, self.nfft_x)

        # MAIN LOOP
        for k in range(self.nb_iter_max):
            logging.info('ITERATION {}'.format(k))

            if k == 0:
                # check if extended search area is used for first iteration
                extended_size = self.ws[k] * self.extend_ratio if self.extend_ratio is not None else None
            else:
                # TODO this should take care of more of the arguments
                extended_size = None
                shift_d, strain_d = self._get_corr_arguments(dp_x_d, dp_y_d, k)

            # get window displacement to subpixel accuracy
            i_peak, j_peak = self.corr(self.ws[k], self.overlap_ratio, extended_size=extended_size, d_shift=shift_d, d_strain=strain_d)

            # update the field with new values
            u_d, v_d = self._update_values(dp_x_d, dp_y_d, mask_d, i_peak, j_peak, k)
            self._log_residual(i_peak, j_peak)

            # VALIDATION
            if k == 0 and self.trust_1st_iter:
                logging.info('No validation--trusting 1st iteration.')
            else:
                u_d, v_d = self._validate_fields(u_d, v_d, x_d, y_d, u_previous_d, v_previous_d, k)

            # NEXT ITERATION
            # go to next iteration: compute the predictors dpx and dpy from the current displacements
            if k < self.nb_iter_max - 1:
                u_previous_d = u_d
                v_previous_d = v_d
                dp_x_d, dp_y_d = self._get_next_iteration_prediction(u_d, v_d, x_d, y_d, k)

                logging.info('[DONE] -----> going to iteration {}.\n'.format(k + 1))

        u_last_d = u_d
        v_last_d = v_d
        u = (u_last_d / self.dt).get()
        v = (v_last_d / -self.dt).get()  # TODO clarify justification for this negation

        logging.info('[DONE]\n')

        frame_a_d.gpudata.free()
        frame_b_d.gpudata.free()

        return u, v

    @property
    def coords(self):
        return self.x, self.y

    @property
    def s2n(self):
        if self.sig2noise is not None:
            s2n = self.sig2noise
        else:
            s2n = self.corr.sig2noise_ratio(method=self.sig2noise_method)
        return s2n

    def _mask_image(self, frame_a, frame_b):
        """Mask the images before sending to device."""
        _check_inputs(frame_a, frame_b, dim=2)

        if self.im_mask is not None:
            # TODO consider accepting an ndarray into gpu_mask
            frame_a_d = gpu_mask(gpuarray.to_gpu(frame_a.astype(DTYPE_i)), self.im_mask)
            frame_b_d = gpu_mask(gpuarray.to_gpu(frame_b.astype(DTYPE_i)), self.im_mask)
        else:
            frame_a_d = gpuarray.to_gpu(frame_a.astype(DTYPE_i))
            frame_b_d = gpuarray.to_gpu(frame_b.astype(DTYPE_i))

        return frame_a_d, frame_b_d

    # TODO this should not depend on k
    def _validate_fields(self, u_d, v_d, x_d, y_d, u_previous_d, v_previous_d, k):
        _check_inputs(u_d, v_d, array_type=gpuarray.GPUArray, dtype=DTYPE_f, dim=2)

        m, n = u_d.shape

        if self.val_tols[0] is not None and self.nb_validation_iter > 0:
            self.sig2noise = self.corr.sig2noise_ratio(method=self.sig2noise_method, width=self.s2n_width)

        for i in range(self.nb_validation_iter):
            # get list of places that need to be validated
            # TODO validation should be done on one field at a time
            val_list, u_mean_d, v_mean_d = gpu_validation(u_d.copy(), v_d.copy(), m, n, self.ws[k], self.sig2noise,
                                                          *self.val_tols)

            # do the validation
            n_val = m * n - np.sum(val_list)
            if n_val > 0:
                logging.info('Validating {} out of {} vectors ({:.2%}).'.format(n_val, m * n, n_val / (m * n)))

                # TODO can simplify this to not require u_previous
                overlap = self.ws - self.spacing
                u_d, v_d = gpu_replace_vectors(x_d, y_d, u_d, v_d, u_previous_d, v_previous_d, val_list, u_mean_d,
                                               v_mean_d, k, self.n_row, self.n_col, self.ws, overlap)

                logging.info('[DONE]\n')
            else:
                logging.info('No invalid vectors!')

        return u_d, v_d

    def _get_corr_arguments(self, dp_x_d, dp_y_d, k):
        """Returns the shift and strain arguments to the correlation class."""
        _check_inputs(dp_x_d, dp_y_d, array_type=gpuarray.GPUArray, dtype=DTYPE_f, dim=2)

        m, n = dp_x_d.shape
        strain_d = None

        # compute the shift
        shift_d = gpuarray.empty((2, m, n), dtype=DTYPE_f)
        shift_d[0, :, :] = dp_x_d
        shift_d[1, :, :] = dp_y_d

        # compute the strain rate
        if self.deform:
            strain_d = gpu_strain(dp_x_d, dp_y_d, self.spacing[k])

        return shift_d, strain_d

    def _get_next_iteration_prediction(self, u_d, v_d, x_d, y_d, k):
        """Returns the velocity field to begin the next iteration."""
        _check_inputs(u_d, v_d, array_type=gpuarray.GPUArray, dtype=DTYPE_f, dim=2)

        # interpolate if dimensions do not agree
        if self.ws[k + 1] != self.ws[k]:
            # TODO can avoid defining these variables?
            u_next_d = gpuarray.zeros((int(self.n_row[k + 1]), int(self.n_col[k + 1])), dtype=DTYPE_f)
            v_next_d = gpuarray.zeros((int(self.n_row[k + 1]), int(self.n_col[k + 1])), dtype=DTYPE_f)

            # TODO what is this?
            v_list = np.ones((self.n_row[-1], self.n_col[-1]), dtype=bool)

            # interpolate velocity onto next iterations grid. Then use it as the predictor for the next step
            # TODO this should be private class method.
            # TODO this should be refactored to return a consistent sized array
            overlap = self.ws - self.spacing
            u_d = gpu_interpolate_surroundings(x_d, y_d, u_d, u_next_d, v_list, self.n_row, self.n_col, self.ws,
                                               overlap, k)
            v_d = gpu_interpolate_surroundings(x_d, y_d, v_d, v_next_d, v_list, self.n_row, self.n_col, self.ws,
                                               overlap, k)

        if self.smooth:
            dp_x_d = gpu_smooth(u_d, s=self.smoothing_par)
            dp_y_d = gpu_smooth(v_d, s=self.smoothing_par)
        else:
            dp_x_d = u_d.copy()
            dp_y_d = v_d.copy()

        return dp_x_d, dp_y_d

    def _update_values(self, dp_x_d, dp_y_d, mask_d, i_peak, j_peak, k):
        """Updates the velocity values after each iteration."""
        if dp_x_d == dp_y_d is None:
            # TODO need variable self.field_shape
            dp_x_d = gpuarray.zeros((int(self.n_row[k]), int(self.n_col[k])), dtype=DTYPE_f)
            dp_y_d = gpuarray.zeros((int(self.n_row[k]), int(self.n_col[k])), dtype=DTYPE_f)
        else:
            _check_inputs(dp_x_d, dp_y_d, array_type=gpuarray.GPUArray, dtype=DTYPE_f, dim=2)
        _check_inputs(i_peak, j_peak, array_type=np.ndarray, dtype=DTYPE_f, dim=2)
        size = DTYPE_i(dp_x_d.size)

        u_d = gpuarray.empty_like(dp_x_d, dtype=DTYPE_f)
        v_d = gpuarray.empty_like(dp_x_d, dtype=DTYPE_f)
        i_peak_d = gpuarray.to_gpu(i_peak)
        j_peak_d = gpuarray.to_gpu(j_peak)
        # TODO this should be on device already
        f6_tmp_d = mask_d[k, :self.n_row[k], 0:self.n_col[k]].copy()

        mod_update = SourceModule("""
            __global__ void update_values(float *f_new, float *f_old, float *peak, float *mask, int size)
            {
                // u_new : output argument
    
                int w_idx = blockIdx.x * blockDim.x + threadIdx.x;
                if (w_idx >= size) {return;}
    
                f_new[w_idx] = (f_old[w_idx] + peak[w_idx]) * mask[w_idx];
            }
            """)
        block_size = 32
        x_blocks = int(self.n_col[k] * self.n_row[k] // block_size + 1)
        update_values = mod_update.get_function("update_values")
        # TODO investigate why the i- and j-peaks are flipped
        update_values(u_d, dp_x_d, j_peak_d, f6_tmp_d, size, block=(block_size, 1, 1), grid=(x_blocks, 1))
        update_values(v_d, dp_y_d, i_peak_d, f6_tmp_d, size, block=(block_size, 1, 1), grid=(x_blocks, 1))

        i_peak_d.gpudata.free()
        j_peak_d.gpudata.free()

        return u_d, v_d

    @staticmethod
    def _log_residual(i_peak, j_peak):
        """Normalizes the residual by the maximum quantization error of 0.5 pixel."""
        _check_inputs(i_peak, j_peak, array_type=np.ndarray, dtype=DTYPE_f, dim=2)

        try:
            normalized_residual = sqrt(np.sum(i_peak ** 2 + j_peak ** 2) / i_peak.size) / 0.5
            logging.info("[DONE]--Normalized residual : {}.\n".format(normalized_residual))
        except OverflowError:
            logging.warning('[DONE]--Overflow in residuals.\n')
            normalized_residual = np.nan

        return normalized_residual


# TODO should share arguments with piv_gpu()
def get_field_shape(image_size, window_size, overlap_ratio):
    """Returns the shape of the resulting velocity field.

    Given the image size, the interrogation window size and the overlap size, it is possible to calculate the number of
    rows and columns of the resulting flow field.

    Parameters
    ----------
    image_size : tuple
        (ht, wd), pixel size of the image first element is number of rows, second element is the number of columns.
    window_size : int
        Size of the interrogation windows.
    overlap_ratio : float
        Ratio by which two adjacent interrogation windows overlap.

    Returns
    -------
    n_row, n_col : int
        The shape of the resulting flow field.

    """
    assert window_size >= 8, "Window size is too small."
    assert window_size % 8 == 0, "Window size must be a multiple of 8."
    assert 0 < overlap_ratio < 1, 'overlap_ratio must be a float between 0 and 1.'

    spacing = window_size * overlap_ratio
    n_row = DTYPE_i((image_size[0] - spacing) // spacing)
    n_col = DTYPE_i((image_size[1] - spacing) // spacing)
    return n_row, n_col


def get_field_coords(field_shape, window_size, overlap_ratio):
    """Returns the coordinates of the resulting velocity field.

    Parameters
    ----------
    field_shape : tuple
        (n_row, n_col), the shape of the resulting flow field.
    window_size : int
        Size of the interrogation windows.
    overlap_ratio : float
        Ratio by which two adjacent interrogation windows overlap.

    Returns
    -------
    x, y : ndarray
        2D float, the shape of the resulting flow field

    """
    assert window_size >= 8, "Window size is too small."
    assert window_size % 8 == 0, "Window size must be a multiple of 8."
    assert 0 < overlap_ratio < 1, 'overlap_ratio should be a float between 0 and 1.'
    n_row, n_col = field_shape

    spacing = window_size * overlap_ratio
    x = np.tile(np.linspace(window_size / 2, window_size / 2 + spacing * (n_col - 1), n_col, dtype=DTYPE_f), (n_row, 1))
    y = np.tile(np.linspace(window_size / 2, window_size / 2 + spacing * (n_row - 1), n_row, dtype=DTYPE_f),
                (n_col, 1)).T

    return x, y


def gpu_mask(frame_d, mask_d):
    """Multiply two integer-type arrays.

    Parameters
    ----------
    frame_d : GPUArray
        2D int, frame to be masked.
    mask_d : GPUArray
        2D int, mask to apply to frame.

    Returns
    -------
    GPUArray
        2D int, masked frame.

    """
    _check_inputs(frame_d, mask_d, array_type=gpuarray.GPUArray, dtype=DTYPE_i, dim=2)

    size = DTYPE_f(frame_d.size)
    m, n = frame_d.shape
    frame_masked_d = gpuarray.empty_like(frame_d, dtype=DTYPE_i)

    mod_update = SourceModule("""
        __global__ void mask_frame(int *frame_masked, int *frame, int *mask, int size)
        {
            // frame_masked : output argument
        
            int w_idx = blockIdx.x * blockDim.x + threadIdx.x;
            if (w_idx >= size) {return;}

            frame_masked[w_idx] = frame[w_idx] * mask[w_idx];
        }
        """)
    block_size = 32
    x_blocks = int(n * m // block_size + 1)
    mask_frame = mod_update.get_function("mask_frame")
    mask_frame(frame_masked_d, frame_d, mask_d, size, block=(block_size, 1, 1), grid=(x_blocks, 1))

    return frame_masked_d


# TODO consider operating on u and v separately. This way non-uniform meshes can be accommodated.
def gpu_strain(u_d, v_d, spacing=1):
    """Computes the full strain rate tensor.

    Parameters
    ----------
    u_d, v_d : GPUArray
        2D float, velocity fields.
    spacing : float
        Spacing between nodes.

    Returns
    -------
    GPUArray
        3D float, full strain tensor of the velocity fields. (4, m, n) corresponds to (u_x, u_y, v_x and v_y).

    """
    _check_inputs(u_d, v_d, array_type=gpuarray.GPUArray, dtype=DTYPE_f)
    assert spacing > 0, 'Spacing must be greater than 0.'

    m, n = u_d.shape
    strain_d = gpuarray.empty((4, m, n), dtype=DTYPE_f)

    mod = SourceModule("""
    __global__ void gradient(float *strain, float *u, float *v, float h, int m, int n)
    {
        // strain : output argument
    
        const int i = blockIdx.x * blockDim.x + threadIdx.x;
        int size = m * n;
        if (i >= size) {return;}

        int row = i / n;
        int col = i % n;

        // x-axis
        // first column
        if (col == 0) {strain[row * n] = (u[row * n + 1] - u[row * n]) / h;  // u_x
        strain[size * 2 + row * n] = (v[row * n + 1] - v[row * n]) / h;  // v_x

        // last column
        } else if (col == n - 1) {strain[(row + 1) * n - 1] = (u[(row + 1) * n - 1] - u[(row + 1) * n - 2]) / h;  // u_x
        strain[size * 2 + (row + 1) * n - 1] = (v[(row + 1) * n - 1] - v[(row + 1) * n - 2]) / h;  // v_x

        // main body
        } else {strain[row * n + col] = (u[row * n + col + 1] - u[row * n + col - 1]) / 2 / h;  // u_x
        strain[size * 2 + row * n + col] = (v[row * n + col + 1] - v[row * n + col - 1]) / 2 / h;  // v_x
        }

        // y-axis
        // first row
        if (row == 0) {strain[size + col] = (u[n + col] - u[col]) / h;  // u_y
        strain[size * 3 + col] = (v[n + col] - v[col]) / h;  // v_y

        // last row
        } else if (row == m - 1) {strain[size + n * (m - 1) + col] = (u[n * (m - 1) + col] - u[n * (m - 2) + col]) / h;  // u_y
        strain[size * 3 + n * (m - 1) + col] = (v[n * (m - 1) + col] - v[n * (m - 2) + col]) / h;  // v_y

        // main body
        } else {strain[size + row * n + col] = (u[(row + 1) * n + col] - u[(row - 1) * n + col]) / 2 / h;  // u_y
        strain[size * 3 + row * n + col] = (v[(row + 1) * n + col] - v[(row - 1) * n + col]) / 2 / h;  // v_y
        }
    }
    """)
    block_size = 32
    n_blocks = int((m * n) // block_size + 1)
    gradient = mod.get_function('gradient')
    gradient(strain_d, u_d, v_d, DTYPE_f(spacing), DTYPE_i(m), DTYPE_i(n), block=(block_size, 1, 1), grid=(n_blocks, 1))

    return strain_d


def gpu_round(f_d):
    """Rounds each element in the gpu array.

    Parameters
    ----------
    f_d : GPUArray
        Array to be rounded.

    Returns
    -------
    GPUArray
        Float, same size as f_d. Floored values of f_d.

    """
    assert type(f_d) == gpuarray.GPUArray, 'Input must a GPUArray.'
    assert f_d.dtype == DTYPE_f, 'Input array must float type.'

    n = DTYPE_i(f_d.size)
    f_round_d = gpuarray.empty_like(f_d)

    mod_round = SourceModule("""
    __global__ void round_gpu(float *dest, float *src, int n)
    {
        // dest : output argument

        int t_id = blockIdx.x * blockDim.x + threadIdx.x;
        if(t_id >= n){return;}

        dest[t_id] = roundf(src[t_id]);
    }
    """)
    block_size = 32
    x_blocks = int(n // block_size + 1)
    round_gpu = mod_round.get_function("round_gpu")
    round_gpu(f_round_d, f_d, n, block=(block_size, 1, 1), grid=(x_blocks, 1))

    return f_round_d


def gpu_smooth(f_d, s=0.5):
    """Smooths a scalar field stored as a GPUArray.

    Parameters
    ----------
    f_d : GPUArray
        Field to be smoothed.
    s : int
        Smoothing parameter in smoothn.

    Returns
    -------
    GPUArray
        Float, same size as f_d. Smoothed field.

    """
    assert type(f_d) == gpuarray.GPUArray, 'Input must a GPUArray.'
    assert f_d.dtype == DTYPE_f, 'Input array must float type.'
    assert len(f_d.shape), 'Inputs must be 2D.'
    assert s > 0, 'Smoothing parameter must be greater than 0.'

    f = f_d.get()
    f_smooth_d = gpuarray.to_gpu(smoothn(f, s=s)[0].astype(DTYPE_f))

    return f_smooth_d


# TODO this shouldn't depend on k, or else there should be a public version which doesn't
def gpu_replace_vectors(x_d, y_d, u_d, v_d, u_previous_d, v_previous_d, validation_list, u_mean_d, v_mean_d, k, n_row,
                        n_col, w, overlap):
    """Replace spurious vectors by the mean or median of the surrounding points.

    Parameters
    ----------
    x_d, y_d : GPUArray
        3D float, grid coordinates
    u_d, v_d : GPUArray
        2D float, velocities at current iteration
    u_previous_d, v_previous_d
        2D float, velocities at previous iteration
    validation_list : ndarray
        2D int, indicates which values must be validate. 1 indicates no validation needed, 0 indicates validation is needed
    u_mean_d, v_mean_d : GPUArray
        3D float, mean velocity surrounding each point
    k : int
        main loop iteration count
    n_row, n_col : ndarray
        int, number of rows an columns in each main loop iteration
    w : ndarray
        int, pixels between interrogation windows
    overlap : ndarray
        int, ratio of overlap between interrogation windows

    """
    # TODO refactor validation_location to be more clear
    # change validation_list to type boolean and invert it. Now - True indicates that point needs to be validated, False indicates no validation
    validation_location = np.invert(validation_list.astype(bool))

    # first iteration, just replace with mean velocity
    if k == 0:
        # get indices and send them to the gpu
        indices = np.where(validation_location.flatten() == 1)[0].astype(DTYPE_i)
        indices_d = gpuarray.to_gpu(indices)

        # get mean velocity at validation points
        u_tmp_d = _gpu_array_index(u_mean_d, indices_d, DTYPE_f)
        v_tmp_d = _gpu_array_index(v_mean_d, indices_d, DTYPE_f)

        # update the velocity values
        # TODO copy() in wrong scope
        _gpu_index_update(u_d, u_tmp_d, indices_d)
        _gpu_index_update(v_d, v_tmp_d, indices_d)

        # you don't need to do all these calculations. Could write a function that only does it for the ones that have been validated

    # case if different dimensions: interpolation using previous iteration
    elif k > 0 and (n_row[k] != n_row[k - 1] or n_col[k] != n_col[k - 1]):
        # TODO this should be private class method.
        # TODO this should be refactored to return a consistent sized array
        # TODO this functions needs to be split--it currently does both interpolation and vector replacement
        u_d = gpu_interpolate_surroundings(x_d, y_d, u_previous_d, u_d, validation_location, n_row, n_col, w, overlap,
                                           k - 1)
        v_d = gpu_interpolate_surroundings(x_d, y_d, v_previous_d, v_d, validation_location, n_row, n_col, w, overlap,
                                           k - 1)

    # case if same dimensions
    elif k > 0 and (n_row[k] == n_row[k - 1] or n_col[k] == n_col[k - 1]):
        # get indices and send them to the gpu
        indices = np.where(validation_location.flatten() == 1)[0].astype(DTYPE_i)
        indices_d = gpuarray.to_gpu(indices)

        # update the velocity values with the previous values.
        # This is essentially a bilinear interpolation when the value is right on top of the other.
        # could replace with the mean of the previous values surrounding the point
        # TODO copy() in wrong scope
        u_tmp_d = _gpu_array_index(u_previous_d, indices_d, DTYPE_f)
        v_tmp_d = _gpu_array_index(v_previous_d, indices_d, DTYPE_f)
        _gpu_index_update(u_d, u_tmp_d, indices_d)
        _gpu_index_update(v_d, v_tmp_d, indices_d)

    return u_d, v_d


# TODO this shouldn't depend on k
def gpu_interpolate_surroundings(x_d, y_d, f_d, f_new_d, v_list, n_row, n_col, w, overlap, k):
    """Interpolate a point based on the surroundings.

    Parameters
    ----------
    x_d, y_d : GPUArray
        2D float, grid coordinates
    f_d, f_new_d : GPUArray
        2D float, data to be interpolated
    v_list : ndarray
        2D bool, indicates which values must be validated. True means it needs to be validated, False means no validation is needed.
    n_row, n_col : ndarray
        2D, number rows and columns in each iteration
    w : ndarray
       int,  number of pixels between interrogation windows
    overlap : ndarray
        int, overlap of the interrogation windows
    k : int
        current iteration

    Mark's note: Separate validation list into multiple lists for each region

    """
    interior_ind_x_d = None
    interior_ind_y_d = None
    interior_ind_d = None
    top_ind_d = None
    bottom_ind_d = None
    left_ind_d = None
    right_ind_d = None

    # set all sides to false for interior points
    interior_list = np.copy(v_list[:n_row[k + 1], :n_col[k + 1]]).astype('bool')
    interior_list[0, :] = 0
    interior_list[-1, :] = 0
    interior_list[:, 0] = 0
    interior_list[:, -1] = 0

    # define array with the indices of the points to be validated
    interior_ind = np.where(interior_list.flatten())[0].astype(DTYPE_i)
    if interior_ind.size != 0:
        # get the x and y indices of the interior points that must be validated
        interior_ind_x = interior_ind // n_col[k + 1]
        interior_ind_y = interior_ind % n_col[k + 1]
        interior_ind_x_d = gpuarray.to_gpu(interior_ind_x)
        interior_ind_y_d = gpuarray.to_gpu(interior_ind_y)

        # use this to update the final d_F array after the interpolation
        interior_ind_d = gpuarray.to_gpu(interior_ind)

    # only select sides and remove corners
    top_list = np.copy(v_list[0, :n_col[k + 1]])
    top_list[0] = 0
    top_list[-1] = 0
    top_ind = np.where(top_list.flatten())[0].astype(DTYPE_i)
    if top_ind.size != 0:
        top_ind_d = gpuarray.to_gpu(top_ind)

    bottom_list = np.copy(v_list[n_row[k + 1] - 1, :n_col[k + 1]])
    bottom_list[0] = 0
    bottom_list[-1] = 0
    bottom_ind = np.where(bottom_list.flatten())[0].astype(DTYPE_i)
    if bottom_ind.size != 0:
        bottom_ind_d = gpuarray.to_gpu(bottom_ind)

    left_list = np.copy(v_list[:n_row[k + 1], 0])
    left_list[0] = 0
    left_list[-1] = 0
    left_ind = np.where(left_list.flatten())[0].astype(DTYPE_i)
    if left_ind.size != 0:
        left_ind_d = gpuarray.to_gpu(left_ind)

    right_list = np.copy(v_list[:n_row[k + 1], n_col[k + 1] - 1])
    right_list[0] = 0
    right_list[-1] = 0
    right_ind = np.where(right_list.flatten())[0].astype(DTYPE_i)
    if right_ind.size != 0:
        right_ind_d = gpuarray.to_gpu(right_ind)

    # --------------------------INTERIOR GRID---------------------------------
    if interior_ind.size != 0:
        # get gpu data for position now
        low_x_d, high_x_d = _f_dichotomy_gpu(x_d[k:k + 2, :, 0].copy(), k, "x_axis", interior_ind_x_d, w, overlap,
                                             n_row, n_col)
        low_y_d, high_y_d = _f_dichotomy_gpu(y_d[k:k + 2, 0, :].copy(), k, "y_axis", interior_ind_y_d, w, overlap,
                                             n_row, n_col)

        # get indices surrounding the position now
        x1_d = _gpu_array_index(x_d[k, :n_row[k], 0].copy(), low_x_d, DTYPE_f)
        x2_d = _gpu_array_index(x_d[k, :n_row[k], 0].copy(), high_x_d, DTYPE_f)
        y1_d = _gpu_array_index(y_d[k, 0, :n_col[k]].copy(), low_y_d, DTYPE_f)
        y2_d = _gpu_array_index(y_d[k, 0, :n_col[k]].copy(), high_y_d, DTYPE_f)
        x_c_d = _gpu_array_index(x_d[k + 1, :n_row[k + 1], 0].copy(), interior_ind_x_d, DTYPE_f)
        y_c_d = _gpu_array_index(y_d[k + 1, 0, :n_col[k + 1]].copy(), interior_ind_y_d, DTYPE_f)

        # get indices for the function values at each spot surrounding the validation points.
        f1_ind_d = low_x_d * n_col[k] + low_y_d
        f2_ind_d = low_x_d * n_col[k] + high_y_d
        f3_ind_d = high_x_d * n_col[k] + low_y_d
        f4_ind_d = high_x_d * n_col[k] + high_y_d

        # return the values of the function surrounding the validation point
        f1_d = _gpu_array_index(f_d, f1_ind_d, DTYPE_f)
        f2_d = _gpu_array_index(f_d, f2_ind_d, DTYPE_f)
        f3_d = _gpu_array_index(f_d, f3_ind_d, DTYPE_f)
        f4_d = _gpu_array_index(f_d, f4_ind_d, DTYPE_f)

        # Do interpolation
        interior_bilinear_d = bilinear_interp_gpu(x1_d, x2_d, y1_d, y2_d, x_c_d, y_c_d, f1_d, f2_d, f3_d, f4_d)

        # Update values. Return a tmp array and destroy after to avoid GPU memory leak.
        ib_tmp_d = f_new_d.copy()
        _gpu_index_update(ib_tmp_d, interior_bilinear_d, interior_ind_d)
        f_new_d[:] = ib_tmp_d

    # ------------------------------SIDES-----------------------------------
    if top_ind.size > 0:
        # get now position and surrounding points
        low_y_d, high_y_d = _f_dichotomy_gpu(y_d[k:k + 2, 0, :].copy(), k, "y_axis", top_ind_d, w, overlap, n_row,
                                             n_col)

        # Get values to compute interpolation
        y1_d = _gpu_array_index(y_d[k, 0, :].copy(), low_y_d, DTYPE_f)
        y2_d = _gpu_array_index(y_d[k, 0, :].copy(), high_y_d, DTYPE_f)
        y_c_d = _gpu_array_index(y_d[k + 1, 0, :].copy(), top_ind_d, DTYPE_f)

        # return the values of the function surrounding the validation point
        f1_d = _gpu_array_index(f_d[0, :].copy(), low_y_d, DTYPE_f)
        f2_d = _gpu_array_index(f_d[0, :].copy(), high_y_d, DTYPE_f)

        # do interpolation
        top_linear_d = linear_interp_gpu(y1_d, y2_d, y_c_d, f1_d, f2_d)

        # Update values. Return a tmp array and destroy after to avoid GPU memory leak.
        tmp_tl_d = f_new_d[0, :].copy()
        _gpu_index_update(tmp_tl_d, top_linear_d, top_ind_d)
        f_new_d[0, :] = tmp_tl_d

    # BOTTOM
    if bottom_ind.size > 0:
        # get position data
        low_y_d, high_y_d = _f_dichotomy_gpu(y_d[k:k + 2, 0, :].copy(), k, "y_axis", bottom_ind_d, w, overlap, n_row,
                                             n_col)

        # Get values to compute interpolation
        y1_d = _gpu_array_index(y_d[k, int(n_row[k] - 1), :].copy(), low_y_d, DTYPE_f)
        y2_d = _gpu_array_index(y_d[k, int(n_row[k] - 1), :].copy(), high_y_d, DTYPE_f)
        y_c_d = _gpu_array_index(y_d[k + 1, int(n_row[k + 1] - 1), :].copy(), bottom_ind_d, DTYPE_f)

        # return the values of the function surrounding the validation point
        f1_d = _gpu_array_index(f_d[-1, :].copy(), low_y_d, DTYPE_f)
        f2_d = _gpu_array_index(f_d[-1, :].copy(), high_y_d, DTYPE_f)

        # do interpolation
        bottom_linear_d = linear_interp_gpu(y1_d, y2_d, y_c_d, f1_d, f2_d)

        # Update values. Return a tmp array and destroy after to avoid GPU memory leak.
        bl_tmp_d = f_new_d[-1, :].copy()
        _gpu_index_update(bl_tmp_d, bottom_linear_d, bottom_ind_d)
        f_new_d[-1, :] = bl_tmp_d

    # LEFT
    if left_ind.size > 0:
        # get position data
        low_x_d, high_x_d = _f_dichotomy_gpu(x_d[k:k + 2, :, 0].copy(), k, "x_axis", left_ind_d, w, overlap, n_row,
                                             n_col)

        # Get values to compute interpolation
        x1_d = _gpu_array_index(x_d[k, :, 0].copy(), low_x_d, DTYPE_f)
        x2_d = _gpu_array_index(x_d[k, :, 0].copy(), high_x_d, DTYPE_f)
        x_c_d = _gpu_array_index(x_d[k + 1, :, 0].copy(), left_ind_d, DTYPE_f)

        # return the values of the function surrounding the validation point
        f1_d = _gpu_array_index(f_d[:, 0].copy(), low_x_d, DTYPE_f)
        f2_d = _gpu_array_index(f_d[:, 0].copy(), high_x_d, DTYPE_f)

        # do interpolation
        left_linear_d = linear_interp_gpu(x1_d, x2_d, x_c_d, f1_d, f2_d)

        # Update values. Return a tmp array and destroy after to avoid GPU memory leak.
        ll_tmp_d = f_new_d[:, 0].copy()
        _gpu_index_update(ll_tmp_d, left_linear_d, left_ind_d)
        f_new_d[:, 0] = ll_tmp_d

    # RIGHT
    if right_ind.size > 0:
        # get position data
        low_x_d, high_x_d = _f_dichotomy_gpu(x_d[k:k + 2, :, 0].copy(), k, "x_axis", right_ind_d, w, overlap, n_row,
                                             n_col)

        # Get values to compute interpolation
        x1_d = _gpu_array_index(x_d[k, :, int(n_col[k] - 1)].copy(), low_x_d, DTYPE_f)
        x2_d = _gpu_array_index(x_d[k, :, int(n_col[k] - 1)].copy(), high_x_d, DTYPE_f)
        x_c_d = _gpu_array_index(x_d[k + 1, :, int(n_col[k + 1] - 1)].copy(), right_ind_d, DTYPE_f)

        # return the values of the function surrounding the validation point
        f1_d = _gpu_array_index(f_d[:, -1].copy(), low_x_d, DTYPE_f)
        f2_d = _gpu_array_index(f_d[:, -1].copy(), high_x_d, DTYPE_f)

        # do interpolation
        right_linear_d = linear_interp_gpu(x1_d, x2_d, x_c_d, f1_d, f2_d)

        # Update values. Return a tmp array and destroy after to avoid GPU memory leak.
        tmp_rl_d = f_new_d[:, -1].copy()
        _gpu_index_update(tmp_rl_d, right_linear_d, right_ind_d)
        f_new_d[:, -1] = tmp_rl_d

    # ----------------------------CORNERS-----------------------------------
    # top left
    if v_list[0, 0] == 1:
        f_new_d[0, 0] = f_d[0, 0]
    # top right
    if v_list[0, n_col[k + 1] - 1] == 1:
        f_new_d[0, -1] = f_d[0, -1]
    # bottom left
    if v_list[n_row[k + 1] - 1, 0] == 1:
        f_new_d[-1, 0] = f_d[-1, 0]
    # bottom right
    if v_list[n_row[k + 1] - 1, n_col[k + 1] - 1] == 1:
        f_new_d[-1, -1] = f_d[-1, int(n_col[k] - 1)]

    return f_new_d


def _f_dichotomy_gpu(range_d, k, side, pos_index_d, w, overlap, n_row, n_col):
    """
    Look for the position of the vectors at the previous iteration that surround the current point in the frame
    you want to validate. Returns the low and high index of the points from the previous iteration on either side of
    the point in the current iteration that needs to be validated.

    Parameters
    ----------
    range_d : GPUArray
        2D float, The x or y locations along the grid for the current and next iteration.
        Example:
        For side = x_axis then the input looks like d_range = x_d[K:K+2, :, 0].copy()
        For side = y_axis then the input looks like d_range = y_d[K:K+2, 0, :].copy()
    k : int
        the iteration you want to use to validate. Typically the previous iteration from the
        one that the code is in now. (1st index for F).
    side : string
        the axis of interest : can be either 'x_axis' or 'y_axis'
    pos_index_d : GPUArray
        1D int, index of the point in the frame you want to validate (along the axis 'side').
    w : ndarray
        1D int, array of window sizes
    overlap : ndarray
        1D int, overlap in number of pixels
    n_row, n_col : ndarray
        1D int, number of rows and columns in the F dataset in each iteration

    Returns
    -------
    low_d : GPUArray
        1D int, largest index at the iteration K along the 'side' axis so that the position of index low in the frame is less than or equal to pos_now.
    high_d : GPUArray
        1D int, smallest index at the iteration K along the 'side' axis so that the position of index low in the frame is greater than or equal to pos_now.

    """
    # Define values needed for the calculations
    w_a = DTYPE_f(w[k + 1])
    w_b = DTYPE_f(w[k])
    k = DTYPE_i(k)
    n = DTYPE_i(pos_index_d.size)
    n_row = DTYPE_i(n_row)
    n_col = DTYPE_i(n_col)

    # create GPU data
    low_d = gpuarray.zeros_like(pos_index_d, dtype=DTYPE_i)
    high_d = gpuarray.zeros_like(pos_index_d, dtype=DTYPE_i)

    mod_f_dichotomy = SourceModule("""
    __global__ void f_dichotomy_x(float *x, int *low, int *high, int K, int *pos_index, float w_a, float w_b, float dxa, float dxb, int Nrow, int NrowMax, int n)
    {
        int w_idx = blockIdx.x*blockDim.x + threadIdx.x;

        if(w_idx >= n){return;}

        // initial guess for low and high values
        low[w_idx] = (int)floorf((w_a/2. - w_b/2. + pos_index[w_idx]*dxa) / dxb);
        high[w_idx] = low[w_idx] + 1*(x[NrowMax + pos_index[w_idx]] != x[low[w_idx]]);

        // if lower than lowest
        low[w_idx] = low[w_idx] * (low[w_idx] >= 0);
        high[w_idx] = high[w_idx] * (low[w_idx] >= 0);

        // if higher than highest
        low[w_idx] = low[w_idx] + (Nrow - 1 - low[w_idx])*(high[w_idx] > Nrow - 1);
        high[w_idx] = high[w_idx] + (Nrow - 1 - high[w_idx])*(high[w_idx] > Nrow - 1);
    }

    __global__ void f_dichotomy_y(float *y, int *low, int *high, int K, int *pos_index, float w_a, float w_b, float dya, float dyb, int Ncol, int NcolMax, int n)
    {
        int w_idx = blockIdx.x*blockDim.x + threadIdx.x;

        if(w_idx >= n){return;}

        low[w_idx] = (int)floorf((w_a/2. - w_b/2. + pos_index[w_idx]*dya) / dyb);
        high[w_idx] = low[w_idx] + 1*(y[NcolMax + pos_index[w_idx]] != y[low[w_idx]]);

        // if lower than lowest
        low[w_idx] = low[w_idx] * (low[w_idx] >= 0);
        high[w_idx] = high[w_idx] * (low[w_idx] >= 0);

        // if higher than highest
        low[w_idx] = low[w_idx] + (Ncol - 1 - low[w_idx])*(high[w_idx] > Ncol - 1);
        high[w_idx] = high[w_idx] + (Ncol - 1 - high[w_idx])*(high[w_idx] > Ncol - 1);
    }
    """)
    block_size = 32
    x_blocks = int(len(pos_index_d) // block_size + 1)

    if side == "x_axis":
        assert pos_index_d[-1].get() < n_row[
            k + 1], "Position index for validation point is outside the grid. Not possible - all points should be on the grid."
        dxa = DTYPE_f(w_a - overlap[k + 1])
        dxb = DTYPE_f(w_b - overlap[k])

        f_dichotomy_x = mod_f_dichotomy.get_function("f_dichotomy_x")
        f_dichotomy_x(range_d, low_d, high_d, k, pos_index_d, w_a, w_b, dxa, dxb, n_row[k], n_row[-1], n,
                      block=(block_size, 1, 1), grid=(x_blocks, 1))

    elif side == "y_axis":
        assert pos_index_d[-1].get() < n_col[
            k + 1], "Position index for validation point is outside the grid. Not possible - all points should be on the grid."
        dya = DTYPE_f(w_a - overlap[k + 1])
        dyb = DTYPE_f(w_b - overlap[k])

        f_dichotomy_y = mod_f_dichotomy.get_function("f_dichotomy_y")
        f_dichotomy_y(range_d, low_d, high_d, k, pos_index_d, w_a, w_b, dya, dyb, n_col[k], n_col[-1], n,
                      block=(block_size, 1, 1), grid=(x_blocks, 1))

    else:
        raise ValueError("Not a proper axis. Choose either x or y axis.")

    return low_d, high_d


def bilinear_interp_gpu(x1_d, x2_d, y1_d, y2_d, x_d, y_d, f1_d, f2_d, f3_d, f4_d):
    """Performs bilinear interpolation on the GPU."""
    n = DTYPE_i(len(x1_d))

    d_f = gpuarray.zeros_like(x1_d, dtype=DTYPE_f)

    mod_bi = SourceModule("""
    __global__ void bilinear_interp(float *f, float *x1, float *x2, float *y1, float *y2, float *x, float *y, float *f1, float *f2, float *f3, float *f4, int n)
    {
        // 1D grid of 1D blocks
        int idx = blockIdx.x * blockDim.x + threadIdx.x;

        if(idx >= n){return;}

        // avoid the points that are equal to each other

        float n1 = f1[idx] * (x2[idx]-x[idx]) * (y2[idx]-y[idx]);
        n1 = n1 * (float)(y1[idx] != y2[idx]) + f1[idx] * (float)(y1[idx] == y2[idx]) * (x2[idx]-x[idx]);
        n1 = n1 * (float)(x1[idx] != x2[idx]) + f1[idx] * (float)(x1[idx] == x2[idx]) * (y2[idx]-y[idx]);
        n1 = n1 * (float)((y1[idx] != y2[idx]) || (x1[idx] != x2[idx])) + f1[idx] * (float)((y1[idx] == y2[idx]) && (x1[idx] == x2[idx]));

        float n2 = f2[idx] * (x2[idx]-x[idx]) * (y[idx]-y1[idx]);
        n2 = n2 * (float)(x1[idx] != x2[idx]) + f2[idx] * (float)(x1[idx] == x2[idx]) * (y[idx]-y1[idx]);
        n2 = n2 * (float)(y1[idx] != y2[idx]);

        float n3 = f3[idx] * (x[idx]-x1[idx]) * (y2[idx]-y[idx]);
        n3 = n3 * (float)(y1[idx] != y2[idx]) + f3[idx] * (float)(y1[idx] == y2[idx]) * (x[idx] - x1[idx]);
        n3 = n3 * (float)(x1[idx] != x2[idx]) * (x1[idx] != x2[idx]);

        float n4 = f4[idx] * (x[idx]-x1[idx]) * (y[idx]-y1[idx]);
        n4 = n4 * (float)(y1[idx] != y2[idx]) * (float)(x1[idx] != x2[idx]);

        float numerator = n1 + n2 + n3 + n4;

        float denominator = (x2[idx]-x1[idx])*(y2[idx]-y1[idx]);
        denominator = denominator * (float)(x1[idx] != x2[idx]) + (y2[idx] - y1[idx]) * (float)(x1[idx] == x2[idx]);
        denominator = denominator * (float)(y1[idx] != y2[idx]) + (x2[idx] - x1[idx]) * (float)(y1[idx] == y2[idx]);
        denominator = denominator * (float)((y1[idx] != y2[idx]) || (x1[idx] != x2[idx])) + 1.0 * (float)((y1[idx] == y2[idx]) && (x1[idx] == x2[idx]));

        f[idx] = numerator / denominator;
    }
    """)
    block_size = 32
    x_blocks = int(len(x1_d) // block_size + 1)
    bilinear_interp = mod_bi.get_function("bilinear_interp")
    bilinear_interp(d_f, x1_d, x2_d, y1_d, y2_d, x_d, y_d, f1_d, f2_d, f3_d, f4_d, n, block=(block_size, 1, 1),
                    grid=(x_blocks, 1))

    return d_f


def linear_interp_gpu(x1_d, x2_d, x_d, f1_d, f2_d):
    """Returns the linear interpolation between two points."""
    n = DTYPE_i(len(x1_d))

    f_d = gpuarray.zeros_like(x1_d, dtype=DTYPE_f)

    mod_lin = SourceModule("""
    __global__ void linear_interp(float *f, float *x1, float *x2, float *x, float *f1, float *f2, int n)
    {
        // 1D grid of 1D blocks
        int idx = blockIdx.x*blockDim.x + threadIdx.x;

        if(idx >= n){return;}

        float tmp = ((x2[idx]-x[idx])/(x2[idx]-x1[idx]))*f1[idx] + ((x[idx]-x1[idx])/(x2[idx]-x1[idx]))*f2[idx];
        f[idx] = tmp * (float)(x2[idx] != x1[idx]) + f1[idx]*(float)(x2[idx] == x1[idx]) ;
    }
    """)
    block_size = 32
    x_blocks = int(len(x1_d) // block_size + 1)
    linear_interp = mod_lin.get_function("linear_interp")
    linear_interp(f_d, x1_d, x2_d, x_d, f1_d, f2_d, n, block=(block_size, 1, 1), grid=(x_blocks, 1))

    return f_d


# TODO shouldn't need to pass dtype
def _gpu_array_index(array_d, indices, dtype):
    """Allows for arbitrary index selecting with numpy arrays

    Parameters
    ----------
    array_d : GPUArray
        nD float or int, array to be selected from
    indices : GPUArray
        1D int, list of indexes that you want to index. If you are indexing more than 1 dimension, then make sure that this array is flattened.
    dtype : dtype
        either int32 or float 32. determines the datatype of the returned array

    Returns
    -------
    GPUArray
        nD float or int, values at the specified indexes.

    """
    # GPU will automatically flatten the input array. The indexing must reference the flattened GPU array.
    assert indices.ndim == 1, "Number of dimensions of indices is wrong. Should be equal to 1"
    assert type(array_d) == gpuarray.GPUArray, 'Input must be GPUArray.'
    assert (array_d.dtype == DTYPE_f) or (array_d.dtype == DTYPE_f), 'Input must have dtype float32 or int32.'

    # send data to the gpu
    return_values_d = gpuarray.zeros(indices.size, dtype=dtype)

    mod_array_index = SourceModule("""
    __global__ void array_index_float(float *return_values, float *array, int *return_list, int r_size )
    {
        // 1D grid of 1D blocks
        int t_idx = blockIdx.x * blockDim.x + threadIdx.x;
        if(t_idx >= r_size){return;}

        return_values[t_idx] = array[return_list[t_idx]];
    }

    __global__ void array_index_int(float *array, int *return_values, int *return_list, int r_size )
    {
        // 1D grid of 1D blocks
        int t_idx = blockIdx.x*blockDim.x + threadIdx.x;
        if(t_idx >= r_size){return;}

        return_values[t_idx] = (int)array[return_list[t_idx]];
    }
    """)
    block_size = 32
    r_size = DTYPE_i(indices.size)
    x_blocks = int(r_size // block_size + 1)

    if dtype == DTYPE_f:
        array_index = mod_array_index.get_function("array_index_float")
        array_index(return_values_d, array_d, indices, r_size, block=(block_size, 1, 1), grid=(x_blocks, 1))
    else:
        array_index = mod_array_index.get_function("array_index_int")
        array_index(return_values_d, array_d, indices, r_size, block=(block_size, 1, 1), grid=(x_blocks, 1))

    return return_values_d


def _gpu_index_update(dest_d, values_d, indices_d):
    """Allows for arbitrary index selecting with numpy arrays.

    Parameters
    ----------
    dest_d : GPUArray
       nD float, array to be updated with new values
    values_d : GPUArray
        1D float, array containing the values to be updated in the destination array
    indices_d : GPUArray
        1D int, array of indices to update

    Returns
    -------
    GPUArray
        nD float, input array with values updated

    """
    r_size = DTYPE_i(values_d.size)

    mod_index_update = SourceModule("""
    __global__ void index_update(float *dest, float *values, int *indices, int r_size)
    {
        // 1D grid of 1D blocks
        int t_idx = blockIdx.x * blockDim.x + threadIdx.x;
        if(t_idx >= r_size){return;}

        dest[indices[t_idx]] = values[t_idx];
    }
    """)
    block_size = 32
    x_blocks = int(r_size // block_size + 1)
    index_update = mod_index_update.get_function("index_update")
    index_update(dest_d, values_d, indices_d, r_size, block=(block_size, 1, 1), grid=(x_blocks, 1))


def _get_gpu_memory():
    nvidia_smi.nvmlInit()
    handle = nvidia_smi.nvmlDeviceGetHandleByIndex(0)
    # card id 0 hardcoded here, there is also a call to get all available card ids, so we could iterate
    info = nvidia_smi.nvmlDeviceGetMemoryInfo(handle)
    nvidia_smi.nvmlShutdown()

    return info.free, info.used, info.total


def _check_inputs(*arrays, array_type=None, dtype=None, shape=None, dim=None):
    first_array = arrays[0]
    if array_type is None:
        array_type = type(first_array)
    if dtype is None:
        dtype = first_array.dtype
    if shape is None:
        shape = first_array.shape
    if dim is None:
        dim = len(first_array.shape)

    assert all([type(array) == array_type for array in arrays]), 'Inputs must be ({}).'.format(array_type)
    assert all([array.dtype == dtype for array in arrays]), 'Inputs must have dtype ({}).'.format(dtype)
    assert all([array.shape == shape for array in arrays]), 'Inputs must have shape ({}, all must be same shape).'.format(shape)
    assert all([len(array.shape) == dim for array in arrays]), 'Inputs must have same dim ({}).'.format(dim)
