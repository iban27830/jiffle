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
    BACKGROUND_MODEL_BIREFNET,
    BACKGROUND_MODEL_BIREFNET_HR,
    BACKGROUND_DEVICE_AUTO,
    BACKGROUND_DEVICE_CUDA,
    BACKGROUND_DEVICE_CPU,
    background_device_value,
    background_model_value,
    HR_MODEL_NAME,
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

# BiRefNet's remote model code imports einops. RMBG-2.0 also imports kornia and
# timm. Keep import names separate from pip requirement strings because they
# are not always identical.
RUNTIME_DEPENDENCIES = (
    ("torch", "torch"),
    ("torchvision", "torchvision"),
    ("transformers", "transformers>=4.39"),
    ("safetensors", "safetensors"),
    ("einops", "einops"),
    ("kornia", "kornia"),
    ("timm", "timm"),
)

PUBLIC_MODEL_NAMES = {MODEL_NAME, HR_MODEL_NAME}


def remove_background(
    image,
    model_root,
    huggingface_token=None,
    model_name=MODEL_NAME,
    background_device=BACKGROUND_DEVICE_AUTO,
):
    configured_mode = background_model_value(model_name)
    requested_device = background_device_value(background_device)
    # Resolve this once per request. In particular, a forced CUDA request must
    # fail before model fallback can accidentally move work to the CPU.
    selected_device = resolve_background_device(requested_device)
    candidates = _candidate_models(
        configured_mode, model_root, huggingface_token, requested_device, selected_device
    )
    last_error = None
    for candidate in candidates:
        try:
            result = _infer_with_model(
                image, model_root, huggingface_token, candidate, requested_device
            )
            result.info["jiffle_model"] = candidate
            result.info["jiffle_device"] = selected_device
            return result
        except BackgroundFailure as error:
            last_error = error
        except Exception as error:
            last_error = BackgroundFailure(
                "background.inference_failed",
                f"{candidate} could not remove the image background.",
                {"model": candidate, "device": selected_device, "reason": type(error).__name__},
            )
    if last_error is not None:
        raise last_error
    raise BackgroundFailure(
        "background.inference_failed",
        "The background model could not process the image.",
    )


def _cuda_available():
    """Return CUDA availability without installing or loading the runtime."""
    try:
        import torch

        return bool(torch.cuda.is_available())
    except Exception:
        return False


def resolve_background_device(device_mode=BACKGROUND_DEVICE_AUTO, torch_module=None):
    """Return the actual device, failing clearly for an unavailable forced GPU."""
    mode = background_device_value(device_mode)
    if torch_module is None:
        # Keep this helper patchable for diagnostics and tests, and avoid
        # importing the full runtime merely to answer a settings question.
        available = _cuda_available()
    else:
        try:
            available = bool(torch_module.cuda.is_available())
        except Exception as error:
            if mode == BACKGROUND_DEVICE_CUDA:
                raise BackgroundFailure(
                    "background.cuda_unavailable",
                    "CUDA could not be initialized by the installed PyTorch runtime.",
                    {"device": BACKGROUND_DEVICE_CUDA, "reason": type(error).__name__},
                ) from error
            available = False
    if mode == BACKGROUND_DEVICE_CUDA and not available:
        raise BackgroundFailure(
            "background.cuda_unavailable",
            "CUDA was requested, but no usable CUDA device is available. Install a CUDA-enabled PyTorch build or select CPU/Automatic.",
            {"device": BACKGROUND_DEVICE_CUDA, "reason": "cuda_unavailable"},
        )
    if mode == BACKGROUND_DEVICE_CPU:
        return BACKGROUND_DEVICE_CPU
    return BACKGROUND_DEVICE_CUDA if available else BACKGROUND_DEVICE_CPU


