import numpy as np
import pytest
from math import sqrt

import pycuda.gpuarray as gpuarray
import scipy.interpolate as interp
from skimage.util import random_noise
from skimage import img_as_ubyte
from scipy.ndimage import shift
from imageio.v2 import imread
from scipy.fft import fftshift

import openpiv.gpu_process as gpu_process

# GLOBAL VARIABLES
# datatypes used in gpu_process
DTYPE_i = np.int32
DTYPE_f = np.float32

# dirs
_temp_dir = './temp/'

# synthetic image parameters
_image_size_rectangle = (1024, 1024)
_image_size_square = (1024, 512)
_u_shift = 8
_v_shift = -4
_accuracy_tolerance = 0.1
_identity_tolerance = 1e-6
_trim_slice = slice(2, -2, 1)

# test parameters
_test_size_tiny = (8, 8)
_test_size_small = (16, 16)
_test_size_medium = (64, 64)
_test_size_large = (256, 256)
_test_size_super = (1024, 1024)
_test_size_small_stack = (8, 16, 9)


# UTILS
def create_pair_shift(image_size, u_shift, v_shift):
    """Creates a pair of images with a roll/shift """
    frame_a = np.zeros(image_size, dtype=np.int32)
    frame_a = random_noise(frame_a)
    frame_a = img_as_ubyte(frame_a)
    frame_b = shift(frame_a, (v_shift, u_shift), mode='wrap')

    return frame_a.astype(np.int32), frame_b.astype(np.int32)


def create_pair_roll(image_size, roll_shift):
    """Creates a pair of images with a roll/shift """
    frame_a = np.zeros(image_size, dtype=np.int32)
    frame_a = random_noise(frame_a)
    frame_a = img_as_ubyte(frame_a)
    frame_b = np.roll(frame_a, roll_shift)

    return frame_a.astype(np.int32), frame_b.astype(np.int32)


def generate_cpu_gpu_pair(size, magnitude=1, dtype=DTYPE_f):
    """Returns a pair of cpu and gpu arrays with random values."""
    np.random.seed(0)
    cpu_array = (np.random.random(size) * magnitude).astype(dtype)
    gpu_array = gpuarray.to_gpu(cpu_array)

    return cpu_array, gpu_array


# UNIT TESTS
def test_gpu_gradient():
    u, u_d = generate_cpu_gpu_pair(_test_size_small)
    v, v_d = generate_cpu_gpu_pair(_test_size_small)

    u_y, u_x = np.gradient(u)
    v_y, v_x = np.gradient(v)
    strain_gpu = (gpu_process.gpu_strain(u_d, v_d)).get()

    assert np.array_equal(u_x, strain_gpu[0])
    assert np.array_equal(u_y, strain_gpu[1])
    assert np.array_equal(v_x, strain_gpu[2])
    assert np.array_equal(v_y, strain_gpu[3])


@pytest.mark.parametrize('mask_d', [None, gpuarray.zeros((7, 7), dtype=DTYPE_i)])
def test_gpu_interpolate(mask_d):
    ws0 = 16
    spacing0 = 8
    ws1 = 8
    spacing1 = 4
    n_row0, n_col0 = gpu_process.get_field_shape(_test_size_medium, ws0, spacing0)
    x0, y0 = gpu_process.get_field_coords(_test_size_medium, ws0, spacing0)
    x1, y1 = gpu_process.get_field_coords(_test_size_medium, ws1, spacing1)
    x0 = x0.astype(DTYPE_f)
    y0 = y0.astype(DTYPE_f)
    x1 = x1.astype(DTYPE_f)
    y1 = y1.astype(DTYPE_f)

    f0, f0_d = generate_cpu_gpu_pair((n_row0, n_col0))
    x0_d = gpuarray.to_gpu(x0[0, :])
    x1_d = gpuarray.to_gpu(x1[0, :])
    y0_d = gpuarray.to_gpu(y0[:, 0])
    y1_d = gpuarray.to_gpu(y1[:, 0])

    interp_2d = interp.interp2d(x0[0, :], y0[:, 0], f0)
    f1 = np.flip(interp_2d(x1[0, :], y1[:, 0]), axis=0)  # interp2d returns interpolation results with increasing y

    f1_d = gpu_process.gpu_interpolate(x0_d, y0_d, x1_d, y1_d, f0_d, mask_d=mask_d)
    f1_gpu = f1_d.get()

    assert np.allclose(f1, f1_gpu, _identity_tolerance)


