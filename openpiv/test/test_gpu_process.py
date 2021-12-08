# -*- coding: utf-8 -*-

import os
import sys
import numpy as np
import pycuda.gpuarray as gpuarray
import pycuda.driver as drv
from pycuda.compiler import SourceModule
from skimage.util import random_noise
from skimage import img_as_ubyte
from scipy.ndimage import shift
from imageio import imread
from math import sqrt

import pyximport
pyximport.install(setup_args={"include_dirs": np.get_include()}, language_level=3)

import openpiv.gpu_process as gpu_process
import openpiv.gpu_validation as gpu_validation

fixture_dir = "./fixtures/"

# synthetic image parameters
_image_size_square = (1024, 1024)
_image_size_rectangle = (1024, 2048)
_u_shift = 8
_v_shift = -4
_threshold = 0.1
_trim_slice = slice(2, -2, 1)


def create_pair_shift(image_size, u_shift, v_shift):
    """Creates a pair of images with a roll/shift """
    frame_a = np.zeros(image_size, dtype=np.int32)
    frame_a = random_noise(frame_a)
    frame_a = img_as_ubyte(frame_a)
    frame_b = shift(frame_a, (v_shift, u_shift), mode='wrap')

    return frame_a.astype(np.int32), frame_b.astype(np.int32)


# def create_pair_roll(image_size, u_roll, v_roll):
#     """Creates a pair of images with a roll/shift """
#     frame_a = np.zeros(image_size, dtype=np.int32)
#     frame_a = random_noise(frame_a)
#     frame_a = img_as_ubyte(frame_a)
#     frame_b = np.roll(frame_a)
#
#     return frame_a.astype(np.int32), frame_b.astype(np.int32)


def test_gpu_extended_search_area_fast():
    """Quick test of the extanded search area function."""
    frame_a_rectangle, frame_b_rectangle = create_pair_shift(_image_size_rectangle, _u_shift, _v_shift)
    u, v = gpu_process.gpu_extended_search_area(
        frame_a_rectangle, frame_b_rectangle, window_size=16, overlap_ratio=0.5, search_area_size=32, dt=1
    )
    assert np.linalg.norm(u[_trim_slice, _trim_slice] - _u_shift) / sqrt(u.size) < _threshold * 2
    assert np.linalg.norm(-v[_trim_slice, _trim_slice] - _v_shift) / sqrt(u.size) < _threshold * 2


def test_gpu_piv_fast_rectangle():
    """Quick test of the main piv function."""
    frame_a_rectangle, frame_b_rectangle = create_pair_shift(_image_size_rectangle, _u_shift, _v_shift)
    x, y, u, v, mask, s2n = gpu_process.gpu_piv(
        frame_a_rectangle,
        frame_b_rectangle,
        mask=None,
        window_size_iters=(1, 2),
        min_window_size=16,
        overlap_ratio=0.5,
        dt=1,
        deform=True,
        smooth=True,
        nb_validation_iter=1,
        validation_method="median_velocity",
        trust_1st_iter=True,
    )
    assert np.linalg.norm(u[_trim_slice, _trim_slice] - _u_shift) / sqrt(u.size) < _threshold
    assert np.linalg.norm(-v[_trim_slice, _trim_slice] - _v_shift) / sqrt(u.size) < _threshold


def test_gpu_piv_fast_square():
    """Quick test of the main piv function."""
    _frame_a_square, _frame_b_square = create_pair_shift(_image_size_square, _u_shift, _v_shift)
    x, y, u, v, mask, s2n = gpu_process.gpu_piv(
        _frame_a_square,
        _frame_b_square,
        mask=None,
        window_size_iters=(1, 2),
        min_window_size=16,
        overlap_ratio=0.5,
        dt=1,
        deform=True,
        smooth=True,
        nb_validation_iter=1,
        validation_method="median_velocity",
        trust_1st_iter=True,
    )
    assert np.linalg.norm(u[_trim_slice, _trim_slice] - _u_shift) / sqrt(u.size) < _threshold
    assert np.linalg.norm(-v[_trim_slice, _trim_slice] - _v_shift) / sqrt(u.size) < _threshold


def test_gpu_piv_py():
    # the images are loaded using imageio.
    frame_a = imread('../data/test1/exp1_001_a.bmp')
    frame_b = imread('../data/test1/exp1_001_b.bmp')

    """Ensures the results of the GPU algorithm remains unchanged."""
    x, y, u, v, mask, s2n = gpu_process.gpu_piv(
        frame_a,
        frame_b,
        mask=None,
        window_size_iters=(1, 1, 2),
        min_window_size=8,
        overlap_ratio=0.5,
        dt=1,
        deform=True,
        smooth=True,
        nb_validation_iter=2,
        validation_method="median_velocity",
        trust_1st_iter=True,
    )

    # # save the results to a numpy file file.
    # if not os.path.exists('./fixtures'):
    #     os.mkdir('./fixtures')
    # np.savez('./fixtures/test_data', u=u, v=v)

    # load the results for comparison
    with np.load('./fixtures/test_data.npz') as data:
        u0 = data['u']
        v0 = data['v']

    # compare with the previous results
    assert np.allclose(u, u0, atol=_threshold)
    assert np.allclose(v, v0, atol=_threshold)


# def test_correlation_function():
#     pass
#
#
# def test_deform():
#     pass
#
#
# def test_gpu_validation_mean():
#     """Validates a field with exactly one spurious vector"""
#     pass
#
#
# def test_gpu_validation_median():
#     """Validates a field with exactly one spurious vector"""
#     pass
#
#
# def test_gpu_validation_():
#     """Validates a field with exactly one spurious vector"""
#     pass
#
#
# def test_gpu_validation_s2n():
#     """Validates a field with exactly one spurious vector"""
#     pass
#
#
# def test_replace_vectors():
#     pass
#
#
# def test_get_field_shape():
#     """Validates a field with exactly one spurious vector"""
#     # test square field
#
#     # test rectangular field
#
#     pass
#
#
# def test_pycuda():
#     # define gpu array
#     a = np.random.randn(4, 4)
#     a = a.astype(np.float32)
#     a_gpu = drv.mem_alloc(a.nbytes)
#     drv.memcpy_htod(a_gpu, a)
#
#     # define the doubling function
#     mod = SourceModule(
#         """
#       __global__ void doublify(float *a)
#       {
#         int idx = threadIdx.x + threadIdx.y*4;
#         a[idx] *= 2;
#       }
#       """
#     )
#     func = mod.get_function("doublify")
#     func(a_gpu, block=(4, 4, 1))
#
#     # double the gpu array
#     a_doubled = np.empty_like(a)
#     drv.memcpy_dtoh(a_doubled, a_gpu)
#
#
# def test_gpu_array():
#     """If this test fails, then the CUDA version is likely not working with PyCUDA."""
#     a = gpuarray.zeros((2, 2, 2), dtype=np.float32)
#     b = gpuarray.zeros((2, 2, 2), dtype=np.int32)
#     c = gpuarray.empty((2, 2, 2), dtype=np.float32)
#     d = gpuarray.empty((2, 2, 2), dtype=np.int32)