def preferred_device_name(device_mode=BACKGROUND_DEVICE_AUTO):
    """Return the device represented by a setting for diagnostics and UI."""
    mode = background_device_value(device_mode)
    if mode == BACKGROUND_DEVICE_CUDA:
        return BACKGROUND_DEVICE_CUDA
    if mode == BACKGROUND_DEVICE_CPU:
        return BACKGROUND_DEVICE_CPU
    return resolve_background_device(mode)


def preferred_model_name(
    model_name=BACKGROUND_MODEL_AUTO,
    background_device=BACKGROUND_DEVICE_AUTO,
):
    """Resolve the model used for a newly generated preview.

    Automatic mode uses the HR checkpoint on CUDA and the standard checkpoint
    on CPU. Explicit modes always resolve to the requested checkpoint.
    """
    mode = background_model_value(model_name)
    if mode == BACKGROUND_MODEL_AUTO:
        device_mode = background_device_value(background_device)
        if device_mode == BACKGROUND_DEVICE_CUDA:
            return HR_MODEL_NAME
        if device_mode == BACKGROUND_DEVICE_CPU:
            return MODEL_NAME
        return HR_MODEL_NAME if _cuda_available() else MODEL_NAME
    return resolve_background_model(mode)


def _candidate_models(
    configured_mode,
    model_root,
    huggingface_token,
    background_device=BACKGROUND_DEVICE_AUTO,
    selected_device=None,
):
    if configured_mode == BACKGROUND_MODEL_AUTO:
        if selected_device == BACKGROUND_DEVICE_CUDA:
            primary = HR_MODEL_NAME
        elif selected_device == BACKGROUND_DEVICE_CPU:
            primary = MODEL_NAME
        else:
            primary = preferred_model_name(configured_mode, background_device)
        candidates = [primary]
        if primary != MODEL_NAME:
            candidates.append(MODEL_NAME)
        if huggingface_token or _model_cache_available(model_root, LEGACY_MODEL_NAME):
            candidates.append(LEGACY_MODEL_NAME)
        return candidates
    if configured_mode == BACKGROUND_MODEL_BIREFNET_HR:
        return [HR_MODEL_NAME]
    if configured_mode == BACKGROUND_MODEL_BIREFNET:
        return [MODEL_NAME]
    return [LEGACY_MODEL_NAME]


def _infer_with_model(
    image,
    model_root,
    huggingface_token,
    model_name,
    background_device=BACKGROUND_DEVICE_AUTO,
):
    model, torch, transforms, device = _load_runtime(
        model_root, huggingface_token, model_name, background_device
    )
    try:
        preprocessing = transforms.Compose(
            [
                transforms.Resize(
                    (2048, 2048) if model_name == HR_MODEL_NAME else (1024, 1024)
                ),
                transforms.ToTensor(),
                transforms.Normalize(
                    [0.485, 0.456, 0.406], [0.229, 0.224, 0.225]
                ),
            ]
        )
        tensor = preprocessing(image.convert("RGB")).unsqueeze(0).to(device)
        try:
            model_dtype = next(model.parameters()).dtype
        except (AttributeError, StopIteration, TypeError):
            model_dtype = getattr(model, "dtype", None)
        if model_dtype is not None and getattr(tensor, "dtype", None) != model_dtype:
            tensor = tensor.to(dtype=model_dtype)
        with torch.no_grad():
            prediction = model(tensor)
        if hasattr(prediction, "logits"):
            prediction = prediction.logits
        while isinstance(prediction, (tuple, list)):
            prediction = prediction[-1]
        if isinstance(prediction, dict) and "logits" in prediction:
            prediction = prediction["logits"]
        # HR inference runs in half precision on CUDA; convert the mask back
        # to float32 before torchvision turns it into a PIL image.
        prediction = prediction.sigmoid().float().cpu()[0].squeeze()
        mask = transforms.ToPILImage()(prediction).resize(
            image.size, Image.Resampling.LANCZOS
        )
        result = image.convert("RGBA")
        result.putalpha(mask)
        result.info["jiffle_device"] = device
        return result
    except BackgroundFailure:
        raise
    except Exception as error:
        raise BackgroundFailure(
            "background.inference_failed",
            f"{model_name} could not remove the image background.",
            {"model": model_name, "device": device, "reason": type(error).__name__},
        ) from error