def test_gpu_interpolate_mask(ndarrays_regression):
    ws0 = 16
    spacing0 = 8
    ws1 = 8
    spacing1 = 4
    n_row0, n_col0 = gpu_process.get_field_shape(_test_size_medium, ws0, spacing0)
    x0, y0 = gpu_process.get_field_coords(_test_size_medium, ws0, spacing0)
    x1, y1 = gpu_process.get_field_coords(_test_size_medium, ws1, spacing1)
    x0 = x0.astype(DTYPE_f)
    y0 = y0.astype(DTYPE_f)
    x1 = x1.astype(DTYPE_f)
    y1 = y1.astype(DTYPE_f)

    f0, f0_d = generate_cpu_gpu_pair((n_row0, n_col0))
    mask_d = gpuarray.zeros((n_row0, n_col0), dtype=DTYPE_i)
    mask_d[4:7, 4:7] = np.ones((3, 3), dtype=DTYPE_i)

    x0_d = gpuarray.to_gpu(x0[0, :])
    x1_d = gpuarray.to_gpu(x1[0, :])
    y0_d = gpuarray.to_gpu(y0[:, 0])
    y1_d = gpuarray.to_gpu(y1[:, 0])

    f1_d = gpu_process.gpu_interpolate(x0_d, y0_d, x1_d, y1_d, f0_d, mask_d=mask_d)
    ndarrays_regression.check({'f1': f1_d.get()})


def test_gpu_ftt_shift():
    correlation_stack, correlation_stack_d = generate_cpu_gpu_pair(_test_size_small_stack)

    shift_stack_cpu = fftshift(correlation_stack, axes=(1, 2))
    shift_stack_gpu = gpu_process.gpu_fft_shift(correlation_stack_d).get()

    assert np.allclose(shift_stack_cpu, shift_stack_gpu, _identity_tolerance)


def test_mask_peak():
    correlation_stack, correlation_stack_d = generate_cpu_gpu_pair(_test_size_small_stack)

    row_peak_d = gpuarray.to_gpu(np.arange(_test_size_small_stack[0], dtype=DTYPE_i))
    col_peak_d = gpuarray.to_gpu(np.arange(_test_size_small_stack[0], dtype=DTYPE_i))

    correlation_stack_masked_d = gpu_process._gpu_mask_peak(correlation_stack_d, row_peak_d, col_peak_d, 2)

    assert np.all(correlation_stack_masked_d.get()[6, 4:9, 4:9] == 0)


def test_mask_rms():
    n_windows, ht, wd = _test_size_small_stack
    correlation_stack, correlation_stack_d = generate_cpu_gpu_pair(_test_size_small_stack)

    corr_peak = np.random.random(_test_size_small_stack[0]).astype(DTYPE_f)
    corr_peak_d = gpuarray.to_gpu(corr_peak)

    a = correlation_stack.reshape((n_windows, ht * wd))
    correlation_stack_masked_cpu = (a * (a < corr_peak.reshape(n_windows, 1) / 2)).reshape(_test_size_small_stack)
    correlation_stack_masked_gpu = gpu_process._gpu_mask_rms(correlation_stack_d, corr_peak_d).get()

    assert np.allclose(correlation_stack_masked_cpu, correlation_stack_masked_gpu, _identity_tolerance)


# @pytest.mark.parametrize('image_size', (_image_size_rectangle, _image_size_square))
# def test_gpu_piv_fast0(image_size):
#     """Quick test of the main piv function."""
#     frame_a, frame_b = create_pair_shift(image_size, _u_shift, _v_shift)
#     args = {'mask': None,
#             'window_size_iters': (1, 2),
#             'min_window_size': 16,
#             'overlap_ratio': 0.5,
#             'dt': 1,
#             'deform': True,
#             'smooth': True,
#             'nb_validation_iter': 1,
#             'validation_method': 'median_velocity',
#             }
#
#     x, y, u, v, mask, s2n = gpu_process.gpu_piv(frame_a, frame_a, **args)
#
#     assert np.linalg.norm(u[_trim_slice, _trim_slice] - _u_shift) / sqrt(u.size) < _accuracy_tolerance
#     assert np.linalg.norm(-v[_trim_slice, _trim_slice] - _v_shift) / sqrt(u.size) < _accuracy_tolerance


