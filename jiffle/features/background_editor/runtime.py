import importlib
import importlib.util
import subprocess
import sys
import threading

from PIL import Image
import requests

from .workflow import (
    BackgroundFailure,
    BACKGROUND_MODEL_AUTO,
    background_model_value,
    LEGACY_MODEL_NAME,
    MODEL_NAME,
    resolve_background_model,
)


_runtime_lock = threading.Lock()
_loaded_runtime = None
_loaded_runtimes = {}
HUGGINGFACE_WHOAMI_URL = "https://huggingface.co/api/whoami-v2"


def _model_config_url(model_name):
    return f"https://huggingface.co/{model_name}/resolve/main/config.json"


# Kept for integrations that imported the old constant directly.
MODEL_CONFIG_URL = _model_config_url(LEGACY_MODEL_NAME)

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


def remove_background(
    image,
    model_root,
    huggingface_token=None,
    model_name=MODEL_NAME,
):
    configured_mode = background_model_value(model_name)
    candidates = [resolve_background_model(configured_mode)]
    if configured_mode == BACKGROUND_MODEL_AUTO and (
        huggingface_token or _model_cache_available(model_root, LEGACY_MODEL_NAME)
    ):
        candidates.append(LEGACY_MODEL_NAME)
    last_error = None
    for candidate in candidates:
        try:
            result = _infer_with_model(image, model_root, huggingface_token, candidate)
            result.info["jiffle_model"] = candidate
            return result
        except BackgroundFailure as error:
            last_error = error
    if last_error is not None:
        raise last_error
    raise BackgroundFailure(
        "background.inference_failed",
        "The background model could not process the image.",
    )


def _infer_with_model(image, model_root, huggingface_token, model_name):
    model, torch, transforms, device = _load_runtime(
        model_root, huggingface_token, model_name
    )
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
            f"{model_name} could not remove the image background.",
        ) from error


def validate_huggingface_token(token, model_name=LEGACY_MODEL_NAME):
    token = token.strip() if isinstance(token, str) else ""
    if not token:
        raise BackgroundFailure(
            "background.huggingface_token_required",
            "Add a Hugging Face access token in Settings before loading the background model.",
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
        access = requests.get(
            _model_config_url(model_name), headers=headers, timeout=30
        )
    except requests.RequestException as error:
        raise BackgroundFailure(
            "background.huggingface_unavailable",
            f"Hugging Face could not verify access to {model_name}. Check the network connection and try again.",
        ) from error
    if access.status_code in {401, 403}:
        raise BackgroundFailure(
            "background.huggingface_access_denied",
            f"This Hugging Face account does not have access to {model_name}. Accept the model terms first.",
        )
    if access.status_code != 200:
        raise BackgroundFailure(
            "background.huggingface_unavailable",
            f"Hugging Face could not verify access to {model_name}. Try again later.",
        )
    try:
        payload = identity.json()
    except ValueError:
        payload = {}
    return payload.get("name") or payload.get("fullname") or "authenticated"


def clear_runtime_cache():
    """Release cached model handles after the model setting changes."""
    global _loaded_runtime
    with _runtime_lock:
        _loaded_runtime = None
        _loaded_runtimes.clear()


def _load_runtime(model_root, huggingface_token=None, model_name=LEGACY_MODEL_NAME):
    global _loaded_runtime
    model_name = resolve_background_model(model_name)
    if model_name == LEGACY_MODEL_NAME and _loaded_runtime is not None:
        return _loaded_runtime
    if model_name in _loaded_runtimes:
        return _loaded_runtimes[model_name]
    with _runtime_lock:
        if model_name == LEGACY_MODEL_NAME and _loaded_runtime is not None:
            return _loaded_runtime
        if model_name in _loaded_runtimes:
            return _loaded_runtimes[model_name]
        cache_available = _model_cache_available(model_root, model_name)
        token_validated = False
        if not cache_available and model_name == LEGACY_MODEL_NAME:
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
                "The installed background-model runtime could not be loaded. Reinstall PyTorch, torchvision, and transformers in this Python environment.",
            ) from error
        try:
            model_root.mkdir(parents=True, exist_ok=True)
            device = "cuda" if torch.cuda.is_available() else "cpu"
            token = huggingface_token or False
            try:
                model = AutoModelForImageSegmentation.from_pretrained(
                    model_name,
                    trust_remote_code=True,
                    cache_dir=model_root,
                    local_files_only=True,
                    token=token,
                )
            except Exception:
                if not token_validated and model_name == LEGACY_MODEL_NAME:
                    validate_huggingface_token(huggingface_token, model_name)
                model = AutoModelForImageSegmentation.from_pretrained(
                    model_name,
                    trust_remote_code=True,
                    cache_dir=model_root,
                    token=huggingface_token,
                )
            model.to(device)
            model.eval()
            runtime = (model, torch, transforms, device)
            _loaded_runtimes[model_name] = runtime
            if model_name == LEGACY_MODEL_NAME:
                _loaded_runtime = runtime
            return runtime
        except BackgroundFailure:
            raise
        except Exception as error:
            raise BackgroundFailure(
                "background.model_download_failed",
                f"{model_name} could not be downloaded or loaded. Check network access and available disk space.",
            ) from error


def _model_cache_available(model_root, model_name=LEGACY_MODEL_NAME):
    model_directory = model_root / f"models--{model_name.replace('/', '--')}" / "snapshots"
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
                "The background-model runtime is missing required packages: "
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
            "The background-model runtime could not install its required packages "
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
                f"The background-model runtime could not import required package '{module}'.",
                {"module": module},
            ) from error