def validate_huggingface_token(token, model_name=LEGACY_MODEL_NAME):
    model_name = resolve_background_model(model_name)
    token = token.strip() if isinstance(token, str) else ""
    if not token and model_name in PUBLIC_MODEL_NAMES:
        try:
            access = requests.get(_model_config_url(model_name), timeout=30)
        except requests.RequestException as error:
            raise BackgroundFailure(
                "background.huggingface_unavailable",
                f"Hugging Face could not verify access to {model_name}. Check the network connection and try again.",
            ) from error
        if access.status_code in {401, 403}:
            raise BackgroundFailure(
                "background.huggingface_access_denied",
                f"Hugging Face denied access to public model {model_name}.",
            )
        if access.status_code != 200:
            raise BackgroundFailure(
                "background.huggingface_unavailable",
                f"Hugging Face could not verify access to {model_name}. Try again later.",
            )
        return "public"
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


def _load_runtime(
    model_root,
    huggingface_token=None,
    model_name=LEGACY_MODEL_NAME,
    background_device=BACKGROUND_DEVICE_AUTO,
):
    global _loaded_runtime
    model_name = resolve_background_model(model_name)
    requested_device = background_device_value(background_device)
    if requested_device == BACKGROUND_DEVICE_CUDA:
        # Fail before downloading a model if the user explicitly selected GPU.
        resolve_background_device(requested_device)
    with _runtime_lock:
        cached_device = None
        if requested_device == BACKGROUND_DEVICE_AUTO:
            cached_device = preferred_device_name(requested_device)
        cache_key = (model_name, cached_device or requested_device)
        if cache_key in _loaded_runtimes:
            return _loaded_runtimes[cache_key]
        cache_available = _model_cache_available(model_root, model_name)
        token_validated = False
        if not cache_available and model_name == LEGACY_MODEL_NAME:
            validate_huggingface_token(huggingface_token)
            token_validated = True
        _ensure_dependencies()
        try:
            _verify_dependency_imports()
            import torch
            from torchvision import transforms
            from transformers import AutoModelForImageSegmentation
        except BackgroundFailure:
            raise
        except Exception as error:
            raise BackgroundFailure(
                "background.runtime_install_failed",
                "The installed background-model runtime could not be loaded. Check the required packages in this Python environment.",
            ) from error
        try:
            model_root.mkdir(parents=True, exist_ok=True)
            device = resolve_background_device(requested_device, torch)
            cache_key = (model_name, device)
            if cache_key in _loaded_runtimes:
                return _loaded_runtimes[cache_key]
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
            if device == "cpu":
                # Some BiRefNet checkpoints are published in half precision,
                # which CPU kernels cannot execute with float32 inputs.
                model.float()
            elif device == "cuda" and model_name == HR_MODEL_NAME:
                model.half()
            model.eval()
            runtime = (model, torch, transforms, device)
            _loaded_runtimes[cache_key] = runtime
            if model_name == LEGACY_MODEL_NAME and device == "cpu":
                _loaded_runtime = runtime
            return runtime
        except BackgroundFailure:
            raise
        except Exception as error:
            raise BackgroundFailure(
                "background.model_download_failed",
                f"{model_name} could not be downloaded or loaded. Check network access and available disk space.",
                {"model": model_name, "device": locals().get("device", requested_device), "reason": type(error).__name__},
            ) from error


def _model_cache_available(model_root, model_name=LEGACY_MODEL_NAME):
    model_directory = model_root / f"models--{model_name.replace('/', '--')}" / "snapshots"
    if not model_directory.is_dir():
        return False
    return any(
        snapshot.is_dir()
        and (snapshot / "config.json").is_file()
        and (snapshot / "model.safetensors").is_file()
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