# INTEGRATION TESTS
@pytest.mark.parametrize('image_size', (_image_size_rectangle, _image_size_square))
def test_gpu_piv_fast(image_size):
    """Quick test of the main piv function."""
    frame_a, frame_b = create_pair_shift(image_size, _u_shift, _v_shift)
    args = {'mask': None,
            'window_size_iters': (1, 2),
            'min_window_size': 16,
            'overlap_ratio': 0.5,
            'dt': 1,
            'deform': True,
            'smooth': True,
            'nb_validation_iter': 1,
            'validation_method': 'median_velocity',
            }

    x, y, u, v, mask, s2n = gpu_process.gpu_piv(frame_a, frame_b, **args)

    assert np.linalg.norm(u[_trim_slice, _trim_slice] - _u_shift) / sqrt(u.size) < _accuracy_tolerance
    assert np.linalg.norm(-v[_trim_slice, _trim_slice] - _v_shift) / sqrt(u.size) < _accuracy_tolerance


@pytest.mark.parametrize('image_size', (_image_size_rectangle, _image_size_square))
def test_gpu_piv_zero(image_size):
    """Tests that zero-displacement is returned when the images are empty."""
    frame_a = frame_b = np.zeros(image_size, dtype=np.int32)
    args = {'mask': None,
            'window_size_iters': (1, 2),
            'min_window_size': 16,
            'overlap_ratio': 0.5,
            'dt': 1,
            'deform': True,
            'smooth': True,
            'nb_validation_iter': 1,
            'validation_method': 'median_velocity',
            }

    x, y, u, v, mask, s2n = gpu_process.gpu_piv(frame_a, frame_b, **args)

    assert np.allclose(u, 0, _identity_tolerance)
    assert np.allclose(v, 0, _identity_tolerance)


def test_extended_search_area():
    """Inputs every s2n method to ensure they don't error out."""
    frame_a, frame_b = create_pair_shift(_image_size_rectangle, _u_shift, _v_shift)
    args = {'mask': None,
            'window_size_iters': (2, 2),
            'min_window_size': 8,
            'overlap_ratio': 0.5,
            'dt': 1,
            'deform': True,
            'smooth': True,
            'nb_validation_iter': 2,
            'extend_ratio': 2
            }

    x, y, u, v, mask, s2n = gpu_process.gpu_piv(frame_a, frame_b, **args)

    assert np.linalg.norm(u[_trim_slice, _trim_slice] - _u_shift) / sqrt(u.size) < _accuracy_tolerance
    assert np.linalg.norm(-v[_trim_slice, _trim_slice] - _v_shift) / sqrt(u.size) < _accuracy_tolerance


@pytest.mark.parametrize('s2n_method', ('peak2peak', 'peak2mean', 'peak2energy'))
def test_sig2noise(s2n_method):
    """Inputs every s2n method to ensure they don't error out."""
    frame_a, frame_b = create_pair_shift(_image_size_rectangle, _u_shift, _v_shift)
    args = {'mask': None,
            'window_size_iters': (1, 2, 2),
            'min_window_size': 8,
            'overlap_ratio': 0.5,
            'dt': 1,
            'deform': True,
            'smooth': True,
            'nb_validation_iter': 2,
            'validation_method': 'median_velocity',
            'return_sig2noise': True,
            'sig2noise_method': s2n_method,
            }

    x, y, u, v, mask, s2n = gpu_process.gpu_piv(frame_a, frame_b, **args)


