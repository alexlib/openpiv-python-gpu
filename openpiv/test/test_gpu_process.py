import os
import sys
import numpy as np
import pytest
from math import sqrt

import pycuda.gpuarray as gpuarray
import pyximport
import scipy.interpolate as interp
from skimage.util import random_noise
from skimage import img_as_ubyte
from scipy.ndimage import shift
from imageio import imread
from scipy.fft import fftshift

import openpiv.gpu_process as gpu_process
import openpiv.gpu_validation as gpu_validation
from openpiv.smoothn import smoothn

pyximport.install(setup_args={"include_dirs": np.get_include()}, language_level=3)
import openpiv.gpu_process_old as gpu_process_old

# GLOBAL VARIABLES
# datatypes used in gpu_process
DTYPE_i = np.int32
DTYPE_f = np.float32

# dirs
_fixture_dir = "./openpiv/test/fixtures/"

# synthetic image parameters
_image_size_rectangle = (1024, 1024)
_image_size_square = (1024, 2048)
_u_shift = 8
_v_shift = -4
_accuracy_tolerance = 0.1
_identity_tolerance = 0.005
_trim_slice = slice(2, -2, 1)

# test parameters
_test_size_tiny = (4, 4)
_test_size_small = (16, 16)
_test_size_medium = (64, 64)
_test_size_large = (256, 256)
_test_size_super = (1024, 1024)
_test_size_small_stack = (8, 16, 9)


# SCRIPTS
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
    cpu_array = (np.random.random(size) * magnitude).astype(dtype)
    gpu_array = gpuarray.to_gpu(cpu_array)

    return cpu_array, gpu_array


# UNIT TESTS
def test_gpu_mask():
    frame, frame_d = generate_cpu_gpu_pair(_test_size_small, magnitude=2, dtype=DTYPE_f)
    mask, mask_d = generate_cpu_gpu_pair(_test_size_small, magnitude=2, dtype=DTYPE_i)

    frame_masked = frame * mask

    frame_masked_gpu = gpu_process.gpu_mask(frame_d, mask_d).get()

    assert np.array_equal(frame_masked, frame_masked_gpu)


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


def test_gpu_smooth():
    f, f_d = generate_cpu_gpu_pair(_test_size_small)

    f_smooth = smoothn(f, s=0.5)[0].astype(DTYPE_f)
    f_smooth_gpu = (gpu_process.gpu_smooth(f_d)).get()

    assert np.array_equal(f_smooth, f_smooth_gpu)


def test_gpu_round():
    f, f_d = generate_cpu_gpu_pair(_test_size_small)

    f_round = np.round(f)
    f_round_gpu = (gpu_process.gpu_round(f_d)).get()

    assert np.array_equal(f_round, f_round_gpu)


def test_gpu_interpolate():
    ws0 = 16
    ws1 = 8
    n_row0, n_col0 = gpu_process.get_field_shape(_test_size_medium, ws0, 0.5)
    x0, y0 = gpu_process.get_field_coords((n_row0, n_col0), ws0, 0.5)
    n_row1, n_col1 = gpu_process.get_field_shape(_test_size_medium, ws1, 0.5)
    x1, y1 = gpu_process.get_field_coords((n_row1, n_col1), ws1, 0.5)

    f0, f0_d = generate_cpu_gpu_pair((n_row0, n_col0))
    x0_d = gpuarray.to_gpu(x0[0, :])
    x1_d = gpuarray.to_gpu(x1[0, :])
    y0_d = gpuarray.to_gpu(y0[:, 0])
    y1_d = gpuarray.to_gpu(y1[:, 0])

    interp_2d = interp.interp2d(x0[0, :], y0[:, 0], f0)
    f1 = np.flip(interp_2d(x1[0, :], y1[:, 0]), axis=0)  # interp2d returns interpolation results with increasing y

    f1_d = gpu_process.gpu_interpolate(x0_d, y0_d, x1_d, y1_d, f0_d)
    f1_gpu = f1_d.get()

    assert np.allclose(f1, f1_gpu, 0.01)


def test_gpu_interpolate_validation():
    ws0 = 16
    ws1 = 8
    n_row0, n_col0 = gpu_process.get_field_shape(_test_size_medium, ws0, 0.5)
    x0, y0 = gpu_process.get_field_coords((n_row0, n_col0), ws0, 0.5)
    n_row1, n_col1 = gpu_process.get_field_shape(_test_size_medium, ws1, 0.5)
    x1, y1 = gpu_process.get_field_coords((n_row1, n_col1), ws1, 0.5)

    f0, f0_d = generate_cpu_gpu_pair((n_row0, n_col0))
    f1, f1_d = generate_cpu_gpu_pair((n_row1, n_col1))
    x0_d = gpuarray.to_gpu(x0[0, :])
    x1_d = gpuarray.to_gpu(x1[0, :])
    y0_d = gpuarray.to_gpu(y0[:, 0])
    y1_d = gpuarray.to_gpu(y1[:, 0])

    val_locations, val_locations_d = generate_cpu_gpu_pair((n_row1, n_col1), magnitude=2, dtype=DTYPE_i)

    interp_2d = interp.interp2d(x0[0, :], y0[:, 0], f0)
    f1_val = val_locations * f1 + np.flip(interp_2d(x1[0, :], y1[:, 0]), axis=0) * (1 - val_locations)  # interp2d returns interpolation results with increasing y

    f1_val_d = gpu_process.gpu_interpolate_replace(x0_d, y0_d, x1_d, y1_d, f0_d, f1_d, val_locations_d=val_locations_d)
    f1_val_gpu = f1_val_d.get()

    assert np.allclose(f1_val, f1_val_gpu, 0.01)


