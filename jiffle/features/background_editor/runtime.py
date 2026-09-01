import importlib
import importlib.util
import subprocess
import sys
import threading

from PIL import Image

from .workflow import BackgroundFailure, MODEL_NAME


_runtime_lock = threading.Lock()
_loaded_runtime = None


def remove_background(image, model_root):
    model, torch, transforms, device = _load_runtime(model_root)
    try:
        preprocessing = transforms.Compose(
            [
                transforms.Resize((1024, 1024)),
                transforms.ToTensor(),
                transforms.Normalize(
                    [0.485, 0.456, 0.406], [0.229, 0.224, 0.225]
                ),
            ]
        )
        tensor = preprocessing(image.convert("RGB")).unsqueeze(0).to(device)
        with torch.no_grad():
            prediction = model(tensor)
        if hasattr(prediction, "logits"):
            prediction = prediction.logits
        while isinstance(prediction, (tuple, list)):
            prediction = prediction[-1]
        if isinstance(prediction, dict) and "logits" in prediction:
            prediction = prediction["logits"]
        prediction = prediction.sigmoid().cpu()[0].squeeze()
        mask = transforms.ToPILImage()(prediction).resize(
            image.size, Image.Resampling.LANCZOS
        )
        result = image.convert("RGBA")
        result.putalpha(mask)
        return result
    except BackgroundFailure:
        raise
    except Exception as error:
        raise BackgroundFailure(
            "background.inference_failed",
            "RMBG-2.0 could not remove the image background.",
        ) from error


def _load_runtime(model_root):
    global _loaded_runtime
    if _loaded_runtime is not None:
        return _loaded_runtime
    with _runtime_lock:
        if _loaded_runtime is not None:
            return _loaded_runtime
        _ensure_dependencies()
        try:
            import torch
            from torchvision import transforms
            from transformers import AutoModelForImageSegmentation
        except Exception as error:
            raise BackgroundFailure(
                "background.runtime_install_failed",
                "The installed RMBG-2.0 runtime could not be loaded. Reinstall PyTorch, torchvision, and transformers in this Python environment.",
            ) from error
        try:
            model_root.mkdir(parents=True, exist_ok=True)
            device = "cuda" if torch.cuda.is_available() else "cpu"
            model = AutoModelForImageSegmentation.from_pretrained(
                MODEL_NAME,
                trust_remote_code=True,
                cache_dir=model_root,
            )
            model.to(device)
            model.eval()
            _loaded_runtime = (model, torch, transforms, device)
            return _loaded_runtime
        except Exception as error:
            raise BackgroundFailure(
                "background.model_download_failed",
                "RMBG-2.0 could not be downloaded or loaded. Check network access and available disk space.",
            ) from error


def _ensure_dependencies():
    modules = ("torch", "torchvision", "transformers", "safetensors")
    missing = [name for name in modules if importlib.util.find_spec(name) is None]
    if not missing:
        return
    try:
        subprocess.run(
            [
                sys.executable,
                "-m",
                "pip",
                "install",
                "torch",
                "torchvision",
                "transformers>=4.39",
                "safetensors",
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=3600,
        )
        importlib.invalidate_caches()
    except Exception as error:
        raise BackgroundFailure(
            "background.runtime_install_failed",
            "The RMBG-2.0 runtime could not be installed automatically. Check network access and Python permissions.",
        ) from error
