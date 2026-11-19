"""Check the installed reconstruction stack using the project virtual environment."""

import importlib.metadata
import json
from pathlib import Path
import platform
import subprocess
import sys

import gtsam
import numpy as np
import specialist.vi_ba._mei_factor_cpp
from specialist_description import get_urdf_path

report_path = Path(__file__).resolve().parents[2] / "agent_logs" / "environment-check.json"
report = {
    "python": platform.python_version(),
    "executable": sys.executable,
    "prefix": sys.prefix,
    "packages": {
        name: importlib.metadata.version(name)
        for name in (
            "gtsam", "numpy", "torch", "triton", "cupy-cuda12x",
            "nvmath-python", "nvidia-cudss-cu12", "nvidia-cuda-runtime-cu12",
            "rerun-sdk", "opencv-python", "telemetry-parser", "pupil-apriltags",
        )
    },
    "checks": {},
}

assert sys.prefix != sys.base_prefix, "Run this check inside the project .venv"
assert sys.version_info[:2] == (3, 14)
assert np.isfinite(gtsam.Pose3().matrix()).all()
urdf = get_urdf_path("specialist_handheld")
assert Path(urdf).is_file()
report["checks"]["native_mei_and_gripper_model"] = "passed"
print("Native MEI extension and gripper URDF: OK", flush=True)

report["nvidia_smi"] = subprocess.check_output(
    ["nvidia-smi", "--query-gpu=name,driver_version,memory.total", "--format=csv,noheader"],
    text=True,
).strip()

import torch

assert torch.cuda.is_available(), "PyTorch cannot access CUDA"
matrix = torch.ones((64, 64), device="cuda")
assert torch.allclose(matrix @ matrix, torch.full_like(matrix, 64.0))
torch.cuda.synchronize()
report["torch_cuda"] = torch.version.cuda
report["gpu"] = torch.cuda.get_device_name(0)
report["checks"]["torch_gpu_matmul"] = "passed"
print("PyTorch GPU matrix multiplication: OK", flush=True)

import cupy as cp

assert int(cp.sum(cp.arange(10)).get()) == 45
report["cupy_cuda_runtime"] = cp.cuda.runtime.runtimeGetVersion()
report["cupy_cuda_driver"] = cp.cuda.runtime.driverGetVersion()
report["checks"]["cupy_gpu_kernel"] = "passed"
print("CuPy GPU kernel compilation and execution: OK", flush=True)

import scipy.sparse as sp
from specialist.vi_ba.gpu_lm import _Cudss

host_matrix = np.array([[4.0, 1.0, 0.0], [1.0, 3.0, 1.0], [0.0, 1.0, 2.0]])
host_rhs = np.array([1.0, 2.0, 3.0])
solver = _Cudss()
try:
    solver.factorize(sp.csr_matrix(host_matrix))
    answer = solver.to_host(solver.apply(cp.asarray(host_rhs)))
    np.testing.assert_allclose(answer, np.linalg.solve(host_matrix, host_rhs), atol=1e-10)
    report["cudss_residual"] = float(np.linalg.norm(host_matrix @ answer - host_rhs))
finally:
    solver.close()
report["checks"]["cudss_sparse_solve"] = "passed"
print("Project cuDSS GPU sparse solver: OK", flush=True)

import imageio_ffmpeg
from pupil_apriltags import Detector

Detector(families="tag36h11")
report["checks"]["apriltag_detector"] = "passed"
report["ffmpeg"] = imageio_ffmpeg.get_ffmpeg_exe()
subprocess.run([report["ffmpeg"], "-version"], check=True, capture_output=True)
report["checks"]["ffmpeg"] = "passed"
report_path.parent.mkdir(parents=True, exist_ok=True)
report_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
print(json.dumps(report, indent=2), flush=True)