def test_gpu_ftt_shift():
    correlation_stack, correlation_stack_d = generate_cpu_gpu_pair(_test_size_small_stack)

    # correlation_stack = correlation_stack * 0
    # correlation_stack[0, 3, 3] = 1
    # correlation_stack_d = gpuarray.to_gpu(correlation_stack)

    shift_stack_cpu = fftshift(correlation_stack, axes=(1, 2))
    shift_stack_gpu = gpu_process.gpu_fft_shift(correlation_stack_d).get()

    assert np.allclose(shift_stack_cpu, shift_stack_gpu, 0.01)


def test_subpixel_approximation():
    pass


# INTEGRATION TESTS
@pytest.mark.parametrize("image_size", (_image_size_rectangle, _image_size_square))
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
            'validation_method': "median_velocity",
            'trust_1st_iter': False,
            }

    x, y, u, v, mask, s2n = gpu_process.gpu_piv(frame_a, frame_b, **args)

    assert np.linalg.norm(u[_trim_slice, _trim_slice] - _u_shift) / sqrt(u.size) < _accuracy_tolerance
    assert np.linalg.norm(-v[_trim_slice, _trim_slice] - _v_shift) / sqrt(u.size) < _accuracy_tolerance


@pytest.mark.parametrize("image_size", (_image_size_rectangle, _image_size_square))
def test_gpu_piv_zero(image_size):
    """Tests that zero-displacement is returned when the images are empty."""
    frame_a = frame_b = np.zeros(image_size, dtype=np.int32)
    args = {'mask': None,
            'window_size_iters': (1, 2),
            'min_window_size': 16,
            'overlap_ratio': 0.5,
            'dt': 1,
            'deform': True,
            'smooth': False,  # this is False so that smoothn doesn't error
            'nb_validation_iter': 1,
            'validation_method': "median_velocity",
            'trust_1st_iter': False,
            }

    x, y, u, v, mask, s2n = gpu_process.gpu_piv(frame_a, frame_b, **args)

    assert np.allclose(u, 0, _identity_tolerance)
    assert np.allclose(v, 0, _identity_tolerance)


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
            'validation_method': "median_velocity",
            'trust_1st_iter': False,
            }

    benchmark(gpu_process.gpu_piv, frame_a, frame_b, **args)
    # benchmark(gpu_process_old.gpu_piv, frame_a, frame_b, **args)


@pytest.mark.parametrize("image_size", (_image_size_rectangle, _image_size_square))
def test_gpu_extended_search_area_fast(image_size):
    """Quick test of the extanded search area function."""
    frame_a_rectangle, frame_b_rectangle = create_pair_shift(image_size, _u_shift, _v_shift)
    u, v = gpu_process.gpu_extended_search_area(
        frame_a_rectangle, frame_b_rectangle, window_size=16, overlap_ratio=0.5, search_area_size=32, dt=1
    )
    assert np.linalg.norm(u[_trim_slice, _trim_slice] - _u_shift) / sqrt(u.size) < _accuracy_tolerance * 2
    assert np.linalg.norm(-v[_trim_slice, _trim_slice] - _v_shift) / sqrt(u.size) < _accuracy_tolerance * 2


def test_gpu_piv_benchmark_oop(benchmark):
    """Benchmarks the PIV """
    frame_a, frame_b = create_pair_shift(_image_size_rectangle, _u_shift, _v_shift)
    args = {'mask': None,
            'window_size_iters': (1, 2, 2),
            'min_window_size': 8,
            'overlap_ratio': 0.5,
            'dt': 1,
            'deform': True,
            'smooth': True,
            'nb_validation_iter': 2,
            'validation_method': "median_velocity",
            'trust_1st_iter': False,
            }

    piv_gpu = gpu_process.PIVGPU(_image_size_rectangle, **args)
    # piv_gpu = gpu_process_old.PIVGPU(_image_size_rectangle, **args)

    @benchmark
    def repeat_10():
        for i in range(10):
            piv_gpu(frame_a, frame_b)


# sweep the input variables to ensure everything is same
@pytest.mark.parametrize('window_size_iters', [1, (1, 1), (1, 1, 1), (1, 1, 2), (1, 2, 2), (2, 2, 2), (1, 2, 1)])
@pytest.mark.parametrize('min_window_size', [8, 16])
@pytest.mark.parametrize('nb_validation_iter', [0, 1, 2])
def test_gpu_piv_py2(window_size_iters, min_window_size, nb_validation_iter):
    # the images are loaded using imageio.
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
            'validation_method': "median_velocity",
            'trust_1st_iter': False,
            'smoothing_par': 0.5
            }

    """Ensures the results of the GPU algorithm remains unchanged."""
    file_str = _fixture_dir + './comparison_data_{}_{}_{}'.format(str(window_size_iters), str(min_window_size),
                                                                  str(nb_validation_iter))

    x, y, u, v, mask, s2n = gpu_process.gpu_piv(frame_a, frame_b, **args)
    # x, y, u, v, mask, s2n = gpu_process_old.gpu_piv(frame_a, frame_b, **args)

    # # save the results to a numpy file.
    # if not os.path.isdir(_fixture_dir):
    #     os.mkdir(_fixture_dir)
    # np.savez(file_str, u=u, v=v)

    # load the results for comparison
    with np.load(file_str + '.npz') as data:
        u0 = data['u']
        v0 = data['v']

    x, y, u, v, mask, s2n = gpu_process.gpu_piv(frame_a, frame_b, **args)

    # compare with the previous results
    assert np.allclose(u, u0, atol=_identity_tolerance)
    assert np.allclose(v, v0, atol=_identity_tolerance)

