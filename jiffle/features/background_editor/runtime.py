import importlib
import importlib.util
import subprocess
import sys
import threading

from PIL import Image
import requests

from .workflow import BackgroundFailure, MODEL_NAME


_runtime_lock = threading.Lock()
_loaded_runtime = None
HUGGINGFACE_WHOAMI_URL = "https://huggingface.co/api/whoami-v2"
MODEL_CONFIG_URL = f"https://huggingface.co/{MODEL_NAME}/resolve/main/config.json"

# RMBG-2.0's trusted remote model code imports kornia and timm in addition to
# the base PyTorch and Transformers packages. Keep the import names separate
# from pip requirement strings because they are not always identical.
RUNTIME_DEPENDENCIES = (
    ("torch", "torch"),
    ("torchvision", "torchvision"),
    ("transformers", "transformers>=4.39"),
    ("safetensors", "safetensors"),
    ("kornia", "kornia"),
    ("timm", "timm"),
)


def remove_background(image, model_root, huggingface_token=None):
    model, torch, transforms, device = _load_runtime(model_root, huggingface_token)
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


def validate_huggingface_token(token):
    token = token.strip() if isinstance(token, str) else ""
    if not token:
        raise BackgroundFailure(
            "background.huggingface_token_required",
            "Add a Hugging Face access token in Settings before loading RMBG-2.0.",
        )
    headers = {"Authorization": f"Bearer {token}"}
    try:
        identity = requests.get(HUGGINGFACE_WHOAMI_URL, headers=headers, timeout=20)
    except requests.RequestException as error:
        raise BackgroundFailure(
            "background.huggingface_unavailable",
            "Hugging Face could not be reached. Check the network connection and try again.",
        ) from error
    if identity.status_code in {401, 403}:
        raise BackgroundFailure(
            "background.huggingface_token_invalid",
            "The Hugging Face token is invalid or has expired.",
        )
    if identity.status_code != 200:
        raise BackgroundFailure(
            "background.huggingface_unavailable",
            "Hugging Face could not verify the access token. Try again later.",
        )
    try:
        access = requests.get(MODEL_CONFIG_URL, headers=headers, timeout=30)
    except requests.RequestException as error:
        raise BackgroundFailure(
            "background.huggingface_unavailable",
            "Hugging Face could not verify access to RMBG-2.0. Check the network connection and try again.",
        ) from error
    if access.status_code in {401, 403}:
        raise BackgroundFailure(
            "background.huggingface_access_denied",
            "This Hugging Face account does not have access to briaai/RMBG-2.0. Accept the model terms first.",
        )
    if access.status_code != 200:
        raise BackgroundFailure(
            "background.huggingface_unavailable",
            "Hugging Face could not verify access to RMBG-2.0. Try again later.",
        )
    try:
        payload = identity.json()
    except ValueError:
        payload = {}
    return payload.get("name") or payload.get("fullname") or "authenticated"


def _load_runtime(model_root, huggingface_token=None):
    global _loaded_runtime
    if _loaded_runtime is not None:
        return _loaded_runtime
    with _runtime_lock:
        if _loaded_runtime is not None:
            return _loaded_runtime
        cache_available = _model_cache_available(model_root)
        token_validated = False
        if not cache_available:
            validate_huggingface_token(huggingface_token)
            token_validated = True
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
            token = huggingface_token or False
            try:
                model = AutoModelForImageSegmentation.from_pretrained(
                    MODEL_NAME,
                    trust_remote_code=True,
                    cache_dir=model_root,
                    local_files_only=True,
                    token=token,
                )
            except Exception:
                if not token_validated:
                    validate_huggingface_token(huggingface_token)
                model = AutoModelForImageSegmentation.from_pretrained(
                    MODEL_NAME,
                    trust_remote_code=True,
                    cache_dir=model_root,
                    token=huggingface_token,
                )
            model.to(device)
            model.eval()
            _loaded_runtime = (model, torch, transforms, device)
            return _loaded_runtime
        except BackgroundFailure:
            raise
        except Exception as error:
            raise BackgroundFailure(
                "background.model_download_failed",
                "RMBG-2.0 could not be downloaded or loaded. Check network access and available disk space.",
            ) from error


def _model_cache_available(model_root):
    model_directory = model_root / "models--briaai--RMBG-2.0" / "snapshots"
    if not model_directory.is_dir():
        return False
    return any(
        snapshot.is_dir() and (snapshot / "config.json").is_file()
        for snapshot in model_directory.iterdir()
    )


def _ensure_dependencies():
    missing = _missing_dependencies()
    if not missing:
        _verify_dependency_imports()
        return
    try:
        subprocess.run(
            [
                sys.executable,
                "-m",
                "pip",
                "install",
                *missing,
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=3600,
        )
        importlib.invalidate_caches()
        still_missing = _missing_dependencies()
        if still_missing:
            raise BackgroundFailure(
                "background.runtime_install_failed",
                "The RMBG-2.0 runtime is missing required packages: "
                + ", ".join(still_missing)
                + ".",
                {"missing_packages": still_missing},
            )
        _verify_dependency_imports()
    except BackgroundFailure:
        raise
    except Exception as error:
        raise BackgroundFailure(
            "background.runtime_install_failed",
            "The RMBG-2.0 runtime could not install its required packages "
            + ", ".join(missing)
            + ". Check network access and Python permissions.",
            {"packages": missing},
        ) from error


def _missing_dependencies():
    return [
        package
        for module, package in RUNTIME_DEPENDENCIES
        if importlib.util.find_spec(module) is None
    ]


def _verify_dependency_imports():
    for module, _package in RUNTIME_DEPENDENCIES:
        try:
            importlib.import_module(module)
        except Exception as error:
            raise BackgroundFailure(
                "background.runtime_install_failed",
                f"The RMBG-2.0 runtime could not import required package '{module}'.",
                {"module": module},
            ) from error