@pytest.mark.parametrize('subpixel_method', ('gaussian', 'centroid', 'parabolic'))
def test_subpixel_peak(subpixel_method):
    """Inputs every s2n method to ensure they don't error out."""
    frame_a, frame_b = create_pair_shift(_image_size_rectangle, _u_shift, _v_shift)
    args = {'mask': None,
            'window_size_iters': (1, 2, 2),
            'min_window_size': 8,
            'overlap_ratio': 0.5,
            'dt': 1,
            'deform': True,
            'smooth': True,
            'nb_validation_iter': 2,
            'validation_method': 'median_velocity',
            'subpixel_method': subpixel_method,
            }

    x, y, u, v, mask, s2n = gpu_process.gpu_piv(frame_a, frame_b, **args)


# s2n must not cause invalid numbers to be passed to smoothn.
@pytest.mark.parametrize('validation_method', ('s2n', 'mean_velocity', 'median_velocity', 'rms_velocity'))
def test_validation(validation_method):
    """Inputs every s2n method to ensure they don't error out."""
    frame_a, frame_b = create_pair_shift(_image_size_rectangle, _u_shift, _v_shift)
    args = {'mask': None,
            'window_size_iters': (1, 2, 2),
            'min_window_size': 8,
            'overlap_ratio': 0.5,
            'dt': 1,
            'deform': True,
            'smooth': True,
            'nb_validation_iter': 2,
            'validation_method': validation_method,
            }

    x, y, u, v, mask, s2n = gpu_process.gpu_piv(frame_a, frame_b, **args)


# sweep the input variables to ensure everything is same
@pytest.mark.parametrize('window_size_iters', [1, (1, 1), (1, 1, 1), (1, 1, 2), (1, 2, 2), (2, 2, 2), (1, 2, 1)])
@pytest.mark.parametrize('min_window_size', [8, 16])
@pytest.mark.parametrize('nb_validation_iter', [0, 1, 2])
def test_gpu_piv_py(window_size_iters, min_window_size, nb_validation_iter, ndarrays_regression):
    """This test checks that the output remains the same."""
    frame_a = imread('./openpiv/data/test1/exp1_001_a.bmp')
    frame_b = imread('./openpiv/data/test1/exp1_001_b.bmp')
    args = {'mask': None,
            'window_size_iters': window_size_iters,
            'min_window_size': min_window_size,
            'overlap_ratio': 0.5,
            'dt': 1,
            'deform': True,
            'smooth': True,
            'nb_validation_iter': nb_validation_iter,
            'validation_method': 'median_velocity',
            'smoothing_par': 0.5,
            'center_field': False
            }

    x, y, u, v, mask, s2n = gpu_process.gpu_piv(frame_a, frame_b, **args)

    ndarrays_regression.check({'u': u, 'v': v})


# BENCHMARKS
@pytest.mark.parametrize('image_size', [(1024, 1024), (2048, 2048)])
@pytest.mark.parametrize('window_size_iters,min_window_size', [((1, 2), 16), ((1, 2, 2), 8)])
def test_gpu_piv_benchmark(benchmark, image_size, window_size_iters, min_window_size):
    """Benchmarks the PIV function."""
    frame_a, frame_b = create_pair_shift(image_size, _u_shift, _v_shift)
    args = {'mask': None,
            'window_size_iters': window_size_iters,
            'min_window_size': min_window_size,
            'overlap_ratio': 0.5,
            'dt': 1,
            'deform': True,
            'smooth': True,
            'nb_validation_iter': 2,
            'validation_method': 'median_velocity',
            }

    benchmark(gpu_process.gpu_piv, frame_a, frame_b, **args)


def test_gpu_piv_benchmark_oop(benchmark):
    """Benchmarks the PIV speed with the objected-oriented interface."""
    frame_a, frame_b = create_pair_shift(_image_size_rectangle, _u_shift, _v_shift)
    args = {'mask': None,
            'window_size_iters': (1, 2, 2),
            'min_window_size': 8,
            'overlap_ratio': 0.5,
            'dt': 1,
            'deform': True,
            'smooth': True,
            'nb_validation_iter': 2,
            'validation_method': 'median_velocity',
            }

    piv_gpu = gpu_process.PIVGPU(_image_size_rectangle, **args)

    @benchmark
    def repeat_10():
        for i in range(10):
            piv_gpu(frame_a, frame_b)
